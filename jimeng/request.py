"""请求模型：参数校验 + 语义归一化 + 成本预估。

这一层负责把"网页端操作"翻译成可计算的 `Plan`，并把几个反直觉行为显式化：

1. 比例是枚举码（16:9 -> 1），`1:1` 是 8 而不是 7。
2. resolution 只是"档位提示"，服务端会归一化最终像素。
3. 4.x 模型一次提交固定产出 4 张（系列组图），所以 n 是最小计费粒度的倍数。
4. 图生图时 prompt 末尾必须补 2 个 '#'/图，这是网页端的隐式约定。

`Plan` 是不可变值对象：需要补充已解析的参考图 URI 时用 `with_references()` 派生新实例，
而不是原地改字段——后者会让"提交用的 plan"和"响应回显的 plan"悄悄不一致。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

from .catalog import DEFAULT_MODEL, RATIO_CODES, RESOLUTION_DIMS, Catalog, ModelSpec
from .errors import CapabilityUnsupported, ParamInvalid

MAX_PROMPT = 2000
MAX_NEGATIVE = 500
MAX_N = 4
REQUEST_FIELDS = frozenset(
    {
        "prompt",
        "model",
        "negative_prompt",
        "images",
        "ratio",
        "resolution",
        "n",
        "seed",
        "sample_strength",
        "intelligent_ratio",
        "response_format",
        "dry_run",
        "wait",
        "timeout_s",
        "poll_interval_s",
        "idempotency_key",
        "max_credits",
        "draft_content_encoding",
    }
)


@dataclass(frozen=True)
class ReferenceImage:
    """参考图来源。五选一：uri（已上传）/ asset_id / url / base64 / path。"""

    uri: Optional[str] = None
    asset_id: Optional[str] = None
    url: Optional[str] = None
    base64: Optional[str] = None
    path: Optional[str] = None
    role: str = "reference"

    @classmethod
    def parse(cls, value: Any) -> "ReferenceImage":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            unknown = set(value) - set(cls.__dataclass_fields__)
            if unknown:
                raise ParamInvalid(f"参考图含未知字段: {sorted(unknown)}")
            return cls(**value)
        if not isinstance(value, str) or not value.strip():
            raise ParamInvalid("images 元素必须是非空字符串或对象")
        if value.startswith("data:"):
            return cls(base64=value)
        if value.startswith(("http://", "https://")):
            return cls(url=value)
        if value.startswith("asset_"):
            return cls(asset_id=value)
        if value.startswith("tos-"):
            return cls(uri=value)
        return cls(path=value)

    def source(self) -> Tuple[str, str]:
        for key in ("uri", "asset_id", "url", "base64", "path"):
            value = getattr(self, key)
            if value:
                return key, value
        raise ParamInvalid("参考图缺少来源字段（uri/asset_id/url/base64/path）")

    def cache_key(self) -> str:
        kind, value = self.source()
        if kind == "base64":
            return "base64:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        if kind == "path":
            return "path:" + value
        return f"{kind}:{value}"

    def to_dict(self) -> Dict[str, Any]:
        kind, value = self.source()
        return {"role": self.role, "source": kind, "value": value if kind in ("uri", "asset_id", "url") else f"<{kind}>"}


@dataclass(frozen=True)
class GenerateRequest:
    prompt: str = ""
    model: str = DEFAULT_MODEL
    negative_prompt: str = ""
    images: Tuple[ReferenceImage, ...] = ()
    ratio: str = "1:1"
    resolution: str = "2k"
    n: int = 1
    seed: int = -1
    sample_strength: float = 0.5
    intelligent_ratio: Optional[bool] = None
    response_format: str = "url"
    dry_run: bool = False
    wait: bool = True
    timeout_s: float = 180.0
    poll_interval_s: float = 3.0
    idempotency_key: Optional[str] = None
    max_credits: Optional[float] = None
    draft_content_encoding: str = "string"

    def __post_init__(self) -> None:
        if not isinstance(self.images, tuple):
            object.__setattr__(self, "images", tuple(self.images))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerateRequest":
        if not isinstance(data, dict):
            raise ParamInvalid("请求体必须是 JSON 对象")
        unknown = set(data) - REQUEST_FIELDS
        if unknown:
            raise ParamInvalid(
                f"未知参数: {sorted(unknown)}",
                details={"allowed": sorted(REQUEST_FIELDS)},
            )
        images = data.get("images") or ()
        if not isinstance(images, (list, tuple)):
            raise ParamInvalid("images 必须是数组")
        payload = {k: v for k, v in data.items() if k != "images"}
        try:
            return cls(images=tuple(ReferenceImage.parse(item) for item in images), **payload)
        except TypeError as exc:
            raise ParamInvalid(f"参数类型不匹配: {exc}") from exc

    # ------------------------------------------------------------------
    def normalize(self, catalog: Catalog) -> "Plan":
        prompt = (self.prompt or "").strip()
        if not prompt:
            raise ParamInvalid("prompt 不能为空")
        if len(prompt) > MAX_PROMPT:
            raise ParamInvalid(f"prompt 超过 {MAX_PROMPT} 字符")
        if len(self.negative_prompt or "") > MAX_NEGATIVE:
            raise ParamInvalid(f"negative_prompt 超过 {MAX_NEGATIVE} 字符")
        if isinstance(self.n, bool) or not isinstance(self.n, int) or not 1 <= self.n <= MAX_N:
            raise ParamInvalid(f"n 必须是 1..{MAX_N} 的整数")

        spec = catalog.get(self.model)
        resolution = (self.resolution or "2k").strip().lower()
        if resolution not in RESOLUTION_DIMS:
            raise ParamInvalid(f"resolution 必须是 {list(RESOLUTION_DIMS)} 之一")
        if not spec.supports(resolution):
            raise CapabilityUnsupported(
                f"{spec.name} 不支持 {resolution} 分辨率",
                details={"model": spec.name, "supported": list(spec.resolutions)},
            )

        ratio = (self.ratio or "1:1").strip()
        auto_ratio = ratio.lower() == "auto"
        intelligent = bool(self.intelligent_ratio) if self.intelligent_ratio is not None else False
        if auto_ratio:
            if not spec.supports_intelligent_ratio:
                raise CapabilityUnsupported(f"{spec.name} 不支持智能比例（ratio=auto）", details={"model": spec.name})
            intelligent, ratio = True, "1:1"
        if ratio not in RATIO_CODES:
            raise ParamInvalid(f"ratio 必须是 {list(RATIO_CODES)} 或 'auto'")
        if intelligent and not spec.supports_intelligent_ratio:
            raise CapabilityUnsupported(f"{spec.name} 不支持 intelligent_ratio")

        if self.images:
            if not spec.supports_reference:
                raise CapabilityUnsupported(f"{spec.name} 不支持参考图")
            if len(self.images) > spec.max_reference_images:
                raise ParamInvalid(
                    f"参考图最多 {spec.max_reference_images} 张",
                    details={"received": len(self.images)},
                )

        if isinstance(self.sample_strength, bool) or not 0.0 <= float(self.sample_strength) <= 1.0:
            raise ParamInvalid("sample_strength 必须在 0..1")
        if self.response_format not in ("url", "b64_json"):
            raise ParamInvalid("response_format 必须是 url 或 b64_json")
        if self.draft_content_encoding not in ("string", "object"):
            raise ParamInvalid("draft_content_encoding 必须是 string 或 object")
        if float(self.timeout_s) <= 0 or float(self.poll_interval_s) < 0.5:
            raise ParamInvalid("timeout_s 必须为正，poll_interval_s 不得小于 0.5")
        if self.max_credits is not None and float(self.max_credits) < 0:
            raise ParamInvalid("max_credits 不能为负")

        width, height = RESOLUTION_DIMS[resolution][ratio]
        unit = float(spec.credits_per_image.get(resolution, 1.0))
        batch = max(1, spec.outputs_per_submit)
        submissions = int(math.ceil(self.n / batch))
        return Plan(
            model=spec,
            prompt=prompt,
            negative_prompt=(self.negative_prompt or "").strip(),
            ratio=ratio,
            ratio_code=None if auto_ratio else RATIO_CODES[ratio],
            intelligent_ratio=intelligent,
            resolution=resolution,
            width=width,
            height=height,
            n=self.n,
            submissions=submissions,
            planned_images=submissions * batch,
            unit_credits=unit,
            estimated_credits=round(submissions * batch * unit, 3),
            seed=self.seed if self.seed is not None else -1,
            sample_strength=float(self.sample_strength),
            image_uris=tuple(None for _ in self.images),
            reference_count=len(self.images),
            response_format=self.response_format,
            draft_content_encoding=self.draft_content_encoding,
        )

    # ------------------------------------------------------------------
    def cache_fingerprint(self) -> str:
        """幂等/缓存键：只包含影响产出的字段。"""
        payload = {
            "prompt": (self.prompt or "").strip(),
            "model": self.model,
            "negative": (self.negative_prompt or "").strip(),
            "ratio": self.ratio,
            "resolution": self.resolution,
            "n": self.n,
            "seed": self.seed,
            "strength": round(float(self.sample_strength), 3),
            "images": sorted(img.cache_key() for img in self.images),
            "intelligent": self.intelligent_ratio,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Plan:
    model: ModelSpec
    prompt: str
    negative_prompt: str
    ratio: str
    ratio_code: Optional[int]
    intelligent_ratio: bool
    resolution: str
    width: int
    height: int
    n: int
    submissions: int
    planned_images: int
    unit_credits: float
    estimated_credits: float
    seed: int
    sample_strength: float
    image_uris: Tuple[Optional[str], ...] = ()
    reference_count: int = 0
    response_format: str = "url"
    draft_content_encoding: str = "string"

    @property
    def effective_prompt(self) -> str:
        return self.prompt + ("#" * (2 * self.reference_count) if self.reference_count else "")

    @property
    def mode(self) -> str:
        return "img2img" if self.reference_count else "text2img"

    def with_references(self, uris: List[str]) -> "Plan":
        if len(uris) != self.reference_count:
            raise ParamInvalid(
                f"参考图数量不匹配：plan 需要 {self.reference_count}，实际 {len(uris)}",
            )
        return replace(self, image_uris=tuple(uris))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "model": self.model.name,
            "req_key": self.model.req_key,
            "prompt": self.prompt,
            "effective_prompt": self.effective_prompt,
            "negative_prompt": self.negative_prompt,
            "ratio": "auto" if self.ratio_code is None else self.ratio,
            "ratio_code": self.ratio_code,
            "resolution": self.resolution,
            "submit_size": {"width": self.width, "height": self.height},
            "n": self.n,
            "submissions": self.submissions,
            "planned_images": self.planned_images,
            "unit_credits": self.unit_credits,
            "estimated_credits": self.estimated_credits,
            "seed": self.seed,
            "sample_strength": self.sample_strength,
            "reference_count": self.reference_count,
            "outputs_per_submit": self.model.outputs_per_submit,
        }
