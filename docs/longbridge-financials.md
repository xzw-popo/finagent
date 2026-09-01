# Longbridge 财报接入

## 定位

V1.3 将 Longbridge 作为只读财务数据提供方，在 Agent 工作流之前生成可审计 EvidenceBundle。采集器不调用 LLM、不读取账户、不构造交易订单。

## 用法

```bash
uv run --no-editable finresearch collect-financials \
  NVDA.US \
  --report af \
  --segments \
  --region global \
  --output runs/nvda-financials-2026-09-01
```

参数：

- `--report af|saf|qf`：年报、半年报或季报口径，默认 `af`。完整三表不接受 `cumul`：资产负债表没有累计口径，实测会返回空表，不应与累计利润/现金流混合成一个包。
- `--segments`：另外采集与财报同频的历史业务分部。
- `--timeout`：每条上游命令的超时秒数，默认 20，范围 `(0, 120]`。
- `--region auto|cn|global`：Longbridge 接入区域。生产调度建议显式设置，避免自动检测选错端点。

symbol 必须带明确市场后缀，不会根据公司名称或裸 ticker 猜测市场。

## 完整性策略

采集器不依赖 `financial-statement --kind ALL`。在 Longbridge CLI 0.28.4 的真实验证中，`ALL` 可能以成功退出码返回空 `list`，而分别请求 `IS`、`BS`、`CF` 能返回数据。因此实现固定执行：

```text
financial-statement SYMBOL --kind IS --report REPORT --format json
financial-statement SYMBOL --kind BS --report REPORT --format json
financial-statement SYMBOL --kind CF --report REPORT --format json
```

显式启用分部时再执行：

```text
business-segments SYMBOL --history --report REPORT --format json
```

三表全空标记为 `no_data`；任意一表缺失，或显式请求的分部缺失，标记为 `partial_result`。存在重复期间/字段 ID、报告口径回显不匹配或响应结构错误时失败关闭。任一步失败都不发布半包。

## 输出

```text
output/
├── raw/
│   ├── financial-statement-is.json
│   ├── financial-statement-bs.json
│   ├── financial-statement-cf.json
│   └── business-segments.json      # 仅 --segments
├── evidence.json
└── collection.json
```

每张表对应一条 `financial_statement_snapshot`，业务分部对应一条 `business_segment_snapshot`。每条 provenance 只引用一个 raw sidecar，并记录 raw/normalized SHA-256、symbol、来源端点和 normalizer 版本。输出目录必须不存在；文件在私有 staging 中写入、校验和 `fsync`，然后用平台原生 no-replace 原语一次发布。

当前安全发布边界需要 macOS/Linux 的 directory-fd、`O_NOFOLLOW` 和原生 no-replace 能力；Windows 会失败关闭，不做不安全降级。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `2` | 参数、配置、symbol 或 report 输入错误 |
| `3` | Longbridge CLI 未安装或不可执行 |
| `4` | 上游命令超时 |
| `5` | 认证、网络、供应商、JSON 或协议/结构错误 |
| `6` | 三表全部无数据 |
| `7` | 任一张必需报表或显式请求的分部缺失 |
| `8` | 本地输出/发布失败 |

运行期上游错误在 stderr 中包含稳定的 `code=<name>` 与 `retryable=true|false`。参数错误由 argparse 输出子命令用法，不伪装成运行期错误。

## 标准化与 PIT

三表标准化字段包含货币、报告口径、财年/期间、期末日、报告日、字段 ID/名称/层级/顺序、数值、`yoy_ratio` 和类型；其中 `0.25` 表示 25%。分部标准化字段包含历史期间、业务/地区名称、数值、`percent` 与 `yoy_percent`；其中 `25` 表示 25%。两种上游口径相差 100 倍，因此标准化字段显式区分单位。未审查的新增上游字段仍保留在 raw 中，不会自动进入 Agent 上下文。

```text
published_at   = null
source_event_at = null
known_at       = 该条完整响应接收时间
retrieved_at   = 该条完整响应接收时间
available_at   = 全部已请求数据标准化完成时间
```

`ff_year/fp_end/rpt_date` 不能证明数据在那个时刻已对市场可得，因此不得用它们绕过 PIT 门禁。当前快照只能证明“系统在本次采集时已获得该数据”，不能回填历史首次披露时点。严格历史回测应改用 SEC accession/accepted time、交易所披露时间或数据供应商的历史可得时间。

Longbridge 财务数据是标准化二手数据，不等同于发行人或监管机构的法定原文。重要结论应与 10-K/10-Q、港交所公告或公司原始财报交叉核验。

本版本中“完整三表”表示 IS/BS/CF 三个端点均返回非空、结构可解析的响应，且报告口径、币种和最新财期一致；它不代表所有历史期均已对齐、三表勾稽关系已复算，也不代表数值已与法定原文核对。这些属于后续确定性财务校验层的职责。

## 安全边界

- 只允许固定的三表与可选分部命令，不接收任意 Longbridge 参数。
- 使用 argv 数组、`shell=False`、`stdin=DEVNULL`、独立进程组、超时与 stdout/stderr 上限。
- 子进程环境只保留运行所需白名单，不传递 DeepSeek API Key 或其他与采集无关的秘密。
- stderr 原文不落盘；manifest 仅记录来源、哈希和字节数。
- `evidence.json` 不得超过 `merge-evidence` 默认的 16 MiB 单包上限；采集器在返回或发布前都会失败关闭，避免产生无法进入合并链路的包。
- 研究策略层只授权 `read_financial_statements`，不授权账户或交易能力。

数据来源：长桥证券。
