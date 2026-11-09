"""Step 0 spike: authenticated observations against live Questrade. RUDRA RUNS THIS, not Claude Code.

READ-ONLY. ``call_read_tool`` refuses anything outside READ_TOOLS before a request is built. The
script never calls preview_* or create_* tools; it only reports whether they appear in tools/list.
Tokens are never printed. Observations go to backend/.spike/observations.jsonl.

Run from D:\\Aurelius\\backend (PowerShell), after `uv run aurelius auth login`:
    uv run python scripts/spike_auth_lifecycle.py call            # new process, headless list_accounts
    uv run python scripts/spike_auth_lifecycle.py force-refresh   # refresh grant, records rotation, then call
    uv run python scripts/spike_auth_lifecycle.py fixtures        # tools/list + scope report + redacted fixtures
    uv run python scripts/spike_auth_lifecycle.py probe           # 401 shape for a bogus token
    uv run python scripts/spike_auth_lifecycle.py probe --stale   # after natural expiry: real expired-token shape
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import secrets
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
from mcp.client import Client

from app.cli import mcp_url, open_store
from app.integrations.questrade.auth import TokenManager, discover
from app.integrations.questrade.transport import read_session

BACKEND = Path(__file__).resolve().parents[1]
OBS_FILE = BACKEND / ".spike" / "observations.jsonl"
FIXTURES = BACKEND / "tests" / "fixtures" / "questrade"

READ_TOOLS = frozenset(
    {
        "list_accounts",
        "get_balances",
        "get_positions",
        "get_order_history",
        "get_account_activities",
        "get_quotes",
        "search_symbols",
        "get_historical_data",
        "get_instruction",
        "list_instructions",
    }
)
PHASE1_ORDER_TOOLS = ("preview_order_instruction", "create_order_instruction")
DOCUMENTED_TOOLS = {
    "list_accounts",
    "get_balances",
    "get_positions",
    "get_order_history",
    "get_account_activities",
    "search_symbols",
    "get_quotes",
    "get_historical_data",
    "get_option_expiries",
    "get_option_chain",
    "list_watchlists",
    "get_watchlist",
    "create_watchlist",
    "edit_watchlist",
    "preview_order_instruction",
    "create_order_instruction",
    "preview_option_instruction",
    "create_option_instruction",
    "get_instruction",
    "list_instructions",
    "preview_custom_index",
    "create_custom_index",
    "list_custom_indexes",
    "get_custom_index",
    "edit_custom_index",
    "get_balance_sheet",
    "get_balance_sheet_account",
    "list_flows",
    "get_flow",
    "get_flow_history",
    "preview_flow",
    "create_flow",
    "update_flow_status",
}


def observe(event: str, **fields: Any) -> None:
    OBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
    with open(OBS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"[observe] {json.dumps(rec)}")


def jwt_claims(token: str) -> dict[str, Any] | None:
    """Unverified decode; keeps only timing/scope claims (no subject or user identifiers)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    out = {k: payload[k] for k in ("iss", "aud", "exp", "iat", "nbf", "scope", "client_id", "amr") if k in payload}
    if "exp" in out and "iat" in out:
        out["lifetime_s"] = out["exp"] - out["iat"]
    return out


async def token_snapshot(label: str) -> None:
    s = await open_store().load()
    t = s.tokens
    if t is None:
        observe(label, tokens=None)
        return
    observe(
        label,
        version=s.version,
        strategy=s.client.strategy if s.client else None,
        granted_scope=t.scope,
        lifetime_s=int(t.lifetime_s),
        has_refresh_token=bool(t.refresh_token),
        refresh_rotated_at=s.refresh_rotated_at.isoformat() if s.refresh_rotated_at else None,
        access_claims=jwt_claims(t.access_token),
    )


async def with_client(fn: Any) -> Any:
    store = open_store()
    async with httpx2.AsyncClient(timeout=30) as http:
        meta = await discover(http, mcp_url())
        tokens = TokenManager(store, http, meta.asm.token_endpoint, meta.prm.resource)
        async with read_session(mcp_url(), tokens) as client:  # read-only session; never an approval session
            return await fn(client)


async def call_read_tool(client: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    if name not in READ_TOOLS:
        raise PermissionError(f"spike refuses non-read tool {name!r}")
    return await client.call_tool(name, args or {})


def payload_of(res: Any) -> Any:
    if res.structured_content is not None:
        return res.structured_content
    parsed: list[Any] = []
    for c in res.content or []:
        text = getattr(c, "text", None)
        if text:
            try:
                parsed.append(json.loads(text))
            except ValueError:
                parsed.append(text)
    return parsed[0] if len(parsed) == 1 else parsed


def shape(obj: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


# --------------------------------------------------------------------------- commands


async def cmd_call() -> None:
    before = (await open_store().load()).version

    async def run(client: Client) -> None:
        t0 = time.perf_counter()
        res = await call_read_tool(client, "list_accounts")
        observe(
            "headless_list_accounts",
            is_error=res.is_error,
            latency_ms=round((time.perf_counter() - t0) * 1000),
            payload_shape=shape(payload_of(res)),
        )

    await with_client(run)
    after = (await open_store().load()).version
    observe("store_version", before=before, after=after, refreshed_during_call=after != before)
    await token_snapshot("tokens_after_call")


async def cmd_force_refresh() -> None:
    await token_snapshot("tokens_before_refresh")
    store = open_store()
    async with httpx2.AsyncClient(timeout=30) as http:
        meta = await discover(http, mcp_url())
        await TokenManager(store, http, meta.asm.token_endpoint, meta.prm.resource).refresh(stale_access_token=None)
    await token_snapshot("tokens_after_refresh")
    await cmd_call()


async def cmd_probe(stale: bool) -> None:
    if stale:
        t = (await open_store().load()).tokens
        if t is None:
            raise SystemExit("no stored tokens")
        token, label = t.access_token, "stale_access_token"
        left = (t.expires_at - datetime.now(UTC)).total_seconds()
        if left > 0:
            print(f"warning: stored token not expired yet ({int(left)}s left)")
    else:
        token, label = "aurelius-bogus-" + secrets.token_hex(8), "bogus_token"
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with httpx2.AsyncClient(timeout=20) as h:
        r = await h.post(
            mcp_url(),
            json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        )
    observe(
        f"probe_{label}",
        status=r.status_code,
        www_authenticate=r.headers.get("www-authenticate"),
        content_type=r.headers.get("content-type"),
        body=r.text[:300],
    )


# --------------------------------------------------------------------------- fixtures + redaction

_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NS = uuid.UUID("6f1d1a52-6a59-4b39-9d0f-a0a1e1a5e000")
_PII_KEYS = re.compile(r"(name|email|address|phone|sin$|birth|owner|holder|beneficiar)", re.I)
_SAFE_KEYS = re.compile(
    r"^(symbol\w*|description|security\w*|instrument\w*|exchange|currency|\w*type|status|side)$", re.I
)
_NUMBER_KEYS = re.compile(r"(accountnumber|number|acctnum)", re.I)
_MONEY_KEYS = re.compile(r"(balance|cash|value|equity|power|pnl|amount|cost|commission|total|margin)", re.I)


def redact(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    if isinstance(obj, str):
        s = _GUID.sub(lambda m: str(uuid.uuid5(_NS, m.group(0).lower())), obj)
        if _NUMBER_KEYS.search(key) and not _GUID.fullmatch(s):
            return "****" + s[-4:] if len(s) > 4 else "****"
        if _PII_KEYS.search(key) and not _SAFE_KEYS.match(key):
            return "REDACTED"
        if re.fullmatch(r"\d{7,}", s):
            return "****" + s[-4:]
        return s
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, int | float) and _MONEY_KEYS.search(key) and "price" not in key.lower():
        return round(obj, -1)
    if isinstance(obj, int) and _NUMBER_KEYS.search(key):
        return int(str(obj)[-4:])
    return obj


def fill_args(schema: dict[str, Any], account_id: str | None) -> dict[str, Any]:
    props: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    today = date.today()
    args: dict[str, Any] = {}
    for name, spec in props.items():
        low = name.lower()
        if low in ("accountid", "account_id") and account_id:
            args[name] = account_id
        elif name not in required:
            continue
        elif "start" in low or low in ("from", "fromdate"):
            args[name] = (today - timedelta(days=30)).isoformat()
        elif "end" in low or low in ("to", "todate"):
            args[name] = today.isoformat()
        elif "symbol" in low:
            args[name] = ["XEQT.TO"] if spec.get("type") == "array" else "XEQT.TO"
        else:
            print(f"  ! cannot auto-fill required arg {name!r}; the tool may return an error")
    return args


async def cmd_fixtures() -> None:
    async def run(client: Client) -> None:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        tools = await client.list_tools()
        catalog = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools.tools]
        (FIXTURES / "_tools_list.json").write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
        names = {t["name"] for t in catalog}
        observe(
            "tools_list",
            count=len(names),
            names=sorted(names),
            missing_vs_docs=sorted(DOCUMENTED_TOOLS - names),
            extra_vs_docs=sorted(names - DOCUMENTED_TOOLS),
        )
        # Decision 4: are the order tools visible under a read-only token? (Listed only, never called.)
        observe(
            "order_tools_under_read_scope",
            granted_scope=(await open_store().load()).tokens.scope,  # type: ignore[union-attr]
            visible={n: (n in names) for n in PHASE1_ORDER_TOOLS},
            all_preview_create_visible=sorted(n for n in names if n.startswith(("preview_", "create_"))),
            annotations={t["name"]: t.get("annotations") for t in catalog if t["name"] in PHASE1_ORDER_TOOLS},
        )

        accts = payload_of(await call_read_tool(client, "list_accounts"))
        (FIXTURES / "list_accounts.json").write_text(json.dumps(redact(accts), indent=2) + "\n", encoding="utf-8")
        guids = sorted(set(_GUID.findall(json.dumps(accts))))
        account_id = guids[0] if guids else None
        by_name = {t["name"]: t for t in catalog}
        for tool in ("get_balances", "get_positions", "get_order_history", "get_account_activities", "get_quotes"):
            if tool not in by_name:
                observe("tool_absent", tool=tool)
                continue
            args = fill_args(by_name[tool].get("inputSchema", {}), account_id)
            try:
                res = await call_read_tool(client, tool, args)
            except Exception as e:  # spike: record the failure and carry on
                observe("tool_call_error", tool=tool, error=type(e).__name__, detail=str(e)[:300])
                continue
            out = {"_args_redacted": redact(args), "_is_error": res.is_error, "result": redact(payload_of(res))}
            (FIXTURES / f"{tool}.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
            print(f"  saved {tool}.json")
        print(f"\nBefore anything is committed, read through {FIXTURES} for personal data.")

    await with_client(run)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("call", "force-refresh", "fixtures"):
        sub.add_parser(c)
    p = sub.add_parser("probe")
    p.add_argument("--stale", action="store_true")
    a = ap.parse_args()
    if a.cmd == "call":
        asyncio.run(cmd_call())
    elif a.cmd == "force-refresh":
        asyncio.run(cmd_force_refresh())
    elif a.cmd == "fixtures":
        asyncio.run(cmd_fixtures())
    else:
        asyncio.run(cmd_probe(a.stale))


if __name__ == "__main__":
    main()
