"""Typed calculator errors (inputs that validate but have no meaningful answer)."""

from __future__ import annotations


class CalculatorError(ValueError):
    """The inputs are well-formed but the calculation can't produce a valid result."""
