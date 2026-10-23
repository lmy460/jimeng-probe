"""能力探测：用最小代价把"网页端能做什么"变成"可验证的事实"。

两类探测：
  static  零消耗。把目录里每个 (模型 × 比例 × 分辨率) 组合编译成报文，
          校验结构完整性与约束冲突，产出能力矩阵。
  live    真实提交，逐个验证比例码、分辨率档位、单次产出张数、单位积分成本。
          受 --max-credits 硬约束，超预算即跳过并在报告里标注 skipped。

探测结论写入 calibration.json，Catalog.load() 下次启动即生效——
这就是"探测 → 校准 → 复用"的闭环。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .catalog import RATIO_CODES, RESOLUTION_DIMS, Catalog
from .client import JimengClient
from .errors import JimengError
from .payload import build_generate_body
from .request import GenerateRequest


@dataclass
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
        model: str = "jimeng-4.5",
        ratios: Optional[List[str]] = None,
        resolutions: Optional[List[str]] = None,
        live: bool = False,
    ) -> Dict[str, Any]:
        selected = probes or (["static", "auth", "count", "ratio", "resolution"] if live else ["static"])
        ratios = ratios or ["1:1", "16:9", "9:16", "3:4"]
        resolutions = resolutions or ["1k", "2k"]
        budget_ceiling = self.max_credits

        for name in selected:
            if name == "static":
                self._static(model)
            elif name == "auth":
                self._auth()
            elif not live:
                self.findings.append(Finding(name, "skipped", detail={"reason": "需要 --live 才会真实提交"}))
            elif name == "count":
                self._count(model, budget_ceiling)
            elif name == "ratio":
                self._ratio(model, ratios, budget_ceiling)
            elif name == "resolution":
                self._resolution(model, resolutions, budget_ceiling)
            elif name == "cost":
                self._cost(model, budget_ceiling)
            else:
                self.findings.append(Finding(name, "skipped", detail={"reason": "未知探测项"}))

        return {
            "account": self.client.account.redacted(),
            "live": live,
            "spend": round(self.spend, 3),
            "budget": budget_ceiling,
            "findings": [f.to_dict() for f in self.findings],
            "calibration": self.calibration,
        }

    # ------------------------------------------------------------------
    def _static(self, model: str) -> None:
        self.findings.append(static_matrix(self.catalog, model))

    def _auth(self) -> None:
        try:
            snap = self.client.refresh_credit()
            self.findings.append(
                Finding("auth", "ok", detail={"sessionid_live": True, "credit": snap.to_dict()})
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.append(Finding("auth", "failed", detail={"error": str(exc)}))

    # ------------------------------------------------------------------
    def _run_once(
        self,
        *,
        model: str,
        ratio: str,
        resolution: str,
        budget: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        spec = self.catalog.get(model)
        estimate = spec.outputs_per_submit * float(spec.credits_per_image.get(resolution, 1.0))
        if self.spend + estimate > budget:
            return None
        kwargs = {"prompt": "capability probe, plain gray background", "model": model, "ratio": ratio, "resolution": resolution, "n": 1, "wait": True, "timeout_s": 240.0}
        if extra:
            kwargs.update(extra)
        try:
            result = self.client.generate(GenerateRequest(**kwargs))
        except JimengError as exc:
            self.findings.append(
                Finding(f"{ratio}@{resolution}", "failed", detail={"error": exc.code, "message": exc.message})
            )
            return None
        self.spend += float(result["usage"]["credits_charged"])
        return result

    def _count(self, model: str, budget: float) -> None:
        result = self._run_once(model=model, ratio="1:1", resolution="1k", budget=budget)
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

    def _ratio(self, model: str, ratios: List[str], budget: float) -> None:
        verified: Dict[str, Any] = {}
        for ratio in ratios:
            result = self._run_once(model=model, ratio=ratio, resolution="1k", budget=budget)
            if result is None:
                verified[ratio] = {"status": "skipped"}
                continue
            first = result["data"][0] if result["data"] else {}
            w, h = first.get("width"), first.get("height")
            target = _aspect(ratio)
            observed = (float(w) / float(h)) if (w and h) else None
            verified[ratio] = {
                "status": "ok",
                "requested": ratio,
                "output": f"{w}x{h}" if w and h else None,
                "aspect_error": round(abs(observed - target) / target, 4) if observed else None,
                "honored": (abs(observed - target) / target < 0.05) if observed else None,
            }
            self.spend += float(result["usage"]["credits_charged"])
        self.calibration["ratios"][model] = verified
        self.findings.append(Finding("ratio", "ok", detail={"model": model, "ratios": verified}))

    def _resolution(self, model: str, resolutions: List[str], budget: float) -> None:
        observed: Dict[str, Any] = {}
        for resolution in resolutions:
            result = self._run_once(model=model, ratio="1:1", resolution=resolution, budget=budget)
            if result is None:
                observed[resolution] = {"status": "skipped"}
                continue
            first = result["data"][0] if result["data"] else {}
            w, h = first.get("width"), first.get("height")
            expected = RESOLUTION_DIMS[resolution]["1:1"]
            observed[resolution] = {
                "status": "ok",
                "submitted": f"{expected[0]}x{expected[1]}",
                "output": f"{w}x{h}" if w and h else None,
                "delta_px": (int(w) - expected[0]) if w else None,
                "honored": (int(w) >= expected[0] * 0.9) if w else None,
            }
            self.spend += float(result["usage"]["credits_charged"])
        self.findings.append(Finding("resolution", "ok", detail={"model": model, "resolutions": observed}))

    def _cost(self, model: str, budget: float) -> None:
        spec = self.catalog.get(model)
        captured: Dict[str, float] = {}
        for resolution in spec.resolutions:
            try:
                before = self.client.refresh_credit()
            except JimengError:
                before = None
            result = self._run_once(model=model, ratio="1:1", resolution=resolution, budget=budget)
            if result is None or before is None:
                continue
            try:
                after = self.client.refresh_credit()
            except JimengError:
                continue
            images = max(1, len(result["data"]))
            delta = max(0.0, before.total - after.total)
            unit = round(delta / images, 3)
            captured[resolution] = unit
            self.spend += delta
            self.findings.append(
                Finding(
                    "cost",
                    "ok",
                    spend=delta,
                    detail={"model": model, "resolution": resolution, "images": images, "credits_total": delta, "unit": unit},
                )
            )
        if captured:
            self.calibration["models"].setdefault(model, {})["credits_per_image"] = captured

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
        for name, patch in (self.calibration.get("models") or {}).items():
            models.setdefault(name, {}).update(patch)
        merged = {"version": 1, "generated_at": int(time.time()), "models": models}
        if self.calibration.get("ratios"):
            merged["ratios"] = {**(existing.get("ratios") or {}), **self.calibration["ratios"]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=2)
        return path


def static_matrix(catalog: Catalog, model: str) -> Finding:
    """零消耗：把目录里每个 (比例 × 分辨率) 编译成报文并校验。"""
    spec = catalog.get(model)
    combos: List[Dict[str, Any]] = []
    issues: List[str] = []
    for resolution in spec.resolutions:
        for ratio in spec.ratios:
            req = GenerateRequest(prompt="probe", model=spec.name, ratio=ratio, resolution=resolution, n=1)
            try:
                plan = req.normalize(catalog)
                body = build_generate_body(plan)
                draft = body["draft_content"]
                if isinstance(draft, str):
                    draft = json.loads(draft)
                component = draft["component_list"][0]
                core = component["abilities"]["generate"]["core_param"]
                ok = (
                    core["image_ratio"] == RATIO_CODES[ratio]
                    and core["large_image_info"]["width"] == RESOLUTION_DIMS[resolution][ratio][0]
                    and core["large_image_info"]["height"] == RESOLUTION_DIMS[resolution][ratio][1]
                    and core["large_image_info"]["resolution_type"] == resolution
                )
                if not ok:
                    issues.append(f"{ratio}@{resolution} 报文与目录不一致")
                combos.append({"ratio": ratio, "resolution": resolution, "ok": ok})
            except JimengError as exc:
                issues.append(f"{ratio}@{resolution}: {exc.code} {exc.message}")
    return Finding(
        "static",
        "ok" if not issues else "failed",
        detail={
            "model": spec.name,
            "req_key": spec.req_key,
            "combos_total": len(combos),
            "combos_ok": sum(1 for c in combos if c["ok"]),
            "outputs_per_submit": spec.outputs_per_submit,
            "estimated_credits_for_n1": spec.outputs_per_submit
            * float(spec.credits_per_image.get(spec.resolutions[0], 1.0)),
            "issues": issues,
        },
    )


def _aspect(ratio: str) -> float:
    left, right = ratio.split(":")
    return float(left) / float(right)
