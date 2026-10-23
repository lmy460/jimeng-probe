"""能力探测：用最小代价把"网页端能做什么"变成"可验证的事实"。

两级探测，先免费后付费：
  static  零消耗。把目录里每个 (模型 × 比例 × 分辨率) 组合编译成报文，
          校验码值/尺寸自洽，产出能力矩阵。
  live    真实提交，逐个验证张数、比例、分辨率、单位成本。
          受 max_credits 硬约束，超预算即跳过并在报告里标注 skipped。

计费口径只有一处：`_run_once` 负责把本次消耗计入 `self.spend`。
探测项自己再累加一次就会让预算闸门失真——这类"看起来只是数字"的 bug
会把 8 积分的上限变成实际 16 积分。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .catalog import RATIO_CODES, RESOLUTION_DIMS, Catalog
from .client import JimengClient
from .errors import JimengError
from .payload import build_generate_body
from .request import GenerateRequest

DEFAULT_MODEL = "jimeng-4.5"


@dataclass(frozen=True)
class Finding:
    name: str
    status: str  # ok | skipped | failed
    spend: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "probe": self.name,
            "status": self.status,
            "spend": round(self.spend, 3),
            "detail": self.detail,
            "evidence": self.evidence,
        }


def static_matrix(catalog: Catalog, model: str) -> Finding:
    """零消耗：把目录里每个 (比例 × 分辨率) 编译成报文并校验。"""
    spec = catalog.get(model)
    combos: List[Dict[str, Any]] = []
    issues: List[str] = []
    for resolution in spec.resolutions:
        for ratio in spec.ratios:
            try:
                plan = GenerateRequest(prompt="probe", model=spec.name, ratio=ratio, resolution=resolution).normalize(
                    catalog
                )
                draft = build_generate_body(plan)["draft_content"]
                draft = json.loads(draft) if isinstance(draft, str) else draft
                core = draft["component_list"][0]["abilities"]["generate"]["core_param"]
                info = core["large_image_info"]
                expected = RESOLUTION_DIMS[resolution][ratio]
                ok = (
                    core["image_ratio"] == RATIO_CODES[ratio]
                    and (info["width"], info["height"]) == expected
                    and info["resolution_type"] == resolution
                )
                if not ok:
                    issues.append(f"{ratio}@{resolution} 报文与目录不一致")
                combos.append({"ratio": ratio, "resolution": resolution, "ok": ok})
            except JimengError as exc:
                issues.append(f"{ratio}@{resolution}: {exc.code} {exc.message}")
    first = spec.resolutions[0]
    return Finding(
        "static",
        "ok" if not issues else "failed",
        detail={
            "model": spec.name,
            "req_key": spec.req_key,
            "combos_total": len(combos),
            "combos_ok": sum(1 for combo in combos if combo["ok"]),
            "outputs_per_submit": spec.outputs_per_submit,
            "estimated_credits_for_n1": spec.outputs_per_submit * float(spec.credits_per_image.get(first, 1.0)),
            "issues": issues,
        },
    )


class ProbeRunner:
    def __init__(self, client: JimengClient, catalog: Optional[Catalog] = None, max_credits: float = 8.0) -> None:
        self.client = client
        self.catalog = catalog or client.catalog
        self.max_credits = max_credits
        self.spend = 0.0
        self.findings: List[Finding] = []
        self.calibration: Dict[str, Any] = {"version": 1, "models": {}, "ratios": {}, "generated_at": int(time.time())}

    # ------------------------------------------------------------------
    def run(
        self,
        probes: Optional[List[str]] = None,
        *,
        model: str = DEFAULT_MODEL,
        ratios: Optional[List[str]] = None,
        resolutions: Optional[List[str]] = None,
        live: bool = False,
    ) -> Dict[str, Any]:
        ratios = ratios or ["1:1", "16:9", "9:16", "3:4"]
        resolutions = resolutions or ["1k", "2k"]
        selected = probes or (["static", "auth", "count", "ratio", "resolution"] if live else ["static"])
        handlers: Dict[str, Callable[[], None]] = {
            "static": lambda: self.findings.append(static_matrix(self.catalog, model)),
            "auth": self._auth,
            "count": lambda: self._count(model),
            "ratio": lambda: self._ratio(model, ratios),
            "resolution": lambda: self._resolution(model, resolutions),
            "cost": lambda: self._cost(model),
        }

        for name in selected:
            handler = handlers.get(name)
            if handler is None:
                self.findings.append(Finding(name, "skipped", detail={"reason": "未知探测项"}))
            elif name in ("static", "auth") or live:
                handler()
            else:
                self.findings.append(Finding(name, "skipped", detail={"reason": "需要 --live 才会真实提交"}))

        return {
            "account": self.client.account.redacted(),
            "live": live,
            "spend": round(self.spend, 3),
            "budget": self.max_credits,
            "findings": [f.to_dict() for f in self.findings],
            "calibration": self.calibration,
        }

    # ------------------------------------------------------------------
    def _auth(self) -> None:
        try:
            snapshot = self.client.refresh_credit()
        except Exception as exc:  # noqa: BLE001 - 探测需要把失败如实报出
            self.findings.append(Finding("auth", "failed", detail={"error": str(exc)}))
            return
        self.findings.append(Finding("auth", "ok", detail={"sessionid_live": True, "credit": snapshot.to_dict()}))

    def _estimate(self, model: str, resolution: str) -> float:
        spec = self.catalog.get(model)
        return spec.outputs_per_submit * float(spec.credits_per_image.get(resolution, 1.0))

    def _run_once(
        self,
        *,
        model: str,
        ratio: str,
        resolution: str,
        charge: bool = True,
    ) -> Optional[Dict[str, Any]]:
        estimate = self._estimate(model, resolution)
        if self.spend + estimate > self.max_credits:
            return None
        try:
            result = self.client.generate(
                GenerateRequest(
                    prompt="capability probe, plain gray background",
                    model=model,
                    ratio=ratio,
                    resolution=resolution,
                    wait=True,
                    timeout_s=240.0,
                )
            )
        except JimengError as exc:
            self.findings.append(
                Finding("submit", "failed", detail={"model": model, "ratio": ratio, "resolution": resolution,
                                                   "error": exc.code, "message": exc.message})
            )
            return None
        if charge:
            self.spend += float(result["usage"]["credits_charged"])
        return result

    # ------------------------------------------------------------------
    def _count(self, model: str) -> None:
        result = self._run_once(model=model, ratio="1:1", resolution="1k")
        if result is None:
            self.findings.append(Finding("count", "skipped", detail={"reason": "预算不足或提交失败"}))
            return
        returned = len(result["data"])
        self.calibration["models"].setdefault(model, {})["outputs_per_submit"] = returned
        self.findings.append(
            Finding(
                "count",
                "ok",
                spend=float(result["usage"]["credits_charged"]),
                detail={
                    "model": model,
                    "requested_n": 1,
                    "images_returned": returned,
                    "billing_granularity": returned,
                    "note": "返回张数即最小计费粒度；n 必须是它的倍数才不会浪费额度",
                },
            )
        )

    def _ratio(self, model: str, ratios: List[str]) -> None:
        observed: Dict[str, Any] = {}
        spent = 0.0
        for ratio in ratios:
            result = self._run_once(model=model, ratio=ratio, resolution="1k")
            observed[ratio] = self._observe_ratio(ratio, result)
            if result is not None:
                spent += float(result["usage"]["credits_charged"])
        self.calibration["ratios"][model] = observed
        self.findings.append(Finding("ratio", "ok", spend=spent, detail={"model": model, "ratios": observed}))

    def _resolution(self, model: str, resolutions: List[str]) -> None:
        observed: Dict[str, Any] = {}
        spent = 0.0
        for resolution in resolutions:
            result = self._run_once(model=model, ratio="1:1", resolution=resolution)
            observed[resolution] = self._observe_resolution(resolution, result)
            if result is not None:
                spent += float(result["usage"]["credits_charged"])
        self.findings.append(
            Finding("resolution", "ok", spend=spent, detail={"model": model, "resolutions": observed})
        )

    def _cost(self, model: str) -> None:
        spec = self.catalog.get(model)
        units: Dict[str, float] = {}
        for resolution in spec.resolutions:
            before = self._snapshot()
            result = self._run_once(model=model, ratio="1:1", resolution=resolution, charge=False)
            after = self._snapshot()
            if result is None or before is None or after is None:
                continue
            images = max(1, len(result["data"]))
            delta = max(0.0, before - after)
            units[resolution] = round(delta / images, 3)
            self.spend += delta
            self.findings.append(
                Finding(
                    "cost",
                    "ok",
                    spend=delta,
                    detail={
                        "model": model,
                        "resolution": resolution,
                        "images": images,
                        "credits_total": delta,
                        "unit": units[resolution],
                    },
                )
            )
        if units:
            self.calibration["models"].setdefault(model, {})["credits_per_image"] = units

    def _snapshot(self) -> Optional[float]:
        try:
            return self.client.refresh_credit().total
        except JimengError:
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _observe_ratio(ratio: str, result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if result is None:
            return {"status": "skipped"}
        first = result["data"][0] if result["data"] else {}
        width, height = first.get("width"), first.get("height")
        left, right = ratio.split(":")
        target = float(left) / float(right)
        observed = (float(width) / float(height)) if width and height else None
        return {
            "status": "ok",
            "requested": ratio,
            "output": f"{width}x{height}" if width and height else None,
            "aspect_error": round(abs(observed - target) / target, 4) if observed else None,
            "honored": (abs(observed - target) / target < 0.05) if observed else None,
        }

    @staticmethod
    def _observe_resolution(resolution: str, result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if result is None:
            return {"status": "skipped"}
        first = result["data"][0] if result["data"] else {}
        width, height = first.get("width"), first.get("height")
        expected = RESOLUTION_DIMS[resolution]["1:1"]
        return {
            "status": "ok",
            "submitted": f"{expected[0]}x{expected[1]}",
            "output": f"{width}x{height}" if width and height else None,
            "delta_px": (int(width) - expected[0]) if width else None,
            "honored": (int(width) >= expected[0] * 0.9) if width else None,
        }

    # ------------------------------------------------------------------
    def write_calibration(self, path: Optional[str] = None) -> str:
        path = path or os.environ.get("JIMENG_CALIBRATION") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "calibration.json"
        )
        existing: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
            except (OSError, ValueError):
                existing = {}

        models = existing.get("models") or {}
        for name, patch in self.calibration.get("models", {}).items():
            models.setdefault(name, {}).update(patch)
        merged: Dict[str, Any] = {"version": 1, "generated_at": int(time.time()), "models": models}
        ratios = {**(existing.get("ratios") or {}), **self.calibration.get("ratios", {})}
        if ratios:
            merged["ratios"] = ratios
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=2)
        return path
