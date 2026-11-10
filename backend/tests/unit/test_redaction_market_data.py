"""Field-aware redaction: market data passes through exactly; account fields are still redacted.

Regression for the 2026-11-10 finding: the Redactor masked every 7-10 digit integer as an account number
(``volume: 2397185`` -> ``"****7185"``) and rounded ``totalReturn.amount``. The fixtures are real SHOP.TO
responses (public market data, GUIDs redacted), promoted with Rudra's approval.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.redaction import ACCOUNT_ID_PLACEHOLDER, FAKE_GUIDS, Redactor, is_market_data_field
from tests.fixture_guard import scan_file, scan_text

MARKET = Path(__file__).resolve().parents[1] / "fixtures" / "questrade" / "market"
FIXTURES = sorted(MARKET.glob("*.json"))


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((MARKET / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _typed(obj: Any) -> Any:
    """Equality that also distinguishes 1 from 1.0 from "1" and True."""
    if isinstance(obj, dict):
        return {k: _typed(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_typed(v) for v in obj]
    return (type(obj).__name__, obj)


def test_fixtures_exist():
    assert {p.stem for p in FIXTURES} == {"search_symbols", "get_quotes", "get_historical_data"}


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_redacting_market_data_changes_nothing(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert _typed(Redactor().redact(data)) == _typed(data)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_market_fixtures_pass_the_guard(path):
    assert scan_file(path) == []


def test_quote_volumes_and_counts_stay_integers():
    q = Redactor().redact(_load("get_quotes"))["response"]["quotes"][0]
    assert q["volume"] == 2397185 and type(q["volume"]) is int
    assert q["sharesOutstanding"] == 1208456928
    assert q["averageVolume20Days"] == 1861365 and q["averageVolume3Months"] == 1955106
    assert q["volumeWeightedAveragePrice"] == 214.314774 and q["priceChangePercent"] == 0.020858


def test_candles_and_total_return_are_unrounded():
    h = Redactor().redact(_load("get_historical_data"))["response"]
    assert h["totalReturn"]["amount"] == -38.920000000000016
    assert h["totalReturn"]["percent"] == -0.1560795636830286
    assert h["candles"][0]["volume"] == 1006000
    assert all(type(c["volume"]) is int for c in h["candles"])


def test_security_guids_are_still_pseudonymized():
    r = Redactor()
    raw = {"securityUuid": "2f3f471c-13b4-4f62-0091-b8a46f400960", "volume": 2397185}
    assert r.redact(raw) == {"securityUuid": FAKE_GUIDS[0], "volume": 2397185}


def test_account_fields_are_still_redacted_next_to_market_data():
    payload = {
        "accountNumber": 51234567,
        "accountId": "bs-acct-778899",
        "email": "jane@example.com",
        "cash": 1234.56,
        "amount": 51234567,  # bare "amount" is account money, not market data
        "volume": 2397185,
        "memo": "moved from 51234567",
    }
    out = Redactor().redact(payload)
    assert out["accountNumber"] == "****4567"
    assert out["accountId"] == ACCOUNT_ID_PLACEHOLDER
    assert out["email"] == "REDACTED"
    assert out["cash"] == 1230
    assert out["amount"] == "****4567"
    assert out["volume"] == 2397185
    assert out["memo"] == "moved from ****4567"
    assert scan_text(json.dumps(out), is_json=True) == []


def test_market_exemption_is_path_aware():
    assert is_market_data_field("amount", "totalReturn")
    assert not is_market_data_field("amount", "")
    assert not is_market_data_field("amount", "activity")
    assert is_market_data_field("volume", "candles")


@pytest.mark.parametrize(
    "ok",
    [
        '{"volume": 2397185}',
        '{"candles": [{"volume": 1006000, "close": 251.01}]}',
        '{"totalReturn": {"amount": -38.92, "percent": -0.156}}',
        '{"accountId": "REDACTED-ACCOUNT-ID"}',
        '{"accountId": "00000000-0000-4000-8000-000000000001"}',
    ],
)
def test_guard_accepts_market_data_numbers(ok):
    assert scan_text(ok, is_json=True) == []


@pytest.mark.parametrize(
    "bad",
    [
        '{"unclassifiedCount": 2397185}',  # unknown numeric field fails closed
        '{"amount": 51234567}',  # generic key outside totalReturn
        '{"volume": "51234567"}',  # exemption covers numbers only, not strings
        '{"accountId": "bs-acct-778899"}',  # account id not redacted
        '{"accountId": 778899}',
        '{"candles": [{"note": "acct 51234567"}]}',
    ],
)
def test_guard_still_flags_outside_the_allowlist(bad):
    assert scan_text(bad, is_json=True), bad


def test_markdown_gets_no_exemption():
    assert scan_text("volume: 2397185\n")
