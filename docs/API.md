# 即梦网页端探测与复用 API 规格 v0.1

> 目标：把「即梦网页版」的免费额度生图能力封装为一个**幂等、可预算、可审计**的本地服务，
> 并把附加图片 / 选模型 / 比例 / 分辨率 / 生成张数这五类网页操作抽象成稳定参数。
>
> 本文件是接口契约。实现见 `jimeng/`，自检见 `python -m unittest discover -s tests -v`（27 项全通过）。
> 设计取舍与探测结论（「张数不是自由变量」等规则）见 [`DESIGN.md`](DESIGN.md)。

---

## 0. 设计结论（先看这 6 条）

| # | 结论 | 影响 |
|---|---|---|
| 1 | **张数不是自由变量**。4.x 模型一次提交固定产出 4 张，`n` 只能是最小计费粒度的倍数；`n=1` 在 4.x 上仍扣 4 张额度 | 成本预估必须先算 `ceil(n/K)*K` |
| 2 | **分辨率是档位提示，不是像素承诺**。实测提交 `1024×576`，产物为 `1280×720` | 不要按提交尺寸做后处理假设，以返回的 `width/height` 为准 |
| 3 | **比例是枚举码**，`16:9 → 1`，`1:1 → 8`（不是 7） | 直接传 `"16:9"` 字符串会失败 |
| 4 | **同账号绝不允许并发**。上游以 `ret=4013`（风控）/ `ret=1310`（并发超限）拒绝 | 每账号一把锁 + 最小请求间隔，默认 3s |
| 5 | **积分必须先预留后提交**。提交即扣/冻结，失败退款不确定 | 所有提交走 预估→预留→提交→结算 |
| 6 | **`dry_run` 是免费的**，只编译报文与算成本，零网络请求 | 所有新场景接入时先用它验证参数 |

---

## 1. 架构

```
调用方 (脚本 / Agent / 业务系统)
        │  HTTP + JSON
        ▼
┌──────────────────────────────────────────┐
│ 网关 jimeng/gateway.py                    │  鉴权、路由、任务表
├──────────────────────────────────────────┤
│ request.py      参数校验 → Plan(含成本)    │
│ payload.py      Plan → 上游 draft_content  │
│ accounts.py     多账号池（可用余额优先）    │
│ client.py       上传→提交→轮询→结算        │
│ credits.py      账本（预留/结算/日配额）    │
│ upload.py       ImageX 上传 + 内容审核      │
│ sign.py         设备身份 + MD5 签名        │
│ transport.py    urllib（零依赖，有限重试）  │
└──────────────────────────────────────────┘
        │  Cookie: sessionid + Device-Time/Sign
        ▼
jimeng.jianying.com (/mweb/v1/*, /commerce/v1/*)
```

**关键分层原则**：`Plan` 是唯一真相源。校验、成本、报文、审计都从同一个 `Plan` 派生，
避免"预估算一套、提交算另一套"的经典超支问题。

---

## 2. 账号与认证

| 项 | 说明 |
|---|---|
| 凭证形态 | 网页登录后的 `sessionid` Cookie |
| 配置方式 | `JIMENG_SESSIONID=sid_xxx` 或 `JIMENG_ACCOUNTS=acc1:sid1,acc2:sid2` |
| 网关鉴权 | `Authorization: Bearer $JIMENG_GATEWAY_TOKEN`（未设置则不校验，仅限本机） |
| 凭证可见性 | **sessionid 永不出现在任何请求参数、响应体、日志中**；对外只暴露 `account_id` |
| 区域 | 默认 `cn`（`jimeng.jianying.com`，`aid=513695`）；国际站需替换 host 与 req_key，当前版本不含 |
| 失效判定 | HTTP 401/403 且空响应体 → `AUTH_EXPIRED`，不重试 |

> 签名算法（网页端轻量防重放，非密钥）：
> `md5("{9e2c}|{uri 末 7 位}|{7}|{appvr}|{device_time}||{11ac}")`，随 `Device-Time`、`Sign-Ver: 1`、`Appid: 513695`、`Pf: 7` 一起发送。
> 它随 web 版本漂移，因此 `appvr` / `da_version` 做成可配置项。

---

## 3. 通用约定

- Base URL：`http://127.0.0.1:8787`
- 编码：UTF-8 JSON；时间戳为 Unix 秒；像素为整数；积分可含小数
- 幂等：`idempotency_key`（或请求指纹）相同 → 直接返回缓存结果，`credits_charged = 0`，`cached = true`，TTL 24h
- 参数严格：**未知字段直接返回 400**（`details.allowed` 列出全部合法键），避免拼写错误静默走默认值
- 重试：仅幂等请求（GET / 余额 / 历史查询）自动重试；`generate`、上传等非幂等 POST **不自动重试**
- 可观测：每个响应带 `X-Request-Id`；错误体里同一 id 出现在 `error.request_id`
- 单位：`n` 为"期望张数"，`submissions` 为"实际提交次数"，二者在 4.x 上不相等
- 错误：一律返回 `{"error": {...}}`，HTTP 状态码语义与 `code` 并行提供

---

## 4. 接口一览

| 方法 | 路径 | 用途 | 消耗积分 |
|---|---|---|---|
| POST | `/v1/images/generations` | 生成（文生图 / 图生图统一入口） | 按 `n` 与模型 |
| GET | `/v1/images/generations/{id}` | 查询提交态任务（`wait=false` 时使用） | 0 |
| POST | `/v1/assets` | 上传参考图，得到 `asset_id` | 0 |
| GET | `/v1/models` | 模型能力矩阵（含探测校准后的置信度） | 0 |
| GET | `/v1/credit` | 各账号余额 | 0 |
| POST | `/v1/probe` | 能力探测（`live=false` 零消耗） | 0 或受 `max_credits` 约束 |
| GET | `/v1/probe/latest` | 最近一次探测报告 | 0 |
| GET | `/healthz` | 存活与账号数 | 0 |

---

## 5. `POST /v1/images/generations`

### 5.1 参数

| 参数 | 类型 | 必填 | 默认 | 约束与语义 |
|---|---|---|---|---|
| `prompt` | string | ✅ | — | 1–2000 字符；图生图时服务端隐式追加 `#` 占位，无需手写 |
| `model` | string | | `jimeng-4.5` | `jimeng-3.0/3.1/4.0/4.1/4.5/4.6/5.0`；也接受内部 `req_key` |
| `negative_prompt` | string | | `""` | ≤500 字符 |
| `images` | array | | `[]` | 参考图，≤10 张；元素为 `{path}` / `{url}` / `{base64}` / `{asset_id}` / `{uri}`，也接受裸字符串（自动判别） |
| `ratio` | string | | `1:1` | `21:9 16:9 3:2 4:3 3:4 2:3 9:16 1:1` 或 `auto`；`auto` 仅 4.0+ |
| `resolution` | string | | `2k` | `1k / 2k / 4k`；须在模型支持集内（3.x 仅 1k/2k） |
| `n` | int | | `1` | 1–4；**受最小计费粒度约束**，见 5.2 |
| `seed` | int | | `-1` | `-1` 为随机 |
| `sample_strength` | float | | `0.5` | 0–1，图生图参考强度 |
| `intelligent_ratio` | bool | | `null` | 显式开关；`ratio=auto` 等价于 `true` |
| `response_format` | string | | `url` | `url` 或 `b64_json` |
| `wait` | bool | | `true` | `false` 时立即返回 `status=submitted` |
| `timeout_s` | float | | `180` | 轮询总超时 |
| `poll_interval_s` | float | | `3` | 轮询间隔（≥0.5） |
| `max_credits` | float | | `null` | 单请求积分上限，超出直接拒绝（提交前拦截） |
| `dry_run` | bool | | `false` | 只编译报文与算成本 |
| `idempotency_key` | string | | `null` | 幂等键，缺省用请求指纹 |
| `account_id` | string | | `null` | 指定账号；缺省按可用余额选择 |
| `draft_content_encoding` | string | | `string` | `string`/`object`，上游两种都接受，用于兼容性探测 |

### 5.2 四个必须理解的语义

**① `n` vs 最小计费粒度**

```
K = model.outputs_per_submit          # 3.x → 1；4.x → 4
submissions = ceil(n / K)
images_returned ≈ submissions * K     # 实际张数由服务端决定，可能多于 n
cost ≈ submissions * K * u(model, resolution)
```

| 请求 | 模型 | submissions | 预计张数 | 预计积分 |
|---|---|---|---|---|
| n=1 | jimeng-4.6 | 1 | 4 | 4 |
| n=2 | jimeng-4.6 | 1 | 4 | 4 |
| n=4 | jimeng-4.6 | 1 | 4 | 4 |
| n=4 | jimeng-3.0 | 4 | 4 | 4 |
| n=2 | jimeng-3.0 | 2 | 2 | 2 |

> 想要"只要 1 张、只花 1 积分"，只能用 3.x；4.x 的最低价就是 4 张。
> 这是免费额度复用里最容易忽略的成本陷阱。

**② `ratio` 是枚举码**

| 比例 | 21:9 | 16:9 | 3:2 | 4:3 | 3:4 | 2:3 | 9:16 | 1:1 |
|---|---|---|---|---|---|---|---|---|
| 码值 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | **8** |

**③ `resolution` 是档位提示**

提交尺寸按档位查表（如 `2k/16:9 = 2560×1440`），但服务端可能归一化输出
（实测 `1k/16:9` 提交 `1024×576` → 产物 `1280×720`）。
故：**不要用请求参数推断产物尺寸，一律读响应的 `data[].width/height`。**

**④ 图生图的两个隐式约定**

- `prompt` 末尾按参考图数量追加 `2 × 张数` 个 `#`（1 张 → `##`）。库内部完成，调用方不写。
- 组件 `generate_type` 从 `generate` 切到 `blend`，并携带 `ability_list`（`byte_edit`）与
  `prompt_placeholder_info_list`（`ability_index` 逐图递增）。

### 5.3 校验顺序（全部在消耗积分之前）

1. `prompt` 非空、长度合法；`n ∈ [1,4]`
2. 模型存在；`resolution` 在 `model.resolutions` 内
3. `ratio=auto` 或 `intelligent_ratio=true` 时，模型必须支持智能比例
4. 参考图数量 ≤ `model.max_reference_images`
5. `sample_strength ∈ [0,1]`；`response_format` 合法
6. `max_credits` 与日配额
7. 余额充足（拉取 `/commerce/v1/benefits/user_credit` 后判断）

任一步失败 → 4xx + 明确 `code`，**不会产生任何提交**。

### 5.4 请求示例

```bash
# 文生图（干跑，零消耗）
curl -s localhost:8787/v1/images/generations -H 'Content-Type: application/json' -d '{
  "prompt": "赛博朋克街道，雨夜，霓虹反射",
  "model": "jimeng-4.6",
  "ratio": "16:9",
  "resolution": "2k",
  "n": 4,
  "dry_run": true
}'

# 图生图（先上传素材）
curl -s localhost:8787/v1/assets -H 'Content-Type: image/png' --data-binary @ref.png
curl -s localhost:8787/v1/images/generations -H 'Content-Type: application/json' -d '{
  "prompt": "改成水彩风格",
  "model": "jimeng-4.5",
  "images": [{"asset_id": "asset_9f2c..."}],
  "ratio": "4:3",
  "resolution": "2k",
  "sample_strength": 0.65,
  "max_credits": 8
}'
```

---

## 6. 响应结构

### 6.1 成功（`status=succeeded`）

```json
{
  "id": "task_5f1c0d9e2a7b4c88",
  "object": "image.generation",
  "created": 1761216085,
  "completed_at": 1761216121,
  "elapsed_ms": 36120,
  "status": "succeeded",
  "model": "jimeng-4.6",
  "request": {
    "mode": "text2img",
    "model": "jimeng-4.6",
    "req_key": "high_aes_general_v42",
    "prompt": "赛博朋克街道，雨夜，霓虹反射",
    "effective_prompt": "赛博朋克街道，雨夜，霓虹反射",
    "negative_prompt": "",
    "ratio": "16:9",
    "ratio_code": 1,
    "resolution": "2k",
    "submit_size": {"width": 2560, "height": 1440},
    "n": 4,
    "submissions": 1,
    "planned_images": 4,
    "unit_credits": 1.0,
    "estimated_credits": 4.0,
    "seed": -1,
    "sample_strength": 0.5,
    "reference_count": 0,
    "outputs_per_submit": 4
  },
  "data": [
    {"index": 0, "url": "https://p3-dreamina-sign.byteimg.com/...", "width": 2560, "height": 1440, "seed": null},
    {"index": 1, "url": "https://p3-dreamina-sign.byteimg.com/...", "width": 2560, "height": 1440, "seed": null}
  ],
  "usage": {
    "images_requested": 4,
    "images_returned": 4,
    "planned_images": 4,
    "submissions": 1,
    "credits_estimated": 4.0,
    "credits_charged": 4.0,
    "credit_source": "gift"
  },
  "account": {"id": "acc1", "region": "cn", "sessionid": "sid_***", "credit": {"balance": {"gift": 62, "total": 62}, "available": 62, "reserved": 0, "spent_today": 4}},
  "upstream": {"submit_id": "6b1f...", "history_ids": ["34561091771148"]},
  "error": null,
  "cached": false
}
```

字段语义要点：

| 字段 | 说明 |
|---|---|
| `request` | 归一化后的**实际生效参数**（含 `effective_prompt` 的 `#` 占位），可直接用于复现 |
| `data[].width/height` | 产物真实像素，非提交尺寸 |
| `usage.credits_charged` | 实测扣减（余额差），非估算；拉余额失败时退化为估算值 |
| `usage.credit_source` | `gift` / `vip` / `purchase_or_mixed` / `unknown` / `cache` / `none`（按结算后余额粗判，非上游流水） |
| `upstream.history_ids` | 网页端"作品 ID"，可用于人工核对或二次取图 |

### 6.2 预览（`dry_run=true`）

`status = "preview"`、`data = []`、`credits_charged = 0`、
`upstream.payload` 给出**完整可直接提交的报文**——这是调试参数最快的路径。

### 6.3 提交态（`wait=false`）

```json
{"status": "submitted", "data": [], "upstream": {"history_ids": ["34561091771148"]}, "pending": ["34561091771148"]}
```

随后 `GET /v1/images/generations/{id}` 会按需回查上游；查到图即转 `succeeded`，不计费、不重复提交。

### 6.4 错误

```json
{
  "error": {
    "code": "CREDIT_INSUFFICIENT",
    "message": "可用积分不足：可用 2，需要 4",
    "type": "billing",
    "retryable": false,
    "details": {"available": 2, "estimated": 4},
    "request_id": "req_9c1f2a7b4e08"
  }
}
```

| code | HTTP | retryable | 触发条件与处置 |
|---|---|---|---|
| `PARAM_INVALID` | 400 | ❌ | 参数缺失/越界；按 `details.available` 修正 |
| `CAPABILITY_UNSUPPORTED` | 422 | ❌ | 模型不支持该分辨率/比例/参考图；换模型或降档 |
| `AUTH_EXPIRED` | 401 | ❌ | sessionid 失效，重新登录取 Cookie |
| `CREDIT_INSUFFICIENT` | 402 | ❌ | 余额不足；换账号或等次日重置 |
| `BUDGET_EXCEEDED` | 402 | ❌ | 单请求上限或日配额触顶；调 `max_credits` |
| `RATE_LIMITED` | 429 | ✅ | 频控；退避后重试 |
| `ACCOUNT_BUSY` | 409 | ✅ | 账号被占用或上游并发超限（`1310`）；等待 |
| `RISK_CONTROL` | 403 | ❌ | 上游风控（`4013`）；**不要重试**，换账号并降低频率 |
| `CONTENT_FILTERED` | 422 | ❌ | 内容审核（`2038`）；改 prompt |
| `TASK_FAILED` | 502 | ⚠️ | 生成失败（`status=30`）；已结算，可重试但会再扣费 |
| `TASK_TIMEOUT` | 504 | ✅ | 轮询超时；任务可能仍在跑，**先查 history 再决定重提** |
| `UPSTREAM_ERROR` | 502 | ✅ | 其他上游错误，透传 `ret/errmsg` |
| `NOT_FOUND` | 404 | ❌ | 路由或任务不存在 |

### 6.5 状态机

```
preview ──(dry_run)──┐
                     ▼
submitted ──→ succeeded ──→ cached(命中幂等)
     │            ▲
     └─→ running ─┘
     ├─→ failed     （已结算为 0，异常向上抛）
     └─→ timeout    （按预估保守结算，返回 pending）
```

---

## 7. `POST /v1/assets`

三种输入，任选其一：

| 方式 | 请求 | 说明 |
|---|---|---|
| 二进制 | `Content-Type: image/png`，body 为原始字节 | 最简，推荐 |
| Base64 | `{"base64": "data:image/png;base64,...", "filename": "a.png"}` | 适合跨进程传递 |
| 远程 URL | `{"url": "https://..."}` | 服务端代下载后上传 |
| 本地路径 | `{"path": "D:/imgs/a.png"}` | 仅本机可用 |

返回：

```json
{"object": "asset", "asset_id": "asset_3f9c2b71e0a4d5c6", "image_uri": "tos-cn-i-tb4s082cfz/xxxx",
 "width": 0, "height": 0, "format": "png", "bytes": 184320, "sha256": "3f9c..."}
```

上游链路：`get_upload_image_proof` → `imagex.bytedanceapi.com` 分片上传 → `submit_audit_job`（审核）。
**未通过审核的图不能作为参考图**；审核失败常见 `ret=3020`（CDN 未就绪），内部会自动重试 2 次。

---

## 8. `GET /v1/models`

```json
{"object": "list", "data": [{
  "id": "jimeng-4.6", "req_key": "high_aes_general_v42", "label": "图片 4.6",
  "outputs_per_submit": 4, "resolutions": ["1k","2k","4k"],
  "ratios": ["21:9","16:9","3:2","4:3","3:4","2:3","9:16","1:1"],
  "supports_reference": true, "supports_intelligent_ratio": true,
  "max_reference_images": 10,
  "credits_per_image": {"1k": 1.0, "2k": 1.0, "4k": 2.0},
  "confidence": "default", "note": ""
}]}
```

`confidence`：`default`（目录推测）→ `calibrated`（探测实测写入 `calibration.json`）。
**生产场景应只信任 `calibrated` 的 `outputs_per_submit` 与 `credits_per_image`。**

---

## 9. `GET /v1/credit`

```json
{"object": "list", "data": [{"account_id": "acc1", "credit": {"gift": 66, "purchase": 0, "vip": 0, "total": 66, "observed_at": 1761216085}}]}
```

上游：`POST /commerce/v1/benefits/user_credit`（空 body）。
解析采用"递归收集 credit 数值叶子 + 键名归一化"，兼容 `giftCredit` / `gift_credit` / 嵌套 `credits.*`。

---

## 10. `POST /v1/probe`

```json
{"live": false, "probes": ["static","auth","count","ratio","resolution","cost"],
 "model": "jimeng-4.5", "ratios": ["1:1","16:9"], "resolutions": ["1k","2k"],
 "max_credits": 8.0, "persist_calibration": true}
```

| 探测项 | 消耗 | 结论 |
|---|---|---|
| `static` | 0 | 每个 (模型 × 比例 × 分辨率) 报文能否编译、码值/尺寸是否自洽 |
| `auth` | 0 | sessionid 是否有效、当前余额 |
| `count` | K 张 | `outputs_per_submit` 实测值 → 计费粒度 |
| `ratio` | 每比例 K 张 | 产物宽高比与请求是否一致（容差 5%） |
| `resolution` | 每档 K 张 | 产物像素 vs 提交像素，识别服务端归一化 |
| `cost` | 每档 K 张 | 余额差 / 张数 = 单位积分 |

`max_credits` 是**硬闸门**：预估超出即跳过该子项，报告里记 `skipped`。
`persist_calibration=true` 时把结论写入 `calibration.json`，下次启动 `Catalog.load()` 自动生效。

---

## 11. 积分调用模型

### 11.1 成本公式

```
cost = ceil(n / K) × K × u(model, resolution)
```

- `K = outputs_per_submit`（3.x=1，4.x=4；以探测值为准）
- `u`：单位积分/张。默认 1k/2k = 1.0，4k = 2.0（**4k 单价为推测，须探测确认**）

### 11.2 免费额度

| 项 | 值 | 依据 |
|---|---|---|
| 每日赠送 | 66 积分（中国站，登录赠送） | 官方活动口径；实测账户余额可见 `giftCredit` |
| 重置时间 | 自然日（+08:00） | 本地账本按日期滚动，跨日清零 `spent_today` |
| 多账号 | 额度独立，不合并 | 每账号独立 `Ledger` |

### 11.3 预留—结算

```
reserve(estimate) → 占额度，阻止并发超支
   ↓ 提交/轮询
settle(actual = 余额前 - 余额后, update_balance=False)
```

- 失败（`TaskFailed` 等非超时错误）→ 全额退回（`actual=0`）
- 超时 → **按预估值保守结算**（任务可能仍在跑并已扣费），响应带 `status=timeout` 与 `pending: [history_id]` 供人工复核
- 拉余额失败 → `actual = estimate`，并 `update_balance=True` 让账本自行扣减，宁可高估

### 11.4 六条硬约束

1. **禁止跨账号并发同一 sessionid**：同账号串行 + 默认 3s 最小间隔。
2. **禁止无预算提交**：生产调用必须带 `max_credits`；网关侧另有 `JIMENG_MAX_CREDITS_PER_REQUEST`。
3. **禁止对 `RISK_CONTROL` 重试**：重试只会加重风控标记。
4. **禁止把 `n` 当无序批量**：`n > K` 会拆多次提交，成本线性叠加，必须提前告知上游调用方。
5. **禁止在响应/日志中回显 sessionid**：`account.redacted()` 已截断。
6. **禁止对非幂等 POST 自动重试**：重放 `generate` 等于二次提交、二次扣费；传输层默认 `retry=0`。
7. **禁止用免费额度做商业转售**：见第 14 节合规边界。

---

## 12. 复用约束（把能力搬到其他场景）

### 12.1 三种复用形态

| 形态 | 适用场景 | 关键约束 |
|---|---|---|
| 进程内 SDK | 单脚本、Agent 工具调用 | 直接 `JimengClient.generate()`；进程退出即丢幂等缓存，长任务需外部持久化 |
| 本地网关 | 多语言服务、团队共用 | 必须设 `JIMENG_GATEWAY_TOKEN`；网关是唯一凭证持有者 |
| 批处理队列 | 批量出图、夜间跑量 | 串行消费 + 每任务 `max_credits`；失败任务进死信，不自动重试超时类 |

### 12.2 复用必须遵守的 8 条

1. **幂等键语义**：同一业务单号 → `idempotency_key`，重试不二次扣费。
2. **缓存键只含影响产出的字段**：prompt/model/ratio/resolution/n/seed/strength/参考图哈希。
3. **参考图先落地再复用**：上传一次得 `image_uri`，同图复用不要重复上传（`sha256` 去重）。
4. **比例/分辨率必须按模型裁剪**：UI 上给用户选，提交前用 `/v1/models` 过滤非法组合。
5. **批量任务要串行**：并发只会换回 `1310`，不会更快。
6. **成本前置展示**：下单前调 `dry_run` 拿 `usage.credits_estimated`，让调用方确认。
7. **产物下载与生成解耦**：CDN URL 可能带签名与时效，建议生成后立即落盘。
8. **降级路径**：余额不足时降 `resolution` 或换 3.x 模型，而不是减少 `n`（4.x 上无效）。

### 12.3 缓存键规范

```python
sha256(json.dumps({
  "prompt": prompt.strip(), "model": model, "negative": negative.strip(),
  "ratio": ratio, "resolution": resolution, "n": n, "seed": seed,
  "strength": round(strength, 3), "images": sorted(image_keys), "intelligent": intelligent_ratio
}, sort_keys=True))
```

---

## 13. 能力矩阵（目录默认值）

| 模型 | 内部 key | 单次张数 | 分辨率 | 参考图 | 智能比例 | 置信度 |
|---|---|---|---|---|---|---|
| jimeng-3.0 | `high_aes_general_v30l` | 1 | 1k/2k | ✅ | ❌ | default |
| jimeng-3.1 | `high_aes_general_v30l_art_fangzhou` | 1 | 1k/2k | ✅ | ❌ | default |
| jimeng-4.0 | `high_aes_general_v40` | 4 | 1k/2k/4k | ✅ | ✅ | default |
| jimeng-4.1 | `high_aes_general_v41` | 4 | 1k/2k/4k | ✅ | ✅ | default |
| jimeng-4.5 | `high_aes_general_v40l` | 4 | 1k/2k/4k | ✅ | ✅ | default |
| jimeng-4.6 | `high_aes_general_v42` | 4 | 1k/2k/4k | ✅ | ✅ | default |
| jimeng-5.0 | `high_aes_general_v50` | 4 | 1k/2k/4k | ✅ | ✅ | default |

> `default` 表示"据公开抓包与网页行为整理"，非本机实测。
> 跑一次 `probe --live --probes count,cost --model <M> --write` 即可升级为 `calibrated`。

分辨率对照（提交尺寸）：

| 档位 | 1:1 | 4:3 | 3:4 | 16:9 | 9:16 | 3:2 | 2:3 | 21:9 |
|---|---|---|---|---|---|---|---|---|
| 1k | 1024² | 1024×768 | 768×1024 | 1024×576 | 576×1024 | 1024×682 | 682×1024 | 1195×512 |
| 2k | 2048² | 2304×1728 | 1728×2304 | 2560×1440 | 1440×2560 | 2496×1664 | 1664×2496 | 3024×1296 |
| 4k | 4096² | 4608×3456 | 3456×4608 | 5120×2880 | 2880×5120 | 4992×3328 | 3328×4992 | 6048×2592 |

---

## 14. 风险与合规边界

**必须明确的三点：**

1. **协议风险**：网页端接口非公开 API，自动化调用通常违反平台用户协议；账号可能被限流、清空额度或封禁。
   本项目定位是**个人额度内的能力研究**，不提供绕过付费、批量注册、多账号刷量的能力。
2. **额度属性**：每日赠送积分属于个人账号权益，**不得转售或作为付费服务交付给第三方**。
3. **内容合规**：生成结果需符合平台内容规范；`2038` 为审核拦截，不应通过改词绕过。

工程侧建议：默认只监听 `127.0.0.1`；生产复用务必加网关令牌与调用方审计（谁、何时、消耗多少）。

---

## 15. 未知项与演进

| 未知项 | 现状 | 验证方式 |
|---|---|---|
| 4k 单价是否真为 2 积分 | 推测 | `probe --live --probes cost --resolutions 4k` |
| 4.x `generate_number` 是否被服务端尊重 | 4.x 固定 4 张，`n` 只影响提交次数 | `probe --live --probes count` |
| 每日 66 积分的发放时点 | 未确认（登录/自然日） | 连续两日 `GET /v1/credit` 对比 |
| 意图比例在 3.x 上的行为 | 不支持，已拦截 | `static` 探测即可确认 |
| 国际站（dreamina）差异 | 未实现 | 需替换 host/req_key/assistant_id |

版本策略：`Plan` 与响应字段保持向后兼容；新增参数只增不改；`calibration.json` 带 `version`，升级时按模型键合并。
