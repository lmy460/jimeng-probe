"""HTTP 网关：把客户端能力暴露成可复用的 REST 接口。

零依赖（http.server），单进程即可跑。设计取舍：
  - 同步优先：默认 wait=true，一次请求拿到结果，调用方不需要理解轮询。
  - wait=false 时返回 pending + history_ids，GET /v1/images/generations/{id} 会按需回查上游，
    不额外占线程、不重复扣费。
  - sessionid 永不出现在请求/响应里，网关持有凭证，调用方只见 account_id。
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .accounts import AccountPool
from .errors import JimengError, NotFound, ParamInvalid
from .parsing import extract_images
from .probe import ProbeRunner
from .request import GenerateRequest

MAX_BODY = 32 * 1024 * 1024


class TaskStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

    def put(self, result: Dict[str, Any]) -> None:
        with self._lock:
            self._items[result["id"]] = {"result": result, "history_ids": (result.get("upstream") or {}).get("history_ids") or []}
            if len(self._items) > 2000:
                for key in list(self._items)[:200]:
                    self._items.pop(key, None)

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

    # ------------------------------------------------------------------
    def handle(self, method: str, path: str, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        parsed = urlparse(path)
        route = parsed.path.rstrip("/") or "/"

        if route == "/healthz":
            return 200, {"ok": True, "uptime_s": int(time.time() - self.started_at), "accounts": len(self.pool.accounts)}

        if method == "GET" and route == "/v1/models":
            return 200, {"object": "list", "data": [m.to_dict() for m in self.pool.catalog.all()]}

        if method == "GET" and route == "/v1/credit":
            return 200, {"object": "list", "data": self.pool.refresh_all()}

        if method == "POST" and route == "/v1/assets":
            return self._assets(body, content_type)

        if method == "POST" and route == "/v1/images/generations":
            return self._generate(body)

        if method == "GET" and route.startswith("/v1/images/generations/"):
            return self._task(route.rsplit("/", 1)[-1])

        if method == "POST" and route == "/v1/probe":
            return self._probe(body)

        if method == "GET" and route == "/v1/probe/latest":
            if not self.probe_report:
                raise NotFound("尚未执行探测")
            return 200, self.probe_report

        raise NotFound(f"未知路由: {method} {route}")

    # ------------------------------------------------------------------
    def _assets(self, body: bytes, content_type: str) -> Tuple[int, Dict[str, Any]]:
        client = self.pool.client()
        if content_type.startswith("image/"):
            asset = client.uploader.upload_bytes(
                body,
                "upload." + content_type.split("/")[-1].split(";")[0],
                sessionid=client.account.sessionid,
                appvr=client.account.appvr,
            )
            client.remember_asset(asset)
            return 201, {"object": "asset", **asset.to_dict()}

        data = _json(body)
        if data.get("path"):
            asset = client.upload_file(data["path"])
        elif data.get("base64"):
            import base64 as _b64

            raw = data["base64"].split(",", 1)[-1]
            asset = client.remember_asset(
                client.uploader.upload_bytes(
                    _b64.b64decode(raw),
                    data.get("filename") or "upload.png",
                    sessionid=client.account.sessionid,
                    appvr=client.account.appvr,
                )
            )
        elif data.get("url"):
            from .request import ReferenceImage

            uri = client.resolve_reference(ReferenceImage(url=data["url"]))
            return 201, {"object": "asset", "asset_id": "url_" + str(abs(hash(uri)))[:12], "image_uri": uri}
        else:
            raise ParamInvalid("需要 path / base64 / url 之一，或以 image/* 直接 POST 二进制")
        return 201, {"object": "asset", **asset.to_dict()}

    def _generate(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        data = _json(body)
        account_id = data.pop("account_id", None) or data.pop("account", None)
        req = GenerateRequest.from_dict(data)
        client = self.pool.client(account_id)
        result = client.generate(req)
        self.tasks.put(result)
        code = 200 if result["status"] in ("succeeded", "preview") else 202
        return code, result

    def _task(self, task_id: str) -> Tuple[int, Dict[str, Any]]:
        entry = self.tasks.get(task_id)
        if not entry:
            raise NotFound(f"未知任务: {task_id}")
        result = entry["result"]
        if result.get("status") != "submitted" or not entry.get("history_ids"):
            return 200, result

        client = self.pool.client(result["account"]["id"])
        payload = client.history(entry["history_ids"])
        images = []
        for record in _history_records(payload):
            for img in extract_images(record):
                img["index"] = len(images)
                images.append(img)
        status = "succeeded" if images else "running"
        updated = dict(result)
        updated["status"] = status
        updated["data"] = images
        updated["usage"] = dict(result["usage"], images_returned=len(images))
        if status == "succeeded":
            self.tasks.put(updated)
        return 200, updated

    def _probe(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        data = _json(body)
        client = self.pool.client(data.get("account_id"))
        runner = ProbeRunner(client, max_credits=float(data.get("max_credits", 8.0)))
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
    except ValueError:
        raise ParamInvalid("请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise ParamInvalid("请求体必须是 JSON 对象")
    return data


def _history_records(payload: Any) -> list:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for key in ("history_list", "history_records", "list"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else []


class Handler(BaseHTTPRequestHandler):
    gateway: Gateway
    server_version = "jimeng-probe/0.1"

    def _authorized(self) -> bool:
        if not self.gateway.token:
            return True
        header = self.headers.get("Authorization") or ""
        return header.strip() == f"Bearer {self.gateway.token}"

    def _dispatch(self, method: str) -> None:
        started = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._respond(413, {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "请求体过大"}})
            return
        body = self.rfile.read(length) if length else b""
        try:
            if not self._authorized():
                self._respond(401, {"error": {"code": "UNAUTHORIZED", "message": "网关令牌无效"}})
                return
            status, payload = self.gateway.handle(method, self.path, body, self.headers.get("Content-Type") or "")
        except JimengError as exc:
            payload = exc.to_dict()
            payload["error"]["request_id"] = "req_" + uuid.uuid4().hex[:12]
            status = exc.http_status
        except Exception as exc:  # noqa: BLE001
            payload = {
                "error": {
                    "code": "INTERNAL",
                    "message": str(exc),
                    "type": "internal",
                    "retryable": False,
                    "trace": traceback.format_exc(limit=3),
                    "request_id": "req_" + uuid.uuid4().hex[:12],
                }
            }
            status = 500
        payload["elapsed_ms"] = int((time.time() - started) * 1000)
        self._respond(status, payload)

    def _respond(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
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

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，避免把 sessionid 类信息落到日志
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
