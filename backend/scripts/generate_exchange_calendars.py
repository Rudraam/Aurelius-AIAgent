"""Generate app/domain/data/exchange_sessions.json from exchange_calendars (dev-only dependency).

The runtime never imports exchange_calendars or pandas; it reads only the committed JSON file
(app/domain/exchange_calendar.py). Regenerate when coverage runs low (a test fails 90 days before the
end) or when exchange_calendars is upgraded:

    uv run python scripts/generate_exchange_calendars.py            # write the file
    uv run python scripts/generate_exchange_calendars.py --check    # exit 1 if the file is stale

Session times are stored as America/Toronto wall-clock times. XNYS trades on New York time, which has
followed the same rules as Toronto since 1974; the generator asserts that every XNYS open and close is
the same wall-clock time in both zones, so a divergence can never be silently baked in.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

OUT = Path(__file__).resolve().parents[1] / "app" / "domain" / "data" / "exchange_sessions.json"
EXCHANGES = ("XTSE", "XNYS")
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2027, 12, 31)
TORONTO = ZoneInfo("America/Toronto")
NATIVE_ZONE = {"XTSE": ZoneInfo("America/Toronto"), "XNYS": ZoneInfo("America/New_York")}


def _hhmm(ts: Any, zone: ZoneInfo) -> str:
    return str(ts.to_pydatetime().astimezone(zone).strftime("%H:%M"))


def build() -> dict[str, Any]:
    out: dict[str, Any] = {
        "_generated_by": "backend/scripts/generate_exchange_calendars.py (do not edit by hand)",
        "source": f"exchange_calendars {version('exchange_calendars')}",
        "wall_clock_zone": "America/Toronto",
        "coverage": {"start": COVERAGE_START.isoformat(), "end": COVERAGE_END.isoformat()},
        "exchanges": {},
    }
    for code in EXCHANGES:
        cal = xcals.get_calendar(code, start=COVERAGE_START.isoformat(), end=COVERAGE_END.isoformat())
        sched = cal.schedule.loc[COVERAGE_START.isoformat() : COVERAGE_END.isoformat()]
        sessions = {ts.date(): row for ts, row in sched.iterrows()}

        opens = [_hhmm(r["open"], TORONTO) for r in sessions.values()]
        closes = [_hhmm(r["close"], TORONTO) for r in sessions.values()]
        regular_open = max(set(opens), key=opens.count)
        regular_close = max(set(closes), key=closes.count)

        closed_weekdays: list[str] = []
        special: dict[str, dict[str, str]] = {}
        d = COVERAGE_START
        while d <= COVERAGE_END:
            row = sessions.get(d)
            if row is None:
                if d.weekday() < 5:
                    closed_weekdays.append(d.isoformat())
            else:
                if d.weekday() >= 5:
                    raise SystemExit(f"{code}: weekend session on {d}; the file format cannot express it")
                o, c = _hhmm(row["open"], TORONTO), _hhmm(row["close"], TORONTO)
                native = NATIVE_ZONE[code]
                if (o, c) != (_hhmm(row["open"], native), _hhmm(row["close"], native)):
                    raise SystemExit(f"{code}: {d} wall-clock differs between Toronto and {native.key}")
                if row.get("break_start") is not None and str(row["break_start"]) != "NaT":
                    raise SystemExit(f"{code}: {d} has a trading break; the file format cannot express it")
                if (o, c) != (regular_open, regular_close):
                    special[d.isoformat()] = {"open": o, "close": c}
            d += timedelta(days=1)

        out["exchanges"][code] = {
            "regular_open": regular_open,
            "regular_close": regular_close,
            "closed_weekdays": closed_weekdays,
            "special_sessions": special,
        }
    return out


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file differs")
    args = ap.parse_args()
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale; regenerate it", file=sys.stderr)
            return 1
        print("up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
