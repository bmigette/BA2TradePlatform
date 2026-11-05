# Management HTTP API, Phase 4 (stats, growth, closed P&L, dividends) -- Implementation Plan

Date: 2026-11-04. Branch `feat/api-phase4-stats`, based on origin/dev 75431922. Spec: `2026-11-04-management-api-design.md`
(section 2 rows for `/stats/overview`, `/accounts/{id}/growth*`, `/pnl/closed`, `/dividends`; section 3). Phases 1-2:
`2026-11-04-management-api-phase1-2.md`.

## Rules for this phase

- **One function, two callers.** The DATA computation moves out of `ui/pages/overview.py` and the Overview widgets
  (`ui/components/*`) into `services/stats.py`, `services/growth.py` and `services/floating_pl.py` (plain dicts / series,
  no NiceGUI). The page keeps rendering, caching and notifications and calls the service.
- **Golden parity first.** Before each extraction, the CURRENT inline code is run on a fixed DB / fixed fake broker data and
  what it computes is written to `tests/golden_overview/*.json` by `tests/golden_overview/capture.py` (run on the
  unrefactored commit; the commit is named in the golden file). After the extraction the service must equal that file
  exactly. The capture reads the inline code's own local variables at function return (`sys.setprofile`), so the figures
  compared are the ones the page draws from, not a re-derivation.
- GET purity: no broker writes, no sync, no failure-memory writes (`services/_readonly.py` around any capital read; every
  broker call through `services.accounts.broker_read`). A figure that cannot be measured raises `Unmeasurable` (503) or marks
  the row `unmeasured`; never 0/null. Unbounded lists are paged or capped; broker-backed figures carry `as_of`. The DB session
  is closed before any broker call (the live app shares the SQLite DB).
- No change under `packages/`.

## Tasks (each one commit; tests first)

1. `services/stats.py`: trade performance (P&L by period, win/loss, open count), order counts per account, profit per expert,
   position distribution by label/category. Golden captures from `OverviewTab._load_trade_performance_data`,
   `_load_order_statistics_data`, `ProfitPerExpertChart.calculate_expert_profits`,
   `InstrumentDistributionChart.calculate_position_distribution`. Switch the page and the two charts.
2. `services/floating_pl.py` + balance usage: the floating P/L calculators (per account / per expert) and
   `BalanceUsagePerExpertChart.calculate_expert_balance_data` move out of the widgets (widgets inherit the calculators, so
   method names and behaviour stay); existing widget tests that patched the widget module's `get_account_instance_from_id`
   now patch the service module. Golden: the existing widget suites plus a new captured fixture for the balance chart.
3. `GET /stats/overview?account`: composes the above. Broker-backed parts (floating P/L, exposure, distribution, balance usage)
   report per section `status` (`ok` | `unmeasurable`) with the reason: one unreadable account does not blank the others, and
   no section ever reads as 0 when it could not be measured; `as_of` on the response.
4. `services/growth.py`: `build_qty_timeline`, monthly realized income (FIFO from fills + dividends), value denominators, total
   account growth series, growth by label, growth by position inside a label, single-position series. Golden captures from
   the `AccountGrowthTab` methods named in the task brief.
5. Growth endpoints: `GET /accounts/{id}/growth?from&to`, `.../growth/labels?labels=`, `.../growth/labels/{label}/positions`.
   Broker data read once (balance history, dividends, filled trades, positions) through `broker_read`; historical closes from the
   same yfinance download the page uses (`fetch_historical_prices`). No prices for held symbols => `Unmeasurable`.
6. `GET /accounts/{id}/pnl/closed?from&to&group_by=day|month|expert|symbol`: closed `Transaction` rows of the account,
   P&L by `calculate_transaction_pnl` (the same function the Overview uses); rows without a computable P&L are counted in
   `unmeasured`, never summed as 0. Capped (`limit`) groups.
7. `GET /accounts/{id}/dividends?from&to&symbol`: broker `get_dividends()` (net amount, cash vs DRIP via
   `cash_dividends_by_symbol`), plus the income ledger (`get_recent_income_events`, `get_open_income_total`).
   An empty broker answer carries `fetch_ok: false` (the adapters turn failures into `[]`).
8. Open P&L on `GET /transactions` and `/transactions/{id}`: `open_pnl` + `pnl_status` (`realized` | `open_measured` |
   `unmeasured`...). Same price source as the Overview floating P/L cards and Live Trades: the broker position price
   (`position_prices.broker_position_prices_from`), the equity formula of the floating P/L cards for stock rows and
   `open_structure_pnl` for option structures. One `get_positions()` per account per request. Unknown price => that row is
   `unmeasured`.
9. Guard test params, route-table invariants, version bump, full api_db sweep twice, touched overview tests,
   `tools/check_package_versions.py`.

## Open questions

(recorded in the final report)
