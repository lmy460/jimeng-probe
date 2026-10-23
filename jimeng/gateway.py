"""HTTP 网关：把客户端能力暴露成可复用 REST 接口。

零依赖（http.server）。设计取舍：

  - 同步优先：默认 wait=true，一次请求拿到结果，调用方不必理解轮询；
  - wait=false 返回 pending，GET 任务时按需回查上游，不额外占用线程、不重复扣费；
  - sessionid 永不出现在请求/响应里，网关持有凭证；
  - 路由用表驱动，新增接口只加一行，不再往 if/elif 链条里塞。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import urlparse

from .accounts import AccountPool
from .errors import JimengError, NotFound, ParamInvalid
from .probe import ProbeRunner
from .request import GenerateRequest, ReferenceImage
from .upload import Asset

MAX_BODY = 32 * 1024 * 1024
TASK_PATH = re.compile(r"^/v1/images/generations/([A-Za-z0-9_\-]+)$")

HandlerFn = Callable[..., Tuple[int, Dict[str, Any]]]


class TaskStore:
    """网关的任务表。只在内存里，重启即失效——这是刻意的最小实现。"""

    def __init__(self, max_items: int = 2000) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._max = max_items

    def put(self, result: Dict[str, Any]) -> None:
        with self._lock:
            self._items[result["id"]] = {
                "result": result,
                "history_ids": (result.get("upstream") or {}).get("history_ids") or [],
            }
            while len(self._items) > self._max:
                self._items.pop(next(iter(self._items)), None)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._items.get(task_id)


class Gateway:
    def __init__(self, pool: AccountPool, *, token: Optional[str] = None) -> None:
        self.pool = pool
        self.token = token
        self.tasks = TaskStore()
        self.probe_report: Optional[Dict[str, Any]] = None
        self.started_at = time.time()
        self._routes: List[Tuple[str, Pattern[str], HandlerFn]] = [
            ("GET", re.compile(r"^/healthz$"), self._health),
            ("GET", re.compile(r"^/v1/models$"), self._models),
            ("GET", re.compile(r"^/v1/credit$"), self._credit),
            ("POST", re.compile(r"^/v1/assets$"), self._assets),
            ("POST", re.compile(r"^/v1/images/generations$"), self._generate),
            ("GET", TASK_PATH, self._task),
            ("POST", re.compile(r"^/v1/probe$"), self._probe),
            ("GET", re.compile(r"^/v1/probe/latest$"), self._probe_latest),
        ]

    # ------------------------------------------------------------------
    def handle(self, method: str, path: str, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        route = urlparse(path).path.rstrip("/") or "/"
        for verb, pattern, handler in self._routes:
            match = pattern.match(route)
            if match and verb == method:
                return handler(body, content_type, *match.groups())
        raise NotFound(f"未知路由: {method} {route}")

    # -- handlers -------------------------------------------------------
    def _health(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        return 200, {
            "ok": True,
            "uptime_s": int(time.time() - self.started_at),
            "accounts": len(self.pool.accounts),
            "routes": len(self._routes),
        }

    def _models(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        return 200, {"object": "list", "data": [spec.to_dict() for spec in self.pool.catalog.all()]}

    def _credit(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        return 200, {"object": "list", "data": self.pool.refresh_all()}

    def _probe_latest(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        if not self.probe_report:
            raise NotFound("尚未执行探测")
        return 200, self.probe_report

    def _assets(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        client = self.pool.client()
        if content_type.startswith("image/"):
            suffix = content_type.split("/")[-1].split(";")[0] or "png"
            asset = client.uploader.upload_bytes(
                body, f"upload.{suffix}", sessionid=client.account.sessionid, appvr=client.account.appvr
            )
            client.remember_asset(asset)
            return 201, {"object": "asset", **asset.to_dict()}

        data = _json(body)
        if data.get("path"):
            asset = client.upload_file(data["path"])
        elif data.get("base64"):
            raw = data["base64"].split(",", 1)[-1]
            try:
                content = base64.b64decode(raw, validate=True)
            except ValueError as exc:
                raise ParamInvalid("base64 内容不合法") from exc
            asset = client.remember_asset(
                client.uploader.upload_bytes(
                    content,
                    data.get("filename") or "upload.png",
                    sessionid=client.account.sessionid,
                    appvr=client.account.appvr,
                )
            )
        elif data.get("url"):
            uri = client.resolve_reference(ReferenceImage(url=data["url"]))
            asset = client.remember_asset(
                Asset(asset_id="url_" + hashlib.sha256(uri.encode()).hexdigest()[:12], image_uri=uri, uri=uri)
            )
        else:
            raise ParamInvalid("需要 path / base64 / url 之一，或以 image/* 直接 POST 二进制")
        return 201, {"object": "asset", **asset.to_dict()}

    def _generate(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        data = _json(body)
        account_id = data.pop("account_id", None) or data.pop("account", None)
        result = self.pool.client(account_id).generate(GenerateRequest.from_dict(data))
        self.tasks.put(result)
        return (200 if result["status"] in ("succeeded", "preview") else 202), result

    def _task(self, body: bytes, content_type: str, task_id: str) -> Tuple[int, Dict[str, Any]]:
        entry = self.tasks.get(task_id)
        if not entry:
            raise NotFound(f"未知任务: {task_id}")
        result = entry["result"]
        if result.get("status") != "submitted" or not entry["history_ids"]:
            return 200, result

        client = self.pool.client(result["account"]["id"])
        images = client.collect(entry["history_ids"])
        status = "succeeded" if images else "running"
        updated = {
            **result,
            "status": status,
            "data": images,
            "usage": dict(result["usage"], images_returned=len(images)),
        }
        if status == "succeeded":
            self.tasks.put(updated)
        return 200, updated

    def _probe(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        data = _json(body)
        runner = ProbeRunner(self.pool.client(data.get("account_id")), max_credits=float(data.get("max_credits", 8.0)))
        report = runner.run(
            data.get("probes"),
            model=data.get("model", "jimeng-4.5"),
            ratios=data.get("ratios"),
            resolutions=data.get("resolutions"),
            live=bool(data.get("live")),
        )
        if data.get("persist_calibration") and data.get("live"):
            report["calibration_path"] = runner.write_calibration()
        self.probe_report = report
        return 200, report


# ----------------------------------------------------------------------
def _json(body: bytes) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ParamInvalid("请求体不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ParamInvalid("请求体必须是 JSON 对象")
    return data


class Handler(BaseHTTPRequestHandler):
    gateway: Gateway
    server_version = "jimeng-probe/0.2"

    def _authorized(self) -> bool:
        if not self.gateway.token:
            return True
        header = self.headers.get("Authorization") or ""
        return hmac.compare_digest(header.strip(), f"Bearer {self.gateway.token}")

    def _dispatch(self, method: str) -> None:
        started = time.time()
        request_id = "req_" + uuid.uuid4().hex[:12]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._respond(413, {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "请求体过大"}}, request_id)
            return
        body = self.rfile.read(length) if length else b""

        try:
            if not self._authorized():
                self._respond(401, {"error": {"code": "UNAUTHORIZED", "message": "网关令牌无效"}}, request_id)
                return
            status, payload = self.gateway.handle(method, self.path, body, self.headers.get("Content-Type") or "")
        except JimengError as exc:
            payload = exc.to_dict()
            payload["error"]["request_id"] = request_id
            status = exc.http_status
        except Exception as exc:  # noqa: BLE001
            payload = {
                "error": {
                    "code": "INTERNAL",
                    "message": str(exc),
                    "type": "internal",
                    "retryable": False,
                    "trace": traceback.format_exc(limit=3),
                    "request_id": request_id,
                }
            }
            status = 500
        payload["elapsed_ms"] = int((time.time() - started) * 1000)
        self._respond(status, payload, request_id)

    def _respond(self, status: int, payload: Dict[str, Any], request_id: str) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-Id", request_id)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默：避免凭证类信息落日志
        return


def serve(pool: AccountPool, host: str = "127.0.0.1", port: int = 8787, token: Optional[str] = None) -> None:
    gateway = Gateway(pool, token=token or os.environ.get("JIMENG_GATEWAY_TOKEN"))
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"[jimeng-probe] listening on http://{host}:{port}  accounts={len(pool.accounts)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
