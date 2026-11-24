"""Small value validators for the deterministic execution simulator."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


class ValidationError(ValueError):
    """Invalid market data or simulation parameters."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)

def number(value: Any, name: str, minimum: Decimal | None = None) -> Decimal:
    require(isinstance(value, (str, int)) and not isinstance(value, bool), f"{name} 用十进制字符串表示")
    require(len(str(value)) <= 80, f"{name} 数值过长")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(f"{name} 不是十进制数") from exc
    require(result.is_finite() and result.adjusted() <= 30 and result.as_tuple().exponent >= -20,
            f"{name} 数值非有限或超出允许精度")
    require(minimum is None or result >= minimum, f"{name} 小于允许值")
    return result

def symbol(value: Any, demo: bool = False) -> str:
    require(isinstance(value, str), "证券代码必须是文本")
    value = value.upper().strip()
    valid = re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value)
    require(bool(valid) or (demo and bool(re.fullmatch(r"DEMO_[A-Z0-9]+", value))),
            "证券标识必须带市场，例如 600000.SH / 000001.SZ / 920001.BJ；该格式不代表已验证交易权限")
    return value
