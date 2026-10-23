"""请求签名与设备身份。

即梦网页端每个 mweb 请求都带三组头：
  - Cookie: sessionid=...            （真正的凭证）
  - Device-Time + Sign + Sign-Ver    （轻量防重放，MD5 固定盐）
  - Appid / Appvr / Pf               （应用与平台标识）

Sign 算法（实测）：md5(f"{salt}|{uri 末 7 位}|{pf}|{appvr}|{device_time}||11ac")
这不是密码学密钥，只是把请求与时间戳绑定；但它会随 web 版本变化，所以做成可配置。
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

CN_HOST = "https://jimeng.jianying.com"
CN_AID = "513695"
SIGN_SALT = "9e2c"
SIGN_TAIL = "11ac"
PLATFORM = "7"  # Pf: web
DEFAULT_APPVR = "5.8.0"
DEFAULT_DA_VERSION = "3.3.9"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def device_sign(uri_path: str, appvr: str = DEFAULT_APPVR, device_time: Optional[int] = None) -> tuple[str, int]:
    """返回 (sign, device_time)。uri_path 需为不含 query 的路径。"""
    ts = int(device_time if device_time is not None else time.time())
    raw = f"{SIGN_SALT}|{uri_path[-7:]}|{PLATFORM}|{appvr}|{ts}||{SIGN_TAIL}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest(), ts


@dataclass
class DeviceIdentity:
    """伪造的 web 设备身份。

    同一进程内复用（webId/did 在浏览器里也是长生命周期的），
    这样并发请求看起来来自同一台机器，不会互相矛盾。
    """

    web_id: str = field(default_factory=lambda: str(random.randint(7_000_000_000_000_000_000, 7_999_999_999_999_999_999)))
    user_id: str = field(default_factory=lambda: str(random.randint(10_000_000_000_000_000_000, 19_999_999_999_999_999_999)))
    did: str = field(default_factory=lambda: str(random.randint(10_000_000_000_000_000_000, 19_999_999_999_999_999_999)))
    appvr: str = DEFAULT_APPVR
    da_version: str = DEFAULT_DA_VERSION

    def query(self, region: str = "cn") -> Dict[str, str]:
        return {
            "aid": CN_AID,
            "device_platform": "web",
            "region": region,
            "webId": self.web_id,
            "da_version": self.da_version,
            "os": "windows",
            "web_component_open_flag": "1",
            "web_version": self.appvr,
        }


def build_headers(
    uri_path: str,
    sessionid: str,
    *,
    appvr: str = DEFAULT_APPVR,
    origin: str = CN_HOST,
    extra: Optional[Dict[str, str]] = None,
    device_time: Optional[int] = None,
) -> Dict[str, str]:
    sign, ts = device_sign(uri_path, appvr=appvr, device_time=device_time)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json",
        "Appid": CN_AID,
        "Appvr": appvr,
        "Pf": PLATFORM,
        "Origin": origin,
        "Referer": origin + "/ai-tool/home",
        "User-Agent": USER_AGENT,
        "Cookie": f"sessionid={sessionid}",
        "Device-Time": str(ts),
        "Sign": sign,
        "Sign-Ver": "1",
    }
    if extra:
        headers.update({k: v for k, v in extra.items() if v is not None})
    return headers
