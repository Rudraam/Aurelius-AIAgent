# Aurelius: conventions for Claude Code

Single-user personal finance agent for Rudra (Ontario). Phase 1 is the Questrade MCP brokerage layer.
The Phase 1 spec is `docs/specs/phase1-questrade.md`. **Its Amendments section (A1 to A15, at the top, followed by the revised milestone plan) wins over the original text below it**, and this file wins over both. When a decision supersedes the spec, add an amendment row with the commit ref. Spike findings are in `docs/spikes/` and runbooks in `docs/runbooks/`. This code touches a real brokerage account.

## Repository layout (monorepo, also in README.md)

- `backend/`: the FastAPI service (Python 3.12, uv). All Python code, tests and scripts live here.
- `mobile/`: the Expo app. **Reserved; do not create it until its phase starts.** Add shared TypeScript packages only when the mobile app needs them.
- `deploy/`: deployment. `deploy/static/` holds the **only publicly served files** (allowlist enforced by `tests/unit/test_publish_guard.py` and by `pages.yml`). Docker compose and the Caddyfile belong here; the Caddyfile is still at the repo root and moves with compose in M1. `deploy/caddy/Dockerfile` is the single, Dependabot-visible pin of the Caddy image.
- `docs/`: specs (`docs/specs/`, where amendments win), spikes (`docs/spikes/`) and runbooks (`docs/runbooks/`).
- `.github/workflows/`:
  - `ci.yml`: gitleaks always; ruff, mypy and pytest when `backend/` or shared config changes; never live tests.
  - `pages.yml`: deploys exactly `deploy/static` to GitHub Pages.
  - Every action is pinned to a full commit SHA with a `# vX.Y.Z` comment, and permissions are granted per job, with `permissions: {}` at the top.
- `.githooks/`: the gitleaks pre-commit hook.
- Don't move existing code to match the layout; place new code by it.

## The repository is PUBLIC (github.com/Rudraam/Aurelius-AIAgent)

- **Real account data never enters git.** That means no account IDs or numbers, balances, positions, activities, order history, names or emails from the live account, in any file, commit message or test.
- **Live observations stay in `backend/.spike/`, which is git-ignored.** That covers `observations.jsonl`, the encrypted auth state, and live fixtures (`backend/.spike/fixtures/`). Never move or copy them anywhere tracked unless the next point is followed.
- **Promoting a fixture into `backend/tests/fixtures/` is a deliberate step:**
  1. redact it with `app/redaction.Redactor`, which maps every GUID onto the fixed `FAKE_GUIDS` set, masks account numbers to `****1234`, and replaces non-GUID account IDs with `REDACTED-ACCOUNT-ID`. It is field-aware: numbers under `MARKET_DATA_KEYS`/`MARKET_DATA_PATHS` (volumes, sizes, prices, returns) are kept exactly, and unknown numeric fields fail closed;
  2. read it by eye;
  3. make sure `tests/unit/test_fixture_guard.py` passes.

  The guard fails on any GUID outside `FAKE_GUIDS`, any standalone 7-10 digit run, any unmasked account-number field, any account ID that is neither a fake GUID nor the placeholder, or any email address.
  - In JSON it checks field by field, and only *numbers* under allowlisted market-data fields may have 7-10 digits. Markdown and other text get no exemption. Classify a new market field in `app/redaction.py`; never widen the exemption to strings.
  - The guard also scans `docs/spikes/`, so don't write example account-number-shaped numbers in docs.
- **gitleaks runs as a pre-commit hook** (`.githooks/pre-commit`, pinned image `gitleaks:v8.21.2@sha256:0e99e88…`). It fails closed: if neither a `gitleaks` binary nor Docker is available, the commit is blocked.
  - Enable it once per clone with `git config core.hooksPath .githooks`.
  - Never use `--no-verify`.
- GitHub secret scanning and push protection are enabled on the repo (by Rudra).

## Non-negotiable invariants (never weaken one to make a test pass; stop and explain instead)

1. **No autonomous orders.** Every create needs explicit confirmation in Aurelius **and** push approval in the Questrade app.
   - Until Questrade allowlists Aurelius (A13), Aurelius never submits. It produces an A15 order plan: limit orders only, a one-line summary, and exact instruction text for Claude. Rudra's comparison of the push approval against that summary is the binding check.
2. **Read-only by default.** `AURELIUS_TRADING_ENABLED=false` makes `create_order_instruction` unreachable.
3. **Tool allowlist.**
   - Read: `list_accounts get_balances get_positions get_order_history get_account_activities search_symbols get_quotes get_historical_data get_instruction list_instructions`.
   - Order: `preview_order_instruction`, plus `create_order_instruction` with `operation="create"` only.
   - Everything else raises `ToolNotAllowed`.
4. **Preview before create.** The create args hash must equal the stored preview's hash.
   - Enforced in `services/order_submission.check_submittable`, before any state change or call, in this order:
     1. trading enabled (invariant 2);
     2. status is `previewed`;
     3. `operation == "create"`;
     4. a stored preview exists;
     5. `order_args_hash(create_args)` equals the stored hash;
     6. the human-confirmed hash equals the stored hash;
     7. the preview is between 0 and 5 minutes old.
   - `order_args_hash` (`domain/order_args.py`) is canonical: it ignores key order, `operation` and Decimal trailing zeros, and **rejects floats**.
   - `create` receives exactly the frozen args that were hashed.
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
   - **Never use a fixed UTC offset for Toronto** (no `timezone(timedelta(hours=-5))`, no `"-05:00"`, no `US/Eastern`).
   - All Toronto logic goes through `app/domain/market_time.TORONTO`, which is IANA `America/Toronto` from the `tzdata` package. It is the only `ZoneInfo(...)` call in `app/`; `tests/unit/test_no_fixed_offsets.py` enforces this.
   - Time rules compare Toronto **wall-clock** time. On spring forward, the maintenance window runs from 01:55 EST straight through to 03:50 EDT with no gap.
   - **Intentional double-block on fall back (approved 2026-11-09):** the repeated hour counts on both passes. So the window blocks 01:55-01:59 EDT (first pass), allows 01:00-01:54 EST, then blocks 01:55-03:50 EST: 120 real minutes instead of 115. This is deliberate and fail-closed. Do not "fix" it to a single pass.
   - **Exchange sessions** come from `app/domain/exchange_calendar.py`, which reads the generated `app/domain/data/exchange_sessions.json`. That file covers XTSE and XNYS, holidays and early closes, in Toronto wall-clock time.
     - `is_regular_session(instant, exchange)` and `check_order_timing(order_type, instant, exchange)` require the exchange.
     - A date outside coverage raises `CalendarNotCovered`. Market orders then get `CALENDAR_NOT_COVERED` (fail closed).
     - The exchange comes from `exchange_for(symbol, listingMarket)`: TSX → XTSE, NASDAQ/NYSE → XNYS, with `.TO` cross-checked. Anything else raises `UnknownExchange`.
     - Regenerate the file with `uv run python scripts/generate_exchange_calendars.py`. Never hand-edit it; a test compares it with the generator.
     - `tests/unit/test_exchange_calendar.py` fails once fewer than 90 days of coverage remain (coverage currently ends 2027-12-31).
     - `exchange_calendars` (and so pandas) is **dev-only**. `app/` must never import it; a test checks this, and the image has no dev group.
   - `tests/unit/test_dst.py` covers 2026-11-01 and 2027-03-14 for the maintenance window, market hours, order timing and the APScheduler 16:30 trigger.

## Auth design (decisions of 2026-11-09)

- **One HTTP stack: `httpx2`.** The MCP SDK transport, the token endpoint and discovery all use it, and `httpx` is not a dependency.
  - On Linux, httpx2 trusts **only the OS CA store** (no certifi), so the Docker image installs `ca-certificates`, and the build fails if it's missing.
- **Client identity: CIMD is the only strategy. There is no DCR code, and none may be added back.**
  - The permanent client_id is `https://aurelius.rivenpath.ai/oauth/client-metadata.json`. The file is `deploy/static/oauth/client-metadata.json`, rendered by `backend/scripts/render_cimd.py`, and guarded by `tests/unit/test_cimd_document.py`.
  - It is served statically, from the one file only (no copies anywhere). Until the VM exists, this repo's GitHub Pages serves it: `.github/workflows/pages.yml` uploads exactly `deploy/static`. After that, Caddy serves the same directory.
  - Only allowlisted files may live in `deploy/static`; `tests/unit/test_publish_guard.py` and the workflow's own guard step both fail otherwise. There is no `CNAME` file: Actions-based Pages ignores it, and the custom domain is set in repo settings.
  - `aurelius auth login` and `aurelius auth refresh` exit with a clear error if `AURELIUS_CIMD_CLIENT_ID_URL` is unset or invalid, before touching the network or the store.
  - Runbook: `docs/runbooks/cimd-login.md`.
- **Invariant (client_id):** every authorize, token and refresh request carries **exactly** the configured CIMD URL as `client_id`.
  - The configured value is a validated `CimdClientId` (https, a host, a non-root path, no query, fragment or credentials). It is passed into `cimd_registration`, `begin_login`, `complete_login`, `_token_request` (the single path for every token call) and `TokenManager`.
  - Each of these calls `CimdClientId.require(...)`, an exact comparison (no case folding, no trailing-slash tolerance), and raises `ClientIdMismatch` **before** sending anything.
  - A stored client that isn't the configured URL is never refreshed. Stored state with a non-CIMD client (`strategy` other than cimd, or a `client_secret`) is refused on load.
  - `tests/unit/test_client_id_invariant.py` enforces all of this. It inspects every request on the wire, injects a foreign `client_id` at each check, and scans `app/` for DCR code.
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
- **Questrade's DCR endpoint is not real registration.** On 2026-11-09, `POST https://mcp.questrade.com/connect/register` (advertised as `registration_endpoint`, on the MCP host rather than the issuer) answered Aurelius with the `client_id` **`claude-code-public`**, which is Claude Code's pre-registered client. The authorize request then hit Questrade's generic error page, because that client doesn't allow our redirect URI.
  - Questrade's technical reference lists only CIMD and pre-registered clients; nothing uses DCR.
  - Never operate under another application's `client_id`. That is why DCR was removed entirely. Details are in `docs/spikes/questrade-auth.md` §3.
- **Questrade allowlists MCP clients (found 2026-11-10).** Aurelius's CIMD login was refused, but Rudra's Claude Code `/mcp` login to the same server succeeded and listed 33 tools. So the account is eligible, and `aurelius.rivenpath.ai` is simply not an allowlisted client.
  - The backend can't be an MCP client until Questrade allowlists it. Keep the CIMD code dormant but intact; don't delete it or "fix" it with another client's identity.
  - Details: `docs/spikes/questrade-auth.md` §1.
- **Questrade tools in Claude Code.** Every mutating tool is denied by exact name (`mcp__questrade__<tool>`) in `.claude/settings.json`; `tests/unit/test_questrade_mcp_denies.py` enforces this.
  - Denies hot-reload: a denied MCP tool disappears from the session's tool pool at once, with no restart (verified 2026-11-10).
  - Never add a `mcp__questrade` rule to `allow` or `ask`. A new tool the server adds must be classified in that test before anyone calls it.
  - The server lists 33 of the 34 documented tools; `preview_order_instruction` is missing (denied anyway).
- **gh denies (decision of 2026-11-10).** `gh api*`, `gh repo delete*`, `gh repo edit*`, `gh secret*`, `gh release delete*`, `gh pr merge --admin*` (and `gh pr merge * --admin*`), `gh ruleset*`, in both shells. `tests/unit/test_claude_denies.py` checks they are present and that the Bash and PowerShell lists mirror each other exactly. Merge PRs with merge commits only (`gh pr merge N --merge`), never squash or rebase, so cited SHAs stay valid.
- mcp 2.x renamed `FastMCP` to `mcp.server.mcpserver.MCPServer`. The fake server and the tests use `MCPServer` with `streamable_http_app(json_response=True, stateless_http=True, transport_security=...(enable_dns_rebinding_protection=False))` over `httpx2.ASGITransport`. Start the app's lifespan with `app.router.lifespan_context(app)`.
- `respx` only patches `httpx`. For httpx2 tests, use `tests/fakes.py::FakeHttp`, which routes requests and counts calls.
- `tests/deploy/` runs the real Caddyfile in `caddy:2.10.2` (marker `docker`, skipped without Docker). Static files mount at `/srv/static`; docker-compose must use the same path.
- Windows Python has no IANA tz database, so `tzdata` is a runtime dependency (approved 2026-11-09). Without it, `market_time` fails at import, which is deliberate.
- APScheduler 3.x (`AsyncIOScheduler`) ships no type information, so it has a mypy override in `pyproject.toml`. Build triggers with `timezone=TORONTO` (`app/jobs/schedules.py`).
- A permissions deny list in `.claude/settings.json` (47 git and 8 gh patterns, each as both a `Bash(...)` and a `PowerShell(...)` rule, plus the Questrade MCP denies above) blocks: **all** `git checkout`, so use `git switch` for branch changes; `git restore`; `git reset --hard`; `git clean`; `git stash drop`/`clear`; `git switch --discard-changes`/`-f`/`--force`/`-C`; force pushes (`--force`, `-f`, `--force-with-lease`, with the flag before or after the remote, and `+refspec`); `git branch -D`; and the `git -C` forms of all of these. Each family was verified with harmless probes. Regenerate the file from a single pattern list mirrored to both tools rather than editing it by hand.
- Mutation-check new safety tests by editing a byte copy and restoring from it. Never use `git checkout --` on files with uncommitted work.

## Working conventions

- Python 3.12, managed with `uv`. Work from `backend/`:
  - `uv run ruff check app tests scripts`
  - `uv run ruff format …`
  - `uv run mypy` (strict, over `app/`)
  - `uv run pytest`
- Docker: `docker build -t aurelius-api:dev backend/`. Check arm64 with `docker buildx build --platform linux/arm64 --load …`.
- Ask before adding a dependency that isn't in the spec stack. Approved additions: `httpx2` (it replaces httpx), `tzdata`, Authlib (approved but unused), and `exchange_calendars` (dev group only). APScheduler is in the spec stack.
- **Supply chain: minimum release age is 14 days, for everything** (Python packages, GitHub Actions, Docker images). Never adopt a version published less than 14 days ago.
  - uv enforces it: `[tool.uv] exclude-newer = "14 days"` in `backend/pyproject.toml`, recorded in `uv.lock` as `exclude-newer-span = "P14D"`.
  - Dependabot (`.github/dependabot.yml`) uses `cooldown: default-days: 14`, runs weekly, and groups minor and patch updates (majors come as their own PRs).
  - Manual pins (actions, Docker images) follow the same rule: check the release date and pick the newest release at least 14 days old. Pin actions to the full commit SHA with a `# vX.Y.Z` comment.
  - **Container images: pin every reference by its multi-arch INDEX digest** (`name:tag@sha256:<index>`, never a per-platform digest), at least 14 days old, with a comment naming the tag and build date.
    - Keep the tag in the reference so Dependabot (`docker` ecosystem: `/backend`, `/deploy/caddy`) can update both the tag and the digest.
    - All architectures in the index must carry the **same** software version. Official images are rebuilt weekly and can briefly mix versions: the 2026-10-26 `python:3.12-slim-bookworm` index had 3.12.16 on amd64 but 3.12.15 on arm64. Check with `docker buildx imagetools inspect`. Older index digests can be found in `docker-library/repo-info` git history.
    - No `# syntax=` directive in Dockerfiles; it pulls an unpinned frontend image.
    - Current pins:
      - `python:3.12-slim-bookworm` (3.12.15, 2026-10-21);
      - `ghcr.io/astral-sh/uv:0.12.5` (2026-08-14);
      - `caddy:2.10.2` (2026-02-11), whose only pin is `deploy/caddy/Dockerfile` (the deploy test reads it from there);
      - `gitleaks:v8.21.2` (2024-10-28) in `.githooks/pre-commit` and `ci.yml`. Dependabot can't see this one; update both places by hand under the same rule.
    - `tests/unit/test_supply_chain.py` fails on any image reference that isn't digest-pinned (Dockerfiles, workflows, hooks, Python, compose).
  - `tests/unit/test_supply_chain.py` guards the config. Applying the rule on 2026-11-09 moved 14 freshly locked packages back one patch release.
- Use conventional commits, in small reviewable steps, and commit at every green checkpoint. gitleaks runs in the pre-commit hook. At the end of each milestone, run ruff, mypy and pytest and show the results.
- **Branch names: lowercase kebab-case, `[a-z0-9-]` only** (e.g. `m1-holiday-calendars`). No slashes, underscores, uppercase or dots. Create and change branches with `git switch` (`git checkout` is denied).
- When the docs and the server disagree, trust the server and record the difference in `docs/spikes/`.

## Milestones

The revised plan (2026-11-10) is in the spec, after Amendments A13-A15.
- **M1 (foundations):** exchange calendars, the field-aware redactor and guard, `app/integrations/json_decimal` (broker JSON goes straight to `Decimal`; floats never reach domain code), and amendments A13-A15.
- **M2 onward** waits until Rudra resolves `list_accounts` returning "You do not have an active account."
- **Before ever setting `AURELIUS_TRADING_ENABLED=true`:** coverage must be at least 90 days, `listingMarket` values observed at M3 must be confirmed, and the A15 handoff must be built (M4).
