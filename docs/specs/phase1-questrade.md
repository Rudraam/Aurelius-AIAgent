# Aurelius — Phase 1 spec: Questrade MCP brokerage layer

## Amendments (these win over the original text below)

The original spec is kept verbatim below this section. Where an amendment and the original disagree,
**the amendment wins**. `CLAUDE.md` holds the operative rules; each row cites the commit(s) that
introduced it.

| # | Amendment | Supersedes (original section) | Commit(s) |
|---|---|---|---|
| A1 | **DCR is removed; CIMD is the only client identity.** Questrade's DCR endpoint answered Aurelius with Claude Code's pre-registered client `claude-code-public`, and Aurelius must never operate under another application's client_id. New invariant: every authorize, token and refresh request carries **exactly** the configured CIMD URL (`https://aurelius.rivenpath.ai/oauth/client-metadata.json`) as `client_id`; anything else fails before sending. `auth login` requires `AURELIUS_CIMD_CLIENT_ID_URL`. | §0.3 decision tree (DCR branch), §3 "Client registration", §8 `oauth_client.strategy (cimd / dcr)` | `31a783b`, `e9c82c0` |
| A2 | **Never auto-retry an approval tool, absolutely, including on HTTP 401.** Approval calls use a separate auth (`ApprovalBearerAuth`) that refreshes *before* the call when <5 min of token lifetime remain and never resends; any 401 raises `AuthExpired`. Reads retry once, only on 401 `error="invalid_token"`. | §4 invariant 5, §11 retries | `1f7eec1` |
| A3 | **New order state `submit_failed`.** An auth failure on create (nothing executed) moves the draft to `submit_failed`; ambiguous failures go to `unknown`. Resubmission is a human action, gated by a `list_instructions` check (window starts 2 min before the attempt for clock skew) and a fresh preview. | §10 state machine | `1f7eec1`, `8daee09` |
| A4 | **One HTTP stack: `httpx2`.** The MCP SDK (2.x) is built on httpx2 and requires `httpx2.Auth`, so token/discovery calls use httpx2 too; `httpx` and `respx` are not dependencies (tests use `tests/fakes.py::FakeHttp`). The Docker image installs `ca-certificates` (httpx2 trusts only the OS CA store on Linux). | §6 stack (httpx, respx), §0.2 "httpx, no SDK" | `1f7eec1` |
| A5 | **Hand-rolled OAuth login; the SDK's `OAuthClientProvider` is not used** (it requests every advertised scope and loses token expiry on restart). No private SDK API. Read-only scopes only; `mcp:brokerage.orders.all` is added at the order milestone via re-login; 403 `insufficient_scope` maps to `ReauthRequired(scope)`. | §0.3 "Use the official MCP Python SDK's client auth support" | `1f7eec1` |
| A6 | **The CIMD document is a static file**, `deploy/static/oauth/client-metadata.json`, served by GitHub Pages from this monorepo (`.github/workflows/pages.yml` publishes exactly `deploy/static`) until the VM exists, then by Caddy at the same permanent URL. There is no FastAPI route for it. | §0.3 "FastAPI serves the client metadata JSON", §12 `GET /oauth/client-metadata.json` | `1f7eec1`, `eea0b2d` |
| A7 | **Submit gates enforced before any state change or call:** trading enabled, status `previewed`, `operation == "create"`, stored preview present, canonical args hash (floats rejected) equals the stored hash, the human-confirmed hash equals it too, preview age in [0, 5 min). | §4 invariants 2 and 4, §12 submit endpoint | `8daee09` |
| A8 | **Time rules use IANA `America/Toronto` (tzdata); fixed offsets are banned in production code.** The maintenance window, market hours and the 16:30 job are tested across DST (2026-11-01, 2027-03-14). On fall back, the repeated hour is blocked on both passes, **intentionally** (fail-closed). | §4 invariant 9, §9 market hours / maintenance window, §13 jobs | `9662eff`, `7d86cc4` |
| A9 | **Exchange holiday calendars are a hard prerequisite for ever enabling trading:** XTSE for `.TO` symbols, XNYS for US symbols, early closes included. They come from a committed file generated with dev-only `exchange_calendars`. Market orders are blocked beyond the file's coverage, and a test fails 90 days before coverage ends. Scheduled for M1. | §9 market hours | `7d86cc4` |
| A10 | **The repository is public.** Real account data never enters git. Live observations and fixtures stay in git-ignored `backend/.spike/`; fixtures are promoted only after redaction (fixed fake GUIDs, masked account numbers) and the fixture guard test. gitleaks runs as a fail-closed pre-commit hook and in CI. | §0.1 "Save redacted responses to `backend/tests/fixtures/questrade/`" | `a4a11b8`, `b40da52` |
| A11 | **Monorepo layout:** `backend/`, `mobile/` (reserved), `deploy/` (`static/` is the only publicly served tree; compose and the Caddyfile move here in M1), `docs/`, `.github/workflows/` (CI and Pages). | §7 repository layout | `084c1a1` |
| A12 | **Test tooling:** the fake server uses `mcp.server.mcpserver.MCPServer` (mcp 2.x renamed `FastMCP`). | §6 "fake Questrade MCP server built with the SDK's FastMCP", §15 | `1f7eec1` |

---

## Original text (verbatim, as received 2026-11-09)

> **Kickoff message to paste into Claude Code:**
> `Read AURELIUS_PHASE1.md in full. Then do Step 0 (discovery spike) only, write the spike report, and stop for my review before building anything else.`

---

## Before you start (human checklist — Rudra does these, not Claude Code)

- [ ] Questrade app is version 2.0.0 or higher on your phone, and **Push Approval** is set up. Without it, every order tool fails immediately.
- [ ] Optional but recommended for Step 0: connect Questrade to this Claude Code session **with read-only permission** (do not grant trade permission to the dev connection):
  `claude mcp add --transport http questrade https://mcp.questrade.com/v1/brokerage/mcp` → `/mcp` → questrade → Log in → grant **read** only.
- [ ] A domain or subdomain you control (for example `aurelius.yourdomain.ca`) pointed at the Oracle VM. It may be needed for OAuth client metadata and redirect URIs.
- [ ] Docker and `uv` installed locally.

---

## 1. Role

You are a senior backend engineer building the brokerage integration layer of **Aurelius**, a single-user personal finance agent owned and operated by one person in Ontario, Canada. This code touches a real brokerage account with real money. Write it like a fintech team would: explicit invariants, typed boundaries, an append-only audit trail, and tests that prove the dangerous paths are unreachable.

Prefer correctness and safety over cleverness. When something is ambiguous, stop and ask rather than guess.

## 2. Phase 1 mission

Build a FastAPI service that:

1. Authenticates to Questrade's hosted MCP server as its own OAuth client and keeps the session alive headlessly.
2. Discovers and snapshots the server's tool catalog, and detects schema drift.
3. Mirrors account state (accounts, balances, positions, activities, order history) into Postgres on a schedule. Questrade's MCP history only covers about 90 days, so Aurelius must keep its own permanent record.
4. Exposes a **guarded, typed brokerage interface**: read endpoints plus an order-drafting flow where every order is previewed, checked by local guardrails, explicitly confirmed, then sent to the Questrade app for push approval.
5. Logs every MCP call and every order state transition to an append-only audit trail.
6. Provides a clean seam (`agent_tools/`) that Aurelius's future CFO agent will use. The agent must never touch raw MCP.

## 3. Ground truth: Questrade MCP

Source: Questrade's public *MCP Technical Reference* and *Connect your Questrade account to AI* pages, retrieved November 9, 2026.

**Treat this as a starting hypothesis.** The server's own `tools/list` output and OAuth metadata are the source of truth. When they disagree with this section, the server wins: document the difference in the spike report.

### Connection
- URL: `https://mcp.questrade.com/v1/brokerage/mcp`
- Transport: **Streamable HTTP, stateless (no session ids)**. The server exposes **tools only, no resources**.
- Auth: OAuth per the MCP authorization spec. An unauthenticated request returns **401 with a `WWW-Authenticate` header** pointing to the protected resource metadata.
- Client registration: Claude, Claude Code, ChatGPT, VS Code and Codex sign in using **CIMD (Client ID Metadata Documents)**. Cursor uses a **pre-registered client**. Aurelius is a new, self-hosted client, so Step 0 must determine which registration path works for it.
- Read and trade permissions are **granted separately** at login.

### Tools (34 total; availability varies by account and some roll out gradually)
| Group | Tools | Kind |
|---|---|---|
| Accounts and portfolio | `list_accounts`, `get_balances`, `get_positions`, `get_order_history`, `get_account_activities` | Read |
| Market data | `search_symbols`, `get_quotes`, `get_historical_data`, `get_option_expiries`, `get_option_chain` | Read |
| Watchlists | `list_watchlists`, `get_watchlist`, `create_watchlist`, `edit_watchlist` | Read / write |
| Equity and ETF orders | `preview_order_instruction`, `create_order_instruction` (`operation` = create, modify or cancel) | Preview / **Approval** |
| Option orders | `preview_option_instruction`, `create_option_instruction` | Preview / **Approval** |
| Approval status | `get_instruction`, `list_instructions` | Read |
| Custom indexing | `preview_custom_index`, `create_custom_index`, `list_custom_indexes`, `get_custom_index`, `edit_custom_index` | Mixed |
| Balance sheet | `get_balance_sheet`, `get_balance_sheet_account` | Read |
| Flows | `list_flows`, `get_flow`, `get_flow_history`, `preview_flow`, `create_flow`, `update_flow_status` | Mixed |

### Order parameters (equities and ETFs)
- `accountId` (required, GUID from `list_accounts`), `instrument` (symbol), `qty`, `side` (buy or sell), `type` (market, limit, stop, stoplimit), `limitPrice`, `stopPrice`, `duration` (Day, GTC, GTEM, overnight; default Day).
- `qty` is a positive number with up to 4 decimal places. **Fractional quantities are accepted only for eligible securities, with market orders and Day duration.**
- IDs are GUIDs. Dates are `YYYY-MM-DD`. Date-times are ISO 8601 with an offset. Symbols are up to 32 characters (for example `SHOP.TO`).

### Approval semantics
- Approval tools never act on their own. They send a push approval request to the Questrade app. If the user declines or the request expires, nothing changes. If no device is enrolled, the call fails immediately.
- Some return immediately with `pending_approval` and an `instructionId`. Poll `get_instruction`. Terminal and intermediate statuses: `pending_approval`, `executing`, `succeeded`, `failed`, `denied`, `expired`.

### Operational limits
- **Maintenance window:** order tools are refused **daily 2:00–3:45 AM ET** with a retryable error that names the reopening time. The schedule can change.
- **Rate limits, per user across all of that user's AI agents:**
  - Read tools: 50 per minute, 1,000 per hour.
  - Create or change tools: 20 per minute, 300 per hour.
  - Rudra's other MCP clients (for example the Claude app) share this budget.
- Order and activity history through MCP covers roughly the **last 90 days**.

## 4. Non-negotiable invariants

These are architectural law. Encode them in `CLAUDE.md` and enforce them with tests.

1. **No autonomous orders.** Aurelius never creates, modifies or cancels an order without (a) an explicit confirmation from Rudra inside Aurelius, then (b) push approval in the Questrade app. The server enforces (b); Aurelius enforces (a) as well.
2. **Read-only by default.** `AURELIUS_TRADING_ENABLED=false` is the default. When false, no code path can reach `create_order_instruction`.
3. **Tool allowlist.** In Phase 1 the client can call only:
   - read tools: `list_accounts`, `get_balances`, `get_positions`, `get_order_history`, `get_account_activities`, `search_symbols`, `get_quotes`, `get_historical_data`, `get_instruction`, `list_instructions`
   - order tools: `preview_order_instruction`, and `create_order_instruction` with `operation="create"` only.

   Everything else is blocked in the client layer with a typed `ToolNotAllowed` error. That includes options, flows, custom indexes, watchlist writes, balance sheet, and modify or cancel operations.
4. **Preview before create, and they must match.** Every create call must reference a stored preview. The create arguments are hashed and must equal the preview's argument hash. Any mismatch aborts.
5. **Never auto-retry an approval tool.** Each draft carries an idempotency key. If a create call times out or returns an ambiguous result, mark the draft `unknown` and reconcile with `list_instructions` and `get_instruction` before any new attempt.
6. **Local guardrails run before preview** (section 9). A guardrail failure is a hard stop, not a warning.
7. **Secrets never leak.**
   - OAuth tokens and client credentials are encrypted at rest with AES-GCM, using a key from the environment.
   - Tokens are never logged.
   - Account numbers are masked to the last 4 characters everywhere outside the raw encrypted store.
   - Only `.env.example` lives in the repo.
8. **Developer safety.** Neither you (Claude Code) nor any test may call an approval tool against the live server. Live order testing happens only through `scripts/manual_order_smoke.py`. That script requires `AURELIUS_TRADING_ENABLED=true` and an interactive typed confirmation, and Rudra runs it himself.
9. **Money is `Decimal`, never `float`.** Store money as `NUMERIC(18,4)`. Store all timestamps in UTC. Display them in `America/Toronto`. Keep CAD and USD separate; never silently convert.

## 5. Step 0: discovery spike (do this first, then stop)

Goal: prove a self-hosted backend can authenticate and stay authenticated, and capture real response shapes. **Do not build the app until this is reviewed.**

### 0.1 Observe real responses (if the dev MCP connection exists)
If Questrade is connected to this Claude Code session, call **read tools only**: `list_accounts`, `get_balances`, `get_positions`, `get_order_history`, `get_account_activities`, `get_quotes`.
- Save **redacted** responses to `backend/tests/fixtures/questrade/<tool>.json`. Mask account numbers, replace GUIDs with stable fake GUIDs, and round balances.
- Record the exact input schemas from the tool definitions.
- Never call `preview_*` or `create_*` tools from this session.

### 0.2 Auth discovery script
Write `backend/scripts/discover_auth.py` (httpx, no SDK) that:
1. Sends an unauthenticated MCP request and captures the `WWW-Authenticate` header.
2. Fetches the protected resource metadata (RFC 9728) and the authorization server metadata (RFC 8414, or OpenID discovery).
3. Prints and saves to `docs/spikes/questrade-oauth-metadata.json`:
   - `client_id_metadata_document_supported` (CIMD)
   - `registration_endpoint` (dynamic client registration)
   - supported scopes
   - PKCE methods
   - `token_endpoint_auth_methods_supported`
   - grant types

### 0.3 Choose the auth strategy (decision tree)
- **CIMD supported:**
  - FastAPI serves the client metadata JSON at `https://<AURELIUS_PUBLIC_BASE_URL>/oauth/client-metadata.json`. That URL is the `client_id`.
  - Fields: `client_name: "Aurelius"`, `redirect_uris`, `grant_types: ["authorization_code","refresh_token"]`, `response_types: ["code"]`, `token_endpoint_auth_method: "none"`.
  - Use PKCE S256.
- **Dynamic client registration supported:** register once, then persist the client information encrypted.
- **Neither works for a self-hosted client:** STOP. Report the options with trade-offs:
  - (a) ask Questrade API support about pre-registration;
  - (b) for Phase 1 reads, fall back to Questrade's official REST API with a personal refresh token;
  - (c) keep a Claude-hosted client as the trading surface and have Aurelius handle only reads and history.

Use the official MCP Python SDK's client auth support (an OAuth client provider plus a custom token storage backed by Postgres) rather than hand-rolling OAuth. **Check the installed SDK version's actual API before writing code.** Hand-roll only the pieces the SDK doesn't cover for this server, and say which.

### 0.4 Prove the lifecycle end to end
1. `uv run aurelius auth login` opens the authorize URL with a loopback or public redirect, completes PKCE, and stores tokens encrypted.
2. Kill the process. Restart it. Call `list_accounts` headlessly.
3. Force a refresh, then call `list_accounts` again.
4. Record:
   - observed access-token lifetime;
   - whether refresh tokens rotate (if they do, persist the new token **atomically before** using the new access token);
   - refresh-token lifetime if discoverable;
   - the exact error shape on expiry.

### 0.5 Spike report
Write `docs/spikes/questrade-auth.md` with:
- what worked;
- the chosen strategy;
- token behavior;
- the actual tool list versus section 3;
- risks;
- your recommended Phase 1 plan with milestones.

**Then stop and summarize it to me.**

## 6. Stack

- **Language and tooling:** Python 3.12, `uv`, `ruff`, `mypy --strict` (required on `app/integrations`, `app/domain`, `app/services`).
- **Web:** FastAPI, Pydantic v2, pydantic-settings.
- **Data:** PostgreSQL 16, SQLAlchemy 2.0 (async) with asyncpg, Alembic.
- **MCP:** official `mcp` Python SDK, Streamable HTTP client.
- **HTTP and infra:** httpx, tenacity (read paths only), structlog (JSON logs), cryptography (AES-GCM), APScheduler (AsyncIOScheduler).
- **Tests:** pytest, pytest-asyncio, hypothesis, respx where useful, and a **fake Questrade MCP server built with the SDK's FastMCP**.
- **Deploy:**
  - Docker Compose with services `api`, `postgres` and `caddy` (Caddy handles automatic HTTPS).
  - Images must run on **linux/arm64** (Oracle Ampere) as well as amd64.
- Ask before adding any dependency not listed here.

## 7. Repository layout

```
aurelius/
├── CLAUDE.md                     # conventions + invariants (you create this)
├── AURELIUS_PHASE1.md            # this file
├── docker-compose.yml
├── Caddyfile
├── .env.example
├── docs/
│   ├── spikes/
│   └── architecture.md
└── backend/
    ├── pyproject.toml
    ├── alembic/
    ├── app/
    │   ├── main.py
    │   ├── config.py
    │   ├── api/                  # routers: health, auth, accounts, portfolio, market, orders, audit
    │   ├── domain/               # money.py (Decimal types), models, enums, order state machine
    │   ├── integrations/questrade/
    │   │   ├── auth.py           # OAuth provider wiring, encrypted token storage
    │   │   ├── client.py         # session, call_tool wrapper, allowlist, rate limiter, error mapping
    │   │   ├── catalog.py        # tools/list snapshots, schema hashing, drift detection
    │   │   ├── adapters.py       # typed functions per tool → domain models
    │   │   └── errors.py
    │   ├── services/
    │   │   ├── sync.py           # snapshot + upsert history
    │   │   ├── guardrails.py
    │   │   └── orders.py         # intent → guardrails → preview → confirm → create → poll → reconcile
    │   ├── audit/                # append-only MCP call + order event logging
    │   ├── jobs/                 # scheduler definitions
    │   ├── agent_tools/          # Phase 3 seam: typed tool specs over services (no LLM calls)
    │   └── db/                   # SQLAlchemy models, session
    ├── scripts/
    │   ├── discover_auth.py
    │   └── manual_order_smoke.py
    └── tests/
        ├── fake_questrade/       # FastMCP fake server + fixtures
        ├── unit/
        ├── integration/
        └── live/                 # @pytest.mark.live, read-only, skipped by default
```

## 8. Data model (Phase 1)

All tables carry `created_at` (timestamptz, UTC). Where a table holds Questrade data, also store the raw payload as `raw jsonb`.

| Table | Purpose and key columns |
|---|---|
| `oauth_client` | Registration info (encrypted blob), strategy (`cimd` / `dcr`), `created_at` |
| `oauth_token` | Encrypted token blob, `access_expires_at`, `refresh_rotated_at`, `version` (optimistic lock for atomic rotation) |
| `mcp_tool_catalog` | `snapshot_id`, `tool_name`, `input_schema jsonb`, `schema_hash`, `taken_at` |
| `mcp_call` | Append-only. `id`, `correlation_id`, `tool_name`, `kind` (read / preview / approval), `args_redacted jsonb`, `outcome`, `error_code`, `latency_ms`, `response_hash` |
| `account` | `questrade_account_id` (GUID, unique), `account_type` (TFSA / FHSA / RRSP / margin / cash), `number_masked`, `status` |
| `balance_snapshot` | `account_id`, `as_of`, `currency`, `cash`, `market_value`, `total_equity`, `buying_power` |
| `position_snapshot` | `account_id`, `as_of`, `symbol`, `security_uuid`, `qty NUMERIC(18,4)`, `avg_cost`, `market_value`, `open_pnl` |
| `activity` | Upserted by a deterministic dedupe key. This is the permanent ledger beyond Questrade's ~90-day window |
| `order_history` | Upserted from `get_order_history` |
| `order_draft` | See below |
| `order_draft_event` | Append-only state transitions: `from_status`, `to_status`, `reason`, `payload jsonb` |

`order_draft` columns:
- `id`, `idempotency_key` (unique)
- `account_id`
- `intent jsonb`
- `guardrail_result jsonb`
- `preview jsonb`, `preview_args_hash`
- `status`
- `instruction_id`
- `confirmed_at`, `submitted_at`, `terminal_at`

Write Alembic migrations with working downgrades.

## 9. Guardrails (local, deterministic, run before preview)

All values are configurable through environment variables. Every violation returns a typed `GuardrailViolation` naming the specific rule.

- **Account type allowlist** (`AURELIUS_ALLOWED_ACCOUNT_TYPES`, default `TFSA,FHSA,RRSP`).
- **Symbol allowlist** (`AURELIUS_SYMBOL_ALLOWLIST`). **An empty allowlist blocks all orders.**
- **Side:** buys only, unless `AURELIUS_ALLOW_SELLS=true`.
- **Order types:** market and limit only.
- **Duration:** Day only. Block GTC, GTEM and overnight in Phase 1.
- **Fractional quantity:** allowed only when `type=market` and `duration=Day`. The server is the final judge of eligibility.
- **Market hours:** market orders are allowed only 09:30–16:00 ET on weekdays. Limit orders are allowed any time except the maintenance window.
- **Maintenance window:** refuse order tools locally from **01:55 to 03:50 ET**, a buffer around the published 2:00–3:45. The window lives in config because the schedule can change.
- **Notional limits:**
  - per order: `AURELIUS_MAX_ORDER_NOTIONAL_CAD`
  - per day across all drafts submitted for approval: `AURELIUS_MAX_DAILY_NOTIONAL_CAD`
  - Notional is computed from a fresh `get_quotes` ask for buys, or from the limit price.
- **Cash check:** notional plus estimated commission must be at most available cash in the account's currency. No margin usage.
- **One in flight:** at most one draft in `pending_approval` per account at a time.

## 10. Order lifecycle (state machine)

```
intent ──guardrails──► rejected_by_guardrail (terminal)
   │
   ▼
previewed ──(user confirms in Aurelius, preview hash matches, trading enabled)──► submitted
   │                                                                              │
   └──(expires after 5 min without confirmation)──► abandoned (terminal)          ▼
                                                                         pending_approval
                                                                                  │ poll get_instruction
                                                    ┌───────────┬───────────┬─────┴─────┬──────────┐
                                                    ▼           ▼           ▼           ▼          ▼
                                                executing   succeeded    failed      denied     expired
                                                    │        (terminal)  (terminal)  (terminal) (terminal)
                                                    └──────► succeeded / failed

create timeout or ambiguous response ──► unknown ──reconcile via list_instructions/get_instruction──► real status
```

- Transitions live in one module (`domain/order_state.py`) with an explicit allowed-transitions table. Illegal transitions raise.
- After `succeeded`, reconcile against `get_order_history` and store the fill details.

## 11. MCP client behavior

- **Rate limiting:** local token buckets at about 80% of published limits: reads 40/min and 800/hr; approval tools 16/min and 240/hr. Return `RateLimited` with retry-after rather than queueing unboundedly.
- **Retries:** read and preview tools retry up to 3 times with exponential backoff and jitter, on 429, 5xx and timeouts. **Approval tools: zero automatic retries.**
- **Error mapping** to typed exceptions:
  - `AuthExpired`
  - `ReauthRequired` (refresh failed: surface on `/health/questrade` and require `aurelius auth login`)
  - `RateLimited`
  - `MaintenanceWindow` (parse the reopening time)
  - `ApprovalDeviceMissing`
  - `ToolUnavailable` (tool absent for this account)
  - `ToolNotAllowed`
  - `SchemaDrift`
  - `GuardrailViolation`
- **Schema drift:**
  - Snapshot `tools/list` at startup and daily. Compare input-schema hashes for allowlisted tools.
  - If an approval-related tool's schema changed, **disable order submission automatically**, log at error level, and report on `/health/questrade` until a human acknowledges it.
- **Graceful degradation:** tools roll out per account. Adapters must handle a missing tool by returning `ToolUnavailable`, not by crashing.
- **Correlation:** every inbound API request gets a correlation id, propagated to every `mcp_call` row and log line.

## 12. API surface (Phase 1)

- **Auth:** single user. Bearer token `AURELIUS_API_TOKEN`, checked with a constant-time comparison.
- **CORS:** locked to configured origins; the Expo app comes later.
- **Rate limiting:** apply it to the API itself too.

| Method | Path | Behavior |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/health/questrade` | Token state, last successful call, catalog drift status, trading enabled flag |
| GET | `/oauth/client-metadata.json` | CIMD document (only if CIMD strategy chosen) |
| GET | `/oauth/callback` | OAuth redirect handler (if using a public redirect) |
| GET | `/accounts` | Accounts with masked numbers and types |
| GET | `/portfolio` | Latest snapshot: per account, totals by currency, asset mix |
| POST | `/sync` | Trigger a sync now (debounced) |
| GET | `/market/quotes?symbols=` | Pass-through to `get_quotes`, cached 15s |
| POST | `/orders/drafts` | Body: `OrderIntent`. Runs guardrails, then preview. Returns the draft with the preview (estimated cost, commission). **Does not submit.** |
| POST | `/orders/drafts/{id}/submit` | Requires: trading enabled, body `{ "confirm": true, "preview_args_hash": "..." }`, a matching hash, and a preview no older than 5 min. Calls `create_order_instruction`. Returns `pending_approval`. |
| GET | `/orders/drafts/{id}` | Current status plus event history |
| GET | `/audit/calls?limit=&tool=` | Recent MCP calls (redacted) |

Return RFC 9457 problem+json for errors.

## 13. Scheduled jobs

- **Daily snapshot:** Mon–Fri at 16:30 America/Toronto. Pull accounts, balances, positions and order history, plus activities for the last 7 days (upserted). Jobs are idempotent: running one twice produces no duplicates.
- **Instruction poller:** every 15s for drafts in `pending_approval` or `executing`. Back off to 60s after 5 minutes. Stop at a terminal status.
- **Catalog drift check:** daily, plus at startup.
- **Token health:** proactively refresh when less than 20% of the access-token lifetime remains.

## 14. Agent seam (prep for Phase 3, no LLM calls now)

Create `app/agent_tools/` with typed tool specs (name, description, Pydantic input/output models, JSON schema export) wrapping **services**, never raw MCP:

- `get_portfolio_summary`
- `get_quotes`
- `get_recent_activity`
- `propose_order`: creates a draft and returns the preview. **It cannot submit.**

There is deliberately **no** submit tool. Submission is always a human action from the Aurelius UI. Add a test asserting that no agent tool can reach `create_order_instruction`.

## 15. Testing requirements

- **Fake Questrade MCP server** (FastMCP) in `tests/fake_questrade/`:
  - Implements the allowlisted tools with the schemas captured in Step 0 and the fixture data.
  - Scriptable failure modes:
    - 401 / expired token, and refresh rotation
    - 429 rate limiting
    - maintenance-window error
    - approval device missing
    - pending → succeeded, pending → denied, pending → expired
    - timeout after create, testing the `unknown` → reconcile path
    - a tool absent from `tools/list`
    - a schema change, triggering drift
- **Invariant tests (must exist and pass):**
  - With trading disabled, no route or service can invoke `create_order_instruction`.
  - Create is impossible without a stored preview whose args hash matches.
  - Approval tools are never retried (assert the call count is 1 under injected failures).
  - Every non-allowlisted tool raises `ToolNotAllowed`, including `operation="modify"` and `operation="cancel"`.
  - Agent tools cannot reach submission.
- **Guardrail property tests** (hypothesis): randomly generated intents violating any rule are always rejected; valid intents pass.
- **Redaction test:** capture all logs during a full test run and assert that no token-shaped strings or unmasked account numbers appear.
- **Money tests:** no `float` crosses the domain boundary. Add a mypy-enforced `Money` type plus a runtime assertion in adapters.
- **Migrations:** upgrade and downgrade round-trip on a clean database.
- **Live tests** (`@pytest.mark.live`, skipped by default): read-only, and only run when `AURELIUS_LIVE_TESTS=1`.
- **Coverage:** ≥ 85% on `integrations/` and `services/`.

## 16. Configuration (`.env.example`)

```
AURELIUS_ENV=dev
AURELIUS_PUBLIC_BASE_URL=https://aurelius.example.ca
AURELIUS_API_TOKEN=change-me
AURELIUS_ENCRYPTION_KEY=base64-32-bytes
DATABASE_URL=postgresql+asyncpg://aurelius:aurelius@postgres:5432/aurelius
QUESTRADE_MCP_URL=https://mcp.questrade.com/v1/brokerage/mcp
AURELIUS_TRADING_ENABLED=false
AURELIUS_ALLOW_SELLS=false
AURELIUS_ALLOWED_ACCOUNT_TYPES=TFSA,FHSA,RRSP
AURELIUS_SYMBOL_ALLOWLIST=
AURELIUS_MAX_ORDER_NOTIONAL_CAD=500
AURELIUS_MAX_DAILY_NOTIONAL_CAD=1500
AURELIUS_MAINTENANCE_WINDOW_ET=01:55-03:50
AURELIUS_TIMEZONE=America/Toronto
AURELIUS_LIVE_TESTS=0
```

## 17. Definition of done

- [ ] Step 0 spike report reviewed and approved.
- [ ] `docker compose up` brings up api, postgres and caddy on arm64 and amd64.
- [ ] `aurelius auth login` works once, and the service then stays authenticated across restarts and token refreshes.
- [ ] Daily sync populates all snapshot tables; a rerun creates no duplicates.
- [ ] `/portfolio` returns correct totals by currency against fixture data.
- [ ] Full order flow works end to end **against the fake server**: draft → guardrails → preview → confirm → pending_approval → each terminal state.
- [ ] Drift detection disables submission automatically.
- [ ] All invariant tests pass; ruff, `mypy --strict` and pytest are green; coverage target met.
- [ ] `CLAUDE.md`, `README.md` (setup, env vars, auth login, running tests, the manual smoke script) and `docs/architecture.md` (component and sequence diagrams in Mermaid) are written.
- [ ] `scripts/manual_order_smoke.py` exists and is documented for Rudra to run himself with a tiny limit order. You never run it.

## 18. Working agreement

1. Read this whole file first. Do Step 0 only, then stop with the spike report.
2. After approval, propose a milestone plan (about 5 milestones) and wait for an OK.
3. Work in small, reviewable commits using conventional commit messages.
4. At the end of each milestone, run `ruff check`, `mypy`, and `pytest`, then show the results and a short summary of what changed.
5. Never call an approval tool against live Questrade. Never weaken an invariant to make a test pass; if an invariant blocks you, stop and explain.
6. When docs and server reality conflict, trust the server, then document the delta in `docs/spikes/`.
7. Keep `CLAUDE.md` updated with conventions, invariants and gotchas, so future sessions inherit them.

## 19. Out of scope for Phase 1

- Expo / Android app and any UI.
- Payday waterfall engine, budgeting and calculators.
- LLM agent logic (only the `agent_tools/` seam).
- Notifications.
- Options, Flows, custom indexes, watchlist writes, balance sheet.
- Modify and cancel order operations.
- Multi-user support.
