"""Local HTML reports and exportable charts; no notebook or web service."""
from __future__ import annotations

import base64
from html import escape
import json
from pathlib import Path

import pandas as pd

from .artifacts import atomic_write


def _table(frame):
    return ("<p>No observations met the declared requirements.</p>" if frame.empty else
            '<div class="table">' + frame.to_html(index=False, escape=True, border=0,
                     float_format=lambda value: f"{value:.5g}", na_rep="—") + "</div>")


def _plot_nav(curves, output):
    if not curves:
        return ""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]}, layout="constrained")
    for name, (daily, initial_cash) in curves.items():
        axes[0].plot(daily.date, daily.nav / initial_cash, label=name, linewidth=1.4)
        axes[1].plot(daily.date, daily.drawdown * 100, label=name, linewidth=1.2)
    axes[0].set_ylabel("NAV / initial cash")
    axes[1].set_ylabel("Drawdown (%)")
    axes[0].set_title("Independent split portfolios, after configured costs")
    axes[0].legend(loc="best")
    for axis in axes:
        axis.grid(alpha=0.2)
    path = output / "nav.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'<img alt="Net asset value and drawdown by split" src="data:image/png;base64,{encoded}">'


def _plot_decay(diagnostics, output):
    if diagnostics.empty or diagnostics.mean_rank_ic.notna().sum() == 0:
        return ""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    fig, axis = plt.subplots(figsize=(10, 4), layout="constrained")
    for (split, factor), part in diagnostics.groupby(["split", "factor"], sort=True):
        part = part.sort_values("horizon")
        axis.plot(part.horizon, part.mean_rank_ic, marker="o", label=f"{split}: {factor}")
    axis.axhline(0, color="black", linewidth=0.7)
    axis.set(xlabel="Forward-return horizon (sessions)", ylabel="Mean daily rank IC",
             title="Predictive association by holding horizon")
    axis.grid(alpha=0.2)
    axis.legend(loc="best", fontsize=8)
    path = output / "ic_decay.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'<img alt="Rank IC by horizon" src="data:image/png;base64,{encoded}">'


def render_report(output, *, manifest, diagnostics, backtests, curves):
    output = Path(output)
    title = escape(manifest["config"]["name"])
    chart = _plot_nav(curves, output)
    decay = _plot_decay(diagnostics, output)
    sources = pd.DataFrame(manifest["data"]["files"])
    splits = pd.DataFrame(manifest["evaluated_splits"])
    links = []
    for path in sorted(output.rglob("*.csv")):
        relative = path.relative_to(output).as_posix()
        links.append(f'<li><a href="{escape(relative, quote=True)}">{escape(relative)}</a></li>')
    assumptions = escape(json.dumps(manifest.get("execution_assumptions", {}), ensure_ascii=False, indent=2))
    config = escape(json.dumps(manifest["config"], ensure_ascii=False, indent=2))
    html = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | FactorLab</title><style>
body{{font:15px/1.6 system-ui,sans-serif;color:#182333;background:#f5f7fa;margin:0}}
main{{max-width:1120px;margin:0 auto;padding:32px}}section{{background:white;padding:24px;margin:20px 0;border-radius:10px}}
h1,h2{{line-height:1.3}}h1{{margin-bottom:8px}}.muted{{color:#586678}}.table{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:9px;text-align:right;border-bottom:1px solid #e1e6ed}}
th{{background:#eef3f8}}td:first-child,th:first-child{{text-align:left}}img{{max-width:100%;height:auto}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:16px}}a{{color:#1b568c}}
</style><main><h1>{title}</h1>
<p class="muted">Local experiment · {escape(manifest['run_id'])} · {escape(manifest['started_at'])}</p>
<section><h2>Scope and timing</h2>{_table(splits)}
<p>Universe: <strong>{escape(manifest['config']['universe']['mode'])}</strong>.
Availability mode: <strong>{escape(manifest['data']['availability_mode'])}</strong>.
Historical reconstruction: <strong>{manifest['data']['historical_reconstruction']}</strong>.</p>
<p>Availability timestamps are declared by the input source. A reconstructed public-history run
is distinct from a record of data actually captured at each historical signal.</p>
<p>Each split starts with fresh cash. Features may use earlier observations for warm-up.
Labels extending beyond a split are purged. Parameter scans exclude test and exploratory splits.</p></section>
<section><h2>Factor diagnostics</h2>{_table(diagnostics)}{decay}
<p>Rank IC is a daily cross-sectional rank correlation. ICIR here is unannualized;
overlapping forward returns do not provide independent samples. Group spread is the
high-score minus low-score mean forward return, before trading costs and constraints.
It is a diagnostic difference, not an executable short portfolio.</p></section>
<section><h2>Portfolio results after costs</h2>{_table(backtests)}{chart}
<p>The daily profile executes at the next session open. Order quantities use prior-session
NAV and raw closing prices and are locked before fills. Capacity uses previous-session
volume as an estimate, not actual opening-auction liquidity. Costs are declared constant
research assumptions. Cash-dividend taxes are not modelled; unsupported held-security
corporate actions stop the run. Inspect rejection counts and order ledgers.</p></section>
<section><h2>Inputs</h2>{_table(sources)}
<p>Source identity: {escape(manifest['data']['source_id'])}.</p></section>
<section><h2>Files</h2><ul><li><a href="manifest.json">Manifest, fingerprints and environment</a></li>
<li><a href="config.resolved.json">Resolved configuration</a></li>{''.join(links)}</ul></section>
<section><h2>Execution assumptions</h2><pre>{assumptions}</pre></section>
<section><h2>Configuration</h2><pre>{config}</pre></section>
</main></html>'''
    atomic_write(output / "report.html", html)
    return output / "report.html"
