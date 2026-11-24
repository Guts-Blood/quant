"""Render the frozen 2026 comparison, quarterly attribution and equity curves."""
import json

import pandas as pd
from matplotlib import pyplot as plt
from matplotlib import dates as mdates, ticker
from matplotlib import font_manager
from pathlib import Path

from .collect_2026 import DIR, PREV, END

OUT=DIR/"asof_reviewed"
NAMES={"value":"盈利收益率（E/P）","blend":"价值＋低波","lowvol":"20 日低波动",
       "reversal":"5 日反转","momentum":"中期动量","control":"固定随机排序对照"}


def pct(x):
    return f"{100*x:+.2f}%"


def nav(strategy):
    return pd.read_csv(OUT/"ytd"/"capital-100000"/f"2026-{strategy}-5bp"/"nav.csv",parse_dates=["date"]).set_index("date")


def chart():
    chinese=Path("C:/Windows/Fonts/msyh.ttc")
    if chinese.exists():
        font_manager.fontManager.addfont(str(chinese))
        plt.rcParams["font.family"]=font_manager.FontProperties(fname=chinese).get_name()
    plt.rcParams["axes.unicode_minus"]=False
    blend=nav("blend")
    returns=pd.DataFrame({"盈利收益率":nav("value").ret,"价值＋低波":blend.ret,
                          "同池等权":blend.pool_return,"中证500全收益":blend.index_return})
    levels=(1+returns).cumprod()-1
    levels.loc[pd.Timestamp(PREV)]=0
    levels=levels.sort_index()
    fig,ax=plt.subplots(figsize=(9,4.2),layout="constrained")
    for col,color in zip(levels,["#16805c","#164991","#8a6231","#707988"]):
        ax.plot(levels.index,levels[col],label=col,color=color,lw=2 if col in ("盈利收益率","价值＋低波") else 1.4)
    ax.axhline(0,color="#89959f",lw=.65)
    lo,hi=ax.get_ylim()
    ax.set_ylim(lo,hi+.065*(hi-lo))
    cutoff=pd.Timestamp("2026-09-18")
    ax.axvspan(cutoff,levels.index[-1],color="#e9edf6",alpha=.55,zorder=0)
    ax.set_title("2026 年初至 11 月 23 日累计收益",loc="left")
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1,decimals=0))
    ax.grid(axis="y",alpha=.2)
    ax.spines[["top","right"]].set_visible(False)
    ax.legend(ncol=4,frameon=False,loc="upper left",fontsize=9)
    ax.set_xlabel("初始 10 万元。阴影内使用实际五分钟成交量，此前按下单前半小时成交量的 1/6 估算。",fontsize=9)
    fig.savefig(OUT/"equity.png",dpi=160)
    plt.close(fig)


def main():
    results=pd.read_csv(OUT/"comparison.csv")
    base=results[results.slippage_bp.eq(5)].set_index(["scenario","capital","strategy"])
    rows=["| 策略 | 年内估算 · 10 万 | 年内估算 · 100 万 | 近 41 日五分钟 · 10 万 | 近 41 日五分钟 · 100 万 | 10 万元年内最大回撤 |",
          "|---|---:|---:|---:|---:|---:|"]
    for key,title in NAMES.items():
        values=[pct(base.loc[(s,c,key),"net"]) for s in ("ytd","exact") for c in (100000,1000000)]
        dd=base.loc[("ytd",100000,key),"drawdown"]
        rows.append("| "+" | ".join([title,*values,f"{dd:.2%}"])+" |")
    for metric,title in [("pool","同池等权"),("index","中证500全收益")]:
        a,b=(pct(base.loc[(s,100000,"blend"),metric]) for s in ("ytd","exact"))
        rows.append(f"| {title} | {a} | {a} | {b} | {b} | — |")
    table="\n".join(rows)
    blend=nav("blend")
    quarter=(1+blend[["ret","pool_return","index_return"]]).groupby(blend.index.quarter).prod()-1
    qtable="\n".join(f"| {i} 季度{'（至 11/23）' if i==4 else ''} | {pct(r.ret)} | {pct(r.pool_return)} | {pct(r.index_return)} |" for i,r in quarter.iterrows())
    cov=pd.read_csv(OUT/"eligible_windows.csv")
    recent=cov[cov.date.ge("2026-09-18")]
    early=cov[cov.date.lt("2026-09-18")]
    chart()

    lookup=lambda scenario,capital,strategy: pct(base.loc[(scenario,capital,strategy),"net"])
    old=pd.read_csv(DIR/"asof_results"/"comparison.csv")
    old=old[old.slippage_bp.eq(5)].set_index(["scenario","capital","strategy"])
    compare=[]
    for capital in (100000,1000000):
        for strategy in ("value","blend"):
            key=("ytd",capital,strategy)
            compare.append(f"| {capital//10000} 万 | {NAMES[strategy]} | {pct(old.loc[key,'net'])} | {lookup(*key)} | {lookup('ytd_thin',capital,strategy)} |")
    compare="\n".join(compare)
    audit=json.loads((OUT/"timing_audit.json").read_bytes())
    report=f"""# 2026 年 A 股日频因子回测与时点审计

2026 年 11 月 24 日复核。回测区间为 1 月 5 日至 11 月 23 日，214 个交易日。两档资金分别从现金起步。各策略以同一窗口提交固定数量的委托，每天至多涉及十只股票。

{table}

![修正版累计收益](equity.png)

## 修订对年内结果的影响

| 初始资金 | 策略 | 旧版 | 修正版 | 修正版前段容量再减半 |
|---|---|---:|---:|---:|
{compare}

旧版让收盘后的整日行情校验参与决定盘中成交，早段使用下单后半小时成交量估计首五分钟容量，且同一批后续买单会随前单反馈改变。修正版在窗口开始前锁定股票和手数。上午执行结果在下午下单前可知。全日行情核对用于事后诊断。

- 1 月 5 日至 9 月 17 日，执行价格取窗口开盘价加滑点，首五分钟的容量按**下单前**完整半小时成交量的六分之一估算。真实队列、当时的首五分钟成交量与实际可成交价格仍未验证。
- 9 月 18 日至 11 月 23 日，使用随后五分钟的实际成交额除以成交量作为成交均价，叠加滑点，并以窗口实际量的 1% 限制整笔成交。窗口结束才能确定该单结果。表中近 41 日从空仓独立回放。
- 正常涨跌停参考价用前一日收盘价和已公告的分配方案计算。与收盘后倒算的参考价相比，两个除息日相差 0.01 元。逐日复核持仓、税费、现金、涨跌停、窗口上限与换股锁定。基准沿用同池等权与中证 500 全收益，策略按实际模拟仓位计收益。

## 退市、停牌与股票池

2026 年历史成分按有效日进入和退出。8 月 5 日之前有逐日快照，此后按已查到的两次临时调样重建，11 月 23 日与官方全名单核对。逐日样本按既有板块和代码哈希规则并要求本地历史覆盖。按板块和哈希本应有 {audit['candidate_hash_count']} 只，实际覆盖 {audit['sample_count']} 只，缺 {len(audit['excluded'])} 只。未覆盖组与本年度交易所退市记录的交集为空；流动性、行业和历史覆盖偏差仍可能影响收益。

将模拟成交与沪深交易所官方退市名册核对，修正版 72 个动态池情景累计 {audit['ledger_checks']['asof_reviewed']['fills']:,} 笔成交，摘牌当日及以后成交 0 笔。同一成交可能出现在多个资金或滑点情景中。历史成分与本年度退市名册的交集只有东兴证券。它自 9 月 15 日起停牌，10 月 19 日摘牌，按每股换 0.4376 股中金公司计入持仓，按整股向下取整；登记尾差是模拟假设。10 月 29 日起才可卖换得的普通流通股。未解决的持仓缺价会令回放报错；强制退市后转板、清算与回收价值仍需逐案补入。

[上交所终止上市名单](https://www.sse.com.cn/assortment/stock/list/delisting/) · [深交所终止上市名单](https://www.szse.cn/market/stock/suspend/index.html) · [东兴摘牌及换股公告](https://static.cninfo.com.cn/finalpage/2026-10-13/1225602055.PDF) · [新增股份上市公告](https://static.cninfo.com.cn/finalpage/2026-10-27/1225627350.PDF)

## 未来信息检查

选股取前一交易日的因子排名。移除所有未来五日收益标签后，逐日排名完全相同。把归档数据分别截到 6 月 15 日、10 月 19 日，此前的排名、估值用行情和执行窗口一致。另在三个日期模拟该日所有下午买单被拒，提交的 {audit['buy_orders_compared']} 笔买单的股票和股数保持一致。抽检删除当日窗口后的行情，亦不改变该窗口的撮合输入。

历史市盈率来自事后下载的按交易日估值表。缺少当时发布版本和历次财报修订的档案，以上代码试验无法证明每个历史 PE 都在当时实际可得。ST 名称变更亦缺全市场完整历史。前瞻模拟和扩大股票覆盖之前，本报告支持候选因子研究。

## 分季度表现

| 期间 | 价值＋低波 · 10 万元 | 同池等权 | 中证500全收益 |
|---|---:|---:|---:|
{qtable}

[修正版各情景](comparison.csv) · [审计结果与缺失股票](timing_audit.json) · [10 万元价值策略逐笔委托](ytd/capital-100000/2026-value-5bp/orders.csv) · [输入文件校验值](sources.json) · [旧版结果](../asof_results/comparison.csv)
"""
    (OUT/"report.md").write_text(report,encoding="utf-8")


if __name__=="__main__":
    main()
