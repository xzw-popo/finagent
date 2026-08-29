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

1. 原始证据与带 `known_at` 的证据账本是事实源，Agent 记忆不是。
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

## 证据与 Claim

`Evidence` 至少包含：

- `evidence_id`
- `publisher`、`uri`、`locator`
- 原文 `excerpt` 与 `content_sha256`
- 覆盖来源、定位、正文和全部时间字段的 `record_sha256`
- `published_at`、`known_at`、`retrieved_at`

`Claim` 至少包含：

- `claim_id`、`kind`、`text`
- `evidence_ids`
- `as_of`、`confidence`
- `status`、`verifier_notes`
- 预测使用的 `assumptions`

进入模型前必须满足 `known_at <= request.as_of`。事实与估计必须引用当前证据包中存在的 Evidence ID；模型生成的未知 ID 会被确定性门禁拒绝。

`record_sha256` 能发现元数据的意外修改，但不能证明上游时间戳没有被伪造后重新计算 hash。V1 的 PIT 门禁建立在输入元数据可信的前提上；生产环境必须保存 SEC/交易所回执、宏观 vintage 或供应商 delivery receipt，并通过 append-only/WORM 存储或签名账本固化。

报告中的摘要、主论点、异议、风险、监控项和失效条件都使用 `GroundedStatement(text, claim_ids, assumptions)`。摘要、主论点、风险、监控项和失效条件只能引用 `verified` Claim；确定性门禁会拒绝未知或状态不合格的 Claim ID。所有这些叙述仍标记为 `UNVERIFIED_NARRATIVE`，因为句法引用不能证明语义支持，必须独立评测并逐句人工复核。

## DeepSeek 适配层

V1 使用 `deepseek-v4-flash` 的 Chat Completions JSON mode，并在本地再次通过 Pydantic 校验。模型不能调用工具；失败仅有限重试，仍不合法则不生成报告。

`deepseek-v4-flash` 是滚动别名。为便于复现，运行产物记录 provider、model、prompt version 和输入哈希；后续还应记录响应中的实际 model、system fingerprint、token usage 与完整参数。

## 演进路线

- V1：本地证据包、受控角色、结构化报告、人工复核。
- V2：不可变原始文档库、双时间数据模型、持久化 checkpoint、内部评测集。
- V3：LangGraph 控制平面、并行专家子图、确定性估值/量化沙箱、观测与回放。
- V4：影子组合与持续监控；完成独立模型验证与合规审批后，才讨论隔离交易网关。
