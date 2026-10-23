"""即梦（Jimeng）网页端探测与复用工具包。

模块边界：
  sign      签名与设备身份
  catalog   模型/比例/分辨率能力目录（可被探测结果校准）
  request   参数校验与成本预估（Plan）
  payload   上游报文构造
  upload    参考图上传（ImageX）
  credits   积分账本（预留/结算/配额）
  client    单账号编排（提交/轮询/幂等）
  accounts  多账号池
  probe     能力探测
  gateway   REST 网关
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
