"""图片上传：先拿 ImageX 上传凭证，再把二进制推到 CDN，最后提交内容审核。

流程（实测可用）：
  1. POST /mweb/v1/get_upload_image_proof  {scene: "aigc_image"}
     -> data.proof_info = {headers, query_params, image_uri}
  2. POST https://imagex.bytedanceapi.com/?<query_params>   multipart 上传
  3. POST /mweb/v1/imagex/submit_audit_job  {image_uri}     未过审的图不能当参考图

上传是**非幂等** POST：失败不自动重试，交由调用方决定（重试最多浪费一次上传，不影响积分）。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import urlencode

from .errors import ParamInvalid, UpstreamError
from .parsing import find_dict
from .sign import CN_AID, CN_HOST, build_headers
from .transport import Response, Transport

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
AUDIT_RETRY_DELAYS = (1.2, 2.0)


@dataclass(frozen=True)
class Asset:
    asset_id: str
    image_uri: str
    uri: str
    width: int = 0
    height: int = 0
    format: str = ""
    bytes: int = 0
    sha256: str = ""

    @classmethod
    def from_content(cls, content: bytes, image_uri: str, filename: str) -> "Asset":
        digest = hashlib.sha256(content).hexdigest()
        return cls(
            asset_id="asset_" + digest[:16],
            image_uri=image_uri,
            uri=image_uri,
            format=os.path.splitext(filename)[1].lstrip(".").lower() or "png",
            bytes=len(content),
            sha256=digest,
        )

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
        proof = self._get_proof(filename, len(content), sessionid=sessionid, appvr=appvr)
        self._push_to_imagex(proof, content, filename)
        self._submit_audit(proof["image_uri"], sessionid=sessionid, appvr=appvr)
        return Asset.from_content(content, proof["image_uri"], filename)

    def upload_path(self, path: str, sessionid: str = "", appvr: str = "") -> Asset:
        if not os.path.exists(path):
            raise ParamInvalid(f"文件不存在: {path}")
        with open(path, "rb") as fh:
            content = fh.read()
        return self.upload_bytes(content, os.path.basename(path), sessionid=sessionid, appvr=appvr)

    # ------------------------------------------------------------------
    def _url(self, uri: str) -> str:
        query = urlencode(
            {
                "aid": CN_AID,
                "device_platform": "web",
                "region": self.region.upper(),
            }
        )
        return f"{self.host}{uri}?{query}"

    def _get_proof(self, filename: str, size: int, *, sessionid: str, appvr: str) -> Dict[str, Any]:
        uri = "/mweb/v1/get_upload_image_proof"
        headers = build_headers(uri, sessionid, appvr=appvr or "5.8.0", origin=self.host)
        payload = self.transport.request(
            "POST",
            self._url(uri),
            headers=headers,
            json_body={
                "scene": "aigc_image",
                "file_name": filename,
                "file_size": size,
                "http_common_info": {"aid": int(CN_AID)},
            },
        ).json()
        info = find_dict(payload, "proof_info")
        if not isinstance(info, dict) or not info.get("image_uri"):
            raise UpstreamError("获取上传凭证失败", details={"upstream": _brief(payload)})
        return {
            "headers": info.get("headers") or {},
            "query_params": info.get("query_params") or {},
            "image_uri": info["image_uri"],
        }

    def _push_to_imagex(self, proof: Dict[str, Any], content: bytes, filename: str) -> None:
        query = urlencode(dict(proof["query_params"]))
        url = "https://imagex.bytedanceapi.com/" + (f"?{query}" if query else "")
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body, content_type = Transport.encode_multipart({}, [("file", filename, content, ctype)])
        headers = dict(proof.get("headers") or {})
        headers["Content-Type"] = content_type
        resp: Response = self.transport.request("POST", url, headers=headers, data=body, timeout=60)
        if resp.status != 200:
            raise UpstreamError("ImageX 上传失败", details={"status": resp.status, "body": resp.text[:300]})

    def _submit_audit(self, image_uri: str, *, sessionid: str, appvr: str) -> None:
        """内容审核。3020 表示 CDN 尚未就绪，短暂等待后重试通常可恢复。"""
        last: Dict[str, Any] = {}
        for attempt, delay in enumerate((0.0, *AUDIT_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            uri = "/mweb/v1/imagex/submit_audit_job"
            last = self.transport.request(
                "POST",
                self._url(uri),
                headers=build_headers(uri, sessionid, appvr=appvr or "5.8.0", origin=self.host),
                json_body={"image_uri": image_uri, "http_common_info": {"aid": int(CN_AID)}},
            ).json()
            ret = str(last.get("ret", "0"))
            if ret in ("0", ""):
                return
            if attempt == len(AUDIT_RETRY_DELAYS):
                break
        raise UpstreamError(
            "参考图审核提交失败",
            details={"ret": last.get("ret"), "errmsg": last.get("errmsg")},
        )


def _brief(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {key: payload[key] for key in list(payload)[:6]}
    return str(payload)[:300]
