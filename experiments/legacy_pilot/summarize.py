"""Audit the generated ledgers and summarize timed holdouts without fitting any factors."""

import json

import numpy as np
import pandas as pd

from .study import NAMES, OUT

LABEL = dict(
    reversal="5 日反转",
    momentum="中期动量",
    lowvol="20 日低波动",
    value="盈利收益率",
    blend="价值＋低波动",
    control="固定哈希对照",
)


def uncertainty(segments):
    # Resample paired active returns, preserving local serial dependence; do not bridge the year reset.
    rng = np.random.default_rng(20261123)
    sims = np.zeros(4000)
    n = sum(len(s) for s in segments)
    for s in segments:
        starts = rng.integers(0, len(s), size=(len(sims), (len(s) + 19) // 20))
        indexes = (starts[:, :, None] + np.arange(20)) % len(s)
        draws = s[indexes.reshape(len(sims), -1)[:, : len(s)]]
        sims += draws.sum(axis=1) * 252 / n
    return np.quantile(sims, [0.025, 0.975])


def pct(x):
    return f"{x*100:.2f}%"


def audit(folder, capital, prices, f):
    tab = pd.read_csv(folder / "comparison.csv")
    assert len(tab) == 36
    data = tab.set_index(["strategy", "year", "slippage_bp"])
    brief = []
    for name in NAMES:
        differences = []
        for year in (2022, 2023, 2024):
            for bp in (5, 10):
                path = folder / f"{year}-{name}-{bp}bp"
                nav = pd.read_csv(path / "nav.csv")
                log = pd.read_csv(path / "orders.csv")
                fills = log[log.reason == "filled"]
                cash = float(capital)
                holdings = {}
                batches = dict(tuple(log.groupby("date", sort=False)))
                for n in nav.itertuples():
                    day = n.date
                    orders = batches.get(day, log.iloc[:0])
                    for code, qty in json.loads(n.share_credits).items():
                        holdings[code] = holdings.get(code, 0) + qty
                    sellable = holdings.copy()
                    assert orders.symbol.nunique() <= 10 and not orders.duplicated(["symbol", "window"]).any()
                    for row in orders.itertuples():
                        assert pd.Timestamp(row.known_at) < pd.Timestamp(day + "T" + row.window + "+08:00")
                        if row.reason == "filled":
                            sign = 1 if row.side == "buy" else -1
                            assert (row.side, row.window) in (("buy", "14:00"), ("sell", "11:00"))
                            if sign < 0:
                                assert sellable.get(row.symbol, 0) >= row.shares
                                sellable[row.symbol] -= row.shares
                            holdings[row.symbol] = holdings.get(row.symbol, 0) + sign * row.shares
                            cash -= sign * row.gross + row.fees
                        assert abs(cash - row.cash_after) < 0.001 and cash >= 0
                    assert abs(n.cash - cash) < 0.001 and json.loads(n.positions) == {
                        c: q for c, q in holdings.items() if q
                    }
                assert np.isclose(np.prod(1 + nav.ret) - 1, data.loc[name, year, bp].net)
                for point in nav.itertuples():
                    holdings = json.loads(point.positions)
                    assert len(holdings) <= 10 and point.attempted <= 10
                    value = (
                        point.cash
                        + point.dividend_reserve
                        + sum(qty * prices[point.date, code] for code, qty in holdings.items())
                    )
                    assert abs(value - point.equity) < 0.001
                assert len(fills) == data.loc[name, year, bp].fills
                if year >= 2023 and bp == 5:
                    differences.append((nav.ret - nav.pool_return).to_numpy())
        if name == "control":
            continue
        lo, hi = uncertainty(differences)
        ok = all(
            data.loc[name, y, b].excess_pool > 0 and data.loc[name, y, b].net > data.loc["control", y, b].net
            for y in (2023, 2024)
            for b in (5, 10)
        )
        brief.append(
            dict(
                strategy=name,
                passed=ok,
                rank_ic=f.rank_ic.mean()[name],
                gross_5d_quintile_spread=f.quintile_spread.mean()[name],
                active_ci_low=lo,
                active_ci_high=hi,
            )
        )
    pd.DataFrame(brief).to_csv(folder / "holdout_evidence.csv", index=False)
    return data, brief


def main():
    universe = pd.read_csv(OUT / "universe.csv").query("date >= '2022-01-01'")
    quality = json.loads((OUT / "quality.json").read_bytes())
    prices = pd.read_csv(OUT / "daily.csv").set_index(["date", "symbol"]).close.to_dict()
    factors = pd.read_csv(OUT / "factor_daily.csv")
    f = factors[factors.date >= "2023"].groupby("strategy")
    data, brief = audit(OUT, 1000000, prices, f)
    small, small_evidence = audit(OUT / "capital-100000", 100000, prices, f)
    lines = [
        "# A 股常见因子历史检验",
        "",
        "按预设的 100 万元资金和成交规则，五组策略均未在两个留出年度持续跑赢同池等权。"
        "资金规模复核给出一个研究候选：10 万元的价值＋低波动组合在 2023 与 2024 年均取得费后超额；"
        "2024 年按上日实际股票仓位匹配基准后，优势约 0.12 个百分点。随后单独查看的 2025 年里，"
        "得分前十股票的未来五日平均毛收益低于同池，暂缓把该组合用于交易。",
        "",
        f"2022 年用于开发，2023、2024 年分别从空仓检验；一共 {quality['sessions']} 个交易日。"
        f"历史中证500成分快照与本地日线交集按证券代码固定抽样，共 {quality['sample_codes']} 个历史代码。"
        f"日均 {universe.eligible.mean():.1f} 只股票可选，范围 {universe.eligible.min()} 至 {universe.eligible.max()} 只。"
        f"前一日可选且当日交易的 {quality['expected_eligible_windows']:,} 个窗口，通过价格、成交量校验的有 "
        f"{quality['matched_eligible_windows']:,} 个。",
        "",
        "## 费后组合表现",
        "",
        "各年独立投入 100 万元。表内净收益、最大回撤均按每日收盘估值，超额按策略费后收益率减同池等权收益率计算。"
        "同池基准使用前一日可选股票等权、每日收盘再平衡的理论收益；固定哈希组合与因子组共用完整交易约束。",
        "",
        "| 选股规则 | 2022 净收益 | 2023 净收益 | 2023 同池超额 | 2024 净收益 | 2024 同池超额 | 2024 最大回撤 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for k in NAMES:
        a, b, c = [data.loc[k, y, 5] for y in (2022, 2023, 2024)]
        lines.append(
            "| "
            + " | ".join(
                [LABEL[k], *map(pct, (a.net, b.net, b.excess_pool, c.net, c.excess_pool, c.drawdown))]
            )
            + " |"
        )
    ctl = [data.loc["control", y, 5] for y in (2022, 2023, 2024)]
    lines += [
        "",
        f"同池等权三个年度依次为 {'、'.join(pct(row.pool) for row in ctl)}。"
        f"中证500全收益指数同期为 {'、'.join(pct(row['index']) for row in ctl)}。"
        "各策略相对指数的收益、仓位、成交笔数和换手见 [comparison.csv](comparison.csv)。",
        "",
        "## 信号与成本",
        "",
        "| 规则 | 2023–24 平均 Rank IC | 前后五分组 5 日毛收益差 | 双倍滑点后 2023 / 2024 同池超额 | 年化日均主动收益的 95% 区间 | 两年均过初筛 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in brief:
        k = row["strategy"]
        lines.append(
            f"| {LABEL[k]} | {row['rank_ic']:.3f} | {pct(row['gross_5d_quintile_spread'])} | "
            f"{pct(data.loc[k,2023,10].excess_pool)} / {pct(data.loc[k,2024,10].excess_pool)} | "
            f"{pct(row['active_ci_low'])} 至 {pct(row['active_ci_high'])} | {'是' if row['passed'] else '否'} |"
        )
    lines += [
        "",
        "## 资金规模对成交的影响",
        "",
        "看到百万资金组合的成交量拒单后，使用相同数据、排名和费率，将虚拟资金改为 10 万元。"
        "这是同一批历史上的事后敏感性分析。表中超额为相对同池等权的百分点差。",
        "",
        "| 10 万元组合 | 2023 净收益 | 2023 超额 | 2024 净收益 | 2024 超额 | 2024 最大回撤 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for k in NAMES:
        a, b = [small.loc[k, y, 5] for y in (2023, 2024)]
        lines.append(
            "| "
            + " | ".join([LABEL[k], *map(pct, (a.net, a.excess_pool, b.net, b.excess_pool, b.drawdown))])
            + " |"
        )
    lo, hi = next(
        (r["active_ci_low"], r["active_ci_high"]) for r in small_evidence if r["strategy"] == "blend"
    )
    lines += [
        "",
        f"价值＋低波动在 10 万元规模、双倍滑点下的同池超额为 "
        f"{pct(small.loc['blend',2023,10].excess_pool)}、{pct(small.loc['blend',2024,10].excess_pool)}。"
        f"该组合年化日均主动收益的分块重采样区间为 {pct(lo)} 至 {pct(hi)}。"
        "2024 年，100 万元与 10 万元的该组合分别成交 44/1,123 笔、108/135 笔。"
        "全成或全拒、每单最多占随后五分钟成交量 1% 的假设，使资金量变化同时影响成交资产与持仓路径。"
        "逐日仓位匹配基准使用上日末股票仓位乘当日同池基准收益，其余现金零收益。"
        "[10 万元完整比较](capital-100000/comparison.csv)",
    ]
    lines += [
        "",
        "Rank IC 衡量当日因子排序与随后五个交易日收盘复权收益的截面相关性。分组收益差取排名最高和最低各五分之一的平均收益差，使用收盘价且未扣费。两项诊断只在年度内有完整未来区间时计算。"
        "区间按日组合收益减同池收益、分年份以 20 个交易日为块重采样 4,000 次，衡量年化算术差的不确定性。五项并列试验共用样本，区间未经多重检验校正。",
        "",
        "## 规则与数据边界",
        "",
        "11:00 最多卖五只，14:00 最多新开五只。每日使用前收打分，最多持十只，原有持仓跌出前二十才退出。"
        "排除 ST、停牌、过去 20 日日均成交额低于 3,000 万元以及近 61 个交易日缺少可比交易价格的股票。"
        "实施公告发布后停止新买相关股票并按退出规则减仓。遇到尚未能解释的复权跳变且有持仓时中止验证。"
        "按窗口后第一根五分钟线开盘价加滑点成交，单笔上限是该段成交量的 1%，盘口排队及部分成交仍是近似。",
        "",
        "基础单边滑点 5 个基点，压力测试 10 个基点；佣金 2.5 个基点、最低 5 元。"
        "过户费在 2022-04-29 由双边 0.2 降到 0.1 个基点，卖出印花税在 2023-08-28 由 10 降到 5 个基点。"
        "[财政部与税务总局公告](https://shanxi.chinatax.gov.cn/web/detail/sx-11400-545-1780448) · [过户费调整通知摘要](https://jrj.wuhan.gov.cn/ynzx_57/xwzx/202204/t20220429_1964160.shtml)",
        "",
        "中证500成分按档案中已到日期的快照更新，档案的半年末快照与官方调整生效日可能存在时差。"
        "本地复权日线覆盖有限，沪深主板与创业板以外的证券未进入实验；剩余样本存在覆盖偏差。"
        "盈利收益率使用历史日线供应商记录的当日 PE-TTM，历史修订与公告时点尚未逐份重建。"
        "结果尚未剥离行业、市值与市场贝塔影响，两个留出年份均属回顾性检验。",
        "",
        "[历史成分和分钟数据](https://huggingface.co/datasets/OpenFinMind/stock_dataset) · "
        "[中证500全收益指数数据](https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=H00905&startDate=20231229&endDate=20241231) · "
        "[样本协议](../../../experiments/legacy_pilot/protocol.md) · [输入文件校验值](sources.json)",
    ]
    holdout = pd.read_csv(OUT / "disjoint_daily.csv")
    grouped = holdout.groupby([holdout.date.str[:4], "strategy"])
    lines += [
        "",
        "## 另一组股票的复核",
        "",
        "在历史成分快照与本地日线交集的其他股票重复排序。这批股票与分钟回测样本不重合。"
        "剔除 ST、停牌、流动性不足和价格历史不足的股票。此项使用复权收盘数据，未应用分红实施公告回避规则，衡量信号的跨股票延续性。",
        "",
        "| 因子 | 2023 Rank IC | 2024 Rank IC | 2023 五分组毛收益差 | 2024 五分组毛收益差 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in NAMES[:-1]:
        a, b = (grouped.get_group((str(y), name)) for y in (2023, 2024))
        lines.append(
            f"| {LABEL[name]} | {a.ic.mean():.3f} | {b.ic.mean():.3f} | {pct(a.spread.mean())} | {pct(b.spread.mean())} |"
        )
    gaps = pd.read_csv(OUT / "eligible_window_gaps.csv")
    repairs = pd.read_csv(OUT / "price_repairs.csv")
    lines += [
        "",
        "## 行情与公司行动审计",
        "",
        f"已用分钟开高低收或独立不复权日线核对并修复 {len(repairs):,} 个股票日的价格换算。"
        f"在满足选股条件的窗口中，按年缺失数量分别为 {dict(gaps.groupby(gaps.date.str[:4]).size())}（未列年份为零）。"
        "缺失窗口直接拒单，同池等权基准仍计入相应股票的日收益。"
        "现金派息如因流动性不足未能在除息前退出，按税前派现额的 80% 计为应收、当年冻结，"
        "独立年度内永不参与再买入；导出 daily.csv 的 pay_date 是资金冻结模拟参数。"
        "有持仓跨越送转的实际案例为 2022-05-11 苏州银行，当日按登记股份十转一入账。"
        "无法解释的持仓复权事件会中止回放。"
        "[苏州银行实施公告](https://static.cninfo.com.cn/finalpage/2022-04-30/1213260767.PDF) · "
        "[原始价修复](price_repairs.csv) · [窗口缺失明细](eligible_window_gaps.csv)",
    ]
    future = pd.read_csv(OUT / "forward_2025_daily.csv")
    lines += [
        "",
        "## 2025 年的独立时间检验",
        "",
        f"冻结价值＋低波动的两个因子和等权组合后，使用 2025 年历史成分快照与本地日线交集做收盘排序复核，"
        f"共有 {len(future)} 个有完整五日结果的信号日，平均每天 {future.observed.mean():.0f} 只股票。"
        f"平均 Rank IC 为 {future.ic.mean():.4f}，前五分组减后五分组的平均五日毛收益差为 {pct(future.spread.mean())}，"
        f"排名前十相对同池平均为 {pct(future.top10_minus_pool.mean())}。"
        "信号日相互重叠，报告值是平均五日差，不能据此推导年度可交易收益。"
        "2025 年尚缺这批股票的两个窗口成交回放，操作成本、持仓缓冲和行业暴露尚待检验。"
        "[2025 年逐日结果](forward_2025_daily.csv)",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(pd.DataFrame(brief).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
