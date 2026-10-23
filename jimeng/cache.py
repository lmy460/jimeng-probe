"""进程内 TTL + LRU 缓存。

get 和 put 都做深拷贝。调用方拿到结果后常会修改字段，例如把 charged 归零或补下载路径，
返回同一对象会改到缓存里的数据。
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 512) -> None:
        if ttl <= 0:
            raise ValueError("ttl 必须为正")
        if max_items <= 0:
            raise ValueError("max_items 必须为正")
        self.ttl = ttl
        self.max_items = max_items
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if time.time() - stored_at > self.ttl:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return copy.deepcopy(value)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.time(), copy.deepcopy(value))
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
