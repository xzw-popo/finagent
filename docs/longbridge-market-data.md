# Longbridge 行情接入

## 定位

V1.1 将 Longbridge 作为研究工作流之前的可选、只读数据采集器。采集器只生成 EvidenceBundle，不调用 LLM、不读取账户、不生成订单，也不改变现有研究状态机。

## 使用方式

先按 [Longbridge 官方安装文档](https://open.longbridge.com/skill/install.md) 安装 CLI。环境要求认证时，按官方流程使用一次性 auth code 登录；不要把 code 或 token 写入仓库。然后更新并检查 CLI：

```bash
longbridge update
longbridge --version
longbridge check
```

采集一个或多个明确市场后缀的 symbol：

```bash
uv run --no-editable finresearch collect-quote \
  NVDA.US 700.HK 600519.SH \
  --output runs/quotes-2026-09-01
```

支持的后缀为 `US`、`HK`、`SH`、`SZ`、`SG` 和 `HAS`。`HK` 代码支持数字证券（如 `700.HK`）和安全的字母数字指数代码（如 `HSI.HK`）。采集器可以把小写转换成大写并去重，但不会根据公司名称或裸 ticker 猜测市场。

可选参数：

- `--timeout 20`：Longbridge 单条命令的超时秒数。
- `--region auto|cn|global`：受控的接入区域；默认让 CLI 自动判断。

输出目录必须不存在，成功后包含：

```text
output/
├── raw-response.json   # Longbridge 原始 stdout 字节
├── evidence.json       # 现有工作流可读取的 EvidenceBundle
└── collection.json     # 采集 manifest 与原始响应 hash
```

成功输出会直接列出本次的 `available_at` 和每个 `evidence_id`。把这些 ID 写入请求的 `allowed_evidence_ids`，并设置不早于 `available_at` 的 `as_of`：

```json
{
  "request_id": "market-research-2026-09-01",
  "question": "截至指定时点，这些证券的行情快照显示了什么？",
  "universe": ["NVDA.US", "700.HK"],
  "as_of": "<available_at 或更晚的带时区时间>",
  "horizon": "point-in-time",
  "allowed_evidence_ids": ["<evidence_id 1>", "<evidence_id 2>"]
}
```

完整命令链：

```bash
uv run --no-editable finresearch validate \
  --request path/to/market-request.json \
  --evidence runs/quotes-2026-09-01/evidence.json

uv run --no-editable finresearch run \
  --mode mock \
  --request path/to/market-request.json \
  --evidence runs/quotes-2026-09-01/evidence.json \
  --output runs/market-research-2026-09-01
```

`run --output` 也必须指向一个尚不存在的目录。需要把行情与财报、公告或知识库证据合并时，使用 V1.2 的受控重打包命令，不要手工拼接 JSON：

```bash
uv run --no-editable finresearch merge-evidence \
  --evidence runs/quotes-2026-09-01/evidence.json \
  --evidence data/filings/evidence.json \
  --output runs/combined-evidence
```

该命令会校验并复制每条 Evidence 引用的 sidecar，重写为内容寻址路径，同时在 `merge.json` 中保留哈希谱系。合并阶段不做 PIT 过滤；后续仍对合并后的 `evidence.json` 执行 `validate/run`。详细契约见 [证据包合并](evidence-merge.md)。

用 EvidenceBundle 运行 `finresearch run` 时，工作流会再次校验 provenance 引用的 sidecar，并把它一起物化到研究运行目录。直接使用采集包时该文件是 `raw-response.json`；使用合并包时则是 `artifacts/sha256/<raw_sha256>`。输出中的 `eligible_evidence.json` 仍是标准 EvidenceBundle，可以从新目录独立加载。

## 退出码

`collect-quote` 的运行期错误会在 stderr 中带有稳定的 `code` 和 `retryable=true|false`，不会伪装成 argparse 用法错误：

| 退出码 | 含义 |
| --- | --- |
| `2` | 参数、配置或 symbol 输入错误 |
| `3` | Longbridge CLI 未安装或不可执行 |
| `4` | 命令超时 |
| `5` | 认证、网络、供应商或协议/结构错误 |
| `6` | 供应商返回空数据 |
| `7` | 请求的 symbol 只返回了一部分 |
| `8` | 本地输出目录或写盘错误 |

## 数据转换

供应商原始响应允许增加字段。采集器只把以下已审查字段送入标准化 Evidence：

```text
symbol, last, change_value, change_percentage,
prev_close, open, high, low, volume, turnover, status,
pre_market, post_market, overnight
```

价格、成交额和成交量使用十进制字符串，避免二进制浮点精度漂移。`null`、`"-"` 和空字符串统一为缺失值。适配层兼容 Longbridge 旧字段名 `last_done`、`trade_status` 和 `*_market_quote`，但会拒绝重复 JSON key、NaN/Infinity 和非法数值。空数组标记为 `no_data`，缺少已请求 symbol 标记为 `partial_result`，额外、重复或非法返回 symbol 标记为 `protocol_mismatch`，便于调度层分别处理。

## PIT 时间语义

一次 quote 响应混合 regular、盘前、盘后和 overnight 状态，顶层没有覆盖整个快照的统一来源时间。因此：

```text
published_at  = null                # 不虚构交易所发布时间
known_at      = 完整响应接收时间
retrieved_at  = 完整响应接收时间
available_at  = 标准化完成时间
source_event_at = null              # 组合快照没有单一来源事件时间
```

研究门禁使用：

```text
(available_at or known_at) <= request.as_of
```

这代表“系统当时实际拥有且已经处理完成的数据”，而不是声称价格在该本地时间发生。盘前、盘后和 overnight 对象内部的时间戳仍保留在标准化快照中。

Longbridge 行情等级取决于账户、市场和网络区域。当前 CLI 响应未提供可统一验证的 entitlement/delay 标志，因此 Evidence 的 `freshness` 保守记录为 `unknown`，不能据此宣称所有市场都是真正实时行情。

## 安全边界

- 固定执行 `longbridge quote SYMBOL... --format json`，不接受任意子命令或附加参数。
- 使用 argv 数组、`shell=False` 和 `stdin=DEVNULL`。
- symbol 必须先通过严格 ASCII 与市场后缀校验。
- Longbridge 在临时工作目录运行，避免日志落入仓库。
- 子进程环境不包含 DeepSeek API Key 或 `LONGBRIDGE_ENV=staging`。
- stdout/stderr 分离；成功时不持久化 warning 原文，manifest 只保留来源、SHA-256 和字节数；失败 stderr 只用于内部分类，对外输出通用错误消息。
- 整批响应必须完整匹配请求 symbol；V1.1 不静默接受 partial result，也不自动重试或切换区域。
- 输出目录默认禁止覆盖。

数据来源：长桥证券。官方接口说明见 [Longbridge Quote API](https://open.longbridge.com/docs/quote/pull/quote)。
