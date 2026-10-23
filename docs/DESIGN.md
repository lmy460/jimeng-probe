# 项目说明：即梦网页端的探测与复用

> 面向读者：要复用这套能力的人（含未来的我）。
> 接口字段级契约在 [`API.md`](API.md)；本文解释**为什么这样设计**，以及探测过程中发现的、必须写进代码里的规则。

---

## 1. 项目解决什么问题

即梦网页版能免费出图，但有三个结构性障碍：

| 障碍 | 具体表现 | 本项目的应对 |
|---|---|---|
| **额度稀缺** | 每日 66 积分，一次 4.x 生成就吃 4 分 | 预算闸门 + 预留结算 + 幂等缓存，杜绝重复扣费 |
| **不可编程** | 只能点网页，请求体是深度嵌套的 `draft_content` | 把嵌套结构编译成 `Plan`，对外只暴露 8 个业务参数 |
| **语义隐晦** | 张数、分辨率、比例的行为与直觉不符 | 探测模块把"猜测"变成"可验证事实"，并写入校准文件 |

交付物是四件东西：

1. **SDK**：`JimengClient.generate(GenerateRequest)`，进程内直接用；
2. **网关**：`/v1/images/generations` 等 REST 接口，供其它语言/服务复用；
3. **探测器**：`probe`，零消耗静态校验 + 受预算约束的实测校准；
4. **校准数据**：`calibration.json`，让"目录推测值"升级为"本机实测值"。

---

## 2. 五类网页操作 → API 参数的映射

| 网页动作 | API 参数 | 落到上游的位置 | 容易踩的坑 |
|---|---|---|---|
| 附加图片 | `images[]`（≤10） | `component.abilities.blend.ability_list[].image_uri_list` + `prompt_placeholder_info_list` | 必须先上传拿 `image_uri` 并过审核；prompt 末尾要补 `#`；组件类型从 `generate` 变 `blend` |
| 选择模型 | `model` | `extend.root_model` 与 `core_param.model` 双写 | 两处必须一致；用户侧名字与内部 `req_key` 不同（`jimeng-4.6` ↔ `high_aes_general_v42`） |
| 图片比例 | `ratio` | `core_param.image_ratio`（枚举码） | 传字符串无效；`1:1` 是 `8`；`auto` 时必须**省略**该字段 |
| 分辨率 | `resolution` | `core_param.large_image_info.{width,height,resolution_type}` | 三字段要成套；档位≠最终像素 |
| 生成张数 | `n` | `gen_option.generate_number` + `metrics_extra.generateCount` | 4.x 上不决定张数，只决定**提交次数** |

这张表是接口设计的全部依据：**对外一个语义参数，对内可能落三四个字段**，且每个字段都有独立的失败模式。把这些收敛在 `payload.py` 一个文件里，是这套封装最主要的工程价值。

---

## 3. 探测得到的规则（核心）

证据等级：**A** = 多来源一致或本机可复现；**B** = 单一来源/间接推断；**C** = 未验证假设。

### R1　张数不是自由变量（等级 A）

**现象**：`jimeng-4.6`、`16:9`、`1k`，`generateCount=1`，返回 **4 张** PNG。
**机制**：4.x 是"系列组图"模型，一次提交固定产出 4 张；`n` 只影响提交次数，不影响单次产出。

**推论**：

```
K = outputs_per_submit         # 3.x → 1，4.x → 4
submissions = ceil(n / K)
实际张数 = submissions × K ≥ n
成本 = submissions × K × 单价
```

| 需求 | 模型 | 提交次数 | 实得张数 | 花费 |
|---|---|---|---|---|
| 1 张 | jimeng-3.0 | 1 | 1 | 1 |
| 1 张 | jimeng-4.6 | 1 | **4** | **4** |
| 4 张 | jimeng-4.6 | 1 | 4 | 4 |
| 4 张 | jimeng-3.0 | 4 | 4 | 4 |

**处置**：`Plan.submissions` 与 `Plan.planned_images` 分离；`usage` 同时回报 `images_requested` 与 `images_returned`；降级时优先降分辨率/换 3.x，**而不是减 `n`**（4.x 上减 `n` 不省钱）。

> 这是整个项目最重要的一条：它决定了成本公式，也决定了很多"以为在省钱其实没省"的调用方式。

### R2　分辨率是档位提示，不是像素承诺（等级 A）

**现象**：`1k + 16:9` 提交 `1024×576`，产物 `1280×720`。
**机制**：提交尺寸只是给服务端的构图提示，最终像素由模型侧归一化（同一比例下统一长边）。

**处置**：`data[].width/height` 是唯一可信尺寸；不要基于请求参数做裁剪、拼图、标注等后处理假设。响应里 `request.submit_size` 与 `data[].width/height` 并列返回，差异一目了然。

### R3　比例是枚举码，且 `1:1` 不连续（等级 A）

`21:9=0, 16:9=1, 3:2=2, 4:3=3, 3:4=4, 2:3=5, 9:16=6, 1:1=8`。

按顺序类推写成 `1:1=7` 会直接失败。代码里以字典为准，且 `static` 探测会逐个校验"码值 ↔ 提交尺寸"是否自洽。

`ratio=auto`（智能比例）只在 4.0+ 生效，且必须**省略** `image_ratio` 字段——传了反而会锁死比例。

### R4　图生图的三个前置条件（等级 A/B）

1. 参考图必须先走 `get_upload_image_proof → ImageX 上传 → submit_audit_job`；
2. `prompt` 末尾按图数补 `2×N` 个 `#`（1 张 `##`，2 张 `####`）；
3. 组件 `generate_type` 切 `blend`，并补 `ability_list` 与 `prompt_placeholder_info_list`（逐图 `ability_index`）。

审核环节是硬门槛：未过审的图不能当参考图（见过 `ret=3020 download file failed`，CDN 未就绪，重试可恢复，等级 B）。

### R5　同账号并发是红线（等级 A）

- 不带浏览器签名的请求可能直接被判风控：`ret=4013`、`fail_starling_key=web_risk_control_message_reject_generation`；
- 并发超限：`ret=1310 exceed_model_parallel_max`。

**处置**：每账号一把非阻塞锁（拿不到就返回 `ACCOUNT_BUSY`，不排队堆积）+ 默认 3s 最小间隔；`RISK_CONTROL` **不重试**（重试等于加重标记）。

**能力边界**：视频生成在不复现浏览器 `msToken`/`a_bogus` 的情况下会被 4013 拦下。本项目因此**只做图像**，不引入浏览器自动化——那是另一套登录态与指纹问题，会显著放大维护成本。

### R6　积分在提交时扣除，退款不确定（等级 B）

余额差观测显示扣费发生在提交/生成阶段，而非下载时。失败是否退还、何时退还，上游没有契约。

**处置**：
- 提交前 `reserve(estimate)`，禁止并发超支；
- 结算以"提交前后余额差"为准（`update_balance=False`，避免二次扣减）；
- 明确的失败（`status=30`）按 0 结算并记录事件；**超时按预估计费**（保守），同时返回 `pending: [history_id]` 供人工复核。

### R7　状态码有三套语义并存（等级 A/B）

`20` 进行中、`30` 失败、`40` 成功（旧版）、`50` 成功（新版常见）、`21` 完成（更早版本）。
另有业务码 `2038`（内容审核拦截）。

**处置**：成功集合取 `{21,40,50}`，失败取 `{30}`，其余视为进行中；同时"有图且状态不在进行中"也判成功——避免新版状态码导致空转。

### R8　免费额度与多账号（等级 B）

每日 66 积分（中国站口径），按自然日滚动；多账号额度独立、不合并。
账本按账号隔离，`GET /v1/credit` 逐账号回报，选择账号时"可用余额最多者优先"而非轮询。

### R9　4k 单价未验证（等级 C）

目录默认 `1k/2k = 1 积分/张`、`4k = 2 积分/张`。4k 这一项是推测，`confidence=default`。
验证方式：`probe --live --probes cost --resolutions 4k --write`。

---

## 4. 架构：为什么这样分层

```
GenerateRequest ──校验──▶ Plan ──编译──▶ draft_content ──提交──▶ history_id
                            │                                      │
                            ├── 成本（credits/单价/张数）            ├── 轮询
                            └── 审计（request 原样回显）             └── 产物 URL
```

**`Plan` 是唯一真相源**。校验规则、成本预估、报文构造、响应回显全部从同一个 `Plan` 派生。
这消除了这类项目最常见的缺陷：预估算一套、提交算另一套，最后账对不上。

三条不可绕过的链路：

1. **成本链路**：`Plan.estimated_credits → reserve → settle(余额差)`；
2. **幂等链路**：`cache_fingerprint → 命中则返回缓存（charged=0）`；
3. **失败链路**：`TaskFailed/RiskControl → 退款且抛错`；`TaskTimeout → 保守计费且返回 pending`。

---

## 5. 成本模型

```
cost = ceil(n / K) × K × u(model, resolution)
```

- `K`：单次产出张数，3.x=1，4.x=4；
- `u`：单位积分，默认 1k/2k=1.0，4k=2.0（待实测）。

典型场景（按默认单价）：

| 场景 | 参数 | 预估积分 | 备注 |
|---|---|---|---|
| 快速验证 | `3.0 / 1k / 1:1 / n=1` | 1 | 最省，适合探测与调试 |
| 常规出图 | `4.6 / 2k / 16:9 / n=4` | 4 | 一次提交，4 张 |
| 高清 | `4.5 / 4k / 16:9 / n=4` | 8 | 单价翻倍（待验证） |
| 批量 12 张 | `4.6 / 2k / n=4` ×3 次 | 12 | 必须拆 3 次提交，串行 |

**反例（常见误判）**：想"只要 1 张、省点额度"，在 4.6 上设 `n=1`——实际仍出 4 张、扣 4 分。正确做法是换 `3.0`，或接受 4 张。

---

## 6. 预算与风控约束（写死在代码里）

| 约束 | 默认值 | 位置 |
|---|---|---|
| 单账号并发 | 1（非阻塞锁） | `Account.lock` |
| 最小请求间隔 | 3s | `Account.cooldown_s` / `JIMENG_COOLDOWN_S` |
| 单次积分上限 | 未设（生产必须设） | `max_credits` / `JIMENG_MAX_CREDITS_PER_REQUEST` |
| 日配额 | 66 | `Ledger.daily_quota` / `JIMENG_DAILY_QUOTA` |
| 参考图上限 | 10 张 | `ModelSpec.max_reference_images` |
| `n` 上限 | 4 | `MAX_N` |
| 重试策略 | 仅网络/5xx，指数退避 | `Transport.retries` |

风控错误一律**不重试**：`RISK_CONTROL(4013)`、`CONTENT_FILTERED(2038)`。
可重试：`RATE_LIMITED`、`ACCOUNT_BUSY`、`TASK_TIMEOUT`（且超时前应先查 `history_id`）。

---

## 7. 探测方法论

**两级探测，先免费后付费：**

1. `static`（0 消耗）：把目录里每个 `模型 × 比例 × 分辨率` 编译一遍，校验码值与尺寸自洽；
2. `live`（受预算约束）：按 `count → ratio → resolution → cost` 顺序，逐步把推测变成实测。

**为什么先测 `count`**：它决定成本公式的分母，测错会让后续所有预算判断失真。
**为什么 `cost` 放最后**：需要"提交前后余额差"，前面的 live 探测会污染余额基线。

**预算闸门**：每次 live 提交前检查 `spend + estimate > max_credits`，超出即标 `skipped`，绝不"试探性超支"。

**校准闭环**：

```
probe --live --write  →  calibration.json  →  Catalog.load()  →  confidence=calibrated
```

校准只覆盖三类字段：`outputs_per_submit`、`credits_per_image`、`resolutions`。其余能力声明保持静态，避免"一次异常探测把能力目录改坏"。

---

## 8. 代码结构与阅读路径

| 顺序 | 文件 | 职责 |
|---|---|---|
| 1 | `errors.py` | 12 类错误码与 HTTP 语义 |
| 2 | `catalog.py` | 模型 / 比例 / 分辨率目录（启动校验 + 校准覆盖） |
| 3 | `request.py` | 参数校验 → 不可变 `Plan`（含成本） |
| 4 | `payload.py` | `Plan` → `draft_content` |
| 5 | `credits.py` | 账本：预留 / 结算 / 日配额 / 流水 |
| 6 | `client.py` | 上传 → 提交 → 轮询 → 结算 → 幂等 |
| 7 | `parsing.py` | 上游返回的容错解析 |
| 8 | `probe.py` | 静态 + 实测探测 |
| 9 | `gateway.py` | REST 网关（表驱动路由，零依赖） |
| 10 | `accounts.py` | 多账号池 |
| 11 | `cache.py` / `transport.py` / `sign.py` / `upload.py` | 幂等缓存 / 传输 / 签名 / ImageX 上传 |

**最短路径理解项目**：`request.py`（参数语义）→ `client.py::generate`（全流程）→ `credits.py`（钱怎么算）。

---

## 9. 测试策略

> 审查过程中发现并修复的 14 处问题（含 3 处资金安全类）记录在 [`REVIEW.md`](REVIEW.md)。

`tests/test_offline.py`，27 项，全部基于 `FakeTransport`，不触网、不消耗积分：

| 覆盖点 | 用例 |
|---|---|
| 报文正确性 | 比例码、提交尺寸、`generate_number`、`root_model` |
| 成本语义 | 最小计费粒度（n=1/2/4/5）、3.x 与 4.x 差异 |
| 能力拦截 | 3.x 不支持 4k / 不支持 `auto`；prompt 为空；n 越界 |
| 资金安全 | `max_credits` 拦截在提交前（断言 0 次调用）、日配额、余额不足 |
| 幂等 | 第二次请求 `cached=true`、`charged=0`、提交次数不增 |
| 图生图 | `generate_type=blend`、prompt 尾部 `##`、`image_uri` 落到 `ability_list` |
| 解析容错 | camelCase / snake_case 余额、嵌套去重、通用兜底 |
| 探测 | 静态 24 组合全通过；live 超预算即跳过 |
| 网关 | 路由、dry-run、素材上传、幂等键、pending 回查 |
| 可靠性与资金安全 | POST 不自动重试、缓存副本隔离、异常路径释放预留 |

**刻意不 mock 的东西**：签名算法、报文嵌套层级、错误码映射。这些正是最容易错的部分，mock 掉就失去了测试价值。

---

## 10. 已知未知

| 项 | 状态 | 验证方式 |
|---|---|---|
| 4k 单价是否 2 积分 | 推测（C） | `probe --live --probes cost --resolutions 4k --write` |
| 每日 66 分的发放时点 | 未确认（C） | 连续两日 `GET /v1/credit` 对比 |
| 失败是否退还积分 | 不确定（B） | 失败样本前后余额对比 |
| 风控触发阈值（频率/并发） | 未量化 | 不建议主动试探 |
| 国际站（dreamina）差异 | 未实现 | 需替换 host / `req_key` / `assistant_id` |
| 视频生成 | 不做 | 需要浏览器 `msToken`/`a_bogus`，超出本项目边界 |

---

## 11. 合规与风险

1. **协议风险**：网页端接口非公开 API，自动化调用通常违反平台用户协议，可能导致限流、清空额度或封号。
2. **额度属性**：每日赠送积分是个人账号权益，**不得转售**或作为付费服务交付第三方。
3. **内容合规**：生成内容需符合平台规范；`2038` 是审核拦截，不应通过改词绕过。
4. **工程默认**：网关只监听 `127.0.0.1`；sessionid 不出现在请求/响应/日志中；生产复用必须加 `JIMENG_GATEWAY_TOKEN` 与调用审计。

本项目定位为**个人额度内的能力研究与工程验证**，不提供绕过付费、批量注册或多账号刷量能力。

---

## 12. 路线图

- [ ] SQLite 账本（当前内存态，进程重启丢流水）
- [ ] 异步任务持久化 + 崩溃恢复（当前 `wait=false` 的任务表在内存）
- [ ] `GET /v1/usage` 审计视图（按调用方/模型/日聚合）
- [ ] OpenAI 兼容层（`/v1/images/generations` 已同形，补 `b64_json` 与错误映射即可）
- [ ] 国际站适配（host / `req_key` / `assistant_id` 三处差异）
- [ ] 成本对账命令（拉 `history_ids` 复核超时任务的真实扣费）
