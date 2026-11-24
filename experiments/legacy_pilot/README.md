# 常见因子选股试验

## 2026 年回放

运行后生成的 `backtests/factor_2026/asof_reviewed/report.md`覆盖 1 月 5 日至 11 月 23 日。窗口委托在看见成交之前一次锁定。早段用开盘价及窗口前半小时成交量估算，9 月 18 日起用实际五分钟成交均价与容量。旧版结果存放在 `asof_results`。

在根目录依次运行 `python -m experiments.legacy_pilot.study_2026`、`python -m experiments.legacy_pilot.audit_2026`、`python -m experiments.legacy_pilot.report_2026`。第一步加 `--fixed-universe` 可单独复算固定池。输入存放在 `backtests/factor_2026`，含沪深退市名册原件，研究环境使用 xlrd、openpyxl 和 matplotlib。

## 2022–2025 年试验

本次研究的规则、开发期和留出期见 [protocol.md](protocol.md)。报告、费后对照、逐日持仓和委托明细位于 运行后的 `backtests/factor_study_v1/results/`。采用 Python 3.11+ 以及研究环境中的 pandas、numpy、requests 和供 pandas 解析 HTML 的 lxml。在仓库根目录执行 `python -m pip install -e ".[legacy]"` 可安装试点依赖。

在本仓库根目录运行下列命令：

```powershell
python -m experiments.legacy_pilot.collect
python -m experiments.legacy_pilot.check_actions
python -m experiments.legacy_pilot.check_prices
python -m experiments.legacy_pilot.study
python -m experiments.legacy_pilot.breadth
python -m experiments.legacy_pilot.breadth --forward-2025
python -m experiments.legacy_pilot.summarize
```

- `collect` 需要本机 `~/.qlib/qlib_data/cn_data/raw_data_back_adjust/` 的历史复权日线。它按历史成分快照和固定代码抽样规则下载原始分钟线、中证500全收益指数和现金分红/送转记录。网络取数可重跑，已缓存成功的原件会复用；失败清单保存在 `backtests/factor_study_v1/fetch-errors.json`。
- `check_actions` 核对实施公告日期与除权日，`check_prices` 用独立不复权日线复核价格换算。它们只补数据，不调整因子或持仓参数。送转股实际可卖日期与出处保存在 `share_listings.json`。
- `study` 使用项目的五分钟窗口撮合、现金与 T+1 规则。按年重新投入初始资金，逐日处理历史费率、经核实的公司行动与实际可卖数量。因为历史流动性约束可能导致卖单被拒，组合可以跨越除权日。现金股息按税前金额的 80% 计入净值，当年冻结；送转按登记股数和每股比例计入整股，未核实上市时点的新增股冻结。除权日对送转股票使用保守的暂停撮合规则。审核输出目录中每个策略的 `nav.csv`（现金、应收、股数、送转）与 `orders.csv`（费用、滑点和拒单原因）。
- `results/daily.csv` 与 `results/bars.csv` 是两个操作窗口的标准输入。基础命令会对送转日标记 `ex` 的既有持仓停算；本试验的扩展回放使用同一撮合函数并单独记送转股。导出的现金分红支付日期是年度冻结假设的释放日。分钟行情文件保存在本机，输入哈希见 `sources.json`。
- `breadth` 在没有重叠的另一组历史股票上检验因子排序与未来五日收盘收益的关系。`summarize` 独立对账后生成报告。历史修订、股票覆盖、行业和市值暴露仍影响解释。
