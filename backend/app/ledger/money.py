"""Money for the ledger: a CAD ``Decimal`` in whole cents (decision P2, invariant 8).

There is no Money class. A ledger amount is a ``Decimal`` that is finite and exactly representable in
cents. Pydantic models use the annotated types below, which are *strict*: a float (or an int, or a
str in Python mode) is rejected outright, so no float can enter the engine.

All arithmetic that can produce sub-cent values (division, percentages) runs under ``ledger_context``
(34 significant digits, traps on) and is rounded **down** to the cent with ``floor_cents`` (P4).
"""

from __future__ import annotations

from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Annotated

from pydantic import AfterValidator, Strict

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal(100)


def ledger_context() -> Context:
    """A fixed decimal context, so results never depend on the caller's global context."""
    return Context(prec=34, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])


def _check_cents(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("money must be finite")
    with localcontext(ledger_context()):
        quantized = value.quantize(CENT)
    if quantized != value:
        raise ValueError(f"money must be whole cents, got {value}")
    return quantized


def _check_non_negative(value: Decimal) -> Decimal:
    value = _check_cents(value)
    if value < 0:
        raise ValueError(f"amount must not be negative, got {value}")
    return value


def _check_positive(value: Decimal) -> Decimal:
    value = _check_cents(value)
    if value <= 0:
        raise ValueError(f"amount must be positive, got {value}")
    return value


def _check_percent(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0 or value > HUNDRED:
        raise ValueError(f"percent must be within [0, 100], got {value}")
    return value


def _check_positive_decimal(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"must be a positive finite decimal, got {value}")
    return value


#: Any whole-cent amount (may be negative, e.g. a balance or an over-contributed room).
Money = Annotated[Decimal, Strict(), AfterValidator(_check_cents)]
#: A whole-cent amount >= 0.
NonNegativeMoney = Annotated[Decimal, Strict(), AfterValidator(_check_non_negative)]
#: A whole-cent amount > 0.
PositiveMoney = Annotated[Decimal, Strict(), AfterValidator(_check_positive)]
#: A percentage in [0, 100] at any precision.
Percent = Annotated[Decimal, Strict(), AfterValidator(_check_percent)]
#: A positive decimal at any precision (e.g. emergency-fund months).
PositiveDecimal = Annotated[Decimal, Strict(), AfterValidator(_check_positive_decimal)]


def cents(value: Decimal) -> Decimal:
    """Validate a whole-cent Decimal and normalise it to two places. Rejects floats."""
    if not isinstance(value, Decimal) or isinstance(value, bool):
        raise TypeError(f"money must be Decimal, got {type(value).__name__}")
    return _check_cents(value)


def floor_cents(value: Decimal) -> Decimal:
    """Round down to the cent (P4). Exact for values already in cents."""
    with localcontext(ledger_context()):
        return value.quantize(CENT, rounding=ROUND_FLOOR)


def divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Full-precision division under the ledger context. Callers round with ``floor_cents``."""
    with localcontext(ledger_context()):
        return numerator / denominator


def multiply(a: Decimal, b: Decimal) -> Decimal:
    with localcontext(ledger_context()):
        return a * b


def percent_of(percent: Decimal, amount: Decimal) -> Decimal:
    """``percent``% of ``amount``, rounded down to the cent."""
    return floor_cents(divide(multiply(percent, amount), HUNDRED))
