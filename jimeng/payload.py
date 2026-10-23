"""上游报文构造：把 Plan 编译成 /mweb/v1/aigc_draft/generate 需要的嵌套结构。

结构高度冗余（每个节点都要 type/id），但字段语义只有几处关键：
  core_param.model            内部模型 key
  core_param.image_ratio      比例枚举码（智能比例时省略）
  core_param.large_image_info 提交尺寸 + resolution_type
  gen_option.generate_number  期望张数（4.x 会被归一化到 4）
  generate_type               text2img -> "generate"，img2img -> "blend"
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .request import Plan
from .sign import CN_AID

DRAFT_VERSION = "3.3.9"
COMPONENT_MIN_VERSION = "3.3.9"


def _uid() -> str:
    return str(uuid.uuid4())


def _short_id() -> str:
    return uuid.uuid4().hex[:16]


def _node(**kwargs: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {"type": "", "id": _uid()}
    base.update(kwargs)
    return base


def build_draft_content(plan: Plan, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    component_id = _uid()
    now = int(now_ms if now_ms is not None else time.time() * 1000)

    core_param: Dict[str, Any] = {
        "type": "",
        "id": _uid(),
        "model": plan.model.req_key,
        "prompt": plan.effective_prompt,
        "negative_prompt": plan.negative_prompt,
        "seed": plan.seed,
        "sample_strength": plan.sample_strength,
        "intelligent_ratio": plan.intelligent_ratio,
        "large_image_info": {
            "type": "",
            "id": _uid(),
            "width": plan.width,
            "height": plan.height,
            "resolution_type": plan.resolution,
        },
    }
    # 智能比例：服务端自行推断，必须省略 image_ratio
    if plan.ratio_code is not None:
        core_param["image_ratio"] = plan.ratio_code

    gen_option = {
        "type": "",
        "id": _uid(),
        "generate_all": False,
        "generate_number": plan.n,
    }
    history_option = _node()

    if plan.reference_count:
        ability_list = []
        for uri in plan.image_uris:
            ability_list.append(
                {
                    "type": "",
                    "id": _uid(),
                    "name": "byte_edit",
                    "image_uri_list": [uri],
                    "image_list": [_image_node(uri)],
                    "strength": plan.sample_strength,
                }
            )

        blend = {
            "type": "",
            "id": _uid(),
            "min_features": [],
            "core_param": core_param,
            "ability_list": ability_list,
            "history_option": history_option,
            "prompt_placeholder_info_list": [
                {"type": "", "id": _uid(), "ability_index": idx} for idx in range(len(ability_list))
            ],
            "postedit_param": {"type": "", "id": _uid(), "generate_type": 0},
        }
        abilities: Dict[str, Any] = {"type": "", "id": _uid(), "blend": blend}
        generate_type = "blend"
    else:
        generate = {
            "type": "",
            "id": _uid(),
            "core_param": core_param,
            "gen_option": gen_option,
            "history_option": history_option,
        }
        abilities = {"type": "", "id": _uid(), "generate": generate}
        generate_type = "generate"

    component = {
        "type": "image_base_component",
        "id": component_id,
        "min_version": COMPONENT_MIN_VERSION,
        "aigc_mode": "workbench",
        "gen_type": 1,
        "generate_type": generate_type,
        "metadata": {
            "type": "",
            "id": _uid(),
            "created_platform": 3,
            "created_platform_version": "",
            "created_time_in_ms": now,
            "created_did": "",
        },
        "abilities": abilities,
    }

    return {
        "type": "draft",
        "id": _short_id(),
        "min_version": DRAFT_VERSION,
        "is_from_tsn": True,
        "version": DRAFT_VERSION,
        "main_component_id": component_id,
        "component_list": [component],
    }


def _image_node(uri: str) -> Dict[str, Any]:
    return {
        "type": "image",
        "id": _uid(),
        "source_from": "upload",
        "platform_type": 1,
        "name": "",
        "image_uri": uri,
        "width": 0,
        "height": 0,
        "format": "",
        "uri": uri,
    }


def build_metrics_extra(plan: Plan, submit_id: str, *, scene: Optional[str] = None) -> str:
    payload = {
        "promptSource": "custom",
        "generateCount": plan.n,
        "enterFrom": "click",
        "position": "page_bottom_box",
        "scene": scene or ("ImageMultiGenerate" if plan.n > 1 else "ImageBasicGenerate"),
        "functionMode": "workbench",
        "resolutionType": plan.resolution,
        "isMultiImage": plan.n > 1,
        "isDefaultSeed": 1 if plan.seed in (-1, None) else 0,
        "originSubmitId": submit_id,
        "submitId": submit_id,
        "referenceCnt": plan.reference_count,
        "modelReqKey": plan.model.req_key,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_generate_body(plan: Plan, submit_id: Optional[str] = None) -> Dict[str, Any]:
    submit_id = submit_id or uuid.uuid4().hex
    draft = build_draft_content(plan)
    body: Dict[str, Any] = {
        "extend": {
            "root_model": plan.model.req_key,
            "template_id": "",
            "workspace_id": 0,
        },
        "submit_id": submit_id,
        "metrics_extra": build_metrics_extra(plan, submit_id),
        "http_common_info": {"aid": int(CN_AID)},
    }
    if plan.draft_content_encoding == "object":
        body["draft_content"] = draft
    else:
        body["draft_content"] = json.dumps(draft, ensure_ascii=False, separators=(",", ":"))
    return body


def build_history_body(history_ids: List[str]) -> Dict[str, Any]:
    return {
        "history_ids": list(history_ids),
        "http_common_info": {"aid": int(CN_AID)},
    }
