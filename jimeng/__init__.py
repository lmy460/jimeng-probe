"""即梦（Jimeng）网页端探测与复用工具包。

模块边界：
  sign      签名与设备身份
  catalog   模型/比例/分辨率能力目录（启动校验，可被探测结果校准）
  request   参数校验与成本预估（生成不可变 Plan）
  payload   上游报文构造
  upload    参考图上传（ImageX）
  credits   积分账本（预留/结算/配额）
  cache     幂等结果缓存（TTL + LRU）
  parsing   上游返回的容错解析
  client    单账号编排（提交/轮询/结算）
  accounts  多账号池
  probe     能力探测
  gateway   REST 网关
  transport HTTP 传输与重试策略
"""

from __future__ import annotations

__version__ = "0.2.0"

from .catalog import Catalog, ModelSpec
from .client import Account, JimengClient
from .errors import JimengError
from .request import GenerateRequest, ReferenceImage

__all__ = [
    "Catalog",
    "ModelSpec",
    "Account",
    "JimengClient",
    "GenerateRequest",
    "ReferenceImage",
    "JimengError",
    "__version__",
]
