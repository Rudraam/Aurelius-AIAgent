# Spike: Questrade MCP tools as observed (2026-11-10)

**How:** Rudra's own Claude Code session, connected to `questrade` (`https://mcp.questrade.com`) through `/mcp`. Aurelius's backend could not be used, because Questrade refused its CIMD client (see `questrade-auth.md` §1).

**Safety first:**
- Before any tool call, every mutating tool was denied by exact name in `.claude/settings.json` (`tests/unit/test_questrade_mcp_denies.py`).
- The deny was proven live: denying `get_quotes` removed it from the session's tool pool at once (no restart), and removing the rule brought it back.
- Denied tools are not merely refused; Claude Code drops them from the tool pool.

**Calls made: 5, all reads.** `list_accounts`, `search_symbols`, `list_instructions`, `get_quotes`, `get_historical_data`. No preview, create, edit or update tool was called. Raw responses are in `backend/.spike/observations.jsonl` and redacted copies in `backend/.spike/fixtures/claude_code_mcp/` (both git-ignored, and nothing promoted to `tests/`).

This file contains no account data.

## 1. Blocker: `list_accounts` → "You do not have an active account."

- `list_accounts` returned an MCP tool error with exactly that text.
- The login itself succeeded, and market-data tools work. So the token is valid, but the server associates **no active brokerage account** with this identity.
- Every account-scoped tool (`get_balances`, `get_positions`, `get_portfolio`, `get_order_history`, `get_account_activities`) needs an `accountId` from `list_accounts`, so none of them could be observed. They were not called.
- Possible causes, to be confirmed by Rudra (not tested):
  - the Questrade login used differs from the one that owns the accounts;
  - the account is not yet funded or active;
  - the consent screen granted market data without account access.
- `list_accounts` was not retried.

## 2. Tool list: 33 listed, 34 documented

**Missing: `preview_order_instruction`.** It appears neither in `tools/list` nor in the server's own instructions text. The server says "only tools enabled for this user are actually registered", so this may be per-account. But `preview_option_instruction` *is* listed. Consequence for the spec (§4 invariant 4, "preview before create"): there may be no server-side equity preview to bind the hash to. See the M1 plan.

### Read tools (22): input schemas

All `accountId`, `securityUuid`, `watchlistId`, `instructionId` and `customIndexUuid` parameters are `string`, `format: uuid`. Dates are `YYYY-MM-DD` and date-times are ISO 8601 with `Z` or an offset.

| Tool | Parameters (required in **bold**) |
|---|---|
| `list_accounts` | none |
| `get_balances` | **`accountId`** |
| `get_positions` | **`accountId`** |
| `get_portfolio` | **`accountId`** |
| `get_order_history` | **`accountId`** (no date filter; open and final orders together) |
| `get_account_activities` | **`accountId`**, `fromDate`, `toDate`, `page` (1-based, server page size; see `metadata.totalPages`), `transactionTypes[]` ∈ {Trades, Interest, Other, Dividends, FX conversion, Dividend reinvestment, Corporate actions, Transfers, Withdrawals, Deposits, Fees and rebates} |
| `get_quotes` | `symbols[]` (≤20, `^[A-Za-z0-9.:-]{1,32}$`), `securityUuids[]` (≤20) |
| `search_symbols` | **`query`**, `limit` (1–20, default 20), `hasOptions` |
| `get_historical_data` | **`securityUuid`**, **`granularity`** ∈ {1m,2m,3m,5m,10m,15m,30m,1h,4h,1d,1wk,1mo}, **`from`**, **`to`** (date or date-time), `tradingHoursOnly` (intraday only; `false` with 1d/1wk/1mo is refused) |
| `get_option_expiries` | **`underlyingSecurityUuid`** (string, not uuid-formatted) |
| `get_option_chain` | `underlyingSecurityUuid` or `symbol`, `optionType` ∈ {All,Call,Put}, `fromExpiryDate`, `toExpiryDate`, at most one of `strikes[]` (≤20) / `strikeRange{min,max}` / `closestStrikes` (1–50), `includeGreeks`, `includeQuotes` (both need a strike filter, ≤50 contracts) |
| `list_watchlists` | none |
| `get_watchlist` | **`watchlistId`**, `includeQuotes` (default true) |
| `get_instruction` | **`instructionId`** |
| `list_instructions` | `status[]` ∈ {pending_approval, executing, succeeded, failed, denied, expired}, `instructionType` (see below), `accountId`, `createdAfter`, `createdBefore`, `limit` (1–100, default 20), `cursor` |
| `list_custom_indexes` | `types[]` ∈ {DRAFT, USED} |
| `get_custom_index` | **`customIndexUuid`** |
| `get_balance_sheet` | `period` ∈ {15D,1M,3M,6M,YTD,1Y,All} |
| `get_balance_sheet_account` | **`accountId`** (balance-sheet id, *not* a brokerage uuid), `period` (no `All`) |
| `list_flows` | `status[]`, `search`, `sortBy`, `sortOrder`, `limit` (1–100), `cursor` |
| `get_flow` | **`flowId`** (string) |
| `get_flow_history` | `flowId`, `eventType`, `status`, `dateRange` xor (`startDate`/`endDate`), `sortBy`, `sortOrder`, `limit` (1–100, default 10), `cursor` |

`list_instructions.instructionType` ∈ {`order.create`, `order.modify`, `order.cancel`, `option_order.create|modify|cancel`, `custom_index.create|edit`, `watchlist.create|edit`, `flow.create|update|delete|activate|pause`}. This confirms that `create_order_instruction` carries modify and cancel operations, and that `create_flow` carries a delete operation.

### Mutating tools (11 listed): schemas not captured, by design

The following are listed but denied:
- `create_order_instruction`, `create_option_instruction`, `preview_option_instruction`
- `create_watchlist`, `edit_watchlist`
- `preview_custom_index`, `create_custom_index`, `edit_custom_index`
- `preview_flow`, `create_flow`, `update_flow_status`

Their input schemas were not recorded. A denied tool's schema can't be loaded in Claude Code, and lifting a deny to read it is not worth the risk. Aurelius will get them from its own `tools/list` once a client is allowlisted, or from Questrade's technical reference.

## 3. Decision 4: write tools listed under the grant

- Under Rudra's Claude Code grant, which Rudra reports as read-only, **all 11 mutating tools above are listed**: both equity and option `create_*`, `preview_option_instruction`, and every watchlist, custom-index and flow write.
- **Being listed does not prove permission.** The server says every write tool requires push-to-approve on an enrolled device and fails immediately without one. Whether a read-only grant would also be refused with `403 insufficient_scope` was **not tested, and must not be tested by Claude Code**.
- Aurelius therefore keeps its own client-side allowlist (`ToolNotAllowed`) and must never infer permission from `tools/list`.

## 4. Where real shapes differ from the planned domain model (spec §8)

| # | Observation | Impact |
|---|---|---|
| 1 | **All numbers are JSON numbers** (floats: `bidPrice: 214.1`, `vwap: 251.890615`, `totalReturn.amount: -38.920000000000016`). | Parse with `json.loads(..., parse_float=Decimal)` at the transport boundary. Never let a float reach `Money`, since `order_args_hash` rejects floats. |
| 2 | **Percentages are fractions**: `priceChangePercent: 0.020858` = 2.09%; also `dividendYield` and `totalReturn.percent`. | Name fields `*_fraction` or convert explicitly; don't display them raw. |
| 3 | **Daily candles start at `T05:00:00Z` on both sides of the DST change** (2026-10-13 under EDT and 2026-11-02 under EST alike). That is midnight *EST*, a fixed offset, not Toronto midnight. | Never treat `start` as an instant. Derive the session date as the Toronto date of `start` (correct under both offsets) via `market_time.TORONTO`. |
| 4 | **Candle gaps.** For `from=2026-10-10` (Sat) to `to=2026-11-09` (Mon), 18 candles came back: 2026-10-12 (Thanksgiving, TSX closed) is absent, as expected. **2026-10-27 (Tue) is also absent, unexplained**, and `to`'s own session (11-09) is not included, even though the quote shows a close for it. `totalReturn` spans the returned candles (10-13 → 11-07 at 05:00Z), not the requested window. | Never assume contiguous candles, and never derive holidays from candle data; the M1 calendars must come from `exchange_calendars`. Treat `to` as exclusive in practice. |
| 5 | `search_symbols` returns `securityUuid`, `symbol`, `description`, `listingMarket` ("TSX", "NASDAQ", "Pink Sheets"), `marketCapitalization`, `sector`, `industryGroup`, and eligibility flags. **No currency.** `industryGroup` contained a U+FFFD replacement character ("Retail�Cyclical"), so the server loses encoding. | Get currency from `get_quotes`. Map calendars from `listingMarket` (TSX → XTSE; NASDAQ/NYSE → XNYS), not only from the `.TO` suffix. Don't key on `industryGroup`. |
| 6 | `get_quotes` gives `currency`, `bidPrice`/`askPrice` + sizes, `lastPrice`, `quoteTime`/`lastTradeTime` (ISO `Z`), `delay` (bool), `marketState` ("Closed"), `isTradingHour`, and fundamentals (`eps`, `peRatio`, `sharesOutstanding`, `averageVolume*`, `lastDividend*`). | The guardrail buy notional can use `askPrice`. Refuse when `delay` is true or `marketState` isn't open (fail closed). |
| 7 | `list_accounts` (per its description, since no data came back): `name` = registration type + number masked by the server ("Margin ••••8001"), `currency`, `productType` ∈ {SD, SDCI, QWP}, `supportTrading`, `accountRoleType` ∈ {PrimaryAccountHolder, AuthorizedTrader}, optional `accountHolderName`. | The spec's `account_type` (TFSA/FHSA/RRSP/margin/cash) and `number_masked` must be **parsed from `name`**, and there is no `status`. Add `product_type`, `support_trading` and `role_type`. Exclude AuthorizedTrader accounts and QWP from any order flow. `accountHolderName` is PII. |
| 8 | `list_instructions` returns `{"items": [], "nextCursor": null}`. | Cursor pagination, not page numbers (unlike `get_account_activities`). |

## 5. Gap found in `app/redaction.Redactor`

Redacting these market-data responses showed two problems:
- Every 7–10 digit **integer** is masked as an account number. So `volume: 1234567` became `"****4567"`, and `sharesOutstanding` and `averageVolume20Days` were changed too. That makes the value a string and corrupts the fixture.
- The `amount` key rule rounded `totalReturn.amount` (public price data) to `-40.0`.

Account data was not affected. The fix is key-aware exemptions for market-data integer fields (`volume`, `*Size`, `sharesOutstanding`, `averageVolume*`, `marketCapitalization`) and for price-derived amounts, with tests. Nothing is promoted until that fix is in and Rudra approves.
