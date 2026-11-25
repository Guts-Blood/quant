# A 股回测数据

当前完整发布为 `2026-11-25-v1`。它延长了交易日历和中证 500 成分的覆盖，并把 510500 原始期权链与仓位实验接入同一版本。新机器恢复、实际覆盖、增量采集和费后回测命令见[第一阶段交付](MARKET_RESEARCH.md)。下文保留初版九文件快照的迁移方法。

本地研究库位于 `datasets/market/2026-11-19`。长期副本保存在团队的 Google Drive 目录 Quant Research Data。版本清单 `configs/market-data/2026-11-19.json` 记录固定来源版本、原文件字节数、SHA-256 和 Drive 分卷顺序。2026-11-19 是日线截止日，其他表各有截止日。

## 在新工作区恢复

以下命令使用当前工作区的恢复工具和回测代码。Drive 的数据文件可独立下载，按数据目录 CSV 中的顺序拼接同一文件的分卷即可得到原始 Parquet。将同一 Drive 文件夹中的数据文件下载到一个目录，保留文件名。使用 Python 3.11 或更新版本，在仓库根目录运行：

```sh
python -m pip install -e '.[market]'
python -m experiments.market_snapshot --archive /path/to/downloaded-drive-folder --catalog
python -m experiments.check_market_replay datasets/market/2026-11-19/market.duckdb
```

不带 `--archive` 并添加 `--public-only` 可直接从固定版本的公开源恢复历史表。采集的盘中快照从 Drive 恢复。重复运行会校验并复用本地文件。目录移动后重新运行 `--catalog` 可更新视图所引用的路径。工作区保留对应的工具版本。

## 云盘与跨云迁移

在新机器克隆本仓库并安装上述 Python 依赖和 rclone。用 `rclone config` 配置名为 `research` 的 Google Drive remote。以下假定 `Quant Research Data` 位于该账号的云盘根目录。如果 remote 已直接指向该文件夹，把路径设为 `research:`。凭据由每台机器的 rclone 管理。

```sh
python -m experiments.market_store restore 'research:Quant Research Data' datasets/market/2026-11-19 \
  --manifest configs/market-data/2026-11-19.json --catalog
python -m experiments.check_market_replay datasets/market/2026-11-19/market.duckdb
```

上述命令按分卷顺序还原九个文件，逐一核对大小和 SHA-256，完成后用本机路径重建 DuckDB 视图。中断后可重跑。回放验收应得到一笔买入和一笔卖出，各 100 股，并在期末清仓。

要将已有版本复制到 S3、其他 Drive 或本地挂载盘，将 `backup:quant` 换成目标 rclone 路径。发布只读取清单中的文件，数据校验通过后才写入发布清单。命令打印的版本路径包含清单的 SHA-256。在另一台机器，用同一份数据对应的路径恢复；remote 名称可按当地配置替换。

```sh
release=$(python -m experiments.market_store publish datasets/market/2026-11-19 backup:quant)
python -m experiments.market_store restore "$release" datasets/restored/2026-11-19 --catalog
python -m experiments.check_market_replay datasets/restored/2026-11-19/market.duckdb
```

## 数据和使用口径

| 表或文件 | 覆盖和使用方法 |
| --- | --- |
| daily、valuation | 2021-05-17 至 2026-11-19。各 6,539,954 条，5,241 个证券代码。按证券与日期一对一连接为 research_daily。日线价格为不复权价，量为股，成交额为元。 |
| fundamentals | 101,463 条。含公告日、报告期、利润等部分财务指标，报告期截至 2026-03-31。缺少逐次修订记录及完整企业价值桥。 |
| dividends | 31,131 条，含实施进度、登记日和除权日。公告日期截至 2026-05-16。 |
| csi500.tsv、calendar.txt | 旧试点使用的历史成分区间与交易日历快照，截至 2026-05-25。超过该日期需要另补股票池与日历。 |
| observations-20261125.zip | 510500 盘中期权链、计算结果、单家公司财务接口观测及对应原始响应。归档内的 iv-quotes.jsonl 可作为组合实验的单日期权输入。 |

DuckDB 的 `research_daily` 保留原始价格和估值字段。交易信号所需的复权收益、历史涨跌停价、停复牌、现金分红及当时成分应按策略时期处理。按公告日使用财务指标时，还需核对公告时刻和修订版本。

## 更新

```sh
python -m experiments.market_snapshot --refresh-public --catalog
python -m experiments.legacy_pilot.risk_source_probe --rounds 1 --underlying 510500
```

第一条命令解析公开上游当前版本，下载并校验日线、估值、财务指标、分红四张表，写入以该版本号命名的本地目录。重复运行同一版本复用已验文件。第二条命令采集运行当时的期权和财务快照并写到独立目录。新版本可用 `python -m experiments.market_store publish datasets/market/<revision> backup:quant --manifest datasets/market/<revision>/manifest.json` 发布。该版本仅含四张公开表；回放若需同期成分、日历和期权，还需对应版本的输入。当前采用按需更新。

正式反向 DCF 仍需历史财报版本、净债务及再投资等经营假设。IV30 需要同一观察时点的历史期权链及预热，单次观测只验证采集流程。验收脚本在 2024 年 1 月首周对 600160 执行固定的买卖指令，检验读取、撮合、费用和期末持仓。
