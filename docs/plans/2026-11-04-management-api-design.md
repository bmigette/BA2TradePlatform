# Management HTTP API -- design

Date: 2026-11-04. Status: validated with the user, not built.

## Goal

Manage a running trade platform instance (8080 dev, 8081 prod, 8082 options) over HTTP: read
trades, account data, overview stats, growth, P&L, dividends and portfolios; adjust TP/SL, close
trades, cancel orders, and manage experts. A future MCP server is a thin HTTP client of this API,
typed from its OpenAPI spec; no platform code goes into the MCP.

Today the app exposes three endpoints (`ui/api_routes.py`: `/api/reload`, `/api/run-schedule`,
`/api/process-recommendations`). Everything else lives inline in the UI pages (`overview.py` is
8.5k lines, `live_trades.py` holds the TP/SL branching).

## 1. Architecture and auth

- `ba2_trade_platform/services/`: UI-free functions returning plain dicts or dataclasses and
  raising typed errors (`NotFound`, `Conflict`, `Invalid`, `BrokerRefused`, `BrokerUnreachable`,
  `Unmeasurable`). Modules: `trades`, `accounts`, `stats`, `growth`, `portfolio`, `experts`,
  `analysis`.
- `ba2_trade_platform/api/`: one FastAPI router per domain under `/api/v1`, mounted in
  `ui/main.py`. The three existing endpoints move under `/api/v1`; the old paths stay as aliases.
- **One function, two callers.** Every UI action or figure with an API twin moves into
  `services/`; the page keeps only dialogs and notifications. The API can then never disagree
  with the UI.
- **Auth.** A new `api_token` AppSetting per instance, generated from the Settings page.
  - Every write needs `Authorization: Bearer <token>`.
  - Reads need no token from 127.0.0.1; from any other address they need the token.
  - With no token set, writes and non-local requests are refused (403). Nothing is open by
    default. This also applies to the three existing endpoints, so local
    `/api/run-schedule` keeps working only once the instance has a token.
- **Audit.** Every write logs an INFO line (caller IP, endpoint, arguments, result, the token's
  last four characters only) and writes an ActivityLog entry with source `api`. TP/SL writes pass
  `source="api"` to `adjust_tp_sl`.
- **Docs.** `/docs` and `/openapi.json`, under the read rule.

## 2. Endpoints (`/api/v1`)

Lists take `limit` and `offset`.

### Reads

| Endpoint | Returns |
|---|---|
| `GET /accounts` | Account definitions plus a snapshot summary |
| `GET /accounts/{id}` | Definition, provider, settings with credentials masked (never returned), live snapshot: balance, equity, cash, buying power, broker multiplier, margin factor, exposure, headroom, pending close and pending entry |
| `GET /accounts/{id}/positions`, `GET /accounts/{id}/orders` | Broker-side positions and orders |
| `GET /accounts/{id}/growth?from&to` | Balance history and total growth (Overview growth tab) |
| `GET /accounts/{id}/growth/labels?labels=` | Growth by label |
| `GET /accounts/{id}/growth/labels/{label}/positions` | Growth by position inside a label |
| `GET /accounts/{id}/pnl/closed?from&to&group_by=day\|month\|expert\|symbol` | Closed P&L |
| `GET /accounts/{id}/dividends?from&to&symbol` | Cash dividends (`get_dividends`) plus the income ledger (`PortfolioIncomeEvent`, open income total) |
| `GET /accounts/{id}/portfolio` | Allocation view payload: per label target vs current weight, invested, floating P&L with and without dividends, symbol counts; per symbol inside each label; valuation mode. 404 when the account has no `PortfolioAllocationConfig` |
| `GET /accounts/{id}/portfolio/runs` | Allocation runs and fill status |
| `GET /transactions?account&expert&status&symbol&from&to` | Trades with open and closed P&L |
| `GET /transactions/{id}` | One trade: orders, TP/SL, override locks, activity |
| `GET /orders?account&status&transaction` | Platform orders, including pending and WAITING_TRIGGER |
| `GET /stats/overview?account` | Overview page figures: P&L by period, per-expert performance, allocation, exposure |
| `GET /experts`, `GET /experts/{id}` | Instance, settings, schedule, virtual and available balance, reserve, last run |
| `GET /analyses?expert&symbol&status&subtype&from`, `GET /analyses/{id}` | Market analyses with skip reasons; one analysis with state, outputs and recommendation |
| `GET /recommendations?expert&symbol&from` | Recommendations |
| `GET /risk-manager/runs?expert`, `GET /risk-manager/runs/{id}` | RiskManagerRun and SmartRiskManagerJob |
| `GET /activity?type&from` | Activity log |

### Writes (token required)

| Endpoint | Action |
|---|---|
| `POST /transactions/{id}/tp-sl` `{take_profit?, stop_loss?, lock?}` | Same path as the Live Trades dialog, including the manual override lock; returns the new broker leg ids |
| `POST /transactions/{id}/tp-sl/unlock` | Clears the override locks |
| `POST /transactions/{id}/close` | `close_transaction`, with the CLOSING guard |
| `POST /orders/{id}/cancel` | Cancels a pending order |
| `PATCH /experts/{id}` `{enabled?, virtual_equity_pct?}` | |
| `PATCH /experts/{id}/settings` `{key: value}` | Validated against `get_settings_definitions()`, then instance cache and schedule reload |
| `POST /experts/{id}/run` `{subtype}` | Today's `/api/run-schedule` |
| `POST /reload`, `POST /process-recommendations` | Unchanged, moved |

Not in v1: opening manual trades, creating or deleting experts and accounts, ruleset edits,
portfolio allocator writes. Each is one service function plus one route later.

## 3. Errors and live-money safety

- JSON errors: `{"error": code, "message": ..., "detail": {...}}`.
  - 404 `not_found`, 409 `conflict`, 422 `invalid`.
  - 502 `broker_refused`, with the broker's own message (e.g. Alpaca 40310000).
  - 503 `broker_unreachable`.
  - A figure that cannot be measured fails the request (`unmeasurable`), never reads as 0 or
    null.
- Guards, reused from the UI:
  - `close` returns 409 on CLOSING, CLOSED or FAILED.
  - `tp-sl` returns 422 on a price on the wrong side of the current price, or when no price is
    given.
  - `cancel` returns 409 on a terminal order.
  - `PATCH settings` validates against the definitions.
  - Writes take the same `TradeManager` processing lock as expert passes, so they wait or return
    409 instead of racing an entry pass.
- `?dry_run=true` on every write returns the orders it would cancel and place, without contacting
  the broker.
- An optional `Idempotency-Key` header: a repeat within 10 minutes returns the first result
  instead of sending a second order.
- Reads never trigger a broker write or a sync. Broker-backed reads use the account caches and
  return `as_of`.
- No rate limiting and no CORS in v1 (one caller, private LAN).

## 4. Testing and rollout

Testing:
- **Services:** pytest on in-memory DB fixtures with a fake account.
- **Golden parity tests per extracted page area:** the service equals the old inline computation
  on a fixed DB.
- **Routes:** FastAPI `TestClient`, covering:
  - the auth matrix (local or remote, read or write, token set or unset);
  - the error codes;
  - dry run;
  - idempotency;
  - the lock 409.
- **Writes:** a call-recording fake broker; the UI action and the API call must produce the same
  broker call sequence.
- **Smoke:** `tools/api_smoke.py`, read-only by default, for manual runs against 8080 or 8082.
  No live calls in CI.

Phases (one branch and merge each, APP bump each):
1. Auth and skeleton: token setting and Settings button, `/api/v1` mount, existing endpoints moved
   with aliases, `/docs`.
2. Reads straight from the DB and account methods: accounts and snapshot, transactions, orders,
   analyses, recommendations, risk-manager runs, activity, experts.
3. Writes: `tp-sl`, `close`, `cancel`, expert PATCHes, `run`. Extracts the Live Trades TP/SL logic
   into `services/trades.py` and switches the page to it.
4. Stats: overview stats, growth, closed P&L, dividends. The `overview.py` extraction, with golden
   tests.
5. Portfolio: allocation view payload, income, runs.
6. Later, separately: the MCP server as an HTTP client of `/openapi.json`.

Deploy: test each phase on 8080, then 8082. Prod 8081 picks it up on restart, which is the
user's call; prod writes work only after its token is generated.
