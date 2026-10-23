"""账号池：多 sessionid 的分配与隔离。

同一 sessionid 不并发，上游会返回 4013 或 1310。账号之间账本独立。
选择时取可用余额最多的账号，额度不均匀时轮询会浪费。
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from .catalog import Catalog
from .client import Account, JimengClient
from .errors import CreditInsufficient, ParamInvalid
from .transport import Transport


def load_accounts(env: Optional[Dict[str, str]] = None) -> List[Account]:
    env = env or os.environ
    raw = (env.get("JIMENG_ACCOUNTS") or "").strip()
    accounts: List[Account] = []
    if raw:
        for idx, chunk in enumerate(raw.split(",")):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                name, sid = chunk.split(":", 1)
            else:
                name, sid = f"acc{idx + 1}", chunk
            accounts.append(_make_account(name.strip(), sid.strip(), env))
    sid = (env.get("JIMENG_SESSIONID") or "").strip()
    if sid and not accounts:
        accounts.append(_make_account("default", sid, env))
    return accounts


def _make_account(name: str, sid: str, env: Dict[str, str]) -> Account:
    return Account(
        id=name,
        sessionid=sid,
        region=(env.get("JIMENG_REGION") or "cn").lower(),
        cooldown_s=float(env.get("JIMENG_COOLDOWN_S") or 3.0),
        daily_quota=float(env["JIMENG_DAILY_QUOTA"]) if env.get("JIMENG_DAILY_QUOTA") else 66.0,
        max_per_request=float(env["JIMENG_MAX_CREDITS_PER_REQUEST"])
        if env.get("JIMENG_MAX_CREDITS_PER_REQUEST")
        else None,
    )


class AccountPool:
    def __init__(self, accounts: List[Account], *, transport: Optional[Transport] = None, catalog: Optional[Catalog] = None) -> None:
        if not accounts:
            raise ParamInvalid("未配置账号：请设置 JIMENG_SESSIONID 或 JIMENG_ACCOUNTS")
        self.accounts = accounts
        self.catalog = catalog or Catalog.load()
        self.transport = transport or Transport()
        self._clients: Dict[str, JimengClient] = {
            acc.id: JimengClient(acc, transport=self.transport, catalog=self.catalog) for acc in accounts
        }

    def clients(self) -> List[JimengClient]:
        return list(self._clients.values())

    def client(self, account_id: Optional[str] = None) -> JimengClient:
        if account_id:
            client = self._clients.get(account_id)
            if not client:
                raise ParamInvalid(f"未知账号: {account_id}", details={"available": list(self._clients)})
            return client
        return self._pick()

    def _pick(self) -> JimengClient:
        best: Optional[JimengClient] = None
        best_score = float("-inf")
        for client in self._clients.values():
            acc = client.account
            if acc.lock.locked():
                continue
            usage = acc.ledger.usage()
            balance = (usage["balance"] or {}).get("total", 0.0) if usage["balance"] else float("inf")
            score = float(balance) - usage["reserved"]
            if score > best_score:
                best, best_score = client, score
        if best is None:
            raise CreditInsufficient("所有账号都在忙，请稍后重试")
        return best

    def refresh_all(self) -> List[Dict[str, object]]:
        out = []
        for client in self._clients.values():
            try:
                snap = client.refresh_credit()
                out.append({"account_id": client.account.id, "credit": snap.to_dict()})
            except Exception as exc:  # noqa: BLE001 - 探测型接口，需要把失败也报出来
                out.append({"account_id": client.account.id, "error": str(exc)})
        return out

    def summary(self) -> List[Dict[str, object]]:
        return [client.account.redacted() for client in self._clients.values()]
