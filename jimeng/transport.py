"""HTTP 传输层：urllib 实现，零第三方依赖。

刻意不引入 requests/httpx：
  - 部署到任何 Python 3.9+ 环境都不需要 pip
  - 便于在受限沙箱（无网络出站代理配置差异）中复现
  - 探测项目本身要尽量减少"依赖行为差异"这个变量
"""

from __future__ import annotations

import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import UpstreamError


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except ValueError:
            raise UpstreamError(
                "上游返回非 JSON",
                details={"status": self.status, "body_head": self.body[:400].decode("utf-8", "replace")},
            )

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Transport:
    """带有限重试的同步 HTTP 客户端。只对幂等/安全场景重试由调用方决定。"""

    def __init__(self, timeout: float = 30.0, retries: int = 2, backoff: float = 1.6) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self._ctx = ssl.create_default_context()

    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Any = None,
        data: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        body = data
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")

        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ctx) as resp:
                    return Response(resp.status, dict(resp.headers), resp.read())
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                # 4xx 不重试（签名/参数问题重试也没用，且可能重复计费）
                if exc.code < 500 or attempt >= self.retries:
                    return Response(exc.code, dict(exc.headers or {}), payload)
                last_err = exc
            except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
                last_err = exc
                if attempt >= self.retries:
                    break
            time.sleep(self.backoff ** attempt + random.random() * 0.3)

        raise UpstreamError(f"网络请求失败: {last_err}", details={"url": url, "method": method})

    # ------------------------------------------------------------------
    @staticmethod
    def encode_multipart(
        fields: Dict[str, str],
        files: Iterable[Tuple[str, str, bytes, str]],
    ) -> Tuple[bytes, str]:
        """构造 multipart/form-data。files: (field, filename, content, content_type)"""
        boundary = "----jimengprobe" + uuid.uuid4().hex
        chunks: List[bytes] = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        for field_name, filename, content, ctype in files:
            chunks.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
                ).encode("utf-8")
            )
            chunks.append(content)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
