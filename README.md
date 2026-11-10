# Aurelius

A single-user personal finance agent. Phase 1 is the brokerage layer: a FastAPI service that talks to
Questrade's hosted MCP server, keeps its own permanent record of the account, and offers a guarded,
human-confirmed order flow. Aurelius never places an order on its own. Every order needs an explicit
confirmation in Aurelius **and** push approval in the Questrade app.

> **This repository is public.** It never contains account data, tokens or keys.
> Live data stays in `backend/.spike/` (git-ignored); see `CLAUDE.md` → "The repository is PUBLIC".

## Repository layout (monorepo)

| Path | What |
|---|---|
| `backend/` | Python 3.12 FastAPI service: Questrade integration, domain logic, scheduler, tests |
| `mobile/` | Expo app, reserved for a later phase (not created yet) |
| `deploy/` | Deployment: `static/` holds the **only publicly served files** (today just the CIMD document); docker compose and the Caddyfile move here in M1 (the Caddyfile is currently at the repo root) |
| `docs/` | Specs, spikes and runbooks |
| `.github/workflows/` | `ci.yml` (gitleaks, ruff, mypy, pytest) and `pages.yml` (publishes `deploy/static` only) |
| `.githooks/` | The gitleaks pre-commit hook |

Shared TypeScript packages will be added only when the mobile app needs them.

## Getting started (backend)

Requirements: [uv](https://docs.astral.sh/uv/), and Docker for the deploy tests and the gitleaks hook.

```powershell
git config core.hooksPath .githooks      # once per clone: gitleaks on every commit (fails closed)
cd backend
uv sync
uv run ruff check app tests scripts
uv run mypy                              # strict
uv run pytest                            # live Questrade tests are skipped unless AURELIUS_LIVE_TESTS=1
```

## Signing in to Questrade

Aurelius is an OAuth public client identified by **CIMD** (Client ID Metadata Document). Its
permanent client_id is `https://aurelius.rivenpath.ai/oauth/client-metadata.json`, which is served
from `deploy/static/` by GitHub Pages (and later by Caddy). There is no dynamic client registration.
Steps: [`docs/runbooks/cimd-login.md`](docs/runbooks/cimd-login.md).

```powershell
$env:AURELIUS_ENCRYPTION_KEY = (uv run aurelius auth genkey)   # keep it: tokens are encrypted with it
$env:AURELIUS_CIMD_CLIENT_ID_URL = "https://aurelius.rivenpath.ai/oauth/client-metadata.json"
uv run aurelius auth login                                       # grant READ permission only
```

## Safety

- **Read-only by default.** Order submission is gated by trading being enabled, a matching stored
  preview less than 5 minutes old, and local guardrails. Approval calls are never retried.
- **Secrets:** tokens are AES-GCM encrypted at rest; tokens, PKCE verifiers and codes are never logged.
- **Public repo:** gitleaks pre-commit hook and CI; fixture guard tests; GitHub secret scanning and push protection.

`CLAUDE.md` holds the full invariants and conventions; `docs/` holds spikes and runbooks.
