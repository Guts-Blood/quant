# FactorLab 工作约定

日频研究按交易日历计算信号，用同一输入快照评价因子和回测组合。

## 研究与回测前必读

设计实验、运行回测或解释结果前，先读 [README 的研究约定](README.md#研究约定)，确认本次使用的策略、数据和成交时点。再按下表阅读所有适用的文档。例如，用真实行情回测中证 500 价值＋低波，需要同时读日频回测、真实行情和中证 500 实验三行。

| 任务 | 必读材料 | 开始前要确认什么 |
| --- | --- | --- |
| 日频因子研究、参数扫描、次日开盘回测 | [日频研究指南](docs/RESEARCH.md) 和本次运行的配置。仓库示例有[反转](configs/reversal.toml)和[动量扫描](configs/momentum_grid.toml)。 | 按信号截止时点还原股票池和财报版本。`public` 看当时已公开可用的数据，`observed` 还要求当时已采集。核对标签区间，选参只用训练期和验证期。组合按前收锁定订单数量，次日开盘撮合。 |
| 11:00 / 14:00 窗口回放 | [窗口回测的输入与撮合规则](docs/BACKTEST.md) | 委托的 `known_at` 必须早于目标窗口。核对五分钟线的结束时间、T+1 可卖股数、价带、容量、费用及拒单原因。 |
| 真实行情回测、快照恢复或数据更新 | [行情数据与校验](docs/MARKET_DATA.md)、[版本覆盖与研究回执](docs/MARKET_RESEARCH.md)和[本次快照清单](configs/market-data/) | 按清单版本和 SHA-256 核实输入，检查所需行情、历史成分和执行表的共同覆盖。使用执行试点或其财报修订数据时，再读[试点窗口与验收命令](docs/EXECUTION_PILOT.md)。 |
| 中证 500 因子对照、IV30 仓位或反向 DCF | [因子实验计划](docs/FACTOR_EXPERIMENTS.md)、[数据依赖与验收条件](docs/DATA_INFRA_ROADMAP.md)和[冻结的价值＋低波及仓位配置](configs/market-data/value-lowvol-iv30.json) | 先确认对照组、共同样本、费用和基准。区分计划所需数据与该快照已验收的覆盖。IV30 对照没有共同可调仓日时，报告有效日数和缺口，费后收益留空。 |
| 复现旧年份试点 | [旧试点复现入口](experiments/legacy_pilot/README.md)。2022–2025 初筛另读[历史截面协议](experiments/legacy_pilot/protocol.md)，容量与风险预期实验另读[冻结协议](experiments/legacy_pilot/risk_expectations_protocol_v1.json)。 | 沿用对应年份的抽样、费用和成交假设。已用于开发或回放的历史，在新实验中标为历史回放。 |

报告结论时附本次运行的回执。日频实验查 `manifest.json`，盘中回放查 `result.json`，真实行情基线或仓位对照查 `run.json`。报告费后表现时，用同一窗口的订单、持仓与净值核对费用、拒单和实际仓位。

## 实现与提交约定

- 通用实现放在 `src/factorlab/`。因子函数只接收历史输入和参数，返回同索引的分数。每个新增因子在 `factors.CATALOG` 登记名称、所需列和研究假设。
- 单次研究的窗口、股票池、组合规则和费用写入 TOML 或 JSON。特定年份的数据采集与复现脚本放在 `experiments/`。
- 收盘信号使用截止时点已可得的输入。财报修订要保留公开时间、可用时间、采集时间和来源。价格缺行按显式交易日历处理。
- 实验分别报告预测能力、分组结果与费后组合表现。参数扫描保留失败记录并排除留出测试期。
- 安装和离线运行见 README。提交前运行 `python -m unittest discover -s tests -v`。数据缓存和实验产物写入 Git 忽略的本地目录。
