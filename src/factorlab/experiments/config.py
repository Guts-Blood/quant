"""Small TOML/JSON experiment configurations with explicit split boundaries."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import tomllib

from ..factors import CATALOG


def slug(value, label="name"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError(f"{label} must use 1-80 ASCII letters, digits, underscores or hyphens")
    return value


def allowed(value, keys, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    extra = set(value) - set(keys)
    if extra:
        raise ValueError(f"unknown {label} options: {sorted(extra)}")


def load_config(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if path.suffix.lower() == ".toml":
        value = tomllib.loads(raw.decode("utf-8-sig"))
    elif path.suffix.lower() == ".json":
        value = json.loads(raw.decode("utf-8-sig"))
    else:
        raise ValueError("configuration must be TOML or JSON")
    # TOML date literals become ISO strings for a single persisted representation.
    value = json.loads(json.dumps(value, default=lambda item: item.isoformat()))
    return value, path, raw


def validate_config(config):
    config = copy.deepcopy(config)
    allowed(config, {"schema_version", "name", "output_dir", "data", "universe", "factors",
                     "transforms", "blend", "labels", "evaluation", "portfolio", "backtest", "splits", "scan"}, "experiment")
    if config.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    slug(config.get("name"))
    allowed(config.get("data"), {"panel", "calendar", "membership", "source_id",
                                "availability_mode", "signal_time"}, "data")
    universe = config.get("universe", {})
    allowed(universe, {"mode", "min_history", "liquidity_window", "min_amount"}, "universe")
    if universe.get("mode") not in {"historical_membership", "supplied_panel"}:
        raise ValueError("declare universe.mode as historical_membership or supplied_panel")
    has_membership = bool(config["data"].get("membership"))
    if (universe["mode"] == "historical_membership") != has_membership:
        raise ValueError("historical_membership mode requires a membership file; supplied_panel does not use one")
    factors = config.get("factors")
    if not isinstance(factors, list) or not factors:
        raise ValueError("provide at least one factor")
    ids = set()
    for factor in factors:
        allowed(factor, {"name", "id", "parameters"}, "factor")
        if factor.get("name") not in CATALOG:
            raise ValueError(f"unknown factor: {factor.get('name')}")
        factor_id = slug(factor.get("id", factor["name"]), "factor id")
        if factor_id in ids:
            raise ValueError("duplicate factor id; give parameter variants distinct ids")
        ids.add(factor_id)
        factor["id"] = factor_id
        if not isinstance(factor.get("parameters", {}), dict):
            raise ValueError("factor parameters must be a table")
    transforms = config.get("transforms", [])
    if not isinstance(transforms, list):
        raise ValueError("transforms must be a list")
    options = {"winsorize": {"lower", "upper"}, "standardize": set(),
               "neutralize": {"by_industry", "by_size", "min_observations"}}
    for transform in transforms:
        if not isinstance(transform, dict) or transform.get("name") not in options:
            raise ValueError("unknown factor transformation")
        allowed(transform, options[transform["name"]] | {"name"}, "transform")
    if "blend" in config:
        blend = config["blend"]
        allowed(blend, {"name", "weights", "missing_policy"}, "blend")
        blend_name = slug(blend.get("name", "blend"), "blend name")
        if blend_name in ids:
            raise ValueError("blend name conflicts with factor id")
        if not isinstance(blend.get("weights"), dict) or not set(blend["weights"]).issubset(ids):
            raise ValueError("blend weights must refer to configured factor ids")
        blend["name"] = blend_name
        ids.add(blend_name)
    allowed(config.get("labels", {}), {"horizons", "price_column", "entry_lag"}, "labels")
    horizons = config.get("labels", {}).get("horizons", [1, 5, 20])
    if not isinstance(horizons, list) or not horizons or any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in horizons):
        raise ValueError("label horizons must be positive integers")
    if len(horizons) != len(set(horizons)):
        raise ValueError("duplicate label horizon")
    allowed(config.get("evaluation", {}), {"quantiles", "min_observations"}, "evaluation")
    portfolio = config.get("portfolio", {})
    allowed(portfolio, {"factor", "top_n", "cash_buffer"}, "portfolio")
    if portfolio.get("factor", factors[0]["id"]) not in ids:
        raise ValueError("portfolio.factor is not configured")
    allowed(config.get("backtest", {}), {"enabled", "initial_cash", "costs", "lot_size",
                "rebalance_every", "max_positions", "missing_signal"}, "backtest")
    if not isinstance(config.get("backtest", {}).get("enabled", True), bool):
        raise ValueError("backtest.enabled must be boolean")
    splits = config.get("splits")
    if not isinstance(splits, list) or not splits:
        raise ValueError("explicit splits are required")
    names = set()
    for split in splits:
        allowed(split, {"name", "role", "start", "end"}, "split")
        name = slug(split.get("name"), "split name")
        if name in names or split.get("role") not in {"train", "validation", "test", "explore"}:
            raise ValueError("split names must be unique and roles explicit")
        names.add(name)
        if not isinstance(split.get("start"), str) or not isinstance(split.get("end"), str):
            raise ValueError("split start and end must be ISO date strings")
    return config
