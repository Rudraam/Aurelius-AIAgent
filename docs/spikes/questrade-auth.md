# Spike: Questrade MCP auth for a self-hosted client (Step 0)

**Date:** 2026-11-09. **Status:** partly done. The unauthenticated discovery is complete. The authenticated lifecycle (0.4) and the fixtures (0.1) need Rudra to run one interactive login (runbook below).

## 1. What ran and what is waiting

| Step | Status | Notes |
|---|---|---|
| 0.1 Observe real responses | ⏸ Waiting on Rudra | Questrade is **not** connected to the Claude Code session, so I made no MCP calls. Instead, `spike_auth_lifecycle.py fixtures` gets the same redacted fixtures through Aurelius's own read-only client. |
| 0.2 Auth discovery | ✅ Done | `backend/scripts/discover_auth.py` → `docs/spikes/questrade-oauth-metadata.json` |
| 0.3 Choose strategy | ✅ Provisional | Both CIMD and DCR are advertised. See §3. |
| 0.4 Lifecycle | ⏸ Code ready, tested against fakes | `uv run aurelius auth login`, then `scripts/spike_auth_lifecycle.py`. It needs one browser login. |
| SDK API check | ✅ Done | `mcp==2.3.0` source read. It has four gaps that matter for us (§4). |
| Decision 4: are order tools visible under a read-only token? | ⏸ Waiting on Rudra | `spike_auth_lifecycle.py fixtures` records the `order_tools_under_read_scope` observation from `tools/list` alone. It calls no preview or create tool. |

**Decisions (2026-11-09, round 1):**
1. CIMD first, served as a static file, with a permanent client_id URL. DCR is a fallback, allowed because `registration_endpoint` is published.
2. Hand-rolled login plus a bearer auth with refresh on 401 (narrowed in round 2: reads only, and only for `invalid_token`).
3. No private SDK API.
4. `orders.all` is deferred, and `403 insufficient_scope` maps to `ReauthRequired(scope)`.

**Decisions (2026-11-09, round 2):**
1. `httpx2` is the only HTTP stack, and `ca-certificates` goes in the image.
2. **No 401 retry for approval calls (absolute).** Read and approval auth are split, and the order flow gains `submit_failed`.
3. The domain is `aurelius.rivenpath.ai`. The CIMD document is rendered, and the login steps are in `docs/runbooks/cimd-login.md`.
4. git init, after a gitleaks check.

**Decisions (2026-11-09, round 3):** DCR is removed entirely: the code, `forget-dcr`, `--strategy`. CIMD is the only strategy, and every authorize, token and refresh request must carry exactly the configured CIMD URL as `client_id` (`tests/unit/test_client_id_invariant.py`). The runbook is now `docs/runbooks/cimd-login.md`.

**Result (2026-11-10): CIMD login refused, and the cause is a client allowlist rather than the account.**
- Rudra's `aurelius auth login` with the CIMD client_id failed at Questrade.
- On the same day, Rudra's own Claude Code `/mcp` login to the same server (`questrade`, `https://mcp.questrade.com`) succeeded and listed **33 tools**. So the account is eligible for MCP access and the consent flow works.
- Conclusion: Questrade accepts only allowlisted clients (pre-registered ones such as Claude Code, plus CIMD hosts it has approved). `aurelius.rivenpath.ai` is not on that list.
- Consequence: the Aurelius backend cannot be an MCP client for now. The CIMD code and document stay **dormant but intact**, pending an allowlist request to Questrade. Nothing is removed, and the client_id invariant still holds.
- The tool observations moved to the Claude Code session, under `.claude/settings.json` denies for every mutating tool. See `docs/spikes/questrade-tools.md`.

See `CLAUDE.md`.

## 2. What the server advertises (observed)

**Unauthenticated `initialize` →** `401`, `WWW-Authenticate: Bearer resource_metadata="https://mcp.questrade.com/.well-known/oauth-protected-resource"`, JSON body `{"error":"unauthorized","error_description":"Bearer token required"}`. Served by Cloudflare → Envoy. This matches §3 of the plan.

**Bad bearer token →** `401`, `WWW-Authenticate: Bearer realm="https://mcp.questrade.com/v1/brokerage/mcp", error="invalid_token"`, `text/plain` body `Jwt is not in the form of Header.Payload.Signature…`.
- **Delta:** this 401 has **no `resource_metadata`**. The bodies differ too: JSON when the token is missing, plain text when it is invalid. Error mapping must key on status + `error=` and must not depend on the body format.
- Access tokens are **JWTs validated at the Envoy edge**. An expired token will most likely say `Jwt is expired`; to be confirmed with `probe --stale`.

**Protected resource metadata (RFC 9728):** `resource` = the MCP URL exactly. The authorization server is `https://login.questrade.com`, and header bearer is the only method. There are 21 scopes:
- coarse: `mcp:read`, `mcp:write`
- read: `mcp:brokerage.{accounts,balances,positions,orders,securities,quotes,charts,watchlists,account-transactions,trading-workflows,custom-indexing}.read`, `mcp:enterprise.balance-sheet.read`
- write: `mcp:brokerage.{orders,watchlists,custom-indexing,trading-workflows}.all`
- plus `openid`, `profile`, `offline_access`.

**Authorization server metadata (RFC 8414)** at `login.questrade.com/.well-known/oauth-authorization-server`. It looks like Duende IdentityServer (`/connect/*` endpoints).

| Field | Value |
|---|---|
| `client_id_metadata_document_supported` | **true** (CIMD) |
| `registration_endpoint` | **`https://mcp.questrade.com/connect/register`** (DCR). **Delta:** it is on the MCP host, not the issuer host. |
| `code_challenge_methods_supported` | `plain`, `S256` (we use S256 only) |
| `token_endpoint_auth_methods_supported` | `client_secret_basic`, `client_secret_post`, `none` |
| `grant_types_supported` | `authorization_code`, `refresh_token`, plus many we will not use (password, client_credentials, device_code, CIBA, token-exchange, …) |
| `authorization_response_iss_parameter_supported` | true (RFC 9207: we validate `iss`) |
| `revocation_endpoint` | present. Its auth methods do **not** include `none`, so a public client may not be able to revoke its own refresh token. To be tested. |
| `dpop_signing_alg_values_supported` | present. DPoP is optional; we do not need it for Phase 1. |

## 3. Auth strategy

> **Finding of 2026-11-09: DCR is ruled out.** A real `auth login --strategy dcr` got back the `client_id` **`claude-code-public`** from `https://mcp.questrade.com/connect/register`. That is Claude Code's pre-registered public client, not a client issued to Aurelius.
> - Questrade's DCR endpoint is a compatibility shim that returns fixed client IDs. This matches the technical reference, which lists no client that uses DCR: everything else signs in with CIMD or a pre-registered client.
> - The authorize request then hit Questrade's generic error page, because `claude-code-public` is not registered with Aurelius's redirect URI.
> - Aurelius must **never** operate under another application's `client_id`, so DCR is not a usable fallback, even if a redirect URI could be made to match.
> - The planned paired probe (our `client_id` vs a bogus one) was skipped: it would have sent requests under Claude Code's identity, and no result could change the decision.
> - **Strategy: CIMD only.** The interim host is this repo's GitHub Pages (an Actions deploy of `deploy/static` only) on `aurelius.rivenpath.ai`; it moves to the VM's Caddy later at the same, permanent URL.

*The analysis below predates the finding above and is kept for the record; the DCR parts are superseded.*

**Original recommendation: CIMD for production, with DCR as the fallback. Use DCR first to prove the lifecycle quickly, because it needs no domain.**

- **CIMD:** Questrade documents CIMD as the path for new clients. Aurelius keeps no server-side registration state, and the client identity is a URL Rudra controls. Two risks remain:
  - Many authorization servers only accept CIMD documents from **allowlisted hosts**. Questrade names only Claude, ChatGPT, VS Code and Codex, so `aurelius.<domain>` may be rejected. That can only be found out empirically.
  - The authorization server may re-fetch the document later, so `/oauth/client-metadata.json` must stay up.
- **DCR:** the endpoint is advertised, but it is unknown whether it is open or needs an initial access token. I did **not** probe it, because a successful POST creates a client registration at Questrade. Risks:
  - the registration may be garbage-collected;
  - dynamically registered clients may get shorter refresh lifetimes.
- **Redirect URI for production:** the API runs on the Oracle VM, where no browser can open, so production should use the public redirect `https://<AURELIUS_PUBLIC_BASE_URL>/oauth/callback`. Loopback (`http://127.0.0.1:8765/callback`) is only for dev/spike logins on Rudra's machine. The CIMD document should list both.
- **Scopes (least privilege):**
  - Phase 1 read: `offline_access mcp:read` + the 8 brokerage `*.read` scopes listed in the script.
  - Trading would add only `mcp:brokerage.orders.all`. It is unknown whether the `preview_*` tools also need it.
  - Never request `watchlists.all`, `custom-indexing.all`, `trading-workflows.all`, or `mcp:write` unless the server requires it.
  - Open question: does Questrade's "read vs trade" consent toggle map to `mcp:read`/`mcp:write` or to the fine-grained scopes? The login step records the granted scope.

## 4a. As built (after the decisions)

`backend/app/integrations/questrade/`:
- `auth.py`: discovery, CIMD client identity (`CimdClientId`, `cimd_registration`; DCR removed in round 3), hand-rolled login (`begin_login`/`complete_login`), and `TokenManager` (single-flight refresh, persist-before-use, `min_remaining`). It also holds the two auth classes:
  - `ReadBearerAuth`: refreshes and resends once, only on a 401 with `error="invalid_token"`.
  - `ApprovalBearerAuth`: refreshes first if less than 5 minutes remain, and never resends; a 401 raises `AuthExpired`.
- `token_store.py`: the `TokenStore` protocol (CAS `version` + `refresh_lock()`), an in-memory store, and an AES-GCM file store for dev.
- `transport.py`: `read_session` and `approval_session`, each with its own auth instance and its own `httpx2.AsyncClient`, using only public SDK API.
- `errors.py`: `find_in_chain`, which recovers our typed errors through the SDK's exception wrapping.
- `app/domain/order_state.py`: the full state machine from spec §10 plus `submit_failed`. Illegal transitions raise.
- `app/services/order_submission.py`:
  - `submit_draft`: exactly one create call; auth failure leads to `submit_failed`, an ambiguous failure to `unknown`.
  - `authorize_resubmission`: needs a human, then a `list_instructions` check. If the check is clean, the draft goes back to `previewed`; otherwise to `unknown`.

**Spec deltas:**
- **`httpx2` is the only HTTP stack**, and `httpx`, `httpcore`, `certifi` and `respx` are gone from the lockfile. The SDK transport needs `httpx2.Auth`.
- On Linux, `httpcore2` trusts **only the OS CA store**. `backend/Dockerfile` installs `ca-certificates`, and the build fails if no CA file is visible. I verified TLS to `login.questrade.com` from inside the container, and the image also builds and runs on linux/arm64.
- `Authlib` is not used: the hand-rolled flow is about 150 lines and fully tested.

**Round 2 tests (74 total):**
- Approval:
  - a 401 (whether or not it says `invalid_token`) raises `AuthExpired` after one request and no refresh;
  - with 4 minutes left, the token is refreshed before the request; with 10 minutes left it isn't;
  - if the pre-refresh fails, nothing is sent.
- Read: a 401 with `invalid_token` gets one refresh and one resend; any other 401 raises `AuthExpired` and is not resent.
- Submission:
  - every failure path calls create exactly once;
  - an auth failure (including one wrapped in an `ExceptionGroup`) leads to `submit_failed`;
  - a timeout, reset or cancellation leads to `unknown`;
  - the resubmission gate is covered (human required, clean check, suspicious instruction or missing timestamp leads to `unknown`, a failed check leaves the draft unchanged);
  - illegal transitions raise.
- Through the real SDK: an approval session hitting a 401 sends one HTTP request, executes `create_order_instruction` **zero** times, and leaves the draft in `submit_failed`.

I mutation-checked these too. Wiring `ReadBearerAuth` into the approval session made 3 integration tests fail. Making the approval auth refresh and resend made 4 tests fail.

**Round 1 tests** (the ones you asked for):

| Required test | Where |
|---|---|
| A state mismatch is rejected, and no token request is sent | `test_state_mismatch_rejected_and_no_token_request`, plus missing-state and `iss` mismatch |
| The verifier, code, state and tokens are never logged or repr'd | `test_login_never_logs_or_reprs_secrets`, `test_refresh_never_logs_tokens` |
| Concurrent refresh leads to exactly one rotation | `test_concurrent_401s_cause_exactly_one_rotation` (20 parallel requests, token endpoint called once, store version +1), `test_concurrent_expiry_refresh_is_single_flight` |
| The rotated refresh token is saved before the new access token is used | `test_rotated_refresh_token_saved_before_new_access_token_used`, `test_new_access_token_never_used_if_save_fails` |
| `403 insufficient_scope` maps to `ReauthRequired(scope)` | `test_403_insufficient_scope_maps_to_reauth_with_scope` |
| No private SDK imports | `test_no_private_sdk.py` |
| It works through the real SDK transport | `tests/integration/test_auth_with_sdk_transport.py`: in-process `MCPServer`, a 401 leads to a refresh and a successful tool call; a restart after expiry refreshes before the first request; `invalid_grant` surfaces `ReauthRequired` through the SDK |

I mutation-checked the two key tests. Removing the single-flight re-check made 2 tests fail. Handing out the new token before the save completed made 3 tests fail.

**Known limit:** if the store write fails *after* the server has rotated the refresh token, the new refresh token is lost, and the next refresh will need `ReauthRequired`/re-login. That is inherent to rotation. It fails closed: the new access token is never used.

## 4. MCP Python SDK (`mcp==2.3.0`): what we use and what we hand-roll

> Superseded by §4a. Kept for the record: we no longer use `OAuthClientProvider` or any private method.

**Deltas from the plan's stack:**
- The SDK now runs on **`httpx2`** (a transitive dependency), not `httpx`.
- **`FastMCP` was renamed `mcp.server.mcpserver.MCPServer`**. The fake server will use `MCPServer`.
- I propose pinning `mcp==2.3.0` exactly, because we override one private method (gap 2).

**What we use from the SDK:**
- `OAuthClientProvider` (an httpx2 `Auth`) handles bearer injection, the refresh grant, PRM/AS discovery, and RFC 8707 `resource`.
- `TokenStorage` is a Protocol; we back it with Postgres.
- The client is `Client(streamable_http_client(url, http_client=create_mcp_http_client(auth=provider)))`.
- Good news: on refresh, the SDK calls `storage.set_tokens()` **before** retrying the request with the new token. If the refresh response omits `refresh_token`, the SDK carries the old one forward. That gives "persist the rotated token before use", provided our `set_tokens` is atomic (optimistic-lock `version` column).

**Gaps found by reading the source:**
1. **The SDK ignores our requested scope.** On a 401 it overwrites `client_metadata.scope` with *all* `scopes_supported` from the PRM. For Questrade that means asking for `orders.all`, `trading-workflows.all`, `custom-indexing.all` and `mcp:write`.
   → **We hand-roll the interactive login** (PKCE S256, state, `iss` check, code exchange, plus DCR or CIMD client info) with our exact scope list, and write the result into the SDK's `TokenStorage` format. The SDK then handles only the headless runtime (bearer + refresh).
2. **Token expiry is not restored on restart.** `_initialize()` loads tokens but leaves `token_expiry_time=None`, so an expired access token counts as valid. The server returns 401, and the SDK then jumps straight to the **interactive** flow instead of refreshing. Restart-after-expiry would therefore always need a re-login.
   → Subclass `_initialize()` to restore the absolute `access_expires_at` from storage. This is about 4 lines, but it is a private API, hence the exact pin plus a test that fails if the upstream behaviour changes.
3. **Refresh failure falls into the interactive flow** and calls `redirect_handler`.
   → The headless `redirect_handler` raises `ReauthRequired`. That error surfaces on `/health/questrade`.
4. **The SDK has no proactive refresh.** It refreshes lazily on the next request after expiry.
   → Our scheduler job marks the token stale when <20% of its lifetime remains, then makes a cheap read call so the SDK refresh path runs (or we call the refresh grant directly through the same storage).

## 5. Token behaviour

| Item | Value |
|---|---|
| Access token format | JWT (inferred from the edge error) |
| Access-token lifetime | **TBD**: `login` records `expires_in` and the JWT `exp − iat` |
| Refresh tokens issued? | **TBD**: needs `offline_access` and consent |
| Refresh rotation | **TBD**: `force-refresh` records `refresh_rotated` |
| Refresh-token lifetime | **TBD**: probably not discoverable, since a public client likely cannot use introspection. Watch for the first natural `ReauthRequired`. |
| Error shape on expiry | **TBD**: run `probe --stale` after expiry. The bad-token shape is above. |

## 6. Tool list compared with plan §3

**Not observed yet.** It needs an authenticated `tools/list`. `fixtures` saves the full catalog with input schemas to `backend/tests/fixtures/questrade/_tools_list.json`, and the diff against the 34 documented tools will be added here.

## 7. Risks

1. CIMD host allowlisting at Questrade could block the clean path. The mitigations are DCR, then plan option (a): ask Questrade API support.
2. Refresh-token lifetime is unknown. If it is short (days), "headless" really means "log in weekly". That would hurt the daily 16:30 sync but is acceptable for Phase 1.
3. We depend on SDK internals (gap 2). Mitigation: exact pin plus a regression test.
4. A shared rate-limit budget with Rudra's other MCP clients (e.g. the Claude app) can starve Aurelius. Mitigation: local buckets at 80% of the published limits, and `RateLimited` errors that are visible to the caller.
5. 401 bodies vary between JSON and plain text, so error mapping must use status + `WWW-Authenticate` only.
6. The authorization server enables risky grants (password, implicit, …). This does not affect Aurelius, but our code must never request them, which a test can assert.

## 8. Proposed Phase 1 milestones (after this spike is approved)

1. **M1: Skeleton + secure auth.** uv project, config, AES-GCM, Postgres `oauth_client`/`oauth_token` with an optimistic lock, hand-rolled `aurelius auth login` (CIMD or DCR), headless SDK provider with the gap fixes, `/health`, `/health/questrade`, `/oauth/*`, Alembic, docker compose (arm64 + amd64), Caddy.
2. **M2: MCP client core.** `call_tool` wrapper, allowlist + `ToolNotAllowed`, token-bucket limiter, error mapping, audit `mcp_call` table, correlation IDs, catalog snapshots + drift, a fake `MCPServer` with scripted failure modes.
3. **M3: Read sync.** Typed adapters + `Money`, the snapshot tables, an idempotent activity ledger, the scheduler, and the `/accounts`, `/portfolio`, `/sync` and `/market/quotes` endpoints.
4. **M4: Orders behind guardrails.** Guardrails (with hypothesis tests), the order state machine, the draft → preview → confirm → create → poll → reconcile flow tested only against the fake, `unknown` reconciliation, drift-triggered disable, and `manual_order_smoke.py`.
5. **M5: Agent seam + hardening.** `agent_tools/`, the full invariant suite, a redaction log test, the migration round-trip, coverage ≥85%, and CLAUDE.md, README and architecture docs.

## 9. Runbook for Rudra (about 10 minutes, read-only)

Run this in PowerShell from `D:\Aurelius\backend`. When Questrade's consent screen appears, **grant read only**.

```powershell
$env:AURELIUS_ENCRYPTION_KEY = (uv run aurelius auth genkey)
# CIMD is the only strategy (round 3). Publish the document first: docs/runbooks/cimd-login.md
$env:AURELIUS_CIMD_CLIENT_ID_URL = "https://aurelius.rivenpath.ai/oauth/client-metadata.json"
uv run aurelius auth login                                   # browser login, read scopes only
uv run python scripts/spike_auth_lifecycle.py call           # new process, headless list_accounts
uv run python scripts/spike_auth_lifecycle.py force-refresh  # records lifetime and rotation
uv run python scripts/spike_auth_lifecycle.py fixtures       # tools/list, order-tool visibility, redacted fixtures
uv run python scripts/spike_auth_lifecycle.py probe          # 401 shape for a bad token
uv run aurelius auth status                                  # note access_expires_in_s
# after the access token expires naturally:
uv run python scripts/spike_auth_lifecycle.py probe --stale
uv run python scripts/spike_auth_lifecycle.py call           # restart-after-expiry: should refresh, not ask for login
```

Keep the same `$env:AURELIUS_ENCRYPTION_KEY` for the whole session; tokens encrypted with one key cannot be read with another. If Questrade rejects the CIMD client_id (host not allowlisted), the error page or the `authorization failed: …` message is the evidence (this happened on 2026-11-10, see §1; there is no DCR fallback). Results go to `backend/.spike/observations.jsonl`, which contains no token values. Send me that file and I will fill in §5–6. Look over `backend/tests/fixtures/questrade/*.json` for anything personal before it is ever committed.
