"""HTTP 传输层：urllib 实现，零第三方依赖。

重试策略是这一层最重要的设计：
  - 只有**幂等**请求（GET/HEAD）默认重试；
  - POST 默认不重试，必须由调用方显式声明 `retry=n`。
    原因很直接：`aigc_draft/generate` 一旦被重放就是二次扣积分，这是资损，不是"偶发失败"。
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

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            raise UpstreamError("上游返回空响应体", details={"status": self.status})
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise UpstreamError(
                "上游返回非 JSON",
                details={"status": self.status, "body_head": self.body[:300].decode("utf-8", "replace")},
            ) from exc

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Transport:
    def __init__(self, timeout: float = 30.0, retries: int = 2, backoff: float = 1.6) -> None:
        if retries < 0:
            raise ValueError("retries 不能为负")
        if backoff < 1:
            raise ValueError("backoff 必须 >= 1")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._ctx = ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Any = None,
        data: Optional[bytes] = None,
        timeout: Optional[float] = None,
        retry: Optional[int] = None,
    ) -> Response:
        method = method.upper()
        body = data
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")

        if retry is not None:
            attempts = max(0, retry)
        else:
            attempts = self.attempts_for(method)

        last_error: Optional[BaseException] = None
        for attempt in range(attempts + 1):
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ctx) as resp:
                    return Response(resp.status, dict(resp.headers), resp.read())
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                if exc.code not in RETRYABLE_STATUS or attempt >= attempts:
                    return Response(exc.code, dict(exc.headers or {}), payload)
                last_error = exc
            except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
            time.sleep(self.backoff ** attempt + random.random() * 0.3)

        raise UpstreamError(f"网络请求失败: {last_error}", details={"url": url, "method": method})

    def attempts_for(self, method: str, retry: Optional[int] = None) -> int:
        """重试次数策略：显式 retry 优先；否则仅幂等方法自动重试。"""
        if retry is not None:
            return max(0, retry)
        return self.retries if method.upper() in IDEMPOTENT_METHODS else 0

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
