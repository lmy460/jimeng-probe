"""上游报文解析工具。

即梦的返回结构在版本间反复变化（history_list / history_records / list，
large_images / image_list / images），因此这里统一用"容错提取"：
优先走已知路径，失败后退化为带语义提示的递归扫描，最后去重。
所有函数都是纯函数，便于单测覆盖。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

IMAGE_URL_KEYS = ("url", "image_url", "download_url")


def pick_history(payload: Any, history_id: str) -> Dict[str, Any]:
    """从 get_history_by_ids 的返回里挑出目标记录。"""
    records = history_records(payload)
    for record in records:
        if not isinstance(record, dict):
            continue
        rid = record.get("history_id") or record.get("history_record_id") or record.get("id")
        if rid is None or str(rid) == str(history_id):
            return record
    return records[0] if records and isinstance(records[0], dict) else {}


def history_records(payload: Any) -> List[Any]:
    """history_list / history_records / list 三种键名的统一入口。"""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for key in ("history_list", "history_records", "list"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else []


def find_dict(obj: Any, key: str, depth: int = 0) -> Optional[Dict[str, Any]]:
    """在嵌套结构里找第一个以 key 为名的 dict（用于 proof_info 这类包装层）。"""
    if depth > 5:
        return None
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, dict):
            return value
        for child in obj.values():
            found = find_dict(child, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_dict(item, key, depth + 1)
            if found is not None:
                return found
    return None


def extract_images(record: Any) -> List[Dict[str, Any]]:
    """提取产物图：优先 item_list[].image.large_images，其次通用扫描。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def add(url: str, node: Dict[str, Any]) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        out.append(
            {
                "url": url,
                "width": node.get("width") or node.get("w"),
                "height": node.get("height") or node.get("h"),
                "seed": node.get("seed"),
            }
        )

    if isinstance(record, dict):
        for item in record.get("item_list") or record.get("items") or []:
            if not isinstance(item, dict):
                continue
            image = item.get("image") if isinstance(item.get("image"), dict) else item
            for key in ("large_images", "image_list", "images"):
                for node in image.get(key) or []:
                    if isinstance(node, dict):
                        add(node.get("url") or node.get("image_url") or "", node)
            for key in IMAGE_URL_KEYS:
                if isinstance(image.get(key), str):
                    add(image[key], image)
            common = item.get("common_attr") if isinstance(item.get("common_attr"), dict) else {}
            if isinstance(common.get("cover_url"), str):
                add(common["cover_url"], common)

    if not out:
        _scan_any(record, add)
    return out


def _scan_any(node: Any, add, depth: int = 0, key_hint: str = "") -> None:
    if depth > 6:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value.startswith("http") and key in IMAGE_URL_KEYS:
                if "image" in key_hint or "image" in key or "image" in value.lower():
                    add(value, node)
            elif isinstance(value, (dict, list)):
                _scan_any(value, add, depth + 1, key)
    elif isinstance(node, list):
        for item in node:
            _scan_any(item, add, depth + 1, key_hint)


def fail_reason(record: Any) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    for key in ("fail_msg", "fail_reason", "errmsg", "message"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    task = record.get("task")
    if isinstance(task, dict):
        for key in ("fail_msg", "fail_reason", "errmsg"):
            value = task.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def find_scalar(obj: Any, keys: Tuple[str, ...], depth: int = 0) -> Optional[Any]:
    """在嵌套结构里找第一个标量（用于 history_record_id 这类单值字段）。"""
    if depth > 5:
        return None
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, (str, int)) and str(value):
                return value
        for value in obj.values():
            found = find_scalar(value, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj[:5]:
            found = find_scalar(item, keys, depth + 1)
            if found is not None:
                return found
    return None


def find_list(obj: Any, keys: Tuple[str, ...], depth: int = 0) -> List[Any]:
    if depth > 5:
        return []
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, list) and value:
                return value
        for value in obj.values():
            found = find_list(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_list(item, keys, depth + 1)
            if found:
                return found
    return []


def has_credit_field(payload: Any) -> bool:
    """余额接口返回是否包含 credit 语义（用于识别"200 但结构不对"的降级场景）。"""
    if isinstance(payload, dict):
        return any("credit" in str(k).lower() for k in payload) or any(
            has_credit_field(v) for v in payload.values() if isinstance(v, (dict, list))
        )
    if isinstance(payload, list):
        return any(has_credit_field(item) for item in payload)
    if isinstance(payload, str):
        return "credit" in payload.lower()
    return False
