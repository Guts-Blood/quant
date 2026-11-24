# 日频研究指南

## 换入自己的行情

将行情与交易日历存入 `datasets/`，复制一份示例配置，修改 `data.panel`、`data.calendar`、`source_id` 与各区间日期。相对路径从配置文件所在目录解析。

面板以 `date,symbol` 为唯一键，交易日历的 `date` 需递增且唯一。基础列为 `adj_close,amount,is_st,tradable,available_at`。成交量以股计，收益率用小数，布尔值可用 true/false 或 1/0。缺行保留在完整日历上，缺价格留空。估值因子额外使用 `pe_ttm`，低波动因子使用 `return`，缺少收益率列时从相邻交易日的复权收盘价计算。默认标签需要 `adj_open`。

模拟成交额外读取原始 `open,close,volume,action,limit_down,limit_up`。开盘和收盘价精确到分，无限价的证券显式标记 `limit_exempt=true`。已核实的现金分红指定 `action=cash,cash_per_share,pay_date`。持仓遇到缺失估值或未支持的公司行动会使实验失败，原因保存在结果目录。费用使用配置指定的常数假设。

## 历史数据时点

`public` 使用来源声明的可用时间重建历史，`observed` 还要求数据在信号截止前已实际采集。时间戳带时区。收盘价的可用时间必须在当日收盘至配置的信号截止时间之间。

`supplied_panel` 的结论限于输入证券。`historical_membership` 另需成员事件文件，列为 `symbol,effective_date,available_at,is_member,source_id`，实际采集模式还需 `ingested_at`。纳入和移出分别记录事件，以生效且当时可知的最近事件决定成员资格。

财报扩展通过 `factorlab.data.asof` 接入。每行保留 `symbol,field,period_end,published_at,available_at,ingested_at,revision,value,source_id`。`asof_records` 在给定时点选取已可得的修订版本，`latest_fundamentals` 再选最新报告期。新增 ROE 或增长类因子时，先按历史截止时点对齐财务字段，再复用评价与回测。当前估值因子的 PE 由输入面板提供。

## 读实验结果

每个运行目录内的 `manifest.json` 记录参数、代码指纹、输入哈希、依赖版本和产物路径。原始因子、处理后分数、股票池和目标权重分别保存。各区间、各持有期都有每日评价、分组收益和年度统计。未来收益标签跨越区间末端时会被剔除。

`scan.grid` 设置有限参数组合，`max_trials` 限制次数。每个组合都有自己的结果目录。扫描汇总保留参数、结果和错误，留出测试期在方案冻结后的单次实验中评价。

旧试点复现步骤见 [legacy_pilot](../experiments/legacy_pilot/README.md)。迁移时，四个基础因子在含缺失值的 90 日固定样本上与旧公式一致，七组窗口回放结果和七组拒单原因也与旧实现一致。
