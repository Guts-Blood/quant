"""Deterministic research IV30 and reverse DCF; no network or account-state writes.

IV30 is a local variance-strip estimate, not an official exchange index.
Reverse DCF reports bracketed numerical roots, never a unique market forecast.
All rates and volatilities use decimal units. Monetary inputs must share a unit.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

METHOD_VERSION = "risk_expectations_v1"
SECONDS_PER_YEAR = 365 * 86400


def _finite(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name}: boolean is not a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: numeric value required") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name}: finite value required")
    return result


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return result


def _variance_strip(rows, asof, rate, min_side_strikes):
    """Discrete option-strip variance with two-consecutive-zero-bid wing cutoff."""
    pairs = defaultdict(dict)
    for row in rows:
        if row["kind"] in pairs[row["strike"]]:
            raise ValueError("duplicate strike/type; check adjusted contracts")
        pairs[row["strike"]][row["kind"]] = row
    paired = [k for k, p in pairs.items()
              if set(p) == {"call", "put"} and all(q["bid"] > 0 for q in p.values())]
    if not paired:
        raise ValueError("no liquid call/put pair for forward estimation")
    anchor = min(paired, key=lambda k: (abs(pairs[k]["call"]["mid"]
                                            - pairs[k]["put"]["mid"]), k))
    expiry = rows[0]["expiry"]
    t = (expiry - asof).total_seconds() / SECONDS_PER_YEAR
    forward = anchor + math.exp(rate * t) * (
        pairs[anchor]["call"]["mid"] - pairs[anchor]["put"]["mid"])
    below = [k for k in pairs if k <= forward]
    if forward <= 0 or not below:
        raise ValueError("forward outside usable strike range")
    k0 = max(below)
    if set(pairs[k0]) != {"call", "put"}:
        raise ValueError("missing call or put at K0")
    if any(q["bid"] <= 0 for q in pairs[k0].values()):
        raise ValueError("zero bid at K0")
    selected = [(k0, sum(q["mid"] for q in pairs[k0].values()) / 2)]
    counts = {}
    for kind, strikes in (
        ("put", sorted((k for k in pairs if k < k0), reverse=True)),
        ("call", sorted(k for k in pairs if k > k0)),
    ):
        zero_run = 0
        count = 0
        for strike in strikes:
            quote = pairs[strike].get(kind)
            if quote is None:
                continue
            if quote["bid"] == 0:
                zero_run += 1
                if zero_run == 2:
                    break
                continue
            zero_run = 0
            selected.append((strike, quote["mid"]))
            count += 1
        counts[kind] = count
        if count < min_side_strikes:
            raise ValueError(f"insufficient {kind} wing strikes")
    selected.sort()
    integral = 0.0
    for i, (strike, premium) in enumerate(selected):
        if i == 0:
            delta_k = selected[1][0] - strike
        elif i == len(selected) - 1:
            delta_k = strike - selected[i - 1][0]
        else:
            delta_k = (selected[i + 1][0] - selected[i - 1][0]) / 2
        integral += delta_k * premium / (strike * strike)
    variance = 2 * math.exp(rate * t) * integral / t - (forward / k0 - 1) ** 2 / t
    if not math.isfinite(variance) or variance <= 0:
        raise ValueError("nonpositive or nonfinite strip variance")
    return {"expiry": expiry.isoformat(), "years": t, "variance": variance,
            "forward": forward, "k0": k0, "rate": rate,
            "strike_count": len(selected), "wing_counts": counts,
            "min_strike": selected[0][0], "max_strike": selected[-1][0]}


def local_iv30(quotes, *, asof, rate, max_quote_age_seconds=300,
               max_relative_spread=0.5, min_side_strikes=3):
    """Compute 30-calendar-day IV from one underlying's synchronized option chain.

    Each quote requires contract_id, underlying, kind, strike, bid, ask, expiry,
    quoted_at and adjusted (bool). Missing adjustment status is rejected.
    rate is an explicit annual continuously compounded rate assumption.
    Bad rows are logged, not filled with zeros. No maturity extrapolation.
    """
    asof = _time(asof)
    rate = _finite(rate, "rate")
    max_age = _finite(max_quote_age_seconds, "max_quote_age_seconds")
    spread_limit = _finite(max_relative_spread, "max_relative_spread")
    if not -0.1 <= rate <= 1 or max_age < 0 or spread_limit <= 0:
        raise ValueError("invalid rate, age or spread policy")
    if type(min_side_strikes) is not int or min_side_strikes < 1:
        raise ValueError("min_side_strikes must be a positive integer")
    groups = defaultdict(list)
    excluded = []
    underlyings = set()
    ages = []
    for position, raw in enumerate(quotes):
        contract = str(raw.get("contract_id", position))
        try:
            if raw.get("adjusted") is not False:
                raise ValueError("adjusted or unverified contract")
            underlying = str(raw["underlying"])
            if not underlying:
                raise ValueError("missing underlying")
            expiry, quoted_at = _time(raw["expiry"]), _time(raw["quoted_at"])
            age = (asof - quoted_at).total_seconds()
            if age < 0 or age > max_age:
                raise ValueError("future or stale quote")
            if expiry <= asof:
                raise ValueError("expired contract")
            kind = raw["kind"]
            strike = _finite(raw["strike"], "strike")
            bid, ask = _finite(raw["bid"], "bid"), _finite(raw["ask"], "ask")
            if kind not in {"call", "put"} or strike <= 0 or bid < 0 or ask <= 0 or bid > ask:
                raise ValueError("invalid side, strike or crossed quote")
            mid = (bid + ask) / 2
            if bid > 0 and (ask - bid) / mid > spread_limit:
                raise ValueError("spread exceeds policy")
            groups[expiry].append(dict(kind=kind, strike=strike, bid=bid, mid=mid,
                                       expiry=expiry, contract_id=contract))
            underlyings.add(underlying)
            ages.append(age)
        except (KeyError, TypeError, ValueError) as exc:
            excluded.append({"contract_id": contract, "reason": str(exc)})
    if len(underlyings) > 1:
        raise ValueError("mixed underlyings are not a single volatility index")
    output = {"method_version": METHOD_VERSION, "status": "unavailable",
              "asof": asof.isoformat(), "underlying": next(iter(underlyings), None),
              "rate_assumption": rate, "excluded_quotes": excluded,
              "max_quote_age_seconds": max(ages, default=None), "excluded_expiries": []}
    terms = []
    for expiry, rows in sorted(groups.items()):
        try:
            terms.append(_variance_strip(rows, asof, rate, min_side_strikes))
        except ValueError as exc:
            output["excluded_expiries"].append({"expiry": expiry.isoformat(), "reason": str(exc)})
    target = 30 / 365
    lower = [term for term in terms if term["years"] <= target]
    upper = [term for term in terms if term["years"] >= target]
    if not lower or not upper:
        output["reason"] = "no usable maturity bracket for 30 days"
        return output
    near, far = lower[-1], upper[0]
    if near["years"] == far["years"]:
        variance = near["variance"]
        used = [near]
    else:
        t1, t2 = near["years"], far["years"]
        total_variance = (t1 * near["variance"] * (t2 - target)
                          + t2 * far["variance"] * (target - t1)) / (t2 - t1)
        variance = total_variance / target
        used = [near, far]
    flags = ["local_research_estimate", "finite_strike_coverage"]
    if (far["years"] - near["years"]) * 365 > 62:
        flags.append("wide_expiry_gap")
    if excluded:
        flags.append("quotes_excluded")
    output.update(status="ok", iv30=math.sqrt(variance), iv30_percent=100 * math.sqrt(variance),
                  annualized_variance=variance, terms=used, quality_flags=flags)
    return output


def _dcf_parameters(model):
    required = ("base_revenue", "tax_rate", "sales_to_capital", "discount_rate",
                "terminal_growth", "terminal_margin", "terminal_roic")
    p = {key: _finite(model[key], key) for key in required}
    margins = [_finite(x, "operating_margin") for x in model["operating_margins"]]
    if not 1 <= len(margins) <= 30 or any(not 0 <= m <= 1 for m in margins):
        raise ValueError("provide 1..30 nonnegative operating margins")
    if p["base_revenue"] <= 0 or p["sales_to_capital"] <= 0:
        raise ValueError("positive revenue and sales-to-capital required")
    if not 0 <= p["tax_rate"] <= 1 or not 0 <= p["terminal_margin"] <= 1:
        raise ValueError("invalid tax or terminal margin")
    if not 0 < p["discount_rate"] <= 1:
        raise ValueError("discount rate must be in (0, 1]")
    if not 0 <= p["terminal_growth"] < p["discount_rate"]:
        raise ValueError("terminal growth must be nonnegative and below discount rate")
    if p["terminal_roic"] <= 0 or p["terminal_growth"] >= p["terminal_roic"]:
        raise ValueError("terminal ROIC must exceed terminal growth")
    return p, margins


def forward_dcf(revenue_growth, model):
    """FCFF model with explicit reinvestment, constant revenue CAGR and margin path.

    Net reinvestment = max(revenue increase, 0) / sales_to_capital.
    Shrinking sales do not automatically release invested capital in this model.
    Terminal reinvestment rate = terminal_growth / terminal_roic.
    Returns operating enterprise value, not equity value.
    """
    growth = _finite(revenue_growth, "revenue_growth")
    if not -1 < growth <= 2:
        raise ValueError("growth outside supported range (-1, 2]")
    p, margins = _dcf_parameters(model)
    revenue = p["base_revenue"]
    explicit_pv = 0.0
    cash_flows = []
    for year, margin in enumerate(margins, 1):
        next_revenue = revenue * (1 + growth)
        reinvestment = max(next_revenue - revenue, 0) / p["sales_to_capital"]
        nopat = next_revenue * margin * (1 - p["tax_rate"])
        fcff = nopat - reinvestment
        discounted = fcff / (1 + p["discount_rate"]) ** year
        explicit_pv += discounted
        cash_flows.append({"year": year, "revenue": next_revenue, "nopat": nopat,
                           "reinvestment": reinvestment, "fcff": fcff, "present_value": discounted})
        revenue = next_revenue
    terminal_nopat = revenue * (1 + p["terminal_growth"]) * p["terminal_margin"] * (1 - p["tax_rate"])
    terminal_fcff = terminal_nopat * (1 - p["terminal_growth"] / p["terminal_roic"])
    terminal_value = terminal_fcff / (p["discount_rate"] - p["terminal_growth"])
    terminal_pv = terminal_value / (1 + p["discount_rate"]) ** len(margins)
    enterprise_value = explicit_pv + terminal_pv
    if not math.isfinite(enterprise_value):
        raise ValueError("nonfinite valuation")
    return {"enterprise_value": enterprise_value, "explicit_present_value": explicit_pv,
            "terminal_present_value": terminal_pv,
            "terminal_value_share": terminal_pv / enterprise_value if enterprise_value else None,
            "cash_flows": cash_flows}


def equity_to_operating_ev(*, market_cap, debt, cash, minority_interests, nonoperating_assets):
    values = {key: _finite(value, key) for key, value in locals().items()}
    if any(value < 0 for value in values.values()):
        raise ValueError("valuation bridge inputs must be nonnegative")
    ev = values["market_cap"] + values["debt"] + values["minority_interests"] - values["cash"] - values["nonoperating_assets"]
    if ev <= 0:
        raise ValueError("nonpositive operating EV is outside this model's scope")
    return {**values, "operating_enterprise_value": ev}


def reverse_dcf(target_enterprise_value, model, *, growth_bounds=(-0.5, 1.0), grid_steps=512):
    """Locate sign-changing root brackets, then bisect; retain all roots found.

    A finite search grid cannot prove uniqueness or rule out tangential roots.
    Status deliberately says 'root_found' or 'no_bracketed_root', not fair value.
    """
    target = _finite(target_enterprise_value, "target_enterprise_value")
    low, high = (_finite(v, "growth_bound") for v in growth_bounds)
    if target <= 0 or not -1 < low < high <= 2:
        raise ValueError("invalid EV or growth bounds")
    if type(grid_steps) is not int or not 16 <= grid_steps <= 10000:
        raise ValueError("grid_steps must be an integer in [16, 10000]")
    _dcf_parameters(model)
    tolerance = max(1e-9, target * 1e-10)
    roots = []
    def residual(growth):
        return forward_dcf(growth, model)["enterprise_value"] - target
    def retain(growth):
        if not any(abs(growth - old) < 1e-7 for old in roots):
            roots.append(growth)
    previous, f_previous = low, residual(low)
    if abs(f_previous) <= tolerance:
        retain(previous)
    for step in range(1, grid_steps + 1):
        current = low + (high - low) * step / grid_steps
        f_current = residual(current)
        if abs(f_current) <= tolerance:
            retain(current)
        elif f_previous * f_current < 0:
            left, right, f_left = previous, current, f_previous
            for _ in range(100):
                middle = (left + right) / 2
                f_middle = residual(middle)
                if abs(f_middle) <= tolerance:
                    break
                if f_left * f_middle <= 0:
                    right = middle
                else:
                    left, f_left = middle, f_middle
            if abs(residual(middle)) <= tolerance:
                retain(middle)
        previous, f_previous = current, f_current
    details = []
    for growth in sorted(roots):
        value = forward_dcf(growth, model)
        details.append({"implied_revenue_growth": growth,
                        "residual": value["enterprise_value"] - target,
                        "terminal_value_share": value["terminal_value_share"]})
    status = "no_bracketed_root" if not details else "root_found" if len(details) == 1 else "multiple_roots_found"
    return {"method_version": METHOD_VERSION, "status": status, "roots": details,
            "uniqueness": "not_proven_by_grid_search", "target_enterprise_value": target,
            "growth_bounds": [low, high], "grid_steps": grid_steps, "assumptions": dict(model),
            "quality_flags": ["conditional_model_estimate", "not_a_market_forecast",
                              "no_capital_release_on_declining_revenue"]}
