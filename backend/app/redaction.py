"""Redaction for anything that may end up in git (the repo is public) or in logs.

- GUIDs (account, security and instruction IDs) are replaced, in order of first appearance, from the
  fixed ``FAKE_GUIDS`` set. ``tests/unit/test_fixture_guard.py`` rejects any GUID outside that set, so
  a real ID can never pass as a fake one.
- Account numbers are masked to ``****`` plus the last 4 characters (spec invariant 7).
- Personal fields (names, emails, addresses, phones) are replaced with ``"REDACTED"``.
- Account IDs that are not GUIDs (e.g. balance-sheet ids) become ``ACCOUNT_ID_PLACEHOLDER``.
- Balance-like amounts are rounded to the nearest 10.
- Market data is field-aware: numbers under ``MARKET_DATA_KEYS`` / ``MARKET_DATA_PATHS`` (prices, sizes,
  volumes, returns) are public and left exactly as received. Unknown numeric fields fail closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

FAKE_GUIDS: Final[tuple[str, ...]] = tuple(f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 257))
FAKE_GUID_SET: Final = frozenset(FAKE_GUIDS)

GUID_RE: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
#: 7 to 10 digits standing alone: the shape of a brokerage account number (Questrade uses 8).
ACCOUNT_NUMBER_RE: Final = re.compile(r"(?<![\w.])\d{7,10}(?![\w.])")
MASKED_RE: Final = re.compile(r"^\*{4}[0-9A-Za-z]{0,4}$")
EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

ACCOUNT_NUMBER_KEY_RE: Final = re.compile(r"(account_?number|acct_?num|^number$)", re.I)
#: Account identifiers that are not account numbers (brokerage GUIDs, balance-sheet string ids).
ACCOUNT_ID_KEY_RE: Final = re.compile(r"^(account_?id|account_?uuid)$", re.I)
ACCOUNT_ID_PLACEHOLDER: Final = "REDACTED-ACCOUNT-ID"

#: Public market-data numbers, left untouched (never masked, never rounded) wherever they appear.
#: Only *numeric* values are exempt; a string under one of these keys is still scanned.
#: An unknown numeric field is NOT exempt: it fails closed (masked/rounded, flagged by the guard)
#: until it is classified here.
MARKET_DATA_KEYS: Final = frozenset(
    {
        # sizes, volumes, counts
        "volume", "bidSize", "askSize", "lastTradeSize", "sharesOutstanding", "averageVolume20Days",
        "averageVolume3Months", "marketCapitalization", "openInterest", "candlesReturned",
        # prices
        "open", "high", "low", "close", "vwap", "bidPrice", "askPrice", "lastPrice", "openPrice",
        "highPrice", "lowPrice", "highPrice52Weeks", "lowPrice52Weeks", "volumeWeightedAveragePrice",
        "midPrice", "strikePrice",
        # changes, ratios, returns
        "priceChangeAmount", "priceChangePercent", "afterHourPriceChangeAmount",
        "afterHourPriceChangePercent", "peRatio", "dividendYield", "eps", "lastDividendAmount",
    }
)  # fmt: skip
#: Generic keys that are market data only under a specific parent (``amount`` elsewhere is account money).
MARKET_DATA_PATHS: Final = frozenset({"totalReturn.amount", "totalReturn.percent"})


def is_market_data_field(key: str, parent: str = "") -> bool:
    return key in MARKET_DATA_KEYS or f"{parent}.{key}" in MARKET_DATA_PATHS


_PII_KEY_RE: Final = re.compile(r"(name|email|address|phone|^sin$|birth|owner|holder|beneficiar)", re.I)
_SAFE_KEY_RE: Final = re.compile(
    r"^(symbol\w*|description|security\w*|instrument\w*|exchange|currency|\w*type|status|side)$", re.I
)
_MONEY_KEY_RE: Final = re.compile(r"(balance|cash|value|equity|power|pnl|amount|cost|commission|total|margin)", re.I)


def mask_account_number(value: str) -> str:
    return "****" + value[-4:] if len(value) > 4 else "****"


class Redactor:
    """Stateful within one fixture run, so the same real GUID always maps to the same fake GUID."""

    def __init__(self) -> None:
        self._guids: dict[str, str] = {}

    def fake_guid(self, real: str) -> str:
        key = real.lower()
        if key in FAKE_GUID_SET:
            return key
        if key not in self._guids:
            if len(self._guids) >= len(FAKE_GUIDS):
                raise RuntimeError("ran out of fake GUIDs; extend FAKE_GUIDS")
            self._guids[key] = FAKE_GUIDS[len(self._guids)]
        return self._guids[key]

    def redact(self, obj: Any, key: str = "", parent: str = "") -> Any:
        """Field-aware: known account fields are redacted by key, allowlisted market-data numbers are
        kept exactly, and anything else is treated conservatively (digit runs masked, money rounded)."""
        if isinstance(obj, Mapping):
            return {k: self.redact(v, str(k), key) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.redact(v, key, parent) for v in obj]
        if isinstance(obj, str):
            return self._redact_str(obj, key)
        if isinstance(obj, bool) or obj is None:
            return obj
        if isinstance(obj, int | float | Decimal):
            if ACCOUNT_NUMBER_KEY_RE.search(key):
                return mask_account_number(str(obj))
            if ACCOUNT_ID_KEY_RE.match(key):
                return ACCOUNT_ID_PLACEHOLDER
            if is_market_data_field(key, parent):
                return obj
            # Account-number shape first: rounding 51234567 to 51234570 would still look like one.
            if isinstance(obj, int) and ACCOUNT_NUMBER_RE.fullmatch(str(obj)):
                return mask_account_number(str(obj))
            if _MONEY_KEY_RE.search(key) and "price" not in key.lower():
                return round(obj, -1)
        return obj

    def _redact_str(self, s: str, key: str) -> str:
        if ACCOUNT_NUMBER_KEY_RE.search(key) and not GUID_RE.fullmatch(s):
            return mask_account_number(s)
        if ACCOUNT_ID_KEY_RE.match(key) and not GUID_RE.fullmatch(s):
            return ACCOUNT_ID_PLACEHOLDER
        if _PII_KEY_RE.search(key) and not _SAFE_KEY_RE.match(key):
            return "REDACTED"
        # Walk GUID / non-GUID segments: GUIDs are pseudonymized whole, and digit runs are masked only
        # between them (a GUID's leading "00000000" must not be mistaken for an account number).
        out: list[str] = []
        last = 0
        for m in GUID_RE.finditer(s):
            out.append(self._mask_free_text(s[last : m.start()]))
            out.append(self.fake_guid(m.group(0)))
            last = m.end()
        out.append(self._mask_free_text(s[last:]))
        return "".join(out)

    @staticmethod
    def _mask_free_text(text: str) -> str:
        text = EMAIL_RE.sub("REDACTED@example.invalid", text)
        return ACCOUNT_NUMBER_RE.sub(lambda m: mask_account_number(m.group(0)), text)
