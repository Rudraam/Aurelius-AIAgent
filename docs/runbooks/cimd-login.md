# Runbook: sign in to Questrade with CIMD

**Who:** Rudra. Every step is read-only toward the brokerage; no orders are involved.

CIMD is Aurelius's **only** sign-in strategy. DCR was removed on 2026-11-09: Questrade's registration endpoint answered with Claude Code's `claude-code-public` client instead of registering Aurelius (see `docs/spikes/questrade-auth.md` §3).

The permanent client_id is `https://aurelius.rivenpath.ai/oauth/client-metadata.json`. It is rendered by `backend/scripts/render_cimd.py` into `deploy/static/oauth/client-metadata.json` and guarded by `tests/unit/test_cimd_document.py`. Never move it or rename it.

## 1. Publish the document (interim: this repo's GitHub Pages)

1. DNS: `CNAME aurelius → rudraam.github.io`. Optionally, verify `rivenpath.ai` under your GitHub account (a TXT record); this prevents someone else's Pages site from taking the subdomain over.
2. `.github/workflows/pages.yml` deploys **only** `deploy/static` from this repo on every push to `main` that touches it (or when run by hand). There is no separate repo and no copy of the file. It refuses to deploy if `deploy/static` contains anything beyond the allowlist.
3. Repo Settings → Pages: Source **GitHub Actions**, custom domain `aurelius.rivenpath.ai`, then **Enforce HTTPS** once the certificate is issued. There is no `CNAME` file; Actions-based Pages ignores one.
4. Verify, from a terminal:
   - HTTPS with a valid certificate;
   - `200` with **no redirect**;
   - a JSON `Content-Type` (Pages sends `application/json; charset=utf-8`);
   - `client_id` exactly equal to the URL;
   - `/` and every other path return `404` (nothing else is published).

Later, the VM's Caddy serves the same file at the same URL (`tests/deploy/test_caddy_cimd.py`). Moving hosts doesn't change the client_id.

## 2. Log in
From `D:\Aurelius\backend`, using the same `AURELIUS_ENCRYPTION_KEY` every time:
```powershell
$env:AURELIUS_CIMD_CLIENT_ID_URL = "https://aurelius.rivenpath.ai/oauth/client-metadata.json"
uv run aurelius auth login      # grant READ only
uv run aurelius auth status     # expect that client_id, a refresh token, a sensible lifetime
uv run python scripts/spike_auth_lifecycle.py call
```
Without `AURELIUS_CIMD_CLIENT_ID_URL`, `auth login` and `auth refresh` stop with a clear error before any network call.

**If Questrade rejects the loopback redirect** (`http://127.0.0.1:8765/callback`), for example with an `invalid_redirect_uri` or the generic error page: stop and report it. The public `/oauth/callback` route is **not** built yet; that is decided together first. The client_id does not change either way; only the document's `redirect_uris` would.

**If Questrade rejects the CIMD client_id itself** (the host isn't allowlisted): record the exact error in `docs/spikes/questrade-auth.md` and ask Questrade API support about allowlisting `aurelius.rivenpath.ai`.

## 3. Verify
- `auth status` shows the CIMD `client_id` and a fresh `access_expires_in_s`.
- `spike_auth_lifecycle.py force-refresh`, then `call`, still work, so the refresh path works under the CIMD client.
- The connected apps in Questrade show one "Aurelius" grant.
