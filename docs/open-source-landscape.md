# 可借鉴的开源项目

不建议直接 fork 某一个项目作为全部底座。更合理的做法是按能力分层吸收：数据层、研究编排、量化实验、证据审计和风险账本分别选型。

| 项目 | 最值得借鉴 | 主要限制 |
|---|---|---|
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 财报研究、估值工具、分工与研报生成 | 偏宽而全的研究平台，生产级 PIT 与权限治理仍需自建 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | LangGraph 状态图、多空辩论、风险角色、checkpoint | 历史新闻/社媒并非严格 PIT，不宜直接用于严谨回测 |
| [Qlib](https://github.com/microsoft/qlib) | 数据、特征、模型、回测与组合研究链路 | 数据许可、历史成分与交易摩擦仍需单独治理 |
| [RD-Agent](https://github.com/microsoft/RD-Agent) | Research/Development Agent 与实验反馈闭环 | 自动研究必须防止窥探、过拟合与试验污染 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | Provider 插件与统一金融数据接口 | AGPL 和上游数据授权需要逐项评估 |
| [AI Hedge Fund](https://github.com/virattt/ai-hedge-fund) | Mandate、统一 signal、确定性风险与账本 | 示例/教育属性较强，实盘组件仍需独立验证 |
| [FinMem](https://github.com/NathanDDDD/finmem-llm-stocktrading) | 信息半衰期、分层记忆、事后反思 | 项目较旧，单股票实验不能直接生产化 |
| [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | 金融 NLP、情绪与数据集 | 更像模型/数据底座，不是工作流控制平面 |
| [FinanceBench](https://github.com/patronus-ai/financebench) | 财报问答与引用评测 | 不能覆盖内部数据、时点泄漏和组合经济性 |
| [InvestorBench](https://github.com/felis33/INVESTOR-BENCH) | 投资任务评测设计 | 公开基准只能作为起点，仍需冻结的内部 gold set |

推荐组合思路：

```text
OpenBB/自建 PIT 数据总线
  + FinRobot 的研究与报告契约
  + TradingAgents 的图式挑战流程
  + Qlib / RD-Agent 的量化实验体系
  + AI Hedge Fund 的确定性风控与账本原则
  + FinMem 的记忆衰减与复盘思想
```

生产采用前必须再次核验每个项目及其依赖的许可证、数据条款、维护状态和安全边界。
