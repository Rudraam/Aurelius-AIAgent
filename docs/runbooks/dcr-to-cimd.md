# Runbook: move Questrade auth from DCR to CIMD

**When:** after `https://aurelius.rivenpath.ai/oauth/client-metadata.json` is live. Until then, `auth login` falls back to DCR.
**Who:** Rudra. Every step is read-only toward the brokerage; no orders are involved.

The CIMD client_id is permanent: `https://aurelius.rivenpath.ai/oauth/client-metadata.json`. It is rendered by `backend/scripts/render_cimd.py` into `deploy/static/oauth/client-metadata.json`, served statically by Caddy, and guarded by `tests/unit/test_cimd_document.py`. Never move it, rename it, or serve it from FastAPI.

## 1. Publish the document
1. DNS: create an A/AAAA record for `aurelius.rivenpath.ai` pointing to the Oracle VM. It returned NXDOMAIN on 2026-11-09.
2. Deploy Caddy with `AURELIUS_DOMAIN=aurelius.rivenpath.ai` and `deploy/static` mounted at `/srv/static`.
3. Check the document: `curl -sS https://aurelius.rivenpath.ai/oauth/client-metadata.json` must return the committed JSON with `Content-Type: application/json`, over a valid certificate.

## 2. Re-login with CIMD
In PowerShell, from `D:\Aurelius\backend`, using the same `AURELIUS_ENCRYPTION_KEY` as before:
```powershell
$env:AURELIUS_CIMD_CLIENT_ID_URL = "https://aurelius.rivenpath.ai/oauth/client-metadata.json"
uv run aurelius auth login --strategy cimd     # grant READ only
uv run aurelius auth status                    # expect "strategy": "cimd", that client_id, and a refresh token
uv run python scripts/spike_auth_lifecycle.py call
```
A successful CIMD login **replaces** the stored DCR client and its tokens in the local store (one client is stored at a time). The CLI prints a note when it does this.

**If Questrade rejects the loopback redirect** (`http://127.0.0.1:8765/callback`), for example with an `invalid_redirect_uri`/`unauthorized_client` error page:
- Re-render the document without it: `uv run python scripts/render_cimd.py --domain aurelius.rivenpath.ai --no-loopback`. Then redeploy and run `pytest tests/unit/test_cimd_document.py`.
- Log in through the public route `https://aurelius.rivenpath.ai/oauth/callback` instead. That route is part of M1 (FastAPI `/oauth/callback` plus `aurelius auth login --redirect public`); until it exists, stay on DCR.
- The client_id does not change, so this is a document edit, not a new client.

**If Questrade rejects the CIMD client_id itself** (host not allowlisted): stay on DCR, record the exact error in `docs/spikes/questrade-auth.md`, and ask Questrade API support about allowlisting `aurelius.rivenpath.ai`.

## 3. Revoke the DCR grant at Questrade
The token endpoint's revocation methods don't include `none`, so a public client probably can't revoke its own refresh token through the API. Revoke it in Questrade instead: open the connected apps / authorized applications page in the Questrade app or web portal, find the **DCR** "Aurelius" entry, and revoke it.

After the CIMD login there may be two "Aurelius" entries. Revoke the one whose client_id is **not** the URL above. Your Questrade connection for Claude (a separate grant) is not touched.

## 4. Delete the DCR client credentials locally
After step 2 the DCR registration is already overwritten in the store. Confirm, and clean up if a DCR client is still there (for example after an aborted CIMD login):
```powershell
uv run aurelius auth status                    # must show "strategy": "cimd"
uv run aurelius auth forget-dcr                # no-op if the stored client is CIMD
uv run aurelius auth forget-dcr --yes          # only if it still shows a DCR client
```
**Postgres store (M1 onwards):** `forget-dcr` deletes the `oauth_client` row with `strategy='dcr'` and its `oauth_token` rows in one transaction. The audit tables keep the history; they never contain secrets.

## 5. Verify
- `auth status` shows `"strategy": "cimd"` and a fresh `access_expires_in_s`.
- `spike_auth_lifecycle.py force-refresh`, then `call`, still work, so the refresh path works under the CIMD client.
- Only one Aurelius grant is left in Questrade's connected apps.
