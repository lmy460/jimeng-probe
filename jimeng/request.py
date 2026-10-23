"""请求模型：参数校验 + 语义归一化 + 成本预估。

这里是"网页端操作"到"稳定 API"的映射层。核心是把网页端几个反直觉的行为
显式化，避免调用方踩坑：

1. 比例是枚举码（16:9 -> 1），且 1:1 是 8 而不是 7。
2. resolution 只是"档位提示"，服务端会对最终像素做归一化
   （实测提交 1024x576，产物为 1280x720）。
3. 4.x 模型一次提交固定产出 4 张（系列组图），所以 n 是最小计费粒度的倍数，
   n=1 在 4.x 上仍会消耗 4 张的额度。
4. 图生图时 prompt 末尾必须补 2 个 '#'/图，这是网页端的隐式约定。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .catalog import DEFAULT_MODEL, RATIO_CODES, RESOLUTION_DIMS, Catalog, ModelSpec
from .errors import CapabilityUnsupported, ParamInvalid

MAX_PROMPT = 2000
MAX_NEGATIVE = 500
MAX_N = 4


@dataclass
class ReferenceImage:
    """参考图来源。四选一：uri（已上传）/ asset_id / url / base64 / path。"""

    uri: Optional[str] = None
    asset_id: Optional[str] = None
    url: Optional[str] = None
    base64: Optional[str] = None
    path: Optional[str] = None
    role: str = "reference"  # reference | first_frame | last_frame（图像模型只用 reference）

    def source(self) -> Tuple[str, str]:
        for key in ("uri", "asset_id", "url", "base64", "path"):
            value = getattr(self, key)
            if value:
                return key, value
        raise ParamInvalid("参考图缺少来源字段（uri/asset_id/url/base64/path）")

    def cache_key(self) -> str:
        kind, value = self.source()
        if kind in ("base64", "path"):
            return f"{kind}:{hashlib.sha256(value.encode()).hexdigest()[:16]}" if kind == "base64" else f"path:{value}"
        return f"{kind}:{value}"

    def to_dict(self) -> Dict[str, Any]:
        kind, value = self.source()
        out: Dict[str, Any] = {"role": self.role, "source": kind}
        out["value"] = value if kind in ("uri", "asset_id", "url") else f"<{kind}>"
        return out


@dataclass
class GenerateRequest:
    prompt: str = ""
    model: str = DEFAULT_MODEL
    negative_prompt: str = ""
    images: List[ReferenceImage] = field(default_factory=list)
    ratio: str = "1:1"          # "auto" = 智能比例（仅 4.0+）
    resolution: str = "2k"
    n: int = 1                  # 期望张数 1..4（受最小计费粒度约束）
    seed: int = -1
    sample_strength: float = 0.5
    intelligent_ratio: Optional[bool] = None
    response_format: str = "url"  # url | b64_json
    dry_run: bool = False
    wait: bool = True
    timeout_s: float = 180.0
    poll_interval_s: float = 3.0
    idempotency_key: Optional[str] = None
    max_credits: Optional[float] = None
    draft_content_encoding: str = "string"  # string | object（服务端两种都接受）

    # -- 由 normalize 填充 ------------------------------------------------
    plan: Optional["Plan"] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerateRequest":
        if not isinstance(data, dict):
            raise ParamInvalid("请求体必须是 JSON 对象")
        images_raw = data.get("images") or []
        if not isinstance(images_raw, list):
            raise ParamInvalid("images 必须是数组")
        images: List[ReferenceImage] = []
        for item in images_raw:
            if isinstance(item, str):
                images.append(_ref_from_string(item))
            elif isinstance(item, dict):
                images.append(ReferenceImage(**{k: v for k, v in item.items() if k in ReferenceImage.__dataclass_fields__}))
            else:
                raise ParamInvalid("images 元素必须是字符串或对象")
        kwargs = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k not in ("images", "plan")}
        return cls(images=images, **kwargs)

    # ------------------------------------------------------------------
    def normalize(self, catalog: Catalog) -> "Plan":
        prompt = (self.prompt or "").strip()
        if not prompt:
            raise ParamInvalid("prompt 不能为空")
        if len(prompt) > MAX_PROMPT:
            raise ParamInvalid(f"prompt 超过 {MAX_PROMPT} 字符")
        if len(self.negative_prompt or "") > MAX_NEGATIVE:
            raise ParamInvalid(f"negative_prompt 超过 {MAX_NEGATIVE} 字符")
        if not isinstance(self.n, int) or not (1 <= self.n <= MAX_N):
            raise ParamInvalid(f"n 必须是 1..{MAX_N} 的整数")

        spec = catalog.get(self.model)
        resolution = (self.resolution or "2k").lower()
        if resolution not in RESOLUTION_DIMS:
            raise ParamInvalid(f"resolution 必须是 {list(RESOLUTION_DIMS)} 之一")
        if not spec.supports(resolution):
            raise CapabilityUnsupported(
                f"{spec.name} 不支持 {resolution} 分辨率",
                details={"model": spec.name, "supported": list(spec.resolutions)},
            )

        ratio = (self.ratio or "1:1").strip()
        intelligent = bool(self.intelligent_ratio) if self.intelligent_ratio is not None else False
        auto_ratio = ratio.lower() == "auto"
        if auto_ratio:
            if not spec.supports_intelligent_ratio:
                raise CapabilityUnsupported(
                    f"{spec.name} 不支持智能比例（ratio=auto）",
                    details={"model": spec.name},
                )
            intelligent, ratio = True, "1:1"  # 占位，提交时省略 image_ratio
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

        if not (0.0 <= float(self.sample_strength) <= 1.0):
            raise ParamInvalid("sample_strength 必须在 0..1")
        if self.response_format not in ("url", "b64_json"):
            raise ParamInvalid("response_format 必须是 url 或 b64_json")
        if self.draft_content_encoding not in ("string", "object"):
            raise ParamInvalid("draft_content_encoding 必须是 string 或 object")

        width, height = RESOLUTION_DIMS[resolution][ratio]
        unit = float(spec.credits_per_image.get(resolution, 1.0))
        batch = max(1, spec.outputs_per_submit)
        submissions = int(math.ceil(self.n / batch))
        planned_images = submissions * batch
        estimated = submissions * batch * unit

        plan = Plan(
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
            planned_images=planned_images,
            unit_credits=unit,
            estimated_credits=estimated,
            seed=self.seed if self.seed is not None else -1,
            sample_strength=float(self.sample_strength),
            image_uris=[img.source()[1] if img.source()[0] == "uri" else None for img in self.images],
            reference_count=len(self.images),
            response_format=self.response_format,
            draft_content_encoding=self.draft_content_encoding,
        )
        self.plan = plan
        return plan

    # ------------------------------------------------------------------
    def cache_fingerprint(self) -> str:
        """幂等/缓存键：只包含影响产出的字段（不含 n 以外的时序字段）。"""
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


@dataclass
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
    image_uris: List[Optional[str]]
    reference_count: int
    response_format: str
    draft_content_encoding: str

    @property
    def effective_prompt(self) -> str:
        # 图生图隐式约定：每张参考图对应两个 '#'
        return self.prompt + ("#" * (2 * self.reference_count) if self.reference_count else "")

    @property
    def mode(self) -> str:
        return "img2img" if self.reference_count else "text2img"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "model": self.model.name,
            "req_key": self.model.req_key,
            "prompt": self.prompt,
            "effective_prompt": self.effective_prompt,
            "negative_prompt": self.negative_prompt,
            "ratio": "auto" if self.intelligent_ratio and self.ratio_code is None else self.ratio,
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


def _ref_from_string(value: str) -> ReferenceImage:
    if value.startswith("data:"):
        return ReferenceImage(base64=value)
    if value.startswith("http://") or value.startswith("https://"):
        return ReferenceImage(url=value)
    if value.startswith("tos-") or value.startswith("asset_"):
        return ReferenceImage(uri=value) if value.startswith("tos-") else ReferenceImage(asset_id=value)
    return ReferenceImage(path=value)
