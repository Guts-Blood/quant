# 中证 500 执行数据试点

版本 `2026-11-25-pilot-v1` 可独立恢复。原始价、估值和执行表覆盖 2023-09-07 至 2024-01-05 的 80 个交易日。前 61 日供信号预热，后 19 日用于持仓回放。此次发布是限定窗口的子版本。需要父版 1,338 个交易日的研究继续使用 `2026-11-25-v1`。[Git 清单](../configs/market-data/2026-11-25-pilot-v1.json) · [Drive 发布清单](https://drive.google.com/file/d/1-cXYMmwklXsYrGv0xPIlG6DahZHLj3m4/view)

## 股票执行与数据口径

逐日保留调样前后两份名单的并集，共 550 只，44,000 行。按公告在 2023-12-11 开盘切换 50 对成分。两个截面来自父版，逐日以第二个来源的成分标记交叉核对。每个交易日有 500 个不同成分，跨表主键均唯一。停牌无交易记录时，独立来源的成员标记也可能缺失。每个源文件的提交与 SHA-256、逐日覆盖和逐股缺口保存在版本中的 `reference-…-execution-audit.zip`。[证券与数据源配置](../configs/market-data/execution-pilot.json) · [调样公告报道](https://www.cnfin.com/gs-lb/detail/20231124/3970957_1.html)

| 字段 | 本版口径 |
|---|---|
| 行情与可交易状态 | BaoStock 日分区，不复权人民币元，成交量为股、成交额为元。逐日校验 549 只股票的原始收盘。九号公司 CDR 用第二来源的原始价和正成交量确认交易。12 个停牌证券日保留原始收盘，成交量额为零。 |
| 复权收益与价带 | 收盘价除以当日除权参考前收再减一，小数收益，使用交易所参考价精度。成交和持仓估值用未复权价。按此时期主板、风险警示、科创板和创业板规则计算上下限，按分四舍五入。窗口全部是常规价带，所有原始日内高低价均在带内。 |
| 估值与暴露 | PE 用当日总市值除以供应商历史行的滚动净利润。12 个停牌证券日缺 PE。每个持仓输出当日市值、申万行业字段与 NAV 权重。估值和行业来自事后取得的历史版本，行业分类修订的完整时点链待补。 |
| 公司行动 | 用参考前收变化与实施分红登记逐日对齐。两份实施公告核实到元/股及支付日。32 个现金分配缺原公告支付日，另有 3 个送转事件，逐项标记。持仓遇到此类事件，整组回放给出不可用回执。 |
| 基准 | `benchmark.parquet` 和运行目录的 `benchmark.csv` 保留 H00905 中证 500 全收益指数同日收盘与相对上一交易日收盘的收益。股票组合从首个可执行开盘建仓，首日指数收益包含前收至开盘。 |

本窗口 190 条候选记录，37 笔委托全部成交，费用 198.37 元。初始 100,000 元，期末 100,022.63 元。每日用成交数量、成交价、分项费用、分红支付和原始收盘价独立重算现金、应收款、股数及净值。专项验收还包括 688289 调出后的 200 股估值和 52.44 元分红、603816 停牌拒单、002195 涨停拒单。原有 600160 两笔固定验收的期末净值为 99,985.14 元。[专项回放](../experiments/check_execution_window.py)

## 510500 与财报

沿用 15:01 上海时间、2% 连续复利年利率、最长 300 秒报价年龄。固定日与盘中原始批次分别重算。2026-11-25 固定批次实收 124/124 份，52 份通过正双边过滤。12 月到期看跌/看涨两翼为 3/7 档，次年 3 月为 1/10 档。远月有效看跌档数低于 3，IV30 缺值。该次固定采集有两批原始尝试。历史 19 日窗口内，有效信号、触发和共同可调仓日均为 0。新采集入口使用 XSHG 交易日历，等待 15:01 后请求行情，启动晚于观察点 60 秒记漏采，每次不可用最多再试一批。程序为每批生成 ZIP 和 SHA-256，回执保留实际可用时间及按到期日划分的两翼。工作流在北京时间 14:35 请求 runner，GitHub 的排队延迟会记入回执。固定观察由默认分支上的工作流启动。当前锁定交易日历截至 2026-12-31，越界时报错。[采集入口](../experiments/scheduled_options.py) · [工作流](../.github/workflows/capture-510500.yml)

`statement_facts` 提供净利润、经营现金流、总资产及 DCF 桥接所需的报表项目。每行记录公告、时间精度、保守可用时间、实际采集时间、修订、单位、期间口径和原报表行。仅知公告日期时，次日上海时间 00:00 生效。字段后缀区分瞬时、单季、年内累计和 TTM；本批原始流量以单季及累计披露入库。环比、TTM 和盈利质量的全池覆盖仍待扩充。贵州茅台 2022 年合并净利润在两次年度披露中的版本为 65,375,136,766.75 元与 65,376,039,957.88 元。这里追踪已收集的披露版本；中间公告需逐份补齐。[原始财报行与文档定位](../configs/market-data/pilot-disclosures.json)

反向 DCF 针对 2023-12-08 冻结名单里的隧道股份，股价 5.40 元乘股本核对至 169.78 亿元市值。按已披露借款、债券、租赁、现金等价物、少数股东权益及单列投资，桥接至经营企业价值 343.76 亿元。9 个月收入年化，5 年核心经营利润率取同期口径。贴现率等假设是回溯敏感性分析的设定。求解器在 −50% 至 +100% 的增长搜索区间内未找到变号根。+3% 与 −3% 收入增长情景分别得到 196.73 亿元和 212.43 亿元。后者较高与增长需要再投资、收缩时模型不释放资本有关。这里覆盖 12 家候选公司中的 1 家、190 条公司/信号日中的 1 条。[情景和估算依据](../configs/market-data/pilot-dcf.json) · [三季报](https://static.cninfo.com.cn/finalpage/2023-10-28/1218181863.PDF)

## 在干净目录验收

使用 Python 3.12，在仓库根目录执行。恢复读取团队 Drive 文件夹，本子版本各逻辑文件均小于 10 MiB。已下载对应 Drive 对象时，也可用 `market_snapshot --archive 本地对象目录 --manifest 清单 --out 目标 --catalog`。

```bash
python -m pip install -c configs/market-data/requirements.txt -e '.[market]'
v=2026-11-25-pilot-v1
snap="datasets/market/$v"
store='research,root_folder_id=1WG-gCvQdpq17iSszhUAre3vFVK2dF_GF:'
python -m experiments.market_store restore "$store" "$snap" --manifest "configs/market-data/$v.json" --catalog
python -m experiments.check_market_replay "$snap/market.duckdb"
python -m experiments.check_execution_window "$snap"
python -m experiments.market_research baseline "$snap" --start 2023-12-11 --end 2024-01-05 --out outputs/pilot-baseline
python -m experiments.market_research compare "$snap" --start 2023-12-11 --end 2024-01-05 --out outputs/pilot-compare
python -m experiments.market_version audit-options "$snap"
python -m experiments.market_statements asof "$snap" 600519 '2023-04-03T16:00:00+08:00' --mode public
python -m experiments.market_statements asof "$snap" 600519 '2024-04-08T16:00:00+08:00' --mode public
python -m experiments.pilot_dcf "$snap" outputs/pilot-baseline outputs/pilot-dcf.json
```

从源重建采用配置中的三个固定 Git 提交。将它们分别克隆至本地，以下按参数顺序传入仓库目录。父目录提供原清单及继承文件，两个被完整替换的大行情、估值文件可留在冷存储。构建器对父清单做固定哈希校验，核对每一份实际使用的归档，并从 Git 对象读取源文件。重复构建时复用本次采集时间，可逐字节比较子版本清单。

```bash
python -m experiments.execution_window datasets/market/2026-11-25-v1 \
  "datasets/rebuilt/$v" /path/to/ashare-daily /path/to/stock_data /path/to/a-stock-history \
  --collected-at '2026-11-25T17:39:22+08:00'
cmp "configs/market-data/$v.json" "datasets/rebuilt/$v/manifest.json"
```
