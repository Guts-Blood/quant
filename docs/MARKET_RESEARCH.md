# A 股研究数据与费后回测

版本 `2026-11-25-v1` 含 15 个逻辑文件。股票日线、估值、中证 500 成分和交易日历共同覆盖 **2021-05-17 至 2026-11-19，共 1,338 个交易日**。原来的 `2026-11-19` 九文件版本可独立恢复，共同覆盖截至 5 月 25 日。Git 中保留代码、DuckDB 表定义、仓位规则与完整清单。Drive 保留分卷、原始响应、标准化分区、成分重建证据及发布清单。[版本清单](../configs/market-data/2026-11-25-v1.json) · [Quant Research Data](https://drive.google.com/drive/folders/1WG-gCvQdpq17iSszhUAre3vFVK2dF_GF)

逐股执行表的首个可恢复子版本覆盖 2023-09-07 至 2024-01-05，含 61 日预热和 19 日真实费后回放。[执行窗口与验收](EXECUTION_PILOT.md)

## 与设计稿对照

| 设计中的能力 | 从 ba8897a 复用 | 本次接通或仍缺的部分 |
|---|---|---|
| 跨机恢复 | 固定上游版本、SHA-256、分卷恢复、rclone、DuckDB、两笔成交回放 | 完整子版本继承父清单。研究记录数据和代码指纹，半途恢复能继续，同名异版会报错。历史文件只留下归档日期的，清单标明日期精度。 |
| 历史股票池 | 历史成分区间、交易日历、日线和估值 | 新增 121 个交易日，逐日与两个行情表核对。成分按公告生效日重建，用完整名单校验，新增股票在成为成分之后才参与收盘排名。 |
| 风险数据 | 新浪原始响应、报价解析、已有方差条带 IV30 | 固定收盘观察、采集重试、原始链与派生值、报价剔除原因、盘中隔离、可重复重算。真实日频历史尚不足。 |
| 费后研究 | 次日开盘撮合、费用、成交与拒单、净值、DCF 求解器 | 单一候选名单、三种目标仓位、共同调仓日期、收盘实际仓位与费后指标。全市场复权收益与历史交易约束面板仍需补。 |
| 短周期实盘验证 | 旧试点的 11:00、14:00 窗口模拟 | 两个股票窗口的历史盘口和真实券商委托回报仍待采集。本期以日频期权和次日开盘回放为验收口径。 |

## 在干净机器复现

需要 Python 3.12、Git 和 rclone。将 rclone 的 `research` 配成可读取团队文件夹的 Google Drive remote。下面直接按文件夹 ID 定位，即使团队盘入口名称不同也能使用。已有本地下载目录时，把 `store` 改为该目录的绝对路径。版本清单 SHA-256 为 `cea41d9f9024784f37577862e555cb98df95ee8f2d97d73c56f2491cf946135c`。

```bash
git clone --branch data/portable-market-store https://github.com/Guts-Blood/quant.git
cd quant
git switch --detach market-research-2026-11-25-v1
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -c configs/market-data/requirements.txt -e '.[market]'
store='research,root_folder_id=1WG-gCvQdpq17iSszhUAre3vFVK2dF_GF:'
version=2026-11-25-v1
snap="datasets/market/$version"
python -m experiments.market_store restore "$store" "$snap" --manifest "configs/market-data/$version.json" --catalog
python -m experiments.check_market_replay "$snap/market.duckdb"
python -m experiments.market_version coverage "$snap" --start 2021-05-17 --end 2026-11-19
python -m experiments.market_version audit-options "$snap"
```

恢复会复用已经校验通过的文件。分卷缺失或损坏时，当前文件无法通过校验，完成标记只在全部成功后写入。恢复其他版本需换目标目录。DuckDB 的外部视图使用本机数据路径，移动目录后，对同一清单重跑带 `--catalog` 的恢复命令即可。

**成交验收**使用原回放中的 600160 和 10 万元初始现金。2024-01-02 买入 100 股，成交价 16.51 元，费用 5.02 元。2024-01-05 卖出 100 股，成交价 16.47 元，费用 5.84 元。合计 10.86 元，期末空仓，净值 99,985.14 元。两笔来自固定验收指令。

## 各表实测覆盖

| 表 | 实际日期 | 数量与口径 |
|---|---|---|
| `daily` | 2021-05-17 至 2026-11-19 | 6,539,954 行，5,241 只。原始开高低收，成交量为股，成交额为元。 |
| `valuation` | 2021-05-17 至 2026-11-19 | 6,539,954 行，5,241 只。PE、PB、PS、换手、ST。与日线按日期和代码关联。 |
| `fundamentals` | 报告期 2021-03-31 至 2026-03-31，公告日 2021-04-07 至 2026-05-15 | 101,463 行。个别指标可用，尚缺完整公告时刻及逐次修订版本。 |
| `dividends` | 公告 2021-01-08 至 2026-05-16，登记 2021-01-14 至 2026-05-22，除权 2021-01-15 至 2026-05-25 | 31,131 行。历史支付日和复杂公司行动仍需核对。 |
| `trading_calendar` | 2000-01-04 至 2026-11-19 | 6,513 个交易日。新日历在重叠区完全一致，新增 121 日同时出现在日线与估值中。 |
| `csi500_intervals` | 2007-01-31 至 2026-11-19 | 22,552 个闭区间，1,803 个历史代码。`csi500_members` 在交易日展开，逐日严格检查 500 个不同代码。 |
| `option_quotes` | 2026-11-25 | 280 条，为一个盘中截面 156 条和一个固定截面 124 条。第二次收盘采集单存原始尝试。 |
| `option_observations` | 2026-11-25 | 两个截面，盘中 1 个可计算值，固定时点 0 个可计算值。 |
| `execution` | 尚未归档 | 全股票回测需要逐股逐日历史复权收益、交易状态、涨跌停、公司行动、现金分配与支付日。 |

成分的旧版本在 2026-05-25 仍含 600079，遗漏 688037。本次从 1 月 9 日的 500 只名单出发，按 1 月 12 日、6 月 15 日、10 月 12 日、10 月 19 日四个生效交易日分别更换 1、50、1、1 只。10 月 19 日对应东兴证券终止上市，调整附件的最新日期为 10 月 13 日。重建与年初、1 至 9 月月末、11 月 24 日官方名单共 11 个截面逐股相符。11 月 24 日名单仅用于事后核验，研究版本截至 11 月 19 日。更早区间沿用原 Qlib 历史快照，部分记录只精确到月末。原始公告、附件、独立成分文件及校验值随快照保存。[1 月调样](https://www.csindex.com.cn/#/about/newsDetail?id=3006027) · [6 月定期调整](https://www.csindex.com.cn/#/about/newsDetail?id=3006137) · [10 月临时调样](https://www.csindex.com.cn/#/about/newsDetail?id=3006261) · [合并调整](https://www.csindex.com.cn/#/about/newsDetail?id=3006227) · [上交所摘牌日公告](https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20261012_10834560.shtml) · [独立日历与月末样本](https://huggingface.co/datasets/joshuaxql/qlib_data/tree/ee0fa6b2299cb5e927d2df491888c84f6c9c027a)

## 510500 日频采集与历史样本门槛

观察点为上海时间 **15:01:00**。保存原始买一、卖一、合约代码、认购认沽、行权价、到期时刻、调整标记、逐合约报价时间、可用及采集时间、使用的连续复利年利率。每个合约取这一批实际收到的最新报价。报价不得晚于观察点，最多回溯 300 秒。原始提供方响应、报价剔除原因、期限、方法版本和标准化分区一起保留。首版利率为假定的平坦 2%，调整标记来自供应商合约名称规则。

| 2026-11-25 上海时间 | 原始双边字段 | 通过过滤的正双边报价 | IV30 |
|---|---:|---:|---|
| 14:13:19 盘中 | 156 / 156 | 89 | **37.268737%**，只进入盘中观测。 |
| 15:01:00 固定 | 124 / 124 | 52 | **缺失**。剔除 28 个调整合约、44 条过时报价，远月有效看跌行权价不足，无法夹住 30 天。两次独立抓取分别于 15:01:18、15:01:32 完成，得到同一缺失原因。 |

历史样本可逐日以 ZIP 导入。有原始逐合约双边、同一观察点、真实市场可用时间和采集时间后，先用 `audit-options` 重算并比较每一条结果。检查每个交易日的预期/收到合约数、正双边、报价年龄、到期日与两翼行权价数量、调整合约和缺失原因。现有样本只覆盖一个自然日，跨日稳定性仍待验收，未采购长历史。供应商仅给日收盘价的资料无法验证双边或报价年龄。

在交易日 15:01 后运行以下命令。使用同一目录重复调用会复用当天结果。临时中断后重抓同一批次，避免把两次请求拼成一个截面。若源端暂缺报价，`--retry-unavailable` 会保留新尝试。将每个额外尝试以 `--attempt` 归档，研究版本对每个标的、日期和观察类型只选择一条分区。历史供应商文件内使用一条 `observation.json`，字段和单位见已恢复的日频分区与 `experiments/option_capture.py`。

```bash
receipt=$(python -m experiments.option_capture datasets/captures --rate .02)
capture=$(python -c 'import json,sys; print(json.load(sys.stdin)["path"])' <<<"$receipt")
next_version="$(date +%Y-%m-%d)-daily-v1"
child="datasets/market/$next_version"
python -m experiments.market_version update "$snap" "$child" --daily "$capture"
python -m experiments.market_version audit-options "$child"
python -m experiments.market_store publish-increment "$child" "$store" --parent-manifest "$snap/manifest.json"
cp "$child/manifest.json" "configs/market-data/$next_version.json"
```

将生成的清单与研究代码一起提交。更新命令校验父版及输入，拒绝同名异版和重复主键。Drive 增量对象以内容哈希命名，发布清单最后写入。重复发布复用相同对象。原版的大 Parquet 分卷保持可恢复。更新四张公开表时先运行 `python -m experiments.market_snapshot --refresh-public`，再将上游版本目录中的 `manifest.json` 作为 `market_version update --replace-manifest` 的输入。成分、日历和历史期权分区随子版本继承。供应商交付完整替换表时，可先用 `market_version prepare` 登记来源和带时区的采集时间。日历交叉核对及成分公告重建的入口分别为 `experiments.extend_market_calendar` 和 `experiments.rebuild_membership`。

## 选股、仓位与反向 DCF

先排股票。前收盘时在当日成分中按正盈利 E/P 与过去 20 个交易日复权收益的负标准差等权排名。非正 PE 或缺失 PE 给最低价值排名。过滤 ST、不可交易、缺 61 日价格历史或 20 日平均成交额低于 3,000 万元的股票。目标 10 只，上一份目标名单仍在前 20 名的优先保留。次日开盘按当日成分拦截已调出的股票，空出的仓位留现金，保留最初名单和排序。引擎用前收锁单、100 股整手、上一日成交量的 1% 估算容量，保存成交、拒单、费用和逐日持仓。交易费用采用相同的一组研究假设，详见[冻结配置](../configs/market-data/value-lowvol-iv30.json)。

再比较仓位。`baseline` 可以独立运行。`compare` 冻结相同候选、相同有效调仓日、相同初始资金及费用。16:00 前已可用的当日固定 IV30 达到此前 252 个交易日第 80 百分位时，动态组把原目标乘以 0.75。此前窗口至少 126 个有效值，排除当日。另两组用原仓位和固定 95% 目标。整手、现金与拒单会使实际仓位偏离目标。共同样本从首个可操作的开盘开始，中间 IV 无效时三组一起持有并继续逐日估值。报告费后区间收益、最大回撤、日末股票市值/NAV 的均值、累计半边换手、费用、可调仓日及触发日数。

```bash
python -m experiments.market_research baseline "$snap" --start 2026-01-05 --end 2026-11-19 --out outputs/market-baseline-v1
python -m experiments.market_research compare "$snap" --start 2026-01-05 --end 2026-11-19 --out outputs/market-iv-v1
python -m unittest discover -s tests -p test_market_pipeline.py -v
```

在配好 Drive 的机器上，可以把 `--store "$store"` 加在研究命令末尾，或设置 `export QUANT_RESEARCH_STORE="$store"`，每次完成后自动把回执和它指向的全部 CSV 一起归档。归档名包含快照版本和 ZIP 的 SHA-256，上传后回读校验。既有研究可运行 `python -m experiments.market_store publish-run outputs/market-iv-v1 "$store"`，同一研究重复发布得到同一对象。解压即可读取回执、原始候选、调仓日和成交明细。

每个输出目录含 `run.json`、`candidates.csv` 和 `schedule.csv`。回执记录版本及清单 SHA、输入与输出哈希、代码指纹、软件版本、评估窗口、费用和参数。有可执行样本时分别写三套净值、持仓和订单。当前真实版本在上述窗口有效比较日为 0，收益、回撤、平均仓位与换手均为空。已有的 2022 至 2026 年历史研究只能做回放。按 2026-11-25 冻结的新规则，后续留出期才可作为前瞻样本外检验。

真实对照还需要 **同一日期段** 的两类数据。一类是固定时点的历史期权链，至少先积累 126 个有效交易日，随后有当日可计算值才能形成首个信号，并需要后续交易日成交和估值。另一类是 `execution.parquet`，主键为 `date, stock_code`，六位证券代码，字段为 `adj_return, tradable, action, limit_down, limit_up, limit_exempt, cash_per_share, pay_date`。回报为小数，价带与成交使用未复权人民币价格，分红以元/股计。`action` 支持 `none, cash, ex, unknown`，未知行动或无法处理的除权会阻断受影响持仓的回放。需核实除息、停复牌、涨跌停豁免及合并换股，估值发布版本也需留档。把完整执行表及来源作为替换输入后，同一回测命令即可运行。

反向 DCF 读取冻结的候选文件及其 SHA，只给入围公司做独立附注。每条输入提供 `signal_date, symbol, available_at, source_id, shares, debt, cash, minority_interests, nonoperating_assets, model, attainable_growth, stress_growth`。金额统一为人民币元，用候选当日价格和当时股本计算市值。模型参数沿用原 FCFF 求解器，包含基础收入、经营利润率路径、税率、销售资本比、折现率、永续增长率、终值利润率和 ROIC。附注保留隐含收入增长的所有可解根、可实现增长、压力情景与原始输入。缺少资料按公司记缺，晚于决策时点的资料会被拒绝。这样可用原名单检验后续改变决策的增量效果。[原有模型与字段](../experiments/legacy_pilot/risk_expectations.py)

```bash
python -m experiments.shortlist_dcf outputs/market-baseline-v1 point-in-time-dcf-inputs.json outputs/dcf-notes-v1.json
```
