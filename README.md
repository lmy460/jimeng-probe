# jimeng-probe

`jimeng.jianying.com` 网页版的接口封装。把附加图片、选择模型、比例、分辨率、生成张数这几个界面操作
映射成接口参数，并处理免费额度的成本与并发限制。

纯标准库实现（`urllib` + `http.server`），Python 3.9 及以上，无第三方依赖。

| 文档 | 内容 |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | **项目说明**：设计取舍、探测发现的规则（张数不是自由变量等）、成本模型、已知未知 |
| [`docs/API.md`](docs/API.md) | **接口契约**：参数定义、返回结构、错误码、积分与复用约束 |
| [`docs/REVIEW.md`](docs/REVIEW.md) | **缺陷与修复记录**：14 个问题的成因、处理与回归用例 |

## 快速开始

```bash
# 1) 零消耗：能力目录、报文自检、静态探测（都不需要登录）
python -m jimeng models
python -m jimeng selftest --model jimeng-4.6 --ratio 16:9 --resolution 2k --n 4
python -m jimeng probe --model jimeng-4.5

# 2) 配置凭证（网页 F12 → Application → Cookies → sessionid）
export JIMENG_SESSIONID=sid_xxx          # Windows PowerShell: $env:JIMENG_SESSIONID="sid_xxx"

# 3) 查余额 / 干跑 / 真跑
python -m jimeng credit
python -m jimeng generate --prompt "雨夜霓虹街道" --ratio 16:9 --resolution 2k --dry-run
python -m jimeng generate --prompt "雨夜霓虹街道" --ratio 16:9 --resolution 2k --n 4 \
        --max-credits 8 --out ./out

# 4) 起网关，供其它程序复用
python -m jimeng serve --port 8787
curl -s localhost:8787/v1/models | head -c 400
```

也可以直接装成命令：

```bash
pip install -e .
jimeng models
```

最小 SDK 示例见 [`examples/quickstart.py`](examples/quickstart.py)。

## 配置项

| 变量 | 说明 | 默认 |
|---|---|---|
| `JIMENG_SESSIONID` | 网页登录后的 sessionid | 无（必填才能真实调用） |
| `JIMENG_ACCOUNTS` | 多账号，格式 `acc1:sid1,acc2:sid2` | 空 |
| `JIMENG_REGION` | 区域标识，当前仅支持 `cn` | `cn` |
| `JIMENG_COOLDOWN_S` | 同账号两次请求的最小间隔（秒） | `3` |
| `JIMENG_DAILY_QUOTA` | 单账号每日积分上限 | `66` |
| `JIMENG_MAX_CREDITS_PER_REQUEST` | 单次请求积分上限 | 不限制 |
| `JIMENG_GATEWAY_TOKEN` | 网关 Bearer 令牌 | 空（不校验，仅限本机） |
| `JIMENG_CALIBRATION` | 校准文件路径 | 项目根目录 `calibration.json` |

## 行为约定

| # | 规则 | 后果 |
|---|---|---|
| 1 | 4.x 一次提交固定出 4 张，`n` 只决定提交次数 | `n=1` 在 4.x 上仍扣 4 积分；想省额度要用 3.x |
| 2 | 分辨率是档位提示，不是像素承诺 | 提交 `1024×576`，产物可能是 `1280×720`，以响应为准 |
| 3 | 比例是枚举码，`1:1 = 8`（不是 7） | 传字符串或顺序类推都会失败 |
| 4 | 图生图需先上传过审，且 prompt 尾部补 `##` | 缺一步就得到审核错误或参考图失效 |
| 5 | 同账号禁止并发（`4013` / `1310`） | 并发不会提速，只会触发风控 |
| 6 | 积分在提交时扣除，退款无契约 | 必须预估→预留→提交→结算，靠余额差对账 |

## 探测与校准

```bash
# 实测单次产出张数与单位积分成本，写入 calibration.json（会消耗积分，受 --max-credits 硬约束）
python -m jimeng probe --live --probes count,cost --model jimeng-4.6 --max-credits 8 --write
python -m jimeng models          # confidence 由 default 变为 calibrated
```

`calibration.json` 是运行时覆盖层：下次启动 `Catalog.load()` 自动读取，成本预估与张数规划立刻改用实测值。

## 目录结构

```
jimeng/
  __main__.py     python -m jimeng 入口
  sign.py         设备身份 + MD5 签名（Device-Time / Sign）
  catalog.py      模型·比例·分辨率目录（启动校验 + 可被探测校准）
  request.py      GenerateRequest → 不可变 Plan（严格参数 + 成本预估）
  payload.py      Plan → draft_content（嵌套报文编译）
  upload.py       参考图上传（proof → ImageX → 审核）
  credits.py      积分账本（不可变快照 / 预留结算 / 日配额）
  cache.py        TTL + LRU 幂等缓存（按值存取）
  client.py       单账号编排（上传 / 提交 / 轮询 / 结算）
  parsing.py      上游返回的容错解析
  accounts.py     多账号池（可用余额优先）
  probe.py        能力探测（static / auth / count / ratio / resolution / cost）
  gateway.py      REST 网关（表驱动路由，零依赖）
  transport.py    urllib 传输层（重试策略显式化）
docs/
  DESIGN.md       项目说明与探测规则
  API.md          接口规格
  REVIEW.md       缺陷与修复记录
examples/
  quickstart.py   最小可运行示例
tests/
  test_offline.py 27 项离线测试（FakeTransport，不触网）
```

## 测试

```bash
python -m unittest discover -s tests -v
# Ran 27 tests ... OK
```

覆盖：报文正确性、最小计费粒度、能力拦截、资金安全（预算/配额/余额/预留释放）、幂等缓存隔离、
图生图链路、余额与历史解析容错、探测预算闸门、网关路由与 pending 回查、POST 不自动重试。

## 风险提示

网页端接口非公开 API，自动化调用可能触发风控并违反平台用户协议。
本项目仅用于个人额度内的能力研究与工程验证，不提供绕过付费、批量注册或额度转售能力。
