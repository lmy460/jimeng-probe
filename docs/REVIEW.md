# 代码审查记录（2026-10-23）

审查对象：v0.1.0（commit `abc530c`）。审查方式：逐文件静态阅读 + 针对可疑逻辑补写回归测试。
原则：**先修会亏钱/会挂的，再修难维护的，最后才动风格。**

结论：共修复 **14 处问题**（3 处 P0、4 处 P1、7 处 P2），其中 P2-2 一次性删掉 4 处死代码；新增 4 项回归测试。
测试从 23 项增至 **27 项全通过**；`client.py` 从 556 行降到 453 行，职责由"什么都干"收敛为"编排"。

---

## 一、P0：会直接造成损失或数据错误

| ID | 位置 | 问题 | 后果 | 修复 | 回归 |
|---|---|---|---|---|---|
| P0-1 | `transport.py` | `request()` 对所有方法无差别重试（5xx/超时） | `aigc_draft/generate` 是**非幂等** POST，一次重试就是一次二次提交、二次扣费。网络抖动时最容易触发 | 重试策略改为：仅 GET/HEAD 默认重试；POST 默认 `retry=0`，必须显式声明 | `test_post_never_retries_by_default` |
| P0-2 | `client.py` `_cache_*` | 幂等缓存命中时 `dict(cached)` 是**浅拷贝**，随后 `cached["usage"]["credits_charged"]=0` 直接改写缓存里的同一个 `usage` 对象 | 第一次命中返回正常；此后所有命中都显示 `charged=0` 且历史数据被污染（数据错，不是性能问题） | 抽出 `cache.py::TTLCache`，`get/put` 双向 `deepcopy`，顺带用 `OrderedDict` 做 LRU | `test_cache_returns_isolated_copies` |
| P0-3 | `client.generate` | 只有 `TaskFailed/RiskControl/ContentFiltered/UpstreamError` 路径会结算，`AuthExpired`、`ParamInvalid`（上传/参考图校验）、`AccountBusy` 等异常会让预留**永久悬挂** | 表现为"明明有额度却持续 CREDIT_INSUFFICIENT"，只能重启进程；这是最难排查的一类 | 把提交阶段包进 `try/except JimengError`，任何异常都先 `settle(success=False)` 再抛出；参考图上传移到 `reserve` 之前 | `test_reservation_released_when_submit_rejected` |

## 二、P1：会算错账、会泄漏、会不一致

| ID | 位置 | 问题 | 修复 |
|---|---|---|---|
| P1-1 | `probe._cost` | `_run_once` 已把本次消耗计入 `self.spend`，`_cost` 又把余额差加了一遍 → 预算消耗翻倍，`max_credits=8` 实际只允许跑 4 | 引入 `charge=False` 显式表达"由调用方记账"，全项目只保留**一处**计费入口 |
| P1-2 | `credits.Ledger` | ① 结算时回写 `self._snapshot.total`，把"观测值"变成"本地估算值"，一次拉取失败就永久偏移；② `_events`、`_reservations` 无界增长 | `CreditSnapshot` 改 `frozen`；新增 `_unverified_spend` 单独记账，`update()` 收到真实快照即清零；流水改 `deque(maxlen=200)`，结算时 `pop` 预留 |
| P1-3 | `request.py` / `client.py` | `GenerateRequest` 上挂 `plan` 字段（请求即状态），且 `client` 原地改 `plan.image_uris` → 同一个 plan 在"提交"和"响应回显"时可能不同 | `Plan` 改 `frozen`，新增 `with_references()` 派生新值；请求对象不再持有派生状态 |
| P1-4 | `gateway._assets` | URL 素材的 `asset_id` 用 `abs(hash(uri))`：① 字符串 hash 受 `PYTHONHASHSEED` 影响，进程重启即变；② 没登记进 client，返回的 id 之后**根本用不了** | 改 `sha256(uri)[:12]`，并 `remember_asset()` 登记，id 可复用 |

## 三、P2：可维护性

| ID | 问题 | 修复 |
|---|---|---|
| P2-1 | `from_dict` 用 `dataclass_fields` 过滤未知键，拼错参数（如 `resolution_type`）静默失效，走默认值 | 严格白名单：未知参数直接 `PARAM_INVALID` 并列出允许项 |
| P2-2 | 死代码：`_post` 里算好却从不使用的 `params`、`cli._pool` 定义后无人调用、`sign.with_query` 从未被引用、`payload` 里累积后丢弃的 `image_list` | 全部删除 |
| P2-3 | `Gateway.handle` 是 8 段 if/elif；`_assets` 里 `import base64 as _b64`；直接访问 `client._assets` 私有字段 | 改表驱动路由（`(method, regex, handler)`）；import 提到模块顶层；改用 `remember_asset()` |
| P2-4 | `upload._submit_audit` 把同一段请求构造写了两遍；`_dig` / `_find_dict` 与 `parsing` 重复 | 统一用 `parsing.find_dict`，审核改单循环重试 |
| P2-5 | 网关令牌用 `==` 比较 | 改 `hmac.compare_digest` |
| P2-6 | `Catalog` 无任何启动校验；`to_dict()` 直接把内部 `credits_per_image` 字典交出去 | 启动即校验（重复模型、未知分辨率/比例、负单价）；`to_dict` 返回副本 |
| P2-7 | 日配额按服务器本地日期滚动，与上游 +08:00 不一致 | 固定 `timezone(+08:00)` |

---

## 四、结构变化

```
新增  cache.py      TTL + LRU，按值存取（修 P0-2）
新增  parsing.py    上游容错解析（上一轮已抽出，本轮补充 history_records / find_dict）
重写  transport.py  重试策略显式化（修 P0-1）
重写  credits.py    不可变快照 + 未核实扣减 + 有界流水（修 P1-2）
重写  request.py    冻结 Plan + 严格参数 + 引用派生（修 P1-3、P2-1）
重写  client.py     上传→预留→提交→结算的线性编排，异常路径必结算（修 P0-3）
重写  gateway.py    表驱动路由 + 常量时间令牌比较 + 确定性 asset_id（修 P1-4、P2-3、P2-5）
重写  probe.py      单一计费入口 + 观测逻辑去重（修 P1-1）
```

`client.generate` 原来是"校验+预算+拉余额+上传+循环提交+轮询+结算+拼响应"的 90 行巨石，
现在拆成 `generate`（入口/锁/缓存）→ `_execute`（预算/预留/结算）→ `_submit_all`（提交批次）
→ `_result`（响应拼装）。每一步都能单独读，异常路径只有一处。

## 五、保留的技术债（明确不装没事）

1. **任务表在内存**：网关重启后 `wait=false` 的任务失联。要持久化就上 SQLite，不该用 JSON 文件凑。
2. **缓存不持久**：进程重启后幂等键失效，可能重复扣费。生产复用应把 `idempotency_key` 与结果落库。
3. **限流是"账号级串行 + 固定间隔"**，不是令牌桶；多进程共享同一 sessionid 时无效（必须单实例或外部锁）。
4. **历史解析仍是启发式**：上游改结构时 `extract_images` 的兜底扫描可能误抓封面图。
5. **HTTP 层无集成测试**：`Transport` 只测了重试策略，未验证真实 5xx/超时行为（本地起 stub server 更合适，留待后续）。
6. `gateway` 的 CORS 固定 `*`：本机使用可接受，公网部署必须收紧。

## 六、度量

| 指标 | 前 | 后 |
|---|---|---|
| 测试数 | 23 | 27 |
| `client.py` 行数 | 556 | 453 |
| 重复的计费入口 | 2 | 1 |
| 资金安全类缺陷（P0） | 3 | 0 |
| 未释放预留的异常路径 | 4 类 | 0 |
| 无界增长的容器 | 2 | 0 |
