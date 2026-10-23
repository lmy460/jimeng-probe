"""客户端：把一次"生成"拆成 上传 → 提交 → 轮询 → 结算 的可审计流程。

并发约束在这里落地，而不是交给调用方"自觉"：
  - 每个账号一把锁 + 最小请求间隔（网页端对同账号并发极敏感，4013/1310 都是这么触发的）
  - 预留积分后才允许提交；失败回滚
  - 幂等键命中直接返回历史结果，零消耗
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .catalog import Catalog
from .credits import CreditSnapshot, Ledger, parse_credit
from .errors import (
    AccountBusy,
    AuthExpired,
    BudgetExceeded,
    ContentFiltered,
    ParamInvalid,
    RiskControl,
    TaskFailed,
    TaskTimeout,
    UpstreamError,
)
from .parsing import extract_images, fail_reason, find_scalar, has_credit_field, pick_history
from .payload import build_generate_body, build_history_body
from .request import GenerateRequest, Plan, ReferenceImage
from .sign import CN_AID, CN_HOST, DEFAULT_APPVR, DeviceIdentity, build_headers
from .transport import Transport
from .upload import Asset, ImageUploader

SUCCESS_STATUS = {21, 40, 50}
FAILED_STATUS = {30}
FILTERED_RET = {"2038"}
RUNNING_STATUS = {20, 42}


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


class JimengClient:
    def __init__(
        self,
        account: Account,
        *,
        transport: Optional[Transport] = None,
        catalog: Optional[Catalog] = None,
    ) -> None:
        self.account = account
        self.transport = transport or Transport()
        self.catalog = catalog or Catalog.load()
        self.uploader = ImageUploader(self.transport, host=account.host, region=account.region)
        self._assets: Dict[str, Asset] = {}
        self._idempotent: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 低层请求
    # ------------------------------------------------------------------
    def _post(self, uri: str, body: Dict[str, Any], *, timeout: float = 30.0) -> Dict[str, Any]:
        wait = self.account.cooldown_s - (time.time() - self.account.last_call_at)
        if wait > 0:
            time.sleep(wait)
        params = self.account.device.query(self.account.region)
        headers = build_headers(uri, self.account.sessionid, appvr=self.account.appvr, origin=self.account.host)
        url = f"{self.account.host}{uri}?aid={CN_AID}&device_platform=web&region={self.account.region.upper()}&da_version={self.account.device.da_version}"
        try:
            resp = self.transport.request("POST", url, headers=headers, json_body=body, timeout=timeout)
        finally:
            self.account.last_call_at = time.time()
        if resp.status in (401, 403) and not resp.body:
            raise AuthExpired("sessionid 失效或请求被拒绝", details={"status": resp.status})
        payload = resp.json()
        ret = str(payload.get("ret", payload.get("ret_code", "0")))
        if ret not in ("0", ""):
            self._raise_upstream(ret, payload)
        return payload

    @staticmethod
    def _raise_upstream(ret: str, payload: Dict[str, Any]) -> None:
        msg = payload.get("errmsg") or payload.get("message") or "上游返回失败"
        key = payload.get("fail_starling_key") or ""
        if ret == "4013" or "risk" in key:
            raise RiskControl(msg, details={"ret": ret, "fail_starling_key": key})
        if ret in FILTERED_RET:
            raise ContentFiltered(msg, details={"ret": ret})
        if ret in ("1310", "1309"):
            raise AccountBusy("上游并发/排队超限", details={"ret": ret})
        raise UpstreamError(msg, details={"ret": ret, "upstream": payload.get("data")})

    # ------------------------------------------------------------------
    # 积分
    # ------------------------------------------------------------------
    def refresh_credit(self) -> CreditSnapshot:
        payload = self._post("/commerce/v1/benefits/user_credit", {})
        snapshot = parse_credit(payload)
        if snapshot.total <= 0 and not has_credit_field(payload):
            raise UpstreamError("积分返回无法解析", details={"upstream": payload.get("data")})
        self.account.ledger.update(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 素材
    # ------------------------------------------------------------------
    def resolve_reference(self, ref: ReferenceImage) -> str:
        kind, value = ref.source()
        if kind == "uri":
            return value
        if kind == "asset_id":
            asset = self._assets.get(value)
            if not asset:
                raise ParamInvalid(f"未知 asset_id: {value}，请先上传")
            return asset.image_uri
        if kind in ("url", "base64", "path"):
            if kind == "url":
                if value in self._assets:
                    return self._assets[value].image_uri
                resp = self.transport.request(
                    "GET", value, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
                )
                if resp.status != 200 or not resp.body:
                    raise UpstreamError(f"下载参考图失败: {value}", details={"status": resp.status})
                content, filename = resp.body, "ref_" + hashlib.sha256(value.encode()).hexdigest()[:10] + ".png"
            elif kind == "base64":
                raw = value.split(",", 1)[-1]
                content, filename = base64.b64decode(raw), "ref_b64.png"
            else:
                with open(value, "rb") as fh:
                    content = fh.read()
                filename = value.replace("\\", "/").rsplit("/", 1)[-1]
            asset = self.uploader.upload_bytes(
                content, filename, sessionid=self.account.sessionid, appvr=self.account.appvr
            )
            self._assets[asset.asset_id] = asset
            self._assets[value] = asset
            return asset.image_uri
        raise ParamInvalid("参考图来源不合法")

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
        return {
            "submit_id": body["submit_id"],
            "history_id": str(history_id),
            "raw": payload.get("data"),
        }

    def history(self, history_ids: List[str]) -> Dict[str, Any]:
        return self._post("/mweb/v1/get_history_by_ids", build_history_body(history_ids))

    def poll(self, history_id: str, *, timeout_s: float, interval_s: float) -> Tuple[int, List[Dict[str, Any]], Dict[str, Any]]:
        deadline = time.time() + max(2.0, timeout_s)
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            payload = self.history([history_id])
            last = payload
            record = pick_history(payload, history_id)
            status = int(record.get("status") or 0) if isinstance(record, dict) else 0
            images = extract_images(record)
            if status in SUCCESS_STATUS or (images and status not in RUNNING_STATUS):
                return status, images, payload
            if status in FAILED_STATUS:
                raise TaskFailed(
                    fail_reason(record) or "生成失败",
                    details={"history_id": history_id, "status": status},
                )
            time.sleep(max(0.5, interval_s))
        raise TaskTimeout(
            f"等待生成超时（{timeout_s:g}s）",
            details={"history_id": history_id, "last_status": pick_history(last, history_id).get("status")},
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def generate(self, req: GenerateRequest) -> Dict[str, Any]:
        plan = req.normalize(self.catalog)
        started = time.time()

        preview = self._preview(plan, req)
        if req.dry_run:
            return preview

        cache_key = req.idempotency_key or req.cache_fingerprint()
        cached = self._cache_get(cache_key)
        if cached is not None:
            cached = dict(cached)
            cached["cached"] = True
            cached["usage"]["credits_charged"] = 0.0
            cached["usage"]["credit_source"] = "cache"
            return cached

        if not self.account.lock.acquire(blocking=False):
            raise AccountBusy("账号正在执行其他任务，请稍后重试")
        try:
            if req.max_credits is not None and plan.estimated_credits > req.max_credits:
                raise BudgetExceeded(
                    f"预估 {plan.estimated_credits:g} 积分超过请求上限 {req.max_credits:g}",
                    details={"estimated": plan.estimated_credits, "max_credits": req.max_credits},
                )

            pre: Optional[CreditSnapshot]
            try:
                pre = self.refresh_credit()
            except UpstreamError:
                pre = None

            reservation = self.account.ledger.reserve(plan.estimated_credits, reason="image.generate")

            # 上传参考图（放 reserve 之前更省，但这里放在之后是为了让"素材问题"可见于流水）
            uris: List[Optional[str]] = []
            for ref in req.images:
                uris.append(self.resolve_reference(ref))
            plan.image_uris = uris

            outputs: List[Dict[str, Any]] = []
            upstream_meta: List[Dict[str, Any]] = []
            error: Optional[Dict[str, Any]] = None
            status = "succeeded"

            try:
                for index in range(plan.submissions):
                    info = self.submit(plan)
                    upstream_meta.append(info)
                    if req.wait:
                        _status, images, _raw = self.poll(
                            info["history_id"], timeout_s=req.timeout_s, interval_s=req.poll_interval_s
                        )
                        for img in images:
                            img["index"] = len(outputs)
                            outputs.append(img)
                    else:
                        status = "submitted"
                    if index + 1 < plan.submissions:
                        time.sleep(max(self.account.cooldown_s, 1.0))
            except TaskTimeout as exc:
                # 超时无法确认是否已扣费：保守按预估结算，并保留 history_ids 供人工复核
                status = "timeout"
                error = exc.to_dict()["error"]
            except (TaskFailed, RiskControl, ContentFiltered, UpstreamError) as exc:
                status = "failed"
                error = exc.to_dict()["error"]
                self.account.ledger.settle(reservation, actual=0.0, success=False)
                raise

            # 结算：优先用余额差作为实测扣减
            charged = plan.estimated_credits
            post: Optional[CreditSnapshot] = None
            try:
                post = self.refresh_credit()
                if pre is not None:
                    charged = max(0.0, pre.total - post.total)
            except UpstreamError:
                pass
            if status == "failed":
                charged = max(0.0, charged)
            self.account.ledger.settle(
                reservation, actual=charged, success=status != "failed", update_balance=post is None
            )

            result = self._result(plan, req, status, outputs, charged, started, upstream_meta, error, post)
            if status in ("submitted", "timeout"):
                result["pending"] = [m["history_id"] for m in upstream_meta]
            if status == "succeeded" and not req.dry_run:
                self._cache_put(cache_key, result)
            return result
        finally:
            self.account.lock.release()

    # ------------------------------------------------------------------
    def _preview(self, plan: Plan, req: GenerateRequest) -> Dict[str, Any]:
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
        status: str,
        outputs: List[Dict[str, Any]],
        charged: float,
        started: float,
        upstream_meta: List[Dict[str, Any]],
        error: Optional[Dict[str, Any]],
        post: Optional[CreditSnapshot],
    ) -> Dict[str, Any]:
        items = []
        for i, img in enumerate(outputs):
            item = {
                "index": i,
                "url": img.get("url"),
                "width": img.get("width"),
                "height": img.get("height"),
                "seed": img.get("seed"),
            }
            if req.response_format == "b64_json" and item["url"]:
                try:
                    resp = self.transport.request("GET", item["url"], headers={"User-Agent": "Mozilla/5.0"})
                    if resp.status == 200:
                        item["b64_json"] = base64.b64encode(resp.body).decode("ascii")
                        item.pop("url", None)
                except UpstreamError:
                    pass
            items.append(item)

        # 上游按 gift → vip → purchase 抵扣，这里只能依据"结算后剩余"做粗判，
        # 精确归属需要上游流水接口（当前未开放），因此不臆造 single source。
        if charged <= 0:
            credit_source = "none"
        elif post is None:
            credit_source = "unknown"
        elif post.gift > 0:
            credit_source = "gift"
        elif post.vip > 0:
            credit_source = "vip"
        else:
            credit_source = "purchase_or_mixed"

        return {
            "id": "task_" + uuid.uuid4().hex[:16],
            "object": "image.generation",
            "created": int(started),
            "completed_at": int(time.time()),
            "elapsed_ms": int((time.time() - started) * 1000),
            "status": status,
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
                "credit_source": credit_source,
            },
            "account": self.account.redacted(),
            "upstream": {
                "submit_id": upstream_meta[-1]["submit_id"] if upstream_meta else None,
                "history_ids": [m["history_id"] for m in upstream_meta],
            },
            "error": error,
            "cached": False,
        }

    # -- 幂等缓存 -----------------------------------------------------------
    CACHE_TTL = 86400.0

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._cache_lock:
            entry = self._idempotent.get(key)
            if not entry:
                return None
            if time.time() - entry[0] > self.CACHE_TTL:
                self._idempotent.pop(key, None)
                return None
            return entry[1]

    def _cache_put(self, key: str, value: Dict[str, Any]) -> None:
        with self._cache_lock:
            if len(self._idempotent) > 512:
                oldest = sorted(self._idempotent.items(), key=lambda kv: kv[1][0])[:128]
                for k, _ in oldest:
                    self._idempotent.pop(k, None)
            self._idempotent[key] = (time.time(), value)
