"""图片上传：先拿 ImageX 上传凭证，再把二进制推到 CDN，最后提交内容审核。

流程（实测可用）：
  1. POST /mweb/v1/get_upload_image_proof  {scene: "aigc_image"}
     -> data.proof_info = {headers, query_params, image_uri}
  2. POST https://imagex.bytedanceapi.com/?<query_params>   multipart 上传
  3. POST /mweb/v1/imagex/submit_audit_job  {image_uri}     （不审核的图无法作为参考图）
返回值 image_uri 形如 tos-cn-i-tb4s082cfz/xxxx，后续作为参考图 URI 使用。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import ParamInvalid, UpstreamError
from .sign import CN_AID, CN_HOST, build_headers
from .transport import Transport

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 网页端单图上限（超过由服务端拒绝）


@dataclass
class Asset:
    asset_id: str
    image_uri: str
    uri: str
    width: int = 0
    height: int = 0
    format: str = ""
    bytes: int = 0
    sha256: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "image_uri": self.image_uri,
            "name": os.path.basename(self.image_uri),
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


class ImageUploader:
    def __init__(self, transport: Transport, host: str = CN_HOST, region: str = "cn") -> None:
        self.transport = transport
        self.host = host
        self.region = region

    # ------------------------------------------------------------------
    def upload_bytes(self, content: bytes, filename: str = "image.png", sessionid: str = "", appvr: str = "") -> Asset:
        if not content:
            raise ParamInvalid("图片内容为空")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ParamInvalid(f"图片超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限")

        digest = hashlib.sha256(content).hexdigest()
        proof = self._get_proof(filename, len(content), sessionid=sessionid, appvr=appvr)
        self._push_to_imagex(proof, content, filename)
        self._submit_audit(proof["image_uri"], sessionid=sessionid, appvr=appvr)

        fmt = os.path.splitext(filename)[1].lstrip(".").lower() or "png"
        return Asset(
            asset_id="asset_" + digest[:16],
            image_uri=proof["image_uri"],
            uri=proof["image_uri"],
            format=fmt,
            bytes=len(content),
            sha256=digest,
        )

    def upload_path(self, path: str, sessionid: str = "", appvr: str = "") -> Asset:
        if not os.path.exists(path):
            raise ParamInvalid(f"文件不存在: {path}")
        with open(path, "rb") as fh:
            content = fh.read()
        return self.upload_bytes(content, os.path.basename(path), sessionid=sessionid, appvr=appvr)

    # ------------------------------------------------------------------
    def _get_proof(self, filename: str, size: int, *, sessionid: str, appvr: str) -> Dict[str, Any]:
        uri = "/mweb/v1/get_upload_image_proof"
        headers = build_headers(uri, sessionid, appvr=appvr or "5.8.0", origin=self.host)
        body = {
            "scene": "aigc_image",
            "file_name": filename,
            "file_size": size,
            "http_common_info": {"aid": int(CN_AID)},
        }
        resp = self.transport.request(
            "POST",
            f"{self.host}{uri}?aid={CN_AID}&device_platform=web&region={self.region.upper()}",
            headers=headers,
            json_body=body,
        )
        payload = resp.json()
        info = _dig(payload, ("data", "proof_info")) or _find_dict(payload, "proof_info")
        if not isinstance(info, dict) or not info.get("image_uri"):
            raise UpstreamError(
                "获取上传凭证失败",
                details={"status": resp.status, "upstream": _brief(payload)},
            )
        return {
            "headers": info.get("headers") or {},
            "query_params": info.get("query_params") or {},
            "image_uri": info["image_uri"],
        }

    def _push_to_imagex(self, proof: Dict[str, Any], content: bytes, filename: str) -> None:
        query = "&".join(f"{k}={v}" for k, v in dict(proof["query_params"]).items())
        url = "https://imagex.bytedanceapi.com/" + (f"?{query}" if query else "")
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body, content_type = Transport.encode_multipart({}, [("file", filename, content, ctype)])
        headers = dict(proof.get("headers") or {})
        headers["Content-Type"] = content_type
        resp = self.transport.request("POST", url, headers=headers, data=body, timeout=60)
        if resp.status != 200:
            raise UpstreamError(
                "ImageX 上传失败",
                details={"status": resp.status, "body": resp.text[:300]},
            )

    def _submit_audit(self, image_uri: str, *, sessionid: str, appvr: str) -> None:
        uri = "/mweb/v1/imagex/submit_audit_job"
        headers = build_headers(uri, sessionid, appvr=appvr or "5.8.0", origin=self.host)
        resp = self.transport.request(
            "POST",
            f"{self.host}{uri}?aid={CN_AID}&device_platform=web&region={self.region.upper()}",
            headers=headers,
            json_body={"image_uri": image_uri, "http_common_info": {"aid": int(CN_AID)}},
        )
        payload = resp.json()
        ret = str(payload.get("ret", "0"))
        # 3020: 审核服务下载不到文件（CDN 尚未就绪），重试一次常见可恢复
        if ret not in ("0", ""):
            for _ in range(2):
                time.sleep(1.2)
                resp = self.transport.request(
                    "POST",
                    f"{self.host}{uri}?aid={CN_AID}&device_platform=web&region={self.region.upper()}",
                    headers=build_headers(uri, sessionid, appvr=appvr or "5.8.0", origin=self.host),
                    json_body={"image_uri": image_uri, "http_common_info": {"aid": int(CN_AID)}},
                )
                payload = resp.json()
                if str(payload.get("ret", "0")) in ("0", ""):
                    return
            raise UpstreamError(
                "参考图审核提交失败",
                details={"ret": payload.get("ret"), "errmsg": payload.get("errmsg")},
            )


# ----------------------------------------------------------------------
def _dig(obj: Any, path: tuple) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _find_dict(obj: Any, key: str, depth: int = 0) -> Optional[Dict[str, Any]]:
    if depth > 5:
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get(key), dict):
            return obj[key]
        for value in obj.values():
            found = _find_dict(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_dict(item, key, depth + 1)
            if found is not None:
                return found
    return None


def _brief(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: payload[k] for k in list(payload)[:6]}
    return str(payload)[:300]
