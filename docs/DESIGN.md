# 即梦网页版接口封装

把 `jimeng.jianying.com` 的出图流程封装成可调用的 SDK 和 HTTP 接口。
接口字段见 [`API.md`](API.md)，这里记录设计上的取舍，以及实测确认、必须写进代码的行为。

## 背景

网页版每天有 66 积分，一次 4.x 生成消耗 4 分；请求体是嵌套很深的 `draft_content`，参数语义和界面上的说法并不一致。
直接照着抓包发请求能跑通，但很难安全地复用：并发、重试、失败退款都会影响额度。

项目提供四部分：

- `JimengClient`：进程内调用，负责一次生成的完整流程。
- HTTP 网关：`/v1/images/generations` 等接口，供其他进程或语言使用。
- 探测命令：校验参数组合，实测张数与单价。
- `calibration.json`：探测结果，启动时覆盖目录里的默认值。

## 网页操作与参数的对应关系

| 网页动作 | 接口参数 | 上游字段 | 备注 |
|---|---|---|---|
| 附加图片 | `images[]`，最多 10 张 | `component.abilities.blend.ability_list[].image_uri_list`、`prompt_placeholder_info_list` | 需要先上传取得 `image_uri` 并通过审核；prompt 末尾补 `#`；组件类型改为 `blend` |
| 选择模型 | `model` | `extend.root_model` 与 `core_param.model` | 两处要一致；对外名称和内部 key 不同，例如 `jimeng-4.6` 对应 `high_aes_general_v42` |
| 图片比例 | `ratio` | `core_param.image_ratio` | 上游要枚举码，不接受字符串；`1:1` 的值是 8 |
| 分辨率 | `resolution` | `core_param.large_image_info` 的三个字段 | 宽、高、档位要成套；档位不等于最终像素 |
| 生成张数 | `n` | `gen_option.generate_number`、`metrics_extra.generateCount` | 4.x 上不控制张数，只控制提交次数 |

对外是一个参数，落到上游往往三四个字段，而且失败方式各不相同。
这些转换集中在 `payload.py`，其他模块只处理业务参数。

## 实测确认的行为

以下结论来自抓包报文和本机提交结果，未验证的条目在最后一节单独列出。

### 生成张数由模型决定

`jimeng-4.6`、`16:9`、`1k`，提交时 `generateCount=1`，返回 4 张 PNG。
4.x 属于系列组图模型，一次提交固定出 4 张，`n` 只影响提交次数：

```
K = 单次产出张数      # 3.x 为 1，4.x 为 4
submissions = ceil(n / K)
实得张数 = submissions * K
花费 = submissions * K * 单价
```

| 需求 | 模型 | 提交次数 | 实得 | 花费 |
|---|---|---|---|---|
| 1 张 | jimeng-3.0 | 1 | 1 | 1 |
| 1 张 | jimeng-4.6 | 1 | 4 | 4 |
| 4 张 | jimeng-4.6 | 1 | 4 | 4 |
| 4 张 | jimeng-3.0 | 4 | 4 | 4 |

要控制成本只能换 3.x 或降低分辨率。在 4.x 上把 `n` 调小不会减少花费。

代码里 `Plan.submissions` 和 `Plan.planned_images` 分开记录，响应的 `usage` 同时给出请求张数和实际张数。

### 分辨率只是档位提示

`1k` 加 `16:9` 提交的尺寸是 `1024x576`，产物是 `1280x720`。服务端会按比例归一化长边。

产物尺寸以响应里的 `data[].width/height` 为准，不要用请求参数推算。`request.submit_size` 和产物尺寸都放在响应里，方便对照。

### 比例使用枚举码

`21:9=0, 16:9=1, 3:2=2, 4:3=3, 3:4=4, 2:3=5, 9:16=6, 1:1=8`。

`1:1` 是 8 而不是 7，按顺序类推会失败。`ratio=auto` 只支持 4.0 及以上，且必须省略 `image_ratio` 字段，传了反而会固定比例。

### 图生图有三个前置条件

1. 参考图走 `get_upload_image_proof`、ImageX 上传、`submit_audit_job` 三步，取得 `image_uri`。
2. prompt 末尾按图片数量补 `2 * N` 个 `#`，一张是 `##`，两张是 `####`。
3. 组件 `generate_type` 改成 `blend`，同时补 `ability_list` 和 `prompt_placeholder_info_list`。

审核不通过时参考图不可用。曾遇到 `ret=3020 download file failed`，是 CDN 尚未就绪，等待后重试可以恢复。

### 同一账号不能并发

- 请求缺少浏览器签名时可能被判风控：`ret=4013`，`fail_starling_key=web_risk_control_message_reject_generation`。
- 并发超过限制：`ret=1310 exceed_model_parallel_max`。

代码里每个账号一把非阻塞锁，拿不到直接返回 `ACCOUNT_BUSY`，不排队；请求之间默认间隔 3 秒。
风控类错误不重试。

视频生成需要浏览器的 `msToken` 和 `a_bogus`，纯接口调用会被 4013 拦下，所以这个项目只做图像，不引入浏览器自动化。

### 扣费发生在提交阶段

余额差显示扣费在提交和生成阶段，不在下载阶段。失败是否退还、多久退还，上游没有明确约定。

处理方式：

- 提交前按预估值 `reserve`，防止并发超支；
- 结算用提交前后的余额差，此时不再做本地扣减，避免重复记账（`settle(balance_is_fresh=True)`）；
- 确定失败（`status=30`）按 0 结算并记录流水；
- 超时按预估计费，响应里带 `pending`，可以用 `history_id` 事后核对。

### 历史状态码有三套

`20` 进行中，`30` 失败，`40` 成功，`50` 成功，`21` 完成。早期版本和当前版本的取值不同。
另外 `2038` 表示内容审核拦截。

代码把 `{21, 40, 50}` 视为成功，`{30}` 视为失败，其余按进行中处理；有产物且状态不在进行中时也判成功，避免状态码变更导致一直轮询。

### 额度按账号计算

每日 66 积分，按北京时间自然日重置。多账号额度独立，不合并。
账本按账号隔离，`GET /v1/credit` 逐个返回；选择账号时取可用余额最多的一个。

4k 单价目前是推测值（2 积分/张），目录里标记为 `default`，跑一次成本探测可以校准。

## 成本计算

```
cost = ceil(n / K) * K * u(model, resolution)
```

`K` 是单次产出张数，`u` 是单位积分，默认 1k 和 2k 为 1，4k 为 2。

| 场景 | 参数 | 预估积分 |
|---|---|---|
| 调试 | `3.0 / 1k / 1:1 / n=1` | 1 |
| 常规 | `4.6 / 2k / 16:9 / n=4` | 4 |
| 高清 | `4.5 / 4k / 16:9 / n=4` | 8 |
| 批量 12 张 | `4.6 / 2k / n=4` 提交 3 次 | 12 |

## 结构

```
GenerateRequest --校验--> Plan --编译--> draft_content --提交--> history_id
                            |                                      |
                            +-- 成本估算                            +-- 轮询
                            +-- 响应回显                            +-- 产物 URL
```

`Plan` 由校验结果一次算出，成本、报文、响应都从它取，避免预估和提交各算一套。

三条主要路径：

1. 成本：`Plan.estimated_credits` → `reserve` → `settle`（余额差）。
2. 幂等：`cache_fingerprint` 命中后直接返回缓存，不再提交，`credits_charged` 为 0。
3. 失败：`TaskFailed`、`RiskControl` 等按 0 结算并抛出；`TaskTimeout` 按预估计费，返回 `pending`。

## 限制

| 约束 | 默认值 | 位置 |
|---|---|---|
| 单账号并发 | 1 | `Account.lock` |
| 请求间隔 | 3 秒 | `Account.cooldown_s`、`JIMENG_COOLDOWN_S` |
| 单次积分上限 | 不限制 | `max_credits`、`JIMENG_MAX_CREDITS_PER_REQUEST` |
| 每日额度 | 66 | `Ledger.daily_quota`、`JIMENG_DAILY_QUOTA` |
| 参考图数量 | 10 | `ModelSpec.max_reference_images` |
| `n` 上限 | 4 | `MAX_N` |
| 重试 | 见 `transport.py` | `Transport.retries` |

`RISK_CONTROL`（4013）和 `CONTENT_FILTERED`（2038）不重试。`RATE_LIMITED`、`ACCOUNT_BUSY` 可以退避重试；`TASK_TIMEOUT` 重提前应先查 `history_id`。

## 探测

`static` 不消耗额度，把目录里每个模型、比例、分辨率的组合编译一遍，校验码值和尺寸是否与目录一致。

`live` 会真实提交，顺序是 `count`、`ratio`、`resolution`、`cost`。先测张数是因为它决定成本公式里的 `K`；成本放最后，因为要读提交前后的余额差，前面的探测会改变余额基线。

每次提交前检查 `spend + estimate` 是否超过 `max_credits`，超过就跳过并记为 `skipped`。

探测结果写到 `calibration.json`，`Catalog.load()` 启动时读取，只覆盖 `outputs_per_submit`、`credits_per_image`、`resolutions` 三个字段，避免一次异常探测改坏能力目录。

## 代码组织

| 文件 | 职责 |
|---|---|
| `errors.py` | 错误码与 HTTP 状态 |
| `catalog.py` | 模型、比例、分辨率目录，启动时校验 |
| `request.py` | 参数校验，生成不可变 `Plan` |
| `payload.py` | `Plan` 转 `draft_content` |
| `credits.py` | 账本，预留、结算、配额、流水 |
| `client.py` | 上传、提交、轮询、结算 |
| `parsing.py` | 上游返回的容错解析 |
| `probe.py` | 静态与实测探测 |
| `gateway.py` | HTTP 网关 |
| `accounts.py` | 多账号池 |
| `cache.py` | 幂等结果缓存 |
| `transport.py` | HTTP 请求与重试 |
| `sign.py` | 签名头与设备信息 |
| `upload.py` | 参考图上传 |

阅读顺序建议：`request.py` 看参数语义，`client.py` 的 `generate` 看整体流程，`credits.py` 看额度怎么算。

## 测试

`tests/test_offline.py` 有 27 个用例，使用假的 transport，不联网也不消耗额度。

覆盖参数校验、计费粒度、能力拦截、预算与配额、幂等缓存、图生图报文、余额与历史解析、探测预算、网关路由，以及几个回归用例（POST 不重试、缓存副本隔离、异常释放预留）。

签名算法、报文嵌套层级、错误码映射没有 mock，这几处最容易出错，mock 掉就测不到问题。

## 未确认

| 项 | 现状 | 确认方式 |
|---|---|---|
| 4k 单价 | 推测 2 积分 | `probe --live --probes cost --resolutions 4k --write` |
| 66 积分的发放时点 | 未确认 | 连续两天 `GET /v1/credit` 对比 |
| 失败是否退还 | 未确认 | 失败前后余额对比 |
| 风控触发阈值 | 未测 | 不建议主动试探 |
| 国际站差异 | 未实现 | 需替换 host、`req_key`、`assistant_id` |
| 视频生成 | 不做 | 需要浏览器签名 |

## 使用限制

网页端接口不是公开 API，自动化调用通常不符合平台用户协议，账号可能被限流、清空额度或封禁。
每日赠送的积分属于个人账号，不要转售，也不要包装成付费服务。

网关默认只监听 `127.0.0.1`，sessionid 不会出现在请求、响应和日志里。对外提供服务时需要设置 `JIMENG_GATEWAY_TOKEN`。

## 后续

- 账本落 SQLite，现在只在内存里。
- 任务表持久化，`wait=false` 的任务重启后会丢。
- `GET /v1/usage`，按调用方和模型统计消耗。
- 补 OpenAI 兼容层的错误映射。
- 国际站适配。
- 超时任务的对账命令，用 `history_id` 查实际扣费。
