# DeepSeek V4 Flash 冒烟实验

实验日期：2026-08-29

## 范围

使用官方 `deepseek-v4-flash` model id 和 `https://api.deepseek.com`，对本仓库的示例研究委托执行真实端到端调用。API Key 通过隐藏输入临时提供，未写入文件、日志或 Git。

工作流包含四次受控 JSON 调用：Claim 提取、Claim 核验、独立反证、投委会综合。模型无 tools、无网页访问、无交易能力。

## 结果

- 四个角色均返回可解析 JSON，并通过本地 Pydantic schema 校验。
- `known_at > as_of` 的未来证据在进入模型之前被剔除。
- 最终状态成功到达 `HUMAN_REVIEW_REQUIRED`。
- 产物包含报告、Claim、反证意见、证据快照和 7 个状态事件的审计轨迹。
- 仓库与生成产物的秘密扫描未发现 API Key 模式。

## 实验发现与修正

第一次真实返回中，模型改写了 Claim 的 `as_of`。这说明控制字段不能交给 LLM。代码随后改为：模型只输出 Claim 内容与判断，`as_of`、证据范围、状态合并、报告分类和人工复核标记全部由确定性代码注入。

独立反证角色也可能把“对事实含义的质疑”误表达成“对事实本身的争议”。因此反证意见现在单独保存在 `dissent`，不再覆盖核验 Agent 给出的 Claim 事实状态。

研究请求中的完整 evidence allowlist 可能暴露已被时点门禁剔除的 Evidence ID。代码已改为只向模型传递清理后的委托字段和 eligible evidence，避免未来证据的名称进入上下文。

初版综合报告的摘要、风险等是自由文本，无法机器确认是否来自已核验 Claim。它们现已改为 `GroundedStatement`，强制携带已存在的 `claim_ids`，并把推断前提显式写入 `assumptions`。

证据完整性现同时检查原文 hash 和覆盖全部元数据的 record hash。该机制能发现意外改写，但不能认证上游时间戳；严格 PIT 仍需要可信来源回执和不可变账本。

## 下一步评测

真实冒烟测试只证明接口兼容和安全路径可运行，不证明研究质量。下一阶段需要冻结的 as-of 数据集，分别评估：引用正确率、数值复算率、PIT 泄漏率、无依据推断率、拒答率，以及相对单 Agent 基线的增量。
