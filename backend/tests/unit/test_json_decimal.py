from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.integrations.json_decimal import FloatInPayload, assert_no_floats, loads

MARKET = Path(__file__).resolve().parents[1] / "fixtures" / "questrade" / "market"


def test_non_integers_become_exact_decimals():
    out = loads('{"bidPrice": 214.1, "amount": -38.920000000000016, "pct": 0.020858, "e": 1.5e-3}')
    assert out == {
        "bidPrice": Decimal("214.1"),
        "amount": Decimal("-38.920000000000016"),
        "pct": Decimal("0.020858"),
        "e": Decimal("0.0015"),
    }
    assert str(out["bidPrice"]) == "214.1"  # not 214.099999999999994315658113919198513031005859375


def test_integers_stay_int():
    out = loads('{"volume": 2397185, "lowPrice": 209}')
    assert type(out["volume"]) is int and type(out["lowPrice"]) is int


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", '{"p": NaN}'])
def test_non_finite_numbers_are_refused(bad):
    with pytest.raises(ValueError, match="non-finite"):
        loads(bad)


def test_accepts_bytes():
    assert loads(b'{"p": 34.10}') == {"p": Decimal("34.10")}


@pytest.mark.parametrize("path", sorted(MARKET.glob("*.json")), ids=lambda p: p.stem)
def test_real_responses_parse_float_free(path):
    assert_no_floats(loads(path.read_text(encoding="utf-8")))


def test_real_candle_values_are_exact():
    h = loads((MARKET / "get_historical_data.json").read_text(encoding="utf-8"))["response"]
    assert h["candles"][0]["vwap"] == Decimal("251.890615")
    assert h["totalReturn"]["amount"] == Decimal("-38.920000000000016")


def test_assert_no_floats_names_the_path():
    with pytest.raises(FloatInPayload, match=r"\$\.quotes\[0\]\.bidPrice"):
        assert_no_floats({"quotes": [{"bidPrice": 214.1}]})
    assert_no_floats({"quotes": [{"bidPrice": Decimal("214.1"), "volume": 1, "ok": True, "x": None}]})
