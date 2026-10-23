"""积分账本：余额解析、成本预估、预留-结算、配额闸门。

为什么需要账本，而不是"直接调用"：
  - 免费额度的核心风险不是单价，而是**不可撤销的并发消耗**：提交即扣（或冻结），
    失败可能不退还或延迟退还，重复提交就是纯亏损。
  - 因此所有提交都必须先经过"预估 → 预留 → 提交 → 结算"四步，
    并在账本里留下可审计的流水，才能在复用场景（多任务、多进程）下不超支。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .errors import BudgetExceeded, CreditInsufficient

CREDIT_KEYS = {
    "gift": ("giftcredit", "gift_credit", "gift"),
    "purchase": ("purchasecredit", "purchase_credit", "purchase"),
    "vip": ("vipcredit", "vip_credit", "vip"),
    "total": ("totalcredit", "total_credit", "total"),
}


@dataclass
class CreditSnapshot:
    gift: float = 0.0
    purchase: float = 0.0
    vip: float = 0.0
    total: float = 0.0
    observed_at: float = field(default_factory=time.time)
    raw: Dict[str, Any] = field(default_factory=dict)

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

    上游字段名在版本间会变（giftCredit / gift_credit / credit.gift_amount），
    所以采用"递归收集所有含 credit 的数值叶子 + 归一化键名"的策略，
    并对 total 做兜底求和。
    """
    buckets: Dict[str, float] = {}
    for key, value in _walk(payload):
        norm = key.replace("-", "").replace(" ", "").lower()
        if "credit" not in norm and norm not in ("gift", "vip", "purchase"):
            continue
        if not isinstance(value, (int, float)):
            continue
        for target, aliases in CREDIT_KEYS.items():
            if target in buckets:
                continue
            if any(norm.endswith(alias) or norm == alias for alias in aliases):
                buckets[target] = float(value)
                break

    total = buckets.get("total")
    parts = sum(buckets.get(k, 0.0) for k in ("gift", "purchase", "vip"))
    if total is None:
        total = parts
    snap = CreditSnapshot(
        gift=buckets.get("gift", 0.0),
        purchase=buckets.get("purchase", 0.0),
        vip=buckets.get("vip", 0.0),
        total=float(total),
        raw=payload if isinstance(payload, dict) else {"payload": payload},
    )
    return snap


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
        self._spent_today = 0.0
        self._day: str = _today()
        self._reservations: Dict[str, Reservation] = {}
        self._events: List[Dict[str, Any]] = []

    # -- 状态 ---------------------------------------------------------------
    @property
    def snapshot(self) -> Optional[CreditSnapshot]:
        return self._snapshot

    def update(self, snapshot: CreditSnapshot) -> CreditSnapshot:
        with self._lock:
            self._snapshot = snapshot
            return snapshot

    def _roll_day(self) -> None:
        today = _today()
        if today != self._day:
            self._day = today
            self._spent_today = 0.0

    def available(self) -> float:
        with self._lock:
            self._roll_day()
            balance = self._snapshot.total if self._snapshot else 0.0
            return max(0.0, balance - self._reserved - self.min_balance)

    # -- 预留 / 结算 --------------------------------------------------------
    def reserve(self, amount: float, *, reason: str = "generate") -> Reservation:
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
                    details={"available": self.available(), "estimated": amount, "snapshot": self._snapshot.to_dict()},
                )
            reservation = Reservation(
                id=f"rsv_{int(time.time() * 1000)}_{len(self._reservations)}",
                account_id=self.account_id,
                amount=amount,
                created_at=time.time(),
            )
            self._reservations[reservation.id] = reservation
            self._reserved += amount
            self._events.append({"type": "reserve", "at": reservation.created_at, "amount": amount, "reason": reason})
            return reservation

    def settle(
        self,
        reservation: Reservation,
        *,
        actual: Optional[float] = None,
        success: bool = True,
        update_balance: bool = True,
    ) -> float:
        """结算。actual 为实测扣减（余额差），None 时按预估计。失败默认全额退回。

        update_balance=False 用于"调用方已重新拉取余额"的场景，避免二次扣减。
        """
        with self._lock:
            self._roll_day()
            if reservation.settled:
                return reservation.charged
            charged = 0.0 if not success else float(actual if actual is not None else reservation.amount)
            self._reserved = max(0.0, self._reserved - reservation.amount)
            reservation.settled = True
            reservation.charged = charged
            self._spent_today += charged
            self._events.append(
                {
                    "type": "settle",
                    "at": time.time(),
                    "reservation": reservation.id,
                    "estimated": reservation.amount,
                    "charged": charged,
                    "success": success,
                }
            )
            if self._snapshot is not None and success and update_balance:
                self._snapshot.total = max(0.0, self._snapshot.total - charged)
            return charged

    # -- 观测 ---------------------------------------------------------------
    def usage(self) -> Dict[str, Any]:
        with self._lock:
            self._roll_day()
            return {
                "account_id": self.account_id,
                "balance": self._snapshot.to_dict() if self._snapshot else None,
                "available": self.available(),
                "reserved": round(self._reserved, 3),
                "spent_today": round(self._spent_today, 3),
                "daily_quota": self.daily_quota,
                "day": self._day,
            }

    def events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return self._events[-limit:]


def _today() -> str:
    return date.today().isoformat()
