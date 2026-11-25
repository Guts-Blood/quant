# A 股日频因子研究 · FactorLab

用普通 Python 在本地计算因子、检查预测能力，再按明确的成交规则回测组合。内置短期反转、动量、低波动和盈利收益率。每次实验生成一份可离线打开的 HTML 报告，以及逐日因子、持仓、委托和净值 CSV。

## 第一次运行

在仓库根目录使用 Python 3.11+。以下 PowerShell 命令创建项目专用环境，两个配置都使用仓库附带的虚构样本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m factorlab run configs/reversal.toml
.\.venv\Scripts\python.exe -m factorlab scan configs/momentum_grid.toml
```

打开命令打印的 `report.html`。单次实验展示各时段的 Rank IC、分组收益、因子覆盖率和费后净值。扫描示例比较两种动量窗口与两种持仓数量的四个组合，仅使用训练期和验证期。

## 目录

| 路径 | 内容 |
| --- | --- |
| `src/factorlab/data/`、`universe.py` | 输入校验、交易日历、历史股票池、财务数据版本和来源 |
| `src/factorlab/factors/`、`transforms.py` | 因子公式、去极值、标准化、行业及市值中性化、分数组合 |
| `src/factorlab/labels.py`、`evaluation/` | 未来收益标签、Rank IC、分组差、跨期稳定性、因子相关性 |
| `src/factorlab/portfolio.py`、`backtest/` | 从分数生成目标权重，模拟成交、持仓、费用和净值 |
| `src/factorlab/experiments/`、`reporting.py` | 运行配置、参数扫描、结果清单和 HTML 报告 |
| `configs/`、`tests/fixtures/` | 可以直接运行的配置和离线样本 |
| `experiments/legacy_pilot/` | 旧年份试点的协议、数据适配、回放和审计 |
| `datasets/`、`outputs/` | 自备数据和生成结果，本地保存，由 Git 忽略 |

## 研究约定

日频信号使用收盘后可得的数据，订单按前一交易日净值与原始收盘价确定数量，下一交易日开盘执行。开盘容量依据前一日成交量估算。输出中分别记录费用、拒单原因、应收股息和每日持仓。分组差用于评价因子预测能力，可执行策略收益以组合净值为准。

维护新因子时，在 `factors/` 写纯函数，在 `CATALOG` 登记输入列和研究假设，再用配置选择窗口和组合规则。数据时间约定、财报修订接口及实验产物说明见[研究指南](docs/RESEARCH.md)。原有两个盘中窗口的示例见[窗口回放说明](docs/BACKTEST.md)。

完整离线检查可运行 `python -m unittest discover -s tests -v`。若要生成较长的虚构样本与一套可修改的 JSON 配置，执行 `python -m factorlab demo --out outputs/demo-new`，使用尚未存在的目录。

真实行情快照的恢复、DuckDB 查询库及交易回放验收见[回测数据说明](docs/MARKET_DATA.md)。
