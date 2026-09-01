# 架构说明

## 目标架构

长期形态采用“单一控制平面 + 动态专家池 + 确定性计算服务 + 证据账本 + 独立挑战 + 人类投委会”，而不是无约束的 Agent 群聊。

```text
Research Mandate
       │
       ▼
Research Supervisor ───────────────┐
       │                           │
       ▼                           ▼
PIT Data Steward             Policy / Budget Gate
       │
       ▼
Evidence Ledger
       │
       ├── Fundamental / Industry / Macro / Event Agents
       ├── Deterministic Valuation & Quant Services
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

关键原则：

1. 原始证据与带 `available_at/known_at` 的证据账本是事实源，Agent 记忆不是。
2. Agent 之间传递 Pydantic 对象和 Evidence ID，不传递无边界自由文本。
3. 多空观点先独立产生，再围绕关键假设挑战；不采用多数投票。
4. 估值、统计检验、组合权重与风险限制由确定性代码执行。
5. 未解决分歧必须进入报告，系统可以输出“证据不足”。
6. 研究与交易隔离。LLM 永远不持有券商密钥，也不能直接下单。

## V1：显式有限状态机

第一版刻意保持单进程和有限状态，便于审计与测试：

```text
VALIDATE_REQUEST
→ LOAD_AND_FILTER_EVIDENCE
→ EXTRACT_CLAIMS
→ VERIFY_CLAIMS
→ CHALLENGE_CLAIMS
→ SYNTHESIZE_REPORT
→ HUMAN_REVIEW_REQUIRED
```

每个状态只能进入白名单中的下一状态。任何跳过时点过滤、Claim 核验或人工复核的跳转都会失败关闭。V1 中“多 Agent”是不同提示词、输入契约和输出 schema 的逻辑角色，而不是微服务。

## V1.1：只读行情采集边界

Longbridge 通过一个位于 LLM 工作流之前的窄适配层接入：

```text
操作员 / 调度器
       │
       ▼
LongbridgeQuoteCollector
       │  固定命令：quote --format json
       ▼
原始响应 + 标准化行情快照 + Provenance
       │
       ▼
EvidenceBundle → PIT / Claim 工作流 → 自包含运行证据包
```

采集器使用参数数组和 `shell=False`，严格校验 `<CODE>.<MARKET>`，移除与采集无关的秘密环境变量，并在临时工作目录执行 CLI。LLM 既不能生成命令，也不能访问 Longbridge 凭据；策略层只授权 `read_market_quote`，订单、账户和通用子进程能力仍被拒绝。

每次采集保存三份材料：

- `raw-response.json`：供应商原始 stdout 字节；单独计算 SHA-256。
- `evidence.json`：只包含已审查字段的标准化 EvidenceBundle。
- `collection.json`：CLI 版本、请求/接收/可用时间、原始哈希和 warning 的来源/哈希/字节数，不保存成功命令的 stderr 原文。

证据加载与研究运行都会校验 `raw_artifact_ref` 的路径边界和 `raw_sha256`。进入运行目录时，原始 sidecar 会按原相对路径安全复制；路径逃逸、哈希不匹配或与报告产物冲突都会失败关闭。

当前 CLI 的组合 quote 快照没有统一的 regular-session 来源事件时间。系统不会用本机时间冒充交易所发布时间：行情 Evidence 的 `published_at` 为 `null`，`known_at` 表示完整响应到达，`available_at` 表示标准化完成。账户行情权限可能导致实时、延迟或其他数据等级，因此在无法机器确认时 `freshness` 固定为 `unknown`。

## V1.2：受控证据合并

多数据源不直接进入 Agent，而是先经过确定性重打包：

```text
Longbridge 快照 ─┐
财报 / 公告      ├→ 独立完整性校验 → merge-evidence → 自包含 EvidenceBundle
知识库摘录     ─┘                                      │
                                                    └→ PIT / Claim 工作流
```

合并器对每个输入的 schema、`content_sha256`、`record_sha256`、sidecar 路径和 `raw_sha256` 分别校验；任何重复 `evidence_id` 都失败关闭。原始 sidecar 被重定位到 `artifacts/sha256/<raw_sha256>`，并重算包含路径的记录哈希。`merge.json` 保存输入包哈希和记录哈希的前后对应，不保存输入绝对路径或原始文件名。

合并只处理结构、完整性和物化，不接收 `ResearchRequest`，不删除任何未来时点证据，也不更改 `known_at/available_at`。PIT 仍只在 `validate/run` 中结合 `request.as_of` 执行，从而避免数据准备阶段造成不可见的历史删除。

输出必须是尚不存在的路径。实现先固定输出父目录，staging 创建、文件写入、发布、回滚与父目录 `fsync` 均使用该 directory fd。私有 staging 内完成有界读取、内容寻址复制、闭环校验和 `fsync` 后，再通过平台原生 no-replace 原语一次发布，并在提交后重新闭环校验。已有文件、目录、有效 symlink 和悬空 symlink 均不会被跟随或覆盖；缺少 `O_NOFOLLOW`/directory-fd 或安全 no-replace 原语时失败关闭，不做不安全降级。

## V1.3：只读财报证据采集

Longbridge 财报适配层位于 LLM 之前，对单个明确市场后缀的 symbol 固定执行 `financial-statement` 的 `IS`、`BS`、`CF` 三条命令。当前 CLI 的 `ALL` 在实测中可能成功返回空数组，因此不作为完整性依据。完整三表只支持 `af/saf/qf`，不将资产负债表缺失的 `cumul` 与其他两表混成一包。任一张表为空、结构错误、币种不一致、最新财期不对齐或请求失败时，整批失败且不发布输出。

每张表生成独立的 `financial_statement_snapshot` Evidence 和独立 raw sidecar，使 provenance 中的哈希只证明一个上游响应。显式启用业务分部时，固定执行同频历史 `business-segments` 并另建 `business_segment_snapshot`。报表的 `yoy_ratio` 与分部的 `yoy_percent` 显式区分百分比单位。发布前会从每个 raw JSON 重放标准化、重建 Evidence，并限制 `evidence.json` 不超过默认 16 MiB 合并入口。全部校验通过后，才在私有 staging 中通过 no-replace 原语一次发布。

财年、报告期、`fp_end` 和 `rpt_date` 均保留在标准化内容中，但不映射为 `published_at/source_event_at`：当前命令没有提供可验证的披露 accepted timestamp。每条证据的 `known_at/retrieved_at` 是该响应完整到达时间，整批 Evidence 共享全部请求解析完成后的 `available_at`。同一财期后来被重述时，新 raw/normalized hash 和 Evidence ID 形成新快照，不覆盖旧证据。

## 证据与 Claim

`Evidence` 至少包含：

- `evidence_id`
- `publisher`、`uri`、`locator`
- 原文 `excerpt` 与 `content_sha256`
- 覆盖来源、定位、正文和全部时间字段的 `record_sha256`
- `published_at`、`known_at`、`retrieved_at`，以及可选的 `available_at`
- 行情/财报快照证据额外包含 provider、symbol、来源端点、原始/标准化 hash 和 normalizer version

`Claim` 至少包含：

- `claim_id`、`kind`、`text`
- `evidence_ids`
- `as_of`、`confidence`
- `status`、`verifier_notes`
- 预测使用的 `assumptions`

进入模型前必须满足 `(available_at or known_at) <= request.as_of`。事实与估计必须引用当前证据包中存在的 Evidence ID；模型生成的未知 ID 会被确定性门禁拒绝。

`record_sha256` 能发现元数据的意外修改，但不能证明上游时间戳没有被伪造后重新计算 hash。V1 的 PIT 门禁建立在输入元数据可信的前提上；生产环境必须保存 SEC/交易所回执、宏观 vintage 或供应商 delivery receipt，并通过 append-only/WORM 存储或签名账本固化。

报告中的摘要、主论点、异议、风险、监控项和失效条件都使用 `GroundedStatement(text, claim_ids, assumptions)`。摘要、主论点、风险、监控项和失效条件只能引用 `verified` Claim；确定性门禁会拒绝未知或状态不合格的 Claim ID。所有这些叙述仍标记为 `UNVERIFIED_NARRATIVE`，因为句法引用不能证明语义支持，必须独立评测并逐句人工复核。

## DeepSeek 适配层

V1 使用 `deepseek-v4-flash` 的 Chat Completions JSON mode，并在本地再次通过 Pydantic 校验。模型不能调用工具；失败仅有限重试，仍不合法则不生成报告。

`deepseek-v4-flash` 是滚动别名。为便于复现，运行产物记录 provider、model、prompt version 和输入哈希；后续还应记录响应中的实际 model、system fingerprint、token usage 与完整参数。

## 演进路线

- V1：本地证据包、受控角色、结构化报告、人工复核。
- V1.1：Longbridge 只读行情快照、市场证据 provenance 与显式 `available_at`。
- V1.2：多 EvidenceBundle 完整性校验、内容寻址 sidecar、哈希谱系与原子发布。
- V1.3：Longbridge 完整三表、可选历史业务分部、财报快照 provenance 与保守 PIT 时间语义。
- V2：不可变原始文档库、双时间数据模型、持久化 checkpoint、内部评测集。
- V3：LangGraph 控制平面、并行专家子图、确定性估值/量化沙箱、观测与回放。
- V4：影子组合与持续监控；完成独立模型验证与合规审批后，才讨论隔离交易网关。
