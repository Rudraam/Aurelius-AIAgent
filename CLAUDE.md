# Aurelius: conventions for Claude Code

Single-user personal finance agent for Rudra (Ontario). Phase 1 is the Questrade MCP brokerage layer.
The full spec is in `AURELIUS_PHASE1.md`. Spike findings are in `docs/spikes/` and runbooks in `docs/runbooks/`. This code touches a real brokerage account.

## Non-negotiable invariants (never weaken one to make a test pass; stop and explain instead)

1. **No autonomous orders.** Every create needs explicit confirmation in Aurelius **and** push approval in the Questrade app.
2. **Read-only by default.** `AURELIUS_TRADING_ENABLED=false` makes `create_order_instruction` unreachable.
3. **Tool allowlist.**
   - Read: `list_accounts get_balances get_positions get_order_history get_account_activities search_symbols get_quotes get_historical_data get_instruction list_instructions`.
   - Order: `preview_order_instruction`, plus `create_order_instruction` with `operation="create"` only.
   - Everything else raises `ToolNotAllowed`.
4. **Preview before create.** The create args hash must equal the stored preview's hash.
5. **Never auto-retry an approval tool. This is absolute, with no exceptions, including HTTP 401.**
   - Approval calls go through `approval_session` / `ApprovalBearerAuth`. It refreshes *before* the call if less than 5 minutes of token lifetime remain, and never resends.
   - Any 401 raises `AuthExpired`, and the draft moves to `submit_failed`.
   - An ambiguous failure (timeout, transport error, cancellation) moves the draft to `unknown`, then reconcile via `list_instructions`/`get_instruction`.
   - Resubmitting from `submit_failed` is a human action, gated by `authorize_resubmission`: a `list_instructions` check must show nothing created for the account since the attempt. Anything found, or anything without a timestamp, moves the draft to `unknown`.
   - The draft then goes back to `previewed` and needs a fresh preview and a new confirmation.
6. **Local guardrails run before preview.** A violation is a hard stop.
7. **Secrets.**
   - Tokens and client secrets are encrypted at rest (AES-GCM, `AURELIUS_ENCRYPTION_KEY`).
   - Never log or put into an exception message any of: access/refresh tokens, the PKCE verifier, the auth code, OAuth `state`, client secrets.
   - Secret dataclass fields use `repr=False`.
   - Account numbers are masked to the last 4.
8. **Developer safety.**
   - Claude Code and tests never call `preview_*`/`create_*` (or any non-read tool) against live Questrade.
   - Live scripts are written for Rudra to run.
   - `scripts/manual_order_smoke.py` is Rudra's alone.
9. **Money and time.** Money is `Decimal` / `NUMERIC(18,4)`, never float. Store timestamps in UTC and display them in America/Toronto. Keep CAD and USD separate.

## Auth design (decisions of 2026-11-09)

- **One HTTP stack: `httpx2`.** The MCP SDK transport, token endpoint, discovery and DCR all use it, and `httpx` is not a dependency.
  - On Linux, httpx2 trusts **only the OS CA store** (no certifi), so the Docker image installs `ca-certificates`, and the build fails if it's missing.
- **Client registration: CIMD.**
  - The permanent client_id is `https://aurelius.rivenpath.ai/oauth/client-metadata.json`. The file is `deploy/static/oauth/client-metadata.json`, rendered by `backend/scripts/render_cimd.py`, served statically by Caddy, and guarded by `tests/unit/test_cimd_document.py`.
  - DCR is a fallback, allowed only because the server publishes a `registration_endpoint` (`choose_strategy`).
  - Migration steps: `docs/runbooks/dcr-to-cimd.md`.
- **Login is hand-rolled** in `app/integrations/questrade/auth.py`: PKCE S256, constant-time `state`, RFC 9207 `iss`, RFC 8707 `resource`. The SDK's `OAuthClientProvider` is not used.
- **Two auth classes, never shared.** Both are `httpx2.Auth`, which the SDK requires.
  - `ReadBearerAuth` (via `read_session`): refreshes and resends once **only** on a 401 with `WWW-Authenticate: error="invalid_token"`. Any other 401 raises `AuthExpired`. A 401 immediately after a refresh raises `ReauthRequired`.
  - `ApprovalBearerAuth` (via `approval_session`): see invariant 5.
  - Both map a 403 `insufficient_scope` to `ReauthRequired(scope=...)`.
  - The SDK wraps errors raised in the transport (task groups). Use `errors.find_in_chain` to recover the typed error.
- **Token rotation.**
  - `TokenManager.refresh` is single-flight: it holds `store.refresh_lock()` and re-checks the stored token after acquiring the lock.
  - A rotated refresh token is durably saved (`TokenStore.save`, compare-and-swap on `version`) before the new access token is returned.
  - The Postgres store must implement `refresh_lock()` with an advisory lock, because the encrypted-file store is single-process only.
- **Scopes.**
  - Phase 1 requests `READ_SCOPES` only.
  - `mcp:brokerage.orders.all` is added at the order milestone via re-login.
  - Never request `mcp:write`, `watchlists.all`, `custom-indexing.all` or `trading-workflows.all`.
- **No private SDK API.**
  - Never import `mcp.*._*` modules or `_`-prefixed names; `tests/unit/test_no_private_sdk.py` enforces this.
  - If a private call ever becomes unavoidable, isolate it in one adapter with a version assertion and a contract test, and document it here.
  - The exact versions are pinned by `backend/uv.lock`.

## Gotchas found against the live server

- Questrade's 401 responses differ:
  - no token: JSON body, with `resource_metadata` in `WWW-Authenticate`;
  - bad or expired token: `text/plain` body, `error="invalid_token"`, and **no** `resource_metadata`.
  - Map errors on status + `WWW-Authenticate` only, never on the body.
- The DCR endpoint is `https://mcp.questrade.com/connect/register`, which is not on the issuer host (`login.questrade.com`).
- mcp 2.x renamed `FastMCP` to `mcp.server.mcpserver.MCPServer`. The fake server and the tests use `MCPServer` with `streamable_http_app(json_response=True, stateless_http=True, transport_security=...(enable_dns_rebinding_protection=False))` over `httpx2.ASGITransport`. Start the app's lifespan with `app.router.lifespan_context(app)`.
- `respx` only patches `httpx`. For httpx2 tests, use `tests/fakes.py::FakeHttp`, which routes requests and counts calls.

## Working conventions

- Python 3.12, managed with `uv`. Work from `backend/`:
  - `uv run ruff check app tests scripts`
  - `uv run ruff format …`
  - `uv run mypy` (strict, over `app/`)
  - `uv run pytest`
- Docker: `docker build -t aurelius-api:dev backend/`. Check arm64 with `docker buildx build --platform linux/arm64 --load …`.
- Ask before adding a dependency that isn't in the spec stack. Approved additions: `httpx2` (it replaces httpx), and Authlib (approved but unused).
- Use conventional commits, in small reviewable steps. Run gitleaks before committing. At the end of each milestone, run ruff, mypy and pytest and show the results.
- When the docs and the server disagree, trust the server and record the difference in `docs/spikes/`.
