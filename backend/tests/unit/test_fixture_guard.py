"""The repo is public: committed fixtures may contain only fake IDs and no account-number-shaped data."""

from __future__ import annotations

import json

import pytest

from app.redaction import FAKE_GUIDS, Redactor, mask_account_number
from tests.fixture_guard import fixture_files, scan_file, scan_text

REALISTIC_PAYLOAD = {  # synthetic, shaped like a Questrade list_accounts/get_balances reply
    "accounts": [
        {
            "id": "8d3f6a2e-1b4c-4e7a-9f0d-2c5b8e1a7d34",
            "number": "51234567",
            "type": "TFSA",
            "clientAccountType": "Individual",
            "ownerName": "Jane Q Investor",
            "status": "Active",
            "email": "jane@example.com",
        },
        {"id": "f1e2d3c4-b5a6-4978-8a6b-5c4d3e2f1a0b", "accountNumber": 59876543, "type": "FHSA"},
    ],
    "balances": [
        {
            "accountId": "8d3f6a2e-1b4c-4e7a-9f0d-2c5b8e1a7d34",
            "currency": "CAD",
            "cash": 1234.56,
            "totalEquity": 98765.43,
            "buyingPower": 1234.56,
        }
    ],
    "note": "transfer to 51234567 completed",
    "quote": {"symbol": "XEQT.TO", "lastTradePrice": 31.25},
}


# --------------------------------------------------------------------------- the real check


@pytest.mark.parametrize("path", fixture_files() or [None], ids=lambda p: "no-fixtures-yet" if p is None else p.name)
def test_committed_fixtures_contain_no_real_account_data(path):
    if path is None:
        pytest.skip("no fixture files yet; the guard activates as soon as one is added")
    assert scan_file(path) == []


# --------------------------------------------------------------------------- guard the guard


@pytest.mark.parametrize(
    "bad",
    [
        '{"id": "8d3f6a2e-1b4c-4e7a-9f0d-2c5b8e1a7d34"}',  # real-looking GUID
        '{"accountId": "00000000-0000-4000-8000-999999999999"}',  # fake-looking but outside the fixed set
        '{"number": "51234567"}',  # unmasked account number
        '{"accountNumber": 51234567}',  # unmasked, numeric
        '{"accountNumber": "5123-4567"}',  # not masked, odd shape
        '{"memo": "moved cash from acct 51234567"}',  # account number in free text
        '{"contact": "jane@example.com"}',  # email
        "51234567\n",  # bare
    ],
)
def test_guard_flags_account_data(bad):
    assert scan_text(bad, is_json=True), bad


@pytest.mark.parametrize(
    "ok",
    [
        '{"id": "00000000-0000-4000-8000-000000000001", "number": "****4567"}',
        '{"qty": 3, "lastTradePrice": 31.25, "date": "2026-11-09", "cash": 1230}',
        '{"contact": "REDACTED@example.invalid", "cf-ray": "a580844eb9a992b6-YYZ"}',
    ],
)
def test_guard_accepts_redacted_data(ok):
    assert scan_text(ok, is_json=True) == []


def test_redactor_output_passes_the_guard():
    redacted = Redactor().redact(REALISTIC_PAYLOAD)
    text = json.dumps(redacted, indent=2)
    assert scan_text(text, is_json=True) == []
    assert scan_text(json.dumps(REALISTIC_PAYLOAD), is_json=True)  # and the raw payload would fail


def test_redactor_maps_guids_consistently_onto_the_fixed_set():
    r = Redactor()
    out = r.redact(REALISTIC_PAYLOAD)
    assert out["accounts"][0]["id"] == FAKE_GUIDS[0]
    assert out["accounts"][1]["id"] == FAKE_GUIDS[1]
    assert out["balances"][0]["accountId"] == FAKE_GUIDS[0]  # same real ID -> same fake ID
    assert out["accounts"][0]["number"] == "****4567"
    assert out["accounts"][1]["accountNumber"] == "****6543"
    assert out["accounts"][0]["ownerName"] == "REDACTED"
    assert out["accounts"][0]["type"] == "TFSA" and out["quote"]["lastTradePrice"] == 31.25
    assert out["balances"][0]["totalEquity"] == 98770.0
    assert out["note"] == "transfer to ****4567 completed"


def test_guid_digits_are_not_mistaken_for_account_numbers():
    """Regression: a GUID's leading 8 digits ("00000000-") once got masked into "****0000-..."."""
    r = Redactor()
    s = "acct 51234567 id 8d3f6a2e-1b4c-4e7a-9f0d-2c5b8e1a7d34 again 00000000-0000-4000-8000-000000000001"
    out = r.redact({"memo": s})["memo"]
    assert out == f"acct ****4567 id {FAKE_GUIDS[0]} again {FAKE_GUIDS[0]}"
    assert scan_text(out) == []


def test_mask_account_number():
    assert mask_account_number("51234567") == "****4567"
    assert mask_account_number("123") == "****"
