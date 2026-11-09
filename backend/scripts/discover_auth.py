# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx2>=2.16,<3"]
# ///
"""Step 0.2 — Questrade MCP OAuth discovery spike.

Unauthenticated, read-only probing of public metadata. Sends no credentials and
calls no tools. Run with:

    uv run backend/scripts/discover_auth.py [--url URL] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2

DEFAULT_URL = "https://mcp.questrade.com/v1/brokerage/mcp"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "docs" / "spikes" / "questrade-oauth-metadata.json"
UA = "Aurelius-discovery/0.1"

INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "aurelius-discovery", "version": "0.1"},
    },
}

KEYS_OF_INTEREST = [
    "issuer",
    "authorization_endpoint",
    "token_endpoint",
    "registration_endpoint",
    "revocation_endpoint",
    "client_id_metadata_document_supported",
    "scopes_supported",
    "code_challenge_methods_supported",
    "token_endpoint_auth_methods_supported",
    "grant_types_supported",
    "response_types_supported",
]


def parse_www_authenticate(header: str) -> dict[str, str]:
    """Parse `Bearer k="v", k2="v2"` into a dict (lenient)."""
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', header):
        params[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return params


def well_known_urls(base: str, suffix: str) -> list[str]:
    """RFC 8414 / RFC 9728 path-insertion form first, then root form."""
    p = urlsplit(base)
    path = p.path.rstrip("/")
    urls = []
    if path:
        urls.append(urlunsplit((p.scheme, p.netloc, f"/.well-known/{suffix}{path}", "", "")))
    urls.append(urlunsplit((p.scheme, p.netloc, f"/.well-known/{suffix}", "", "")))
    if suffix == "openid-configuration" and path:
        urls.append(urlunsplit((p.scheme, p.netloc, f"{path}/.well-known/{suffix}", "", "")))
    return urls


def fetch_json(client: httpx2.Client, url: str, log: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        r = client.get(url, headers={"Accept": "application/json"})
    except httpx2.HTTPError as e:
        log.append({"url": url, "error": repr(e)})
        return None
    entry: dict[str, Any] = {"url": url, "status": r.status_code, "content_type": r.headers.get("content-type")}
    log.append(entry)
    if r.status_code != 200:
        entry["body_snippet"] = r.text[:300]
        return None
    try:
        data = r.json()
    except ValueError:
        entry["body_snippet"] = r.text[:300]
        return None
    return data if isinstance(data, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    result: dict[str, Any] = {"retrieved_at": datetime.now(UTC).isoformat(), "mcp_url": args.url, "requests": []}
    log: list[dict[str, Any]] = result["requests"]

    with httpx2.Client(timeout=20, follow_redirects=False, headers={"User-Agent": UA}) as client:
        # 1. Unauthenticated MCP initialize
        r = client.post(
            args.url,
            json=INIT_BODY,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        www = r.headers.get("www-authenticate")
        result["unauthenticated"] = {
            "status": r.status_code,
            "www_authenticate": www,
            "www_authenticate_parsed": parse_www_authenticate(www) if www else None,
            "body_snippet": r.text[:500],
            "headers": {k: v for k, v in r.headers.items() if k.lower() not in {"set-cookie"}},
        }

        # 2. Protected resource metadata (RFC 9728)
        prm_candidates: list[str] = []
        if www:
            rm = parse_www_authenticate(www).get("resource_metadata")
            if rm:
                prm_candidates.append(rm)
        prm_candidates += well_known_urls(args.url, "oauth-protected-resource")
        prm = None
        for u in dict.fromkeys(prm_candidates):
            prm = fetch_json(client, u, log)
            if prm:
                result["protected_resource_metadata_url"] = u
                break
        result["protected_resource_metadata"] = prm

        # 3. Authorization server metadata (RFC 8414, then OIDC)
        as_list = (prm or {}).get("authorization_servers") or []
        if not as_list:
            p = urlsplit(args.url)
            as_list = [f"{p.scheme}://{p.netloc}"]
        asm = None
        for issuer in as_list:
            for suffix in ("oauth-authorization-server", "openid-configuration"):
                for u in well_known_urls(issuer, suffix):
                    asm = fetch_json(client, u, log)
                    if asm:
                        result["authorization_server_metadata_url"] = u
                        break
                if asm:
                    break
            if asm:
                break
        result["authorization_server_metadata"] = asm

    summary = {k: (asm or {}).get(k) for k in KEYS_OF_INTEREST}
    summary["prm_scopes_supported"] = (prm or {}).get("scopes_supported")
    summary["prm_resource"] = (prm or {}).get("resource")
    summary["cimd_supported"] = bool((asm or {}).get("client_id_metadata_document_supported"))
    summary["dcr_supported"] = bool((asm or {}).get("registration_endpoint"))
    result["summary"] = summary

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nunauthenticated status: {result['unauthenticated']['status']}")
    print(f"WWW-Authenticate: {www}")
    print(f"saved -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
