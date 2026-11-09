"""Render the static CIMD document that Caddy serves at /oauth/client-metadata.json.

The URL of that document IS Aurelius's permanent OAuth client_id. Render it once for the final domain
and never move it: changing the host or path is a different client to Questrade.

    uv run python scripts/render_cimd.py --domain aurelius.example.ca
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.cli import LOOPBACK_REDIRECT
from app.integrations.questrade.auth import cimd_document

OUT = Path(__file__).resolve().parents[2] / "deploy" / "static" / "oauth" / "client-metadata.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, help="final public host, no scheme, e.g. aurelius.example.ca")
    ap.add_argument("--no-loopback", action="store_true", help="omit the dev loopback redirect URI")
    a = ap.parse_args()
    if "/" in a.domain or ":" in a.domain:
        raise SystemExit("--domain is a bare host name such as aurelius.example.ca")
    client_id = f"https://{a.domain}/oauth/client-metadata.json"
    redirects = [f"https://{a.domain}/oauth/callback"]
    if not a.no_loopback:
        redirects.append(LOOPBACK_REDIRECT)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cimd_document(client_id, redirects), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}\nclient_id = {client_id}\nset AURELIUS_CIMD_CLIENT_ID_URL={client_id}")


if __name__ == "__main__":
    main()
