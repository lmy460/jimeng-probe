# 缺陷与修复记录

开发与联调阶段遇到的问题，按影响程度排列。每条记录成因、处理方式和对应的回归测试。

## 资金安全

### 非幂等请求被自动重试

`transport.py` 最初对所有方法一视同仁地重试 5xx 与超时。`aigc_draft/generate` 是非幂等 POST，
重放一次就是一次重复提交，额度会被扣两次。网络抖动时最容易踩到。

改成按方法区分：GET/HEAD 默认重试，POST 默认零重试，需要重试的调用点显式传 `retry`。
余额和历史查询是只读的，各自传了 `retry=1`。
回归：`test_post_never_retries_by_default`。

### 幂等缓存被自身污染

命中缓存时用 `dict(cached)` 做浅拷贝，随后把 `usage.credits_charged` 置零，改的是缓存里同一个
`usage` 对象。第一次命中正常，之后所有命中都返回 `charged=0`，历史数据也被改坏。

抽出 `cache.py`，`TTLCache` 在 `get`/`put` 两侧都做深拷贝，并用 `OrderedDict` 限制容量。
回归：`test_cache_returns_isolated_copies`。

### 异常路径不释放积分预留

原先只有 `TaskFailed`、`RiskControl`、`ContentFiltered`、`UpstreamError` 会结算。
遇到 `AuthExpired`、参考图上传失败或 `AccountBusy`，预留会一直挂着，表现为有额度却持续返回
`CREDIT_INSUFFICIENT`，只能重启进程。

提交阶段改为 `except JimengError` 后统一按失败结算再抛出；参考图上传移到预留之前，
上传失败不进入账本。
回归：`test_reservation_released_when_submit_rejected`。

## 计费与账本

### 成本探测重复计费

`probe._cost` 依赖余额差计算单价，但 `_run_once` 已经把本次消耗计入 `spend`，两者相加使
预算消耗翻倍，`max_credits=8` 实际只能跑 4。

`_run_once` 增加 `charge` 参数，由调用方决定是否记账，成本探测传 `False` 后自行累加余额差。

### 快照被写回，流水无上限

账本结算时直接改 `snapshot.total`，把上游观测值变成了"上次返回值减本地估算"，一次拉取失败
就会永久偏移。同时 `_events` 和 `_reservations` 都不回收，长期运行会持续涨内存。

`CreditSnapshot` 改为不可变；未拿到新快照时的扣减记在 `_unverified_spend`，下次拉取成功即清零；
流水改为 `deque(maxlen=200)`，结算时移除对应预留。

## 接口与结构

### 请求对象承担了派生状态

`GenerateRequest` 上挂了 `plan` 字段，`client` 还会原地修改 `plan.image_uris`。
提交用的参数和响应里回显的参数可能不是同一份。

`Plan` 改为不可变，新增 `with_references()` 生成带参考图 URI 的新实例，请求对象只保留入参。

### 未知参数被静默忽略

`from_dict` 用 dataclass 字段表过滤未知键，`resolution_type` 这类拼写错误不会报错，而是走默认值。

改为白名单校验，未知字段返回 `PARAM_INVALID` 并列出允许的键。

### URL 素材的 asset_id 不稳定

网关给 URL 素材生成 id 时用了 `abs(hash(uri))`。字符串 hash 受 `PYTHONHASHSEED` 影响，
进程重启即变；而且该 id 没有登记，返回后无法用于后续请求。

改用 `sha256(uri)[:12]`，并调用 `remember_asset()` 登记。

### 其他整理

- 网关的 8 段 if/elif 改为路由表，新增接口只需加一行；
- 令牌比较改用 `hmac.compare_digest`；
- 目录增加启动校验（重复模型、未知分辨率或比例、负单价）；
- 日配额固定按 +08:00 划分，与上游一致；
- 删除未使用的 `params`、`with_query`、`_pool`、`image_list`；
- 上传与解析中的重复逻辑合并到 `parsing.find_dict`。

## 未处理

- 网关任务表与幂等缓存都在内存，重启后失效；
- 限流是账号级串行加固定间隔，多进程共享同一 sessionid 时不起作用；
- 历史记录解析仍是启发式，上游改结构时可能误取封面图；
- HTTP 层只测了重试策略，未做本地 stub server 的集成测试；
- CORS 固定为 `*`，本机使用可以，公网部署需要收紧。
