"""客户端：一次生成的完整流程，包含上传、提交、轮询和结算。

并发与额度控制都在这一层：

- 每个账号一把非阻塞锁，请求之间保留固定间隔；
- 参考图先上传再预留，上传失败不影响账本；
- 预留要在异常路径释放，否则会一直占用额度；
- 幂等键命中时直接返回缓存，不再提交；
- 只有提交成功的结果才写缓存，失败和超时不写。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .cache import TTLCache
from .catalog import Catalog
from .credits import CreditSnapshot, Ledger, parse_credit
from .errors import (
    AccountBusy,
    AuthExpired,
    BudgetExceeded,
    ContentFiltered,
    JimengError,
    ParamInvalid,
    RiskControl,
    TaskFailed,
    TaskTimeout,
    UpstreamError,
)
from .parsing import extract_images, fail_reason, find_scalar, has_credit_field, history_records, pick_history
from .payload import build_generate_body, build_history_body
from .request import GenerateRequest, Plan, ReferenceImage
from .sign import CN_HOST, DEFAULT_APPVR, DeviceIdentity, build_headers
from .transport import Transport
from .upload import MAX_UPLOAD_BYTES, Asset, ImageUploader

SUCCESS_STATUS = frozenset({21, 40, 50})
FAILED_STATUS = frozenset({30})
FILTERED_RETS = frozenset({"2038"})
RUNNING_STATUS = frozenset({20, 42})
BUSY_RETS = frozenset({"1309", "1310"})


@dataclass
class Account:
    id: str
    sessionid: str
    region: str = "cn"
    host: str = CN_HOST
    appvr: str = DEFAULT_APPVR
    cooldown_s: float = 3.0
    daily_quota: Optional[float] = 66.0
    max_per_request: Optional[float] = None
    device: DeviceIdentity = field(default_factory=DeviceIdentity)
    ledger: Ledger = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_call_at: float = 0.0

    def __post_init__(self) -> None:
        self.ledger = Ledger(self.id, daily_quota=self.daily_quota, max_per_request=self.max_per_request)

    def redacted(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "sessionid": self.sessionid[:4] + "***" if self.sessionid else "",
            "credit": self.ledger.usage(),
        }


@dataclass
class Outcome:
    """一次提交批次的结果。`failed` 走异常，不落在这里。"""

    status: str
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    submissions: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[Dict[str, Any]] = None

    @property
    def history_ids(self) -> List[str]:
        return [item["history_id"] for item in self.submissions]


class JimengClient:
    CACHE_TTL = 86400.0

    def __init__(
        self,
        account: Account,
        *,
        transport: Optional[Transport] = None,
        catalog: Optional[Catalog] = None,
        cache: Optional[TTLCache] = None,
    ) -> None:
        self.account = account
        self.transport = transport or Transport()
        self.catalog = catalog or Catalog.load()
        self.uploader = ImageUploader(self.transport, host=account.host, region=account.region)
        self._assets: Dict[str, Asset] = {}
        self._cache = cache or TTLCache(ttl=self.CACHE_TTL, max_items=256)

    # ------------------------------------------------------------------
    # 低层请求
    # ------------------------------------------------------------------
    def _post(self, uri: str, body: Dict[str, Any], *, timeout: float = 30.0, retry: int = 0) -> Dict[str, Any]:
        """调用 mweb 接口。

        retry 默认 0：生成类接口重放会二次扣费。只有明确幂等的查询接口才由调用方传 retry>0。
        """
        wait = self.account.cooldown_s - (time.time() - self.account.last_call_at)
        if wait > 0:
            time.sleep(wait)
        query = urlencode(self.account.device.query(self.account.region))
        headers = build_headers(uri, self.account.sessionid, appvr=self.account.appvr, origin=self.account.host)
        url = f"{self.account.host}{uri}?{query}"
        try:
            resp = self.transport.request("POST", url, headers=headers, json_body=body, timeout=timeout, retry=retry)
        finally:
            self.account.last_call_at = time.time()

        if resp.status in (401, 403) and not resp.body:
            raise AuthExpired("sessionid 失效或请求被拒绝", details={"status": resp.status})
        payload = resp.json()
        ret = str(payload.get("ret", payload.get("ret_code", "0")))
        if ret not in ("0", ""):
            raise raise_upstream(ret, payload)
        return payload

    # ------------------------------------------------------------------
    # 积分
    # ------------------------------------------------------------------
    def refresh_credit(self) -> CreditSnapshot:
        payload = self._post("/commerce/v1/benefits/user_credit", {}, retry=1)
        snapshot = parse_credit(payload)
        if snapshot.total <= 0 and not has_credit_field(payload):
            raise UpstreamError("积分返回无法解析", details={"upstream": payload.get("data")})
        self.account.ledger.update(snapshot)
        return snapshot

    def _snapshot_or_none(self) -> Optional[CreditSnapshot]:
        try:
            return self.refresh_credit()
        except JimengError:
            return None

    # ------------------------------------------------------------------
    # 素材
    # ------------------------------------------------------------------
    def resolve_reference(self, ref: ReferenceImage) -> str:
        kind, value = ref.source()
        if kind == "uri":
            return value
        if kind == "asset_id":
            asset = self._assets.get(value)
            if asset is None:
                raise ParamInvalid(f"未知 asset_id: {value}，请先上传")
            return asset.image_uri
        cached = self._assets.get(value)
        if cached is not None:
            return cached.image_uri
        content, filename = self._load_reference(kind, value)
        asset = self.uploader.upload_bytes(
            content, filename, sessionid=self.account.sessionid, appvr=self.account.appvr
        )
        self.remember_asset(asset)
        if kind == "url":
            self._assets[value] = asset
        return asset.image_uri

    def _load_reference(self, kind: str, value: str) -> Tuple[bytes, str]:
        if kind == "url":
            resp = self.transport.request("GET", value, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            if resp.status != 200 or not resp.body:
                raise UpstreamError(f"下载参考图失败: {value}", details={"status": resp.status})
            if len(resp.body) > MAX_UPLOAD_BYTES:
                raise ParamInvalid(f"参考图超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
            return resp.body, "ref_" + hashlib.sha256(value.encode()).hexdigest()[:10] + ".png"
        if kind == "base64":
            raw = value.split(",", 1)[-1]
            try:
                content = base64.b64decode(raw, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ParamInvalid("base64 图片内容不合法") from exc
            return content, "ref_b64.png"
        with open(value, "rb") as fh:
            content = fh.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise ParamInvalid(f"参考图超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
        return content, value.replace("\\", "/").rsplit("/", 1)[-1]

    def upload_file(self, path: str) -> Asset:
        asset = self.uploader.upload_path(path, sessionid=self.account.sessionid, appvr=self.account.appvr)
        return self.remember_asset(asset)

    def remember_asset(self, asset: Asset) -> Asset:
        """登记素材，供后续以 asset_id 复用（避免同一张图反复上传）。"""
        self._assets[asset.asset_id] = asset
        return asset

    # ------------------------------------------------------------------
    # 提交 / 轮询
    # ------------------------------------------------------------------
    def submit(self, plan: Plan) -> Dict[str, Any]:
        body = build_generate_body(plan)
        payload = self._post("/mweb/v1/aigc_draft/generate", body, timeout=60)
        history_id = find_scalar(payload, ("history_record_id", "history_id", "historyId"))
        if not history_id:
            raise UpstreamError(
                "提交成功但未返回 history_id",
                details={"submit_id": body["submit_id"], "upstream": payload.get("data")},
            )
        return {"submit_id": body["submit_id"], "history_id": str(history_id), "raw": payload.get("data")}

    def history(self, history_ids: List[str]) -> Dict[str, Any]:
        return self._post("/mweb/v1/get_history_by_ids", build_history_body(history_ids), retry=1)

    def poll(
        self, history_id: str, *, timeout_s: float, interval_s: float
    ) -> Tuple[int, List[Dict[str, Any]], Dict[str, Any]]:
        deadline = time.time() + max(2.0, timeout_s)
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = self.history([history_id])
            record = pick_history(last, history_id)
            status = int(record.get("status") or 0) if isinstance(record, dict) else 0
            images = extract_images(record)
            if status in SUCCESS_STATUS or (images and status not in RUNNING_STATUS):
                return status, images, last
            if status in FAILED_STATUS:
                raise TaskFailed(fail_reason(record) or "生成失败", details={"history_id": history_id, "status": status})
            time.sleep(max(0.5, interval_s))
        raise TaskTimeout(
            f"等待生成超时（{timeout_s:g}s）",
            details={"history_id": history_id, "last_status": pick_history(last, history_id).get("status")},
        )

    def collect(self, history_ids: List[str]) -> List[Dict[str, Any]]:
        """按需回查已有提交（供网关的 pending 任务使用，不产生新提交）。"""
        payload = self.history(history_ids)
        images: List[Dict[str, Any]] = []
        for record in history_records(payload):
            for image in extract_images(record):
                image["index"] = len(images)
                images.append(image)
        return images

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def generate(self, req: GenerateRequest) -> Dict[str, Any]:
        plan = req.normalize(self.catalog)
        if req.dry_run:
            return self._preview(plan)

        cache_key = req.idempotency_key or req.cache_fingerprint()
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached["cached"] = True
            cached["usage"]["credits_charged"] = 0.0
            cached["usage"]["credit_source"] = "cache"
            return cached

        if not self.account.lock.acquire(blocking=False):
            raise AccountBusy("账号正在执行其他任务，请稍后重试")
        try:
            return self._execute(plan, req, cache_key)
        finally:
            self.account.lock.release()

    def _execute(self, plan: Plan, req: GenerateRequest, cache_key: str) -> Dict[str, Any]:
        started = time.time()
        if req.max_credits is not None and plan.estimated_credits > req.max_credits:
            raise BudgetExceeded(
                f"预估 {plan.estimated_credits:g} 积分超过请求上限 {req.max_credits:g}",
                details={"estimated": plan.estimated_credits, "max_credits": req.max_credits},
            )

        plan = plan.with_references([self.resolve_reference(ref) for ref in req.images])

        before = self._snapshot_or_none()
        reservation = self.account.ledger.reserve(plan.estimated_credits, reason="image.generate")
        try:
            outcome = self._submit_all(plan, req)
        except JimengError:
            self.account.ledger.settle(reservation, actual=0.0, success=False)
            raise

        after = self._snapshot_or_none()
        charged = self._charged_amount(plan, before, after)
        self.account.ledger.settle(
            reservation,
            actual=charged,
            success=outcome.status != "failed",
            balance_is_fresh=after is not None,
        )
        result = self._result(plan, req, outcome, charged, after, started)
        if outcome.status == "succeeded":
            self._cache.put(cache_key, result)
        return result

    def _submit_all(self, plan: Plan, req: GenerateRequest) -> Outcome:
        outputs: List[Dict[str, Any]] = []
        submissions: List[Dict[str, Any]] = []
        status = "succeeded"
        for index in range(plan.submissions):
            info = self.submit(plan)
            submissions.append(info)
            if not req.wait:
                status = "submitted"
            else:
                try:
                    _status, images, _raw = self.poll(
                        info["history_id"], timeout_s=req.timeout_s, interval_s=req.poll_interval_s
                    )
                except TaskTimeout as exc:
                    return Outcome("timeout", outputs, submissions, exc.to_dict()["error"])
                for image in images:
                    outputs.append({**image, "index": len(outputs)})
            if index + 1 < plan.submissions:
                time.sleep(max(self.account.cooldown_s, 1.0))
        return Outcome(status, outputs, submissions)

    def _charged_amount(
        self,
        plan: Plan,
        before: Optional[CreditSnapshot],
        after: Optional[CreditSnapshot],
    ) -> float:
        """优先用余额差，拿不到前后余额时用预估值。"""
        if before is not None and after is not None:
            return round(max(0.0, before.total - after.total), 3)
        return plan.estimated_credits

    # ------------------------------------------------------------------
    def _preview(self, plan: Plan) -> Dict[str, Any]:
        body = build_generate_body(plan)
        return {
            "id": "preview_" + uuid.uuid4().hex[:12],
            "object": "image.generation.preview",
            "created": int(time.time()),
            "status": "preview",
            "dry_run": True,
            "model": plan.model.name,
            "request": plan.to_dict(),
            "data": [],
            "usage": {
                "images_requested": plan.n,
                "images_planned": plan.planned_images,
                "credits_estimated": plan.estimated_credits,
                "credits_charged": 0.0,
                "credit_source": "none",
            },
            "account": self.account.redacted(),
            "upstream": {"submit_id": body["submit_id"], "payload": body},
            "error": None,
            "cached": False,
        }

    def _result(
        self,
        plan: Plan,
        req: GenerateRequest,
        outcome: Outcome,
        charged: float,
        after: Optional[CreditSnapshot],
        started: float,
    ) -> Dict[str, Any]:
        items = [self._render_item(image, req.response_format) for image in outcome.outputs]
        result = {
            "id": "task_" + uuid.uuid4().hex[:16],
            "object": "image.generation",
            "created": int(started),
            "completed_at": int(time.time()),
            "elapsed_ms": int((time.time() - started) * 1000),
            "status": outcome.status,
            "model": plan.model.name,
            "request": plan.to_dict(),
            "data": items,
            "usage": {
                "images_requested": plan.n,
                "images_returned": len(items),
                "planned_images": plan.planned_images,
                "submissions": plan.submissions,
                "credits_estimated": plan.estimated_credits,
                "credits_charged": round(charged, 3),
                "credit_source": credit_source(charged, after),
            },
            "account": self.account.redacted(),
            "upstream": {
                "submit_id": outcome.submissions[-1]["submit_id"] if outcome.submissions else None,
                "history_ids": outcome.history_ids,
            },
            "error": outcome.error,
            "cached": False,
        }
        if outcome.status in ("submitted", "timeout"):
            result["pending"] = outcome.history_ids
        return result

    def _render_item(self, image: Dict[str, Any], response_format: str) -> Dict[str, Any]:
        item = {
            "index": image.get("index", 0),
            "url": image.get("url"),
            "width": image.get("width"),
            "height": image.get("height"),
            "seed": image.get("seed"),
        }
        if response_format == "b64_json" and item["url"]:
            try:
                resp = self.transport.request("GET", item["url"], headers={"User-Agent": "Mozilla/5.0"})
                if resp.status == 200:
                    item["b64_json"] = base64.b64encode(resp.body).decode("ascii")
                    item.pop("url", None)
            except JimengError:
                pass
        return item


# ----------------------------------------------------------------------
def raise_upstream(ret: str, payload: Dict[str, Any]) -> JimengError:
    """把上游业务码映射成异常实例（调用方 raise）。"""
    message = payload.get("errmsg") or payload.get("message") or "上游返回失败"
    starling = payload.get("fail_starling_key") or ""
    if ret == "4013" or "risk" in starling:
        return RiskControl(message, details={"ret": ret, "fail_starling_key": starling})
    if ret in FILTERED_RETS:
        return ContentFiltered(message, details={"ret": ret})
    if ret in BUSY_RETS:
        return AccountBusy("上游并发/排队超限", details={"ret": ret})
    return UpstreamError(message, details={"ret": ret, "upstream": payload.get("data")})


def credit_source(charged: float, after: Optional[CreditSnapshot]) -> str:
    """按结算后余额粗判来源。上游未开放流水接口，不臆造单一来源。"""
    if charged <= 0:
        return "none"
    if after is None:
        return "unknown"
    if after.gift > 0:
        return "gift"
    if after.vip > 0:
        return "vip"
    return "purchase_or_mixed"
