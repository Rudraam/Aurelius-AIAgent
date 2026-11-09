"""Canonical hashing of order arguments (invariant 4: preview and create must match exactly).

The hash covers the order itself: account, instrument, qty, side, type, prices, duration. It excludes
``operation``, which exists only on ``create_order_instruction`` and is checked separately (must be
"create"). Money and quantities must be ``Decimal`` or ``str``; a float raises, so a float can never
produce an equal-looking but different order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

EXCLUDED_KEYS = frozenset({"operation"})


def _normalize(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float):
        raise TypeError("float in order arguments; use Decimal (invariant 9)")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal in order arguments")
        # Decimal("10.50") and Decimal("10.5") are the same price: hash the normalized value.
        return {"$decimal": format(value.normalize(), "f")}
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    raise TypeError(f"unsupported type in order arguments: {type(value).__name__}")


def order_args_hash(args: Mapping[str, Any]) -> str:
    body = {k: _normalize(v) for k, v in args.items() if k not in EXCLUDED_KEYS}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
