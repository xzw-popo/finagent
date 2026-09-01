# Fin Research Agent

一个“证据优先、显式时点门禁、默认拒绝自动交易”的金融投研多 Agent 项目。

当前产品里程碑是可运行的 V1.3（Python 包版本 `0.4.0`）：四个 LLM 角色分别负责提取、核验、反证与综合，确定性代码负责只读行情与财报采集、证据合并、时点过滤、引用检查、状态流转和最终人工复核。V1.3 新增 Longbridge 完整三表与可选业务分部证据采集。系统不是自由聊天式 swarm，也不会生成或提交订单。

## 为什么这样设计

金融研究的难点不是让多个角色聊得更热闹，而是让每条重要结论都能回答：来源是什么、市场在当时是否已经知道、模型用了什么输入、冲突是否被保留、谁批准了最终输出。

```text
研究委托        Longbridge / 财报 / 知识库证据包
  │                         ↓
  │             merge-evidence（完整性校验、重打包）
  │                         ↓
  └───────────────→ 自包含 EvidenceBundle
  ↓
PIT 证据过滤（available_at/known_at ≤ as_of）
  ↓
证据分析 Agent → 核验 Agent → 反证 Agent → 综合 Agent
  ↓                 ↑
引用/状态/权限等确定性门禁
  ↓
HUMAN_REVIEW_REQUIRED
```

详细方案见 [架构说明](docs/architecture.md)，现有开源项目调研见 [项目参考](docs/open-source-landscape.md)。

## 快速开始

要求 Python 3.11+，推荐使用 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --dev --no-editable
uv run --no-editable finresearch validate \
  --request examples/request.json \
  --evidence examples/evidence.json

uv run --no-editable finresearch run \
  --mode mock \
  --request examples/request.json \
  --evidence examples/evidence.json \
  --output runs/mock-demo
```

Mock 模式不访问网络、不需要 API Key，并会生成结构化研究报告与 JSONL 审计轨迹。

## Longbridge 行情证据

安装 Longbridge CLI（环境要求时完成 OAuth）后，可以把一个或多个行情快照转换成现有工作流可直接读取的 EvidenceBundle。安装与授权步骤见 [Longbridge 官方文档](https://open.longbridge.com/skill/install.md)。

```bash
uv run --no-editable finresearch collect-quote \
  NVDA.US 700.HK \
  --output runs/quotes-2026-09-01
```

采集器内部只允许固定的 `longbridge quote ... --format json`，不接受任意 Longbridge 子命令。输出目录包含原始响应、标准化证据和采集 manifest；默认拒绝覆盖已有目录。普通 quote 响应没有覆盖整个快照的统一交易所时间戳，因此系统不会伪造 `published_at`，而是单独记录响应接收时间和标准化完成后的 `available_at`。详见 [Longbridge 行情接入](docs/longbridge-market-data.md)。

将采集得到的 Evidence ID 写入研究委托的 `allowed_evidence_ids`，并确保 `as_of >= available_at` 后，就可以执行研究工作流：

```bash
uv run --no-editable finresearch run \
  --mode mock \
  --request path/to/market-request.json \
  --evidence runs/quotes-2026-09-01/evidence.json \
  --output runs/market-research-2026-09-01
```

工作流会重新验证原始响应哈希，并把引用的 sidecar 复制到运行目录，因此输出的 `eligible_evidence.json` 可以脱离原采集目录独立重载和校验完整性。

## Longbridge 财报证据

`collect-financials` 对单只证券分别执行固定的利润表、资产负债表和现金流量表命令；任一张表缺失都不会发布半成品。`--segments` 可选采集同频历史业务分部：

```bash
uv run --no-editable finresearch collect-financials \
  NVDA.US \
  --report af \
  --segments \
  --region global \
  --output runs/nvda-financials-2026-09-01
```

输出包含每条上游响应的原始 JSON、三条（启用分部时四条）标准化 Evidence 和采集 manifest。财年期末、`rpt_date` 等字段只是业务日期，不是可验证的对外披露时间；本版本保守地记录响应接收和整批标准化完成时间，作为后续 `validate/run` 执行 PIT 门禁的依据。详见 [Longbridge 财报接入](docs/longbridge-financials.md)。

## 合并多个证据包

行情、财报和知识库证据不应手工拼接 JSON。V1.2 用显式合并阶段先对每个输入做结构与哈希完整性校验，再生成一个自包含证据包：

```bash
uv run --no-editable finresearch merge-evidence \
  --evidence runs/quotes-2026-09-01/evidence.json \
  --evidence data/filings/evidence.json \
  --output runs/combined-evidence
```

输出中的 sidecar 使用 `artifacts/sha256/<raw_sha256>` 内容寻址：同内容只保存一份，同名不同内容不会覆盖。`merge.json` 记录输入包哈希和记录重写前后的哈希链，但不保存本机绝对路径。合并不做 PIT 过滤，也不改写时间字段；`validate/run` 仍在最后依据请求的 `as_of` 和 allowlist 执行门禁。详见 [证据包合并](docs/evidence-merge.md)。

这些本地哈希只能证明“重打包前后的字节是否一致”，不能证明发行方、时间戳、数据授权或内容本身真实。

`merge-evidence` 当前需要 macOS/Linux 提供的 POSIX directory-fd、`O_NOFOLLOW` 与原生 no-replace 发布能力；Windows 会失败关闭，不做不安全降级。

## DeepSeek V4 Flash 实验

项目使用 DeepSeek 官方 OpenAI-compatible Chat Completions 接口：

- model id：`deepseek-v4-flash`
- base URL：`https://api.deepseek.com`
- 默认 thinking effort：`low`
- 输出：JSON mode，再由本地 Pydantic 严格校验
- tools：V1 明确禁用

推荐用隐藏输入提供密钥，避免写入文件或 shell 历史：

```bash
uv run --no-editable finresearch run \
  --mode deepseek \
  --ask-key \
  --request examples/request.json \
  --evidence examples/evidence.json \
  --output runs/deepseek-demo
```

也可以使用环境变量 `DEEPSEEK_API_KEY`。真实密钥不得写入 `.env.example`、代码、测试、日志或 Git；详见 [安全说明](SECURITY.md)。

官方参考：[模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[更新日志](https://api-docs.deepseek.com/updates/)。

## 输出内容

每次运行会在指定目录写入：

- `request.json`：研究委托
- `eligible_evidence.json`：时点与白名单过滤后的 EvidenceBundle
- 行情/财报证据引用的原始 sidecar：保留原相对路径并再次核对 SHA-256
- `rejected_evidence.json`：被拒绝证据及原因
- `claims.json`：核验后的 Claim
- `challenge.json`：反证、风险和缺失信息
- `report.json`：最终结构化报告；结论、风险和监控项均关联已核验 Claim ID
- `events.jsonl`：状态、时间、版本和输入哈希

报告始终带有 `human_review_required: true` 和 `narrative_requires_human_verification: true`，且终态固定为 `HUMAN_REVIEW_REQUIRED`。所有 LLM 生成的叙述字段均标记为 `UNVERIFIED_NARRATIVE`；Claim ID 绑定不是语义真实性证明，必须逐句人工复核。

V1.3 会校验原文 hash、整条证据记录 hash、行情/财报原始 sidecar hash，以及 `available_at <= as_of`（旧证据回退到 `known_at <= as_of`），但本地 hash 不能证明上游提供的时间戳真实。生产级 PIT 仍需交易所 accession/accepted time、宏观数据 vintage 或可信数据供应商回执，并写入不可变账本。

## 开发

```bash
uv run --no-editable pytest
uv run --no-editable ruff check .
```

CI 只运行 mock 与单元测试，不读取或调用任何真实 API Key。

## 边界

这是研究工程原型，不构成投资建议。V1.3 只包含本地证据处理、人工触发的一次性只读行情/财报快照和受控的证据包合并，不包含持续推送、自动网页抓取、回测、组合优化、账户读取或订单执行。任何未来交易能力都必须作为独立系统，经确定性风控与人工授权后才能接入。

## 许可

仓库当前公开可查看，但尚未授予开源许可证；保留全部权利。若未来开放复制、修改和分发，应先完成依赖与数据许可审查，再明确选择并加入 LICENSE。
