# Fin Research Agent

一个“证据优先、基于时间元数据的 PIT 门禁、全链路可审计、交易能力默认关闭”的金融投研多 Agent 工程原型。

> 当前里程碑：V1.3，Python 包版本 `0.4.0`。项目可以运行完整的证据采集、合并、PIT 过滤、多角色研究和人工复核交接流程，但不会生成或提交交易订单。

## 项目定位

Fin Research Agent 不是让多个 Agent 无约束聊天的 swarm。它把金融研究拆成可检查的确定性环节和受控的 LLM 角色，使每条重要结论都能回答：

- 证据来自哪里，原始数据是否仍可复核；
- 基于输入的时间元数据，系统在研究截止时点是否已拥有可用证据；
- 模型使用了哪些 Evidence 和 Claim；
- 哪些结论被核验、驳回或标记为证据不足；
- 哪些冲突、假设和风险仍需要人工判断。

### 当前能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 结构化研究委托 | 已实现 | 校验 `universe`、`as_of`、`horizon` 和 Evidence 白名单 |
| PIT 时点门禁 | 已实现 | 进模型前执行 `(available_at or known_at) <= as_of` |
| Longbridge 行情快照 | 已实现 | 只读固定命令，保留 raw JSON、哈希和 provenance |
| Longbridge 完整三表 | 已实现 | IS / BS / CF，可选历史业务分部 |
| 多证据包合并 | 已实现 | 内容寻址 sidecar、哈希谱系、原子 no-replace 发布 |
| 提取 / 核验 / 反证 / 综合 | 已实现 | 四个逻辑 Agent，使用严格 Pydantic 输入输出 |
| Mock 与 DeepSeek 适配器 | 已实现 | Mock 用于离线测试；DeepSeek 使用 JSON mode |
| 人工复核交接 | 强制 | 每次运行终态固定为 `HUMAN_REVIEW_REQUIRED`，尚无审批签收系统 |
| 账户读取与交易下单 | 不支持 | 研究与交易隔离，LLM 不持有券商交易权限 |

## 架构

### 当前可运行架构

```text
研究委托 ResearchRequest
            │
            ├── Longbridge 行情 / 财报采集
            ├── 预先构建的文档 / 知识库 EvidenceBundle
            │
            ▼
  Evidence 结构、时间线与哈希校验
            │
            ├── merge-evidence（可选）
            │
            ▼
  PIT 过滤 + Evidence ID 白名单
            │
            ▼
  证据提取 Agent
            │
            ▼
  Claim 核验 Agent
            │
            ▼
  独立反证 Agent
            │
            ▼
  投委会综合 Agent
            │
            ▼
  引用 / 状态 / 权限确定性门禁
            │
            ▼
  HUMAN_REVIEW_REQUIRED
```

V1.3 中的“多 Agent”是不同的职责、提示词、输入契约和输出 schema，由单一有限状态机编排，而不是多个不受控的微服务。仓库当前可以消费符合 schema 的文档或知识库 EvidenceBundle，但尚未内置公告抓取器或通用知识库连接器。

### 目标架构

```text
Research Supervisor
        │
        ├── PIT Data Steward ──→ Evidence Ledger
        ├── Policy / Budget Gate
        └── 动态专家池
                ├── 基本面 / 行业 / 宏观 / 事件
                ├── 确定性估值与量化服务
                └── Bull / Bear / Forensic Challenger
                              │
                              ▼
                       IC Synthesizer
                              │
                              ▼
               Citation / Numeric / PIT / Compliance Gates
                              │
                              ▼
                         Human Approval
```

目标架构会继续保留单一控制平面和确定性门禁；估值、统计检验、组合权重和风险限额不交给 LLM 自由计算。详细设计见 [架构说明](docs/architecture.md)。

## 核心概念

### Evidence 不是 Agent 记忆

Evidence 是带来源、定位、时间、内容哈希和记录哈希的事实输入。Agent 只能引用当前证据包中存在的 Evidence ID，不能把对话记忆当作事实源。

### PIT 是什么

PIT 是 Point-in-Time。当前实现在信任输入时间元数据的前提下，判断系统在指定截止时点是否已拥有并处理完成该证据。进入模型前，系统强制执行：

```text
(evidence.available_at or evidence.known_at) <= request.as_of
```

财年期末、报告日或内容所属日期并不等于市场已经获得该信息的时间。这个区分用于防止历史研究和回测中的未来函数。

### Claim 与叙述

LLM 首先从 Evidence 中提取原子 Claim，再由独立角色标记为 `verified`、`disputed` 或 `insufficient`。使用 `GroundedStatement` 的摘要、主论点、异议、风险、监控项和失效条件必须关联合格 Claim，并统一标记为 `UNVERIFIED_NARRATIVE`。Claim 文本、核验意见、缺失信息等其他模型字段仍需人工检查，但不会被错误描述为都带有该字段标记。

## 快速开始

### 环境要求

- Python 3.11+；
- [uv](https://docs.astral.sh/uv/)；
- macOS 或 Linux：`collect-financials` 和 `merge-evidence` 的强化安全发布依赖 POSIX directory-fd、`O_NOFOLLOW` 和原生 no-replace，这两个命令在 Windows 当前会失败关闭；
- Longbridge CLI：仅在采集真实行情或财报时需要，当前实测版本为 0.28.4。

### 安装

```bash
git clone https://github.com/xzw-popo/finagent.git
cd finagent
uv sync --dev --frozen --no-editable
uv run --no-editable finresearch --version
```

### 不联网跑通示例

```bash
uv run --no-editable finresearch validate --request examples/request.json --evidence examples/evidence.json
uv run --no-editable finresearch run --mode mock --request examples/request.json --evidence examples/evidence.json --output runs/mock-demo
```

示例校验应输出 `valid: 2 eligible, 1 rejected by policy`。Mock 模式不访问网络，不需要 API Key；研究报告位于 `runs/mock-demo/report.json`。

所有发布命令都要求 `--output` 指向一个尚不存在的新路径；重复运行时请更换目录名。

## CLI 命令

| 命令 | 用途 |
| --- | --- |
| `finresearch validate` | 校验研究委托、Evidence 结构、哈希、白名单和 PIT 门禁 |
| `finresearch collect-quote` | 采集一个或多个 Longbridge 只读行情快照 |
| `finresearch collect-financials` | 采集单只证券的 IS / BS / CF 和可选业务分部 |
| `finresearch merge-evidence` | 将两个或更多 EvidenceBundle 合并为自包含证据包 |
| `finresearch run` | 以 Mock 或 DeepSeek 执行受控多 Agent 工作流 |

查看任一命令的完整参数：

```bash
uv run --no-editable finresearch collect-financials --help
```

## 接入 Longbridge 真实数据

先按 [Longbridge 官方安装与授权文档](https://open.longbridge.com/skill/install.md) 安装 CLI 并完成 OAuth，然后确认：

```bash
longbridge --version
```

Longbridge 凭据由外部 CLI 管理，本项目不会把凭据复制进运行产物。

### 1. 采集行情

```bash
uv run --no-editable finresearch collect-quote NVDA.US 700.HK --region global --output runs/quotes-2026-09-01
```

symbol 必须带明确市场后缀，例如 `NVDA.US`、`700.HK` 或 `600519.SH`。采集器只执行固定只读 quote 命令。账户权限可能使行情为实时、延迟或其他等级；无法机器确认时，`freshness` 保守标记为 `unknown`。

### 2. 采集财报

```bash
uv run --no-editable finresearch collect-financials NVDA.US --report af --segments --region global --output runs/nvda-financials-2026-09-01
```

`--report` 支持 `af`（年报）、`saf`（半年报）和 `qf`（季报）。完整三表不支持 `cumul`，因为资产负债表没有可与累计利润表和现金流量表直接组包的累计口径。

采集器分别请求 IS、BS 和 CF，要求三表非空、币种一致且最新财期对齐，并在发布前从 raw JSON 重放标准化。“完整三表”不代表所有历史期已对齐、会计勾稽关系已重算或已与法定原文核对。

**数据来源：长桥证券。**

### 3. 合并证据包

```bash
uv run --no-editable finresearch merge-evidence --evidence runs/quotes-2026-09-01/evidence.json --evidence runs/nvda-financials-2026-09-01/evidence.json --output runs/nvda-combined-2026-09-01
```

合并器会校验 schema、`content_sha256`、`record_sha256` 和 raw sidecar 哈希。sidecar 被重定位到 `artifacts/sha256/<raw_sha256>`，`merge.json` 保存记录哈希转换谱系。成功输出中的 `minimum_as_of_for_all_evidence` 是让全部证据通过 PIT 的最早请求时点。

合并本身不执行 PIT 过滤，也不会悄然删除未来时点的证据。

### 4. 创建研究委托

把待用 Evidence ID 写入非空的 `allowed_evidence_ids`，并确保带时区的 `as_of` 不早于证据可用时间：

```json
{
  "request_id": "nvda-2026-09-01",
  "question": "基于截止时点可得证据，评估 NVDA 的增长质量与主要风险。",
  "universe": ["NVDA.US"],
  "as_of": "2026-09-01T23:59:59+08:00",
  "horizon": "12 months",
  "allowed_evidence_ids": [
    "<evidence-id-1>",
    "<evidence-id-2>"
  ]
}
```

### 5. 校验并运行

```bash
uv run --no-editable finresearch validate --request path/to/request.json --evidence runs/nvda-combined-2026-09-01/evidence.json
uv run --no-editable finresearch run --mode mock --request path/to/request.json --evidence runs/nvda-combined-2026-09-01/evidence.json --output runs/nvda-research-2026-09-01
```

## DeepSeek 模式

项目通过 OpenAI-compatible Chat Completions 适配器调用 `deepseek-v4-flash` 滚动别名，使用 JSON mode，并对返回结果再次执行本地 Pydantic 严格校验。V1.3 不向模型开放 tools。

推荐使用隐藏输入，避免密钥进入 shell 历史：

```bash
uv run --no-editable finresearch run --mode deepseek --ask-key --request examples/request.json --evidence examples/evidence.json --output runs/deepseek-demo
```

也可以在运行环境中设置 `DEEPSEEK_API_KEY`。不要把真实 Key 写入代码、测试、日志、Issue 或 Git 历史；如果凭据曾被粘贴到对话或命令行，应立即轮换。

DeepSeek 模式会把研究委托、通过 PIT 的 Evidence 和各角色所需的中间材料发送给模型服务商。只有在数据许可、保密要求和组织政策允许时才应启用；敏感或受限数据优先使用 Mock 或未来的私有部署适配器。

## 证据与运行产物

### Evidence 类型

| `evidence_type` | 用途 |
| --- | --- |
| `document` | 公告、研报、知识库摘录等预先构建的文档证据 |
| `market_quote_snapshot` | 只读市场行情快照 |
| `financial_statement_snapshot` | 利润表、资产负债表或现金流量表快照 |
| `business_segment_snapshot` | 历史业务或地区分部快照 |

快照 Evidence 额外包含 provider、symbol、来源端点、raw / normalized SHA-256 和 normalizer version。在本地信任假设下，这些哈希用于校验字节一致性和发现意外修改；能够同时修改内容与重算哈希的操作者不在该保证范围内。本地哈希也不能独立证明上游发布者、时间戳、授权或内容本身真实。

### `run` 输出

每次工作流运行会生成：

- `request.json`：研究委托快照；
- `eligible_evidence.json`：通过白名单与 PIT 门禁的证据；
- `rejected_evidence.json`：被拒绝证据及原因；
- 引用的 raw sidecar：复制到运行目录并重新校验 SHA-256；
- `claims.json`：核验后的 Claim；
- `challenge.json`：反证、风险和缺失信息；
- `report.json`：最终结构化研究报告，包含 provider、model 和 prompt version；
- `events.jsonl`：状态、发生时间、输入哈希和阶段详情审计轨迹。

`report.json` 始终包含：

```json
{
  "stage": "HUMAN_REVIEW_REQUIRED",
  "human_review_required": true,
  "narrative_requires_human_verification": true
}
```

这表示系统已完成研究流水线并等待外部人工复核，不表示仓库已经实现审批、签名或发布授权。

## 数据与 PIT 边界

- 普通 quote 响应没有覆盖整个快照的统一交易所事件时间，因此系统不会伪造 `published_at`；
- 财报中的财年、期末日与 `rpt_date` 是业务日期，不是可验证的首次对外披露时间；
- 当前快照只能证明“本系统在采集时已获得该数据”，不能倒推历史首次可得时间；
- 严格历史回测应接入 SEC accession / accepted time、交易所披露时间、宏观 vintage 或可信供应商 delivery receipt；
- Longbridge 财务数据是标准化二手数据，重要结论应与 10-K / 10-Q、港交所公告或发行人原始财报交叉核验。

## 安全边界

- Longbridge 采集仅允许固定只读命令，使用 argv 数组和 `shell=False`；
- symbol、report、region、超时和输出大小都有确定性边界；
- 子进程环境变量经过白名单过滤，不传递 DeepSeek Key 等无关秘密；
- `collect-financials` 和 `merge-evidence` 在私有 staging 中校验和 `fsync`，再使用原生 no-replace 原语发布；`collect-quote` 和 `run` 使用临时目录与独占创建目标，尚未提供同等的 `fsync` / 原生 no-replace 保证；
- 既有输出路径、symlink、哈希不匹配、不完整财报或无法安全发布的平台都会失败关闭；
- LLM 不能调用工具、访问券商凭据或跳过人工复核状态；
- `runs/` 已被 Git 忽略，但采集与研究产物仍可能包含敏感或受许可数据，不应提交到仓库。

更多说明见 [安全策略](SECURITY.md)。

## 项目结构

```text
.
├── examples/                     # 可直接运行的研究委托与证据
├── docs/                         # 架构、数据接入与调研文档
├── src/finresearch/
│   ├── fundamentals/             # Longbridge 财报与业务分部
│   ├── marketdata/               # Longbridge 行情
│   ├── llm/                      # Mock / DeepSeek 适配器
│   ├── evidence.py               # Evidence 加载、哈希与 PIT
│   ├── evidence_merge.py         # 受控证据合并
│   ├── schemas.py                # Pydantic 契约
│   ├── policy.py                 # 能力白名单
│   └── workflow.py               # 有限状态研究工作流
├── tests/                        # 单元、安全边界与端到端测试
├── SECURITY.md
└── pyproject.toml
```

## 开发与测试

```bash
uv run --no-editable pytest -q
uv run --no-editable ruff check .
uv run --no-editable python -m compileall -q src tests
```

测试只使用 Mock 或本地测试桩，不读取或调用真实 API Key。

## 路线图

- **V1.0**：本地 EvidenceBundle、受控角色、结构化报告与人工复核状态；
- **V1.1**：Longbridge 只读行情快照与显式 `available_at`；
- **V1.2**：多证据包合并、内容寻址 sidecar、哈希谱系与原子发布；
- **V1.3**：Longbridge 完整三表、可选历史业务分部与保守 PIT 时间语义；
- **V2**：SEC / 交易所法定原文、双时间证据账本、三表勾稽、确定性估值服务和内部评测集；
- **V3**：并行专家子图、持久化 checkpoint、回放、观测和量化沙箱；
- **V4**：影子组合与持续监控；只有完成独立模型验证与合规审批后，才讨论隔离交易网关。

## 文档

- [整体架构](docs/architecture.md)
- [开源项目调研](docs/open-source-landscape.md)
- [Longbridge 行情接入](docs/longbridge-market-data.md)
- [Longbridge 财报接入](docs/longbridge-financials.md)
- [证据包合并](docs/evidence-merge.md)
- [DeepSeek 冒烟测试](docs/deepseek-smoke-test.md)
- [安全策略](SECURITY.md)

## 声明与许可

本项目是金融研究工程原型，不构成投资建议、要约、交易指令或任何收益保证。所有模型输出必须由具备相应资质和责任的人员复核。

仓库当前公开可查看，但尚未授予开源许可证，默认保留全部权利。在加入 LICENSE 前，请勿假设可以复制、修改或重新分发。
