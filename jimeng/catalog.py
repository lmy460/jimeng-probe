"""模型 / 比例 / 分辨率 能力目录。

所有数值都来自对网页端的探测与公开抓包整理，属于"经验值"，因此每条记录都带
confidence 字段：default（默认推测） / calibrated（探测实测写入）。

关键设计：目录是可探测、可覆盖的，而不是硬编码后不可变。探测模块跑完后会把
实测结论写回 calibration 文件，client 在运行时优先读取实测值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 比例：网页端 image_ratio 是枚举码，不是字符串
# ---------------------------------------------------------------------------
RATIO_CODES: Dict[str, int] = {
    "21:9": 0,
    "16:9": 1,
    "3:2": 2,
    "4:3": 3,
    "3:4": 4,
    "2:3": 5,
    "9:16": 6,
    "1:1": 8,
}

# 分辨率档位 -> 各比例下的提交尺寸（服务端可能对最终像素做归一化）
RESOLUTION_DIMS: Dict[str, Dict[str, Tuple[int, int]]] = {
    "1k": {
        "1:1": (1024, 1024),
        "4:3": (1024, 768),
        "3:4": (768, 1024),
        "16:9": (1024, 576),
        "9:16": (576, 1024),
        "3:2": (1024, 682),
        "2:3": (682, 1024),
        "21:9": (1195, 512),
    },
    "2k": {
        "1:1": (2048, 2048),
        "4:3": (2304, 1728),
        "3:4": (1728, 2304),
        "16:9": (2560, 1440),
        "9:16": (1440, 2560),
        "3:2": (2496, 1664),
        "2:3": (1664, 2496),
        "21:9": (3024, 1296),
    },
    "4k": {
        "1:1": (4096, 4096),
        "4:3": (4608, 3456),
        "3:4": (3456, 4608),
        "16:9": (5120, 2880),
        "9:16": (2880, 5120),
        "3:2": (4992, 3328),
        "2:3": (3328, 4992),
        "21:9": (6048, 2592),
    },
}

RESOLUTIONS: Tuple[str, ...] = ("1k", "2k", "4k")
RATIOS: Tuple[str, ...] = tuple(RATIO_CODES.keys())


@dataclass(frozen=True)
class ModelSpec:
    """单个图像模型的静态能力描述。"""

    name: str
    req_key: str
    label: str
    outputs_per_submit: int = 1
    resolutions: Tuple[str, ...] = ("1k", "2k")
    ratios: Tuple[str, ...] = RATIOS
    supports_reference: bool = True
    supports_intelligent_ratio: bool = False
    max_reference_images: int = 10
    credits_per_image: Dict[str, float] = field(default_factory=lambda: {"1k": 1.0, "2k": 1.0})
    confidence: str = "default"
    note: str = ""

    def supports(self, resolution: str) -> bool:
        return resolution in self.resolutions

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.name,
            "req_key": self.req_key,
            "label": self.label,
            "outputs_per_submit": self.outputs_per_submit,
            "resolutions": list(self.resolutions),
            "ratios": list(self.ratios),
            "supports_reference": self.supports_reference,
            "supports_intelligent_ratio": self.supports_intelligent_ratio,
            "max_reference_images": self.max_reference_images,
            "credits_per_image": self.credits_per_image,
            "confidence": self.confidence,
            "note": self.note,
        }


# 中国站（jimeng.jianying.com）图像模型。4.x 默认一次出 4 张（系列组图），
# 这是"指定张数"语义差异的根源，必须由探测确认。
_DEFAULT_MODELS: Tuple[ModelSpec, ...] = (
    ModelSpec(
        name="jimeng-3.0",
        req_key="high_aes_general_v30l",
        label="图片 3.0 通用",
        outputs_per_submit=1,
        resolutions=("1k", "2k"),
        supports_intelligent_ratio=False,
        credits_per_image={"1k": 1.0, "2k": 1.0},
        note="单图输出，成本最低，适合做探测基线",
    ),
    ModelSpec(
        name="jimeng-3.1",
        req_key="high_aes_general_v30l_art_fangzhou",
        label="图片 3.1 艺术",
        outputs_per_submit=1,
        resolutions=("1k", "2k"),
        note="艺术风格分支，仅中国站",
    ),
    ModelSpec(
        name="jimeng-4.0",
        req_key="high_aes_general_v40",
        label="图片 4.0",
        outputs_per_submit=4,
        resolutions=("1k", "2k", "4k"),
        supports_intelligent_ratio=True,
        credits_per_image={"1k": 1.0, "2k": 1.0, "4k": 2.0},
        note="系列组图，一次提交固定出 4 张",
    ),
    ModelSpec(
        name="jimeng-4.1",
        req_key="high_aes_general_v41",
        label="图片 4.1",
        outputs_per_submit=4,
        resolutions=("1k", "2k", "4k"),
        supports_intelligent_ratio=True,
        credits_per_image={"1k": 1.0, "2k": 1.0, "4k": 2.0},
    ),
    ModelSpec(
        name="jimeng-4.5",
        req_key="high_aes_general_v40l",
        label="图片 4.5（默认）",
        outputs_per_submit=4,
        resolutions=("1k", "2k", "4k"),
        supports_intelligent_ratio=True,
        credits_per_image={"1k": 1.0, "2k": 1.0, "4k": 2.0},
        note="默认模型，4k 与 intelligent_ratio 支持最完整",
    ),
    ModelSpec(
        name="jimeng-4.6",
        req_key="high_aes_general_v42",
        label="图片 4.6",
        outputs_per_submit=4,
        resolutions=("1k", "2k", "4k"),
        supports_intelligent_ratio=True,
        credits_per_image={"1k": 1.0, "2k": 1.0, "4k": 2.0},
    ),
    ModelSpec(
        name="jimeng-5.0",
        req_key="high_aes_general_v50",
        label="图片 5.0 预览",
        outputs_per_submit=4,
        resolutions=("1k", "2k", "4k"),
        supports_intelligent_ratio=True,
        credits_per_image={"1k": 1.0, "2k": 1.0, "4k": 2.0},
        confidence="default",
        note="预览模型，可能随时变更",
    ),
)

DEFAULT_MODEL = "jimeng-4.5"


class Catalog:
    """模型目录 + 运行期校准覆盖。"""

    def __init__(self, models: Optional[List[ModelSpec]] = None) -> None:
        self._models: Dict[str, ModelSpec] = {m.name: m for m in (models or _DEFAULT_MODELS)}
        self._aliases: Dict[str, str] = {}

    # -- 查询 ---------------------------------------------------------------
    def names(self) -> List[str]:
        return list(self._models.keys())

    def get(self, name: Optional[str]) -> ModelSpec:
        from .errors import ParamInvalid

        if not name:
            name = DEFAULT_MODEL
        key = self._aliases.get(name.lower(), name)
        spec = self._models.get(key)
        if spec is None:
            # 允许直接传内部 req_key
            for m in self._models.values():
                if m.req_key == name:
                    return m
            raise ParamInvalid(
                f"未知模型: {name}",
                details={"available": self.names()},
            )
        return spec

    def all(self) -> List[ModelSpec]:
        return list(self._models.values())

    # -- 校准 ---------------------------------------------------------------
    def apply_calibration(self, payload: Dict[str, object]) -> None:
        """把探测结论（outputs_per_submit / credits_per_image / resolutions）写回目录。"""
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            return
        for name, patch in models.items():
            spec = self._models.get(name)
            if spec is None or not isinstance(patch, dict):
                continue
            changes = {}
            if isinstance(patch.get("outputs_per_submit"), int) and patch["outputs_per_submit"] > 0:
                changes["outputs_per_submit"] = patch["outputs_per_submit"]
            if isinstance(patch.get("credits_per_image"), dict):
                merged = dict(spec.credits_per_image)
                merged.update({k: float(v) for k, v in patch["credits_per_image"].items()})
                changes["credits_per_image"] = merged
            if isinstance(patch.get("resolutions"), list) and patch["resolutions"]:
                changes["resolutions"] = tuple(str(x) for x in patch["resolutions"])
            if changes:
                changes["confidence"] = "calibrated"
                self._models[name] = replace(spec, **changes)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Catalog":
        cat = cls()
        path = path or os.environ.get("JIMENG_CALIBRATION") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "calibration.json"
        )
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cat.apply_calibration(json.load(fh))
            except (OSError, ValueError):
                pass
        return cat

    def dump(self) -> Dict[str, object]:
        return {"models": [m.to_dict() for m in self.all()]}
