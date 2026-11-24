# A 股 11:00 / 14:00 离线回测

[常见因子历史研究](../experiments/legacy_pilot/README.md)提供中证500历史子样本、分钟线交叉校验、两种资金规模及逐笔审计的复现方法。

在项目根目录运行，Python 3.11+。回放使用独立的历史数据文件和虚拟现金。每次回放结果在 `outputs/backtests/<id>/result.json`，包含输入 SHA-256、参数、成交和拒单明细、每日现金、持仓和收盘净值。

```powershell
python -m factorlab backtest run --daily examples/backtest/daily.csv --bars examples/backtest/bars.csv --strategy reversal5
python -m factorlab backtest run --daily examples/backtest/daily.csv --bars examples/backtest/bars.csv --orders examples/backtest/orders.csv
python -m factorlab backtest run --help
```

第一条演示如何从历史行情生成因子委托。每日收盘，以连续六个可比收盘价计算过去五个交易日收益。次日 14:00 从未持仓且当日尚未尝试交易的股票中选收益最低的一只买 100 股，相同收益按证券代码排序。第 5 个后续交易日 11:00 尝试卖出，拒单后下个交易日再试。停牌、除息、样本断档会重置该股信号窗口。两股样例用于检验信号时点和账本。行业内反转需要另行提供历史行业分类和当日成分，在策略侧生成委托。

第二条读入预先冻结的委托。样例第一天上午买入、当天下午尝试卖出、次日上午再卖，用于核对 T+1。外部策略可按相同格式接入。时间戳由策略负责如实记录，回测器校验它早于目标窗口。

## 数据与来源

随项目附有 2026-11-03 至 2026-11-20 两股各 14 个交易日的公开行情，包括招商银行 600036.SH、美的集团 000333.SZ。日收盘来自腾讯不复权日线，另取 11 月 2 日前收基数。该样本逐日核对了原始和前复权开高低收一致，两只普通主板股票的样例上下限按各自上一日收盘 ±10% 四舍五入到分。正式研究应提供适用当日、板块与证券状态的实际限价。窗口行情来自新浪 5 分钟 K 线，抓取时用各股票 15:00 收盘与腾讯日收盘逐日交叉核对。样本选择和 100 股仓位仅用于运行验收。[腾讯招商银行日线](https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600036,day,2026-11-01,2026-11-20,30,) · [腾讯美的集团日线](https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz000333,day,2026-11-01,2026-11-20,30,) · [新浪 5 分钟行情示例](https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=sh600036&scale=5&ma=no&datalen=1023)

所有 CSV 为 UTF-8，日期和窗口使用上海时间。成交价、估值价和涨跌停价均使用未复权元价，成交量按股计。支持沪深主板与创业板普通 A 股的 100 股整手委托。

| 文件 | 必需列 | 约定 |
| --- | --- | --- |
| 日线 | `date,symbol,close,limit_down,limit_up,status,action` | 每股票每日一行，日期并集就是回测交易日历。提供完整交易日、当时的候选池及已有持仓的估值。停牌日写入有效估值与 `suspended`。正常交易用 `trading`。无涨跌幅限制时上下限均填 `unlimited`。 |
| 窗口行情 | `date,end,symbol,open,volume` | 每只股票每天两行，`end` 是 5 分钟 K 线的结束时间，11:05 / 14:05。开始时间标记的行情要先对齐。缺行拒单，零成交量拒单。 |
| 外部委托 | `date,window,known_at,symbol,side,shares` | `window` 选 11:00 / 14:00，`known_at` 含时区并严格早于窗口。同一窗口每股只接受一条净买或净卖委托，`side` 选 buy / sell。同一方向用输入行顺序表达策略优先级。 |
| 可选基准 | `date,level` | `--benchmark` 接受正数全收益指数点位。提供起始日的前一交易日收盘作期初，回测区间内的基准日期与净值日期必须完全匹配。超额是同期间策略净收益率减基准收益率。 |

`action=none` 表示当日已核对无公司行动。现金分红用 `cash`，另加 `cash_per_share,pay_date` 两列，以除息日前收盘股数计算应收，在支付日首个窗口前入账。这是按输入净派现额、支付日开盘可用的简化假设。送转、配股、退市结算等尚未建模的事件标为 `ex`，待核资料标为 `unknown`。若当日开盘前有相关持仓，回放会终止并指出日期和代码；零持仓时跳过该标的委托。缺持仓日线也会终止。

准备好自己的日线后，近期窗口可用下面的命令补齐。抓取器逐股查询最近至多 1023 根 5 分钟线，校验完整收盘及候选窗口，成功后发布新的 CSV。更早的日期可接入历史分钟数据导出。跨年研究还需当时可知的股票池、退市和公司行动记录，并按时期处理费率变化。

```powershell
python -m factorlab backtest fetch-bars --daily examples/backtest/daily.csv --out backtests/recent-bars.csv
```

## 撮合与读账

- 每日两个批次合计最多 **10 个不同证券代码**，同股跨窗口只占一席。保守按已提交的代码计数，拒单也占一席，超额记为 `daily_symbol_cap`。可用 `--max-symbols` 设为 1 至 10。每个窗口先处理预定卖单再处理买单，卖出款当批可用。
- 窗口开始后首根 5 分钟线的开盘价作为成交代理价，买单加滑点向上取整到分，卖单减滑点向下取整到分。买在涨停价、卖在跌停价，以及含滑点价超出限价时均拒单。默认单笔最多占该 5 分钟成交量 1%，超出则整笔拒单。该段成交量只用于事后估计可成交性。分钟线无法重建盘口排队和精确秒级成交。
- T+1 按可卖股数记账。若已有昨仓，今天新买后仍可卖出昨仓部分。现金、可卖股数或成交容量不足均保留原资产。外部委托按指定窗口有效，重试需新委托。
- 演示使用初始现金 100,000 元、双边佣金 2.5 个基点且每笔最低 5 元、卖出印花税 5 个基点、双边过户费 0.1 个基点及单边滑点 5 个基点。所有费率、最低佣金、滑点、资金与成交量参与比例均可通过 CLI 调整。费率在一次运行内固定，长历史回测需按适用费率制度扩展。规则与费用应按研究时期核对。[上交所交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml) · [深交所交易规则](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)
- 收盘净值为现金、应收股息和收盘持仓市值之和。已发生的税费与滑点都体现在净值里，`slippage_cost` 单列便于归因。最大回撤按日末净值、以初始资金为首个峰值计算。期末保留持仓，以收盘估值。结果中的 `expected_window_bars` 和 `available_window_bars` 可对照行情覆盖率。
