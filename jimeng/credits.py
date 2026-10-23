"""积分账本：余额解析、成本预估、预留-结算、配额闸门。

三条不变量（改这个文件前先读）：

1. **快照是观测值，不是账本余额**。`CreditSnapshot` 不可变；结算不得回写快照。
   否则"余额"就变成"上次接口返回值减去本地估算"，一旦某次拉取失败就永久偏移。
2. **预留必须在 finally 里释放**。任何未结算的预留都会变成幽灵占用，
   表现为"明明有额度却一直 CREDIT_INSUFFICIENT"。
3. **审计流水有上界**。网关是长驻进程，无界 list 就是内存泄漏。

为什么需要账本，而不是"直接调用"：免费额度的核心风险不是单价，而是不可撤销的
并发消耗——提交即扣（或冻结），失败退款无契约，重复提交就是纯亏损。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

from .errors import BudgetExceeded, CreditInsufficient

CREDIT_ALIASES = {
    "gift": ("giftcredit", "gift_credit", "gift"),
    "purchase": ("purchasecredit", "purchase_credit", "purchase"),
    "vip": ("vipcredit", "vip_credit", "vip"),
    "total": ("totalcredit", "total_credit", "total"),
}

# 上游按北京时间的自然日重置额度，与服务器本地时区无关
QUOTA_TZ = timezone(timedelta(hours=8))
EVENT_BUFFER = 200


@dataclass(frozen=True)
class CreditSnapshot:
    gift: float = 0.0
    purchase: float = 0.0
    vip: float = 0.0
    total: float = 0.0
    observed_at: float = field(default_factory=time.time)
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gift": self.gift,
            "purchase": self.purchase,
            "vip": self.vip,
            "total": self.total,
            "observed_at": int(self.observed_at),
        }


def parse_credit(payload: Any) -> CreditSnapshot:
    """从 /commerce/v1/benefits/user_credit 的返回里提取余额。

    字段名在版本间会变（giftCredit / gift_credit / credits.*），
    采用"递归收集含 credit 的数值叶子 + 键名归一化"，total 缺失时求和兜底。
    """
    buckets: Dict[str, float] = {}
    for key, value in _walk(payload):
        if not isinstance(value, (int, float)):
            continue
        norm = key.replace("-", "").replace(" ", "").lower()
        if "credit" not in norm and norm not in ("gift", "vip", "purchase"):
            continue
        for target, aliases in CREDIT_ALIASES.items():
            if target in buckets:
                continue
            if any(norm.endswith(alias) or norm == alias for alias in aliases):
                buckets[target] = float(value)
                break

    total = buckets.get("total")
    if total is None:
        total = sum(buckets.get(k, 0.0) for k in ("gift", "purchase", "vip"))
    return CreditSnapshot(
        gift=buckets.get("gift", 0.0),
        purchase=buckets.get("purchase", 0.0),
        vip=buckets.get("vip", 0.0),
        total=float(total),
        raw=payload if isinstance(payload, dict) else {"payload": payload},
    )


def _walk(obj: Any, depth: int = 0):
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1)
            else:
                yield str(key), value
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, depth + 1)


@dataclass
class Reservation:
    id: str
    account_id: str
    amount: float
    created_at: float
    settled: bool = False
    charged: float = 0.0


class Ledger:
    """单账号账本。多账号由 AccountPool 每账号持有一个实例。"""

    def __init__(
        self,
        account_id: str = "default",
        *,
        daily_quota: Optional[float] = 66.0,
        min_balance: float = 0.0,
        max_per_request: Optional[float] = None,
    ) -> None:
        self.account_id = account_id
        self.daily_quota = daily_quota
        self.min_balance = min_balance
        self.max_per_request = max_per_request
        self._lock = threading.RLock()
        self._snapshot: Optional[CreditSnapshot] = None
        self._reserved = 0.0
        self._unverified_spend = 0.0
        self._spent_today = 0.0
        self._day = _today()
        self._reservations: Dict[str, Reservation] = {}
        self._events: Deque[Dict[str, Any]] = deque(maxlen=EVENT_BUFFER)
        self._seq = 0

    # -- 状态 ---------------------------------------------------------------
    @property
    def snapshot(self) -> Optional[CreditSnapshot]:
        return self._snapshot

    def update(self, snapshot: CreditSnapshot) -> CreditSnapshot:
        """接受一次上游观测；清除本地未核实扣减，因为快照已包含真实结果。"""
        with self._lock:
            self._snapshot = snapshot
            self._unverified_spend = 0.0
            return snapshot

    def _roll_day(self) -> None:
        today = _today()
        if today != self._day:
            self._day = today
            self._spent_today = 0.0

    def available(self) -> float:
        with self._lock:
            self._roll_day()
            if self._snapshot is None:
                return 0.0
            return max(
                0.0,
                self._snapshot.total - self._reserved - self._unverified_spend - self.min_balance,
            )

    # -- 预留 / 结算 --------------------------------------------------------
    def reserve(self, amount: float, *, reason: str = "generate") -> Reservation:
        if amount < 0:
            raise ValueError("预留金额不能为负")
        with self._lock:
            self._roll_day()
            if self.max_per_request is not None and amount > self.max_per_request:
                raise BudgetExceeded(
                    f"单次预估 {amount:g} 积分超过上限 {self.max_per_request:g}",
                    details={"estimated": amount, "max_per_request": self.max_per_request},
                )
            if self.daily_quota is not None and self._spent_today + amount > self.daily_quota:
                raise BudgetExceeded(
                    f"今日已用 {self._spent_today:g}，本次需 {amount:g}，超过日配额 {self.daily_quota:g}",
                    details={"spent_today": self._spent_today, "estimated": amount, "quota": self.daily_quota},
                )
            if self._snapshot is not None and self.available() < amount:
                raise CreditInsufficient(
                    f"可用积分不足：可用 {self.available():g}，需要 {amount:g}",
                    details={
                        "available": self.available(),
                        "estimated": amount,
                        "snapshot": self._snapshot.to_dict(),
                    },
                )
            self._seq += 1
            reservation = Reservation(
                id=f"rsv_{int(time.time() * 1000)}_{self._seq}",
                account_id=self.account_id,
                amount=amount,
                created_at=time.time(),
            )
            self._reservations[reservation.id] = reservation
            self._reserved += amount
            self._log({"type": "reserve", "amount": amount, "reason": reason, "reservation": reservation.id})
            return reservation

    def settle(
        self,
        reservation: Reservation,
        *,
        actual: Optional[float] = None,
        success: bool = True,
        balance_is_fresh: bool = False,
    ) -> float:
        """结算并释放预留。

        actual: 实测扣减（余额差），None 时按预估计。
        success: False 表示确定失败，全额退回。
        balance_is_fresh: 调用方已在提交后重新拉取余额。为 True 时不再做本地扣减，
        避免"快照已含扣费 + 再减一次"的双重记账。
        """
        with self._lock:
            self._roll_day()
            if reservation.settled:
                return reservation.charged
            charged = 0.0 if not success else float(actual if actual is not None else reservation.amount)
            self._reserved = max(0.0, self._reserved - reservation.amount)
            self._reservations.pop(reservation.id, None)
            reservation.settled = True
            reservation.charged = charged
            self._spent_today += charged
            if success and charged > 0 and not balance_is_fresh:
                self._unverified_spend += charged
            self._log(
                {
                    "type": "settle",
                    "reservation": reservation.id,
                    "estimated": reservation.amount,
                    "charged": charged,
                    "success": success,
                    "balance_is_fresh": balance_is_fresh,
                }
            )
            return charged

    # -- 观测 ---------------------------------------------------------------
    def usage(self) -> Dict[str, Any]:
        with self._lock:
            self._roll_day()
            return {
                "account_id": self.account_id,
                "balance": self._snapshot.to_dict() if self._snapshot else None,
                "balance_known": self._snapshot is not None,
                "available": round(self.available(), 3),
                "reserved": round(self._reserved, 3),
                "unverified_spend": round(self._unverified_spend, 3),
                "spent_today": round(self._spent_today, 3),
                "daily_quota": self.daily_quota,
                "day": self._day,
                "open_reservations": len(self._reservations),
            }

    def events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    # ------------------------------------------------------------------
    def _log(self, event: Dict[str, Any]) -> None:
        event["at"] = round(time.time(), 3)
        event["account_id"] = self.account_id
        self._events.append(event)


def _today() -> str:
    return datetime.now(QUOTA_TZ).date().isoformat()
