# Fin Research Agent

一个“证据优先、显式时点门禁、默认拒绝自动交易”的金融投研多 Agent 项目。

当前版本是可运行的 V1 骨架：四个 LLM 角色分别负责提取、核验、反证与综合，确定性代码负责时点过滤、证据引用检查、状态流转和最终人工复核门禁。它不是自由聊天式 swarm，也不会连接券商或生成订单。

## 为什么这样设计

金融研究的难点不是让多个角色聊得更热闹，而是让每条重要结论都能回答：来源是什么、市场在当时是否已经知道、模型用了什么输入、冲突是否被保留、谁批准了最终输出。

```text
研究委托
  ↓
PIT 证据过滤（known_at ≤ as_of）
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
- `eligible_evidence.json`：时点与白名单过滤后的证据
- `rejected_evidence.json`：被拒绝证据及原因
- `claims.json`：核验后的 Claim
- `challenge.json`：反证、风险和缺失信息
- `report.json`：最终结构化报告；结论、风险和监控项均关联已核验 Claim ID
- `events.jsonl`：状态、时间、版本和输入哈希

报告始终带有 `human_review_required: true` 和 `narrative_requires_human_verification: true`，且终态固定为 `HUMAN_REVIEW_REQUIRED`。所有 LLM 生成的叙述字段均标记为 `UNVERIFIED_NARRATIVE`；Claim ID 绑定不是语义真实性证明，必须逐句人工复核。

V1 会校验原文 hash、整条证据记录 hash 和 `known_at <= as_of`，但本地 hash 不能证明上游提供的时间戳真实。生产级 PIT 仍需交易所 accession/accepted time、宏观数据 vintage 或可信数据供应商回执，并写入不可变账本。

## 开发

```bash
uv run --no-editable pytest
uv run --no-editable ruff check .
```

CI 只运行 mock 与单元测试，不读取或调用任何真实 API Key。

## 边界

这是研究工程原型，不构成投资建议。V1 不包含实时行情、自动网页抓取、回测、组合优化、券商连接或订单执行。任何未来交易能力都必须作为独立系统，经确定性风控与人工授权后才能接入。

## 许可

仓库当前公开可查看，但尚未授予开源许可证；保留全部权利。若未来开放复制、修改和分发，应先完成依赖与数据许可审查，再明确选择并加入 LICENSE。
