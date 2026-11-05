# Management HTTP API, Phase 5 (portfolio reads) -- Implementation Plan

Spec: `docs/plans/2026-11-04-management-api-design.md` (section 2 rows `GET /accounts/{id}/portfolio` and
`/portfolio/runs`, section 3). Build pattern: `docs/plans/2026-11-04-management-api-phase1-2.md`.
Dividends and growth belong to phase 4 and are not touched.

**Goal.** Move the data loaders of the Portfolio Allocation page into `ba2_trade_platform/services/portfolio.py`
(ONE FUNCTION, TWO CALLERS), switch the page to them, and add two pure GET endpoints on top.

## Loaders moved (page name -> service name)

| Page function | Service function | Notes |
|---|---|---|
| `_enabled_expert_names`, `_load_gate` | `enabled_expert_names`, `load_gate` | verbatim |
| `_load_protection_payload` | `load_protection_payload` | verbatim (already display only) |
| `refresh_symbol_facts` | `refresh_symbol_facts` | WRITES (broker call + DB); page only |
| `_load_view_payload` | `load_view_payload(account_id, mode, base_override=None, *, read_only=False, account=None, config=None)` | `read_only=True` is the API's mode |
| `_load_valuation_mode` | `load_valuation_mode` | creates the config row (page); the API uses `peek_allocation_config` |
| `_load_income_panel` | `load_income_panel` = `sync_income_events` + `read_income_panel` | the API calls only `read_income_panel` |

The page keeps thin wrappers under the old names (tests and `asyncio.to_thread(_load_x, ...)` call sites keep working).
Tests patch `core.utils.get_account_instance_from_id` and `page.logger`; the service imports the factory lazily from
`core.utils` and uses the shared `logger` object, so both patches still hit.

## Purity of the GET (design section 3)

`load_view_payload(read_only=True)` differs from the page call in exactly these places:
* symbol facts: the STORED `AccountSymbolFacts` rows, never `refresh_symbol_facts` (a broker call plus a DB write);
  the response carries the newest `fetched_at` as `stored.symbol_facts_fetched_at`.
* the allocation config is READ, never created (`peek_allocation_config`); no row -> 404 `not_found`
  "no portfolio on this account".
Everything else already is a read: positions, mark prices, snapshot, `get_dividends` (broker reads, through
`accounts.broker_read`), stored stats (`load_symbol_stats`, age = `stored.symbol_stats_fetched_at`), exclusions,
protection (DB only). `sync_income_events`, `refresh_symbol_stats`, `refresh_orders_from_broker` never run from a GET.
Income: the API returns the stored ledger; the ledger has no sync stamp, so the response says so
(`income.synced_by: "page load / Refresh"`) and gives the newest event's `created_at`.

## Unmeasured, never 0

* Held market-mode symbol with no quote (`SymbolRow.measurable == False`): `current_value`, `pct_of_label`,
  `pct_of_total` are null, `measured: false`; the label lists it in `unmeasured_symbols` and `complete: false`.
* Snapshot unreadable (the page omits its reserve row): `base_notional` / `available_buying_power` /
  `account_value` null and named in top-level `unmeasured`; `pct_of_base` and `target_value` null.
* `get_positions() is None` (`PositionFetchFailed`) -> 503 `broker_unreachable`. Broker errors map via `broker_read`.
* Dividends: `get_dividends` returns `[]` on failure, so an empty feed is reported as `dividends.payments: 0` with a
  note and `with_dividends` stays null (the engine's own rule), never a 0.

## Tasks (TDD, one commit each)

1. Golden capture: a deterministic scenario (`tests/portfolio_golden.py`) run against the CURRENT page loaders writes
   `tests/golden/portfolio_loaders.json`; `tests/test_portfolio_service_parity.py` asserts the loaders equal it.
2. `services/portfolio.py` with the moved loaders; page wrappers; parity test now targets the service AND the page.
3. `read_only` mode + `peek_allocation_config` + `read_income_panel` (purity tests: no write, no refresh call).
4. `portfolio_view` shaping + `GET /accounts/{id}/portfolio` (404, 503, unmeasured, scrub).
5. `allocation_runs` + `GET /accounts/{id}/portfolio/runs` (SQL paging, list columns, fill status).
6. APP_VERSION bump, full verification.

## Addendum (review fixes, same day)

* **I1 / base lower bound.** In market mode `compute_base_notional` values an unpriced, non-excluded managed holding
  at 0. The PAGE already flags this: a danger banner "No quote for N held symbol(s)" (`missing_quote_symbols`) and
  Allocate is blocked (`held_no_price_block`). The page's display is unchanged. The API response sets `base_notional`
  null, reports the figure as `base_notional_lower_bound`, lists `unpriced_managed_holdings`, names `base_notional`
  in `unmeasured`, and nulls every base-derived field (label `target_value` and `current_pct_of_base`, row
  `target_value` and `pct_of_label_target`).
* **I2.** `unmeasured` is derived from the response values being null (an all-None snapshot does not raise on some
  adapters).
* **I3.** `pct_of_label` is null for every row of an incomplete label; `pct_of_total` (rows) and `pct_of_managed`
  (labels) are null whenever any non-excluded symbol in the book is unmeasured.
* **M1.** `get_dividends()` is cached 60 s per account on the API path only (`cached_dividends`); only a non-empty
  answer is cached (`[]` is also a failed fetch). `dividends.age_s` / `dividends.cached` report it.
* **M2.** On the API path the stored symbol facts and stats are read with strict readers: a read failure is 503
  `unmeasurable` (`detail.stored_read_failed`), never "no facts". The page keeps `load_symbol_facts` / `load_symbol_stats`.
* **M3 / M4.** A run that never finalised (`income_consumed_at` null, orders still working) reports
  `filled_buy_value` / `filled_sell_value` null with `filled_note`. `fill.status` is `no_execution_orders` when
  `order_ids` exist but are all protective legs.
* **M5.** `collect_order_fills(..., on_missing=cb)`: the runs list logs the missing order ids once per request at
  WARNING; the default (page) behaviour, `logger.error` per id, is unchanged.
* **M6.** A corrupt `order_ids` entry is 503 `unmeasurable` naming the run (`detail.run_id`).
* **M8.** A line-continuation slip joined `else         refresh_symbol_facts(...)` onto one line; restored, and
  runs of 3+ blank lines between the moved functions collapsed.
