"""错误模型：统一网关层与上游层的错误语义。

设计原则：
1. 每个错误都有稳定的字符串 code，调用方可据此分支，而不依赖 message 文案。
2. retryable 明确标注是否值得重试，避免调用方盲目重试导致重复扣积分。
3. 上游原始返回通过 details.upstream 透传，便于探测阶段做归因。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class JimengError(Exception):
    """所有可预期错误的基类。"""

    code: str = "JIMENG_ERROR"
    http_status: int = 500
    retryable: bool = False
    type: str = "upstream"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        http_status: Optional[int] = None,
        retryable: Optional[bool] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "type": self.type,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


class ParamInvalid(JimengError):
    code = "PARAM_INVALID"
    http_status = 400
    type = "invalid_request"


class CapabilityUnsupported(JimengError):
    """参数组合在当前模型/区域下不受支持（在提交前拦截，不消耗积分）。"""

    code = "CAPABILITY_UNSUPPORTED"
    http_status = 422
    type = "invalid_request"


class AuthExpired(JimengError):
    code = "AUTH_EXPIRED"
    http_status = 401
    type = "auth"


class RiskControl(JimengError):
    """上游风控（ret=4013 / 1310 并发超限 / 2038 内容审核）。"""

    code = "RISK_CONTROL"
    http_status = 403
    type = "upstream_risk"


class CreditInsufficient(JimengError):
    code = "CREDIT_INSUFFICIENT"
    http_status = 402
    type = "billing"


class BudgetExceeded(JimengError):
    code = "BUDGET_EXCEEDED"
    http_status = 402
    type = "billing"


class RateLimited(JimengError):
    code = "RATE_LIMITED"
    http_status = 429
    type = "throttle"
    retryable = True


class AccountBusy(JimengError):
    code = "ACCOUNT_BUSY"
    http_status = 409
    type = "throttle"
    retryable = True


class ContentFiltered(JimengError):
    code = "CONTENT_FILTERED"
    http_status = 422
    type = "upstream_policy"


class TaskFailed(JimengError):
    code = "TASK_FAILED"
    http_status = 502
    type = "upstream"


class TaskTimeout(JimengError):
    code = "TASK_TIMEOUT"
    http_status = 504
    type = "upstream"
    retryable = True


class UpstreamError(JimengError):
    code = "UPSTREAM_ERROR"
    http_status = 502
    type = "upstream"
    retryable = True


class NotFound(JimengError):
    code = "NOT_FOUND"
    http_status = 404
    type = "invalid_request"
