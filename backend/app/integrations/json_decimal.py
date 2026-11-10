"""Parse broker JSON so that no float ever exists (spec invariant 9, amendment A14).

Questrade sends every price and amount as a JSON number (``"bidPrice": 214.1``,
``"amount": -38.920000000000016``). Parsing those with plain ``json.loads`` creates binary floats, which
are lossy and are rejected by ``order_args_hash``. Parse the response *text* here instead:

- non-integer numbers become ``Decimal`` built from the exact JSON text (no float in between);
- integers stay ``int`` (volumes, sizes, counts);
- ``NaN``, ``Infinity`` and ``-Infinity`` are refused (fail closed).

Never call ``Decimal(some_float)`` or ``float(...)`` on broker data; use ``loads`` on the raw text.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


class FloatInPayload(TypeError):
    """A float was found where only Decimal/int are allowed."""


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON number {name} refused")


def loads(text: str | bytes) -> Any:
    """``json.loads`` with ``Decimal`` for every non-integer number and no non-finite values."""
    return json.loads(text, parse_float=Decimal, parse_constant=_refuse_constant)


def assert_no_floats(obj: Any, path: str = "$") -> None:
    """Raise ``FloatInPayload`` naming the first float found anywhere in ``obj``."""
    if isinstance(obj, float):
        raise FloatInPayload(f"float at {path}; parse broker JSON with json_decimal.loads")
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_no_floats(v, f"{path}.{k}")
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            assert_no_floats(v, f"{path}[{i}]")
