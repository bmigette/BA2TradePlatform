# FactorRanker Expert

**Configurable cross-sectional multi-factor equity ranker (momentum / value / quality / PEAD).**

FactorRanker ranks a candidate universe each rebalance by a weighted blend of classic
equity factors and holds the long-only top slice. Unlike the recommendation-based experts
(TradingAgents, FMPRating, …), it **self-executes**: a dedicated `FactorPortfolioManager`
diffs the target book against current holdings and submits the buy/sell deltas directly.

- **Type:** systematic factor / portfolio expert
- **Data sources:** FMP (daily prices, income/balance/cash-flow statements, company profile, earnings) via the existing data providers; `StockScreener` for screener universes
- **Instrument selection:** expert-driven (`instrument_selection_method="expert"`) — one batch run per rebalance
- **Execution:** self-contained via `FactorPortfolioManager` — **no `ExpertRecommendation`, no SmartRiskManager** (`uses_risk_manager=False`)
- **API key:** `FMP_API_KEY`

---

## How it works

One rebalance = one batch run of `run_analysis("EXPERT", market_analysis)`:

1. **Resolve the universe** (`universe_source`):
   - `static` — the instruments configured under the Instruments tab (`enabled_instruments`).
   - `screener` — runs `StockScreener` with the expert's `screener_*` settings and ranks the matched symbols.
2. **Liquidity guard** — drop symbols below `min_price` (when > 0).
3. **Compute the enabled factors** (those with weight > 0):
   - **momentum** — 12‑1 month total return (skips the most recent month to avoid short-term reversal).
   - **value** — equal blend of earnings yield (E/P) and FCF/EV yield (higher = cheaper).
   - **quality** — ROE + gross profitability (gross profit / total assets) − Sloan accruals ratio.
   - **pead** — *Post-Earnings-Announcement Drift*: standardized unexpected earnings (SUE = `(actual EPS − estimated EPS) / estimate dispersion`), counted only while the stock is still within the post-earnings drift window (`pead_drift_window_days`); 0 otherwise.
4. **Combine** — winsorize each factor (`winsorize_pct`), cross-sectional z-score, multiply by its weight, and sum into a composite score; rank descending.
5. **Construct** — long-only target weights for the top `top_n` names: `equal` (1/N) or `score`-proportional `weighting`, with a per-name cap (`max_weight_per_name`, enforced by water-filling) scaled to `gross_exposure`.
6. **Rebalance** — `FactorPortfolioManager` reads current holdings (this expert's `OPENED` transactions), prices them, computes whole-share deltas vs the targets, and submits market orders (buys for new/added names, sells for dropped/reduced names). New positions pre-create a `Transaction` stamped with `expert_id` so the holding is attributed to this expert on the next rebalance.
7. **Audit** — the ranked book (per-symbol factor z-scores, composite, rank, target weight, action) is written to `MarketAnalysis.state` and an `AnalysisOutput`, and rendered in the analysis detail UI.

Symbols whose data can't be gathered (FMP error / missing fundamentals) are **dropped from that run with a logged warning** — the rebalance proceeds with the rest, no fail-safe defaults.

---

## Scheduling

- FactorRanker runs as a **single batch job** per rebalance (`should_expand_instrument_jobs=False`) — not one job per symbol.
- It handles entries *and* exits in that one run, so it renders **only the Enter-Market schedule** (`schedules_open_positions=False`).
- Schedules can be **weekly** (day-of-week + time) or **monthly** on the Nth weekday (e.g. *1st Monday*), configured in General Settings → Enter-Market schedule.

> **Operational note — FMP rate limits.** Running many FactorRanker instances at the same time hammers the FMP fundamentals/earnings endpoints, which respond to throttling with an error payload. **Stagger instance schedules** (e.g. 15 minutes apart) rather than firing them all at once. The FMP providers retry rate-limit responses with backoff and log the raw payload on persistent failure.

---

## Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `universe_source` | str | `static` | Candidate universe: `static` (enabled_instruments) or `screener` (StockScreener filters). |
| `factor_weight_momentum` | float | `1.0` | Momentum factor weight (0 disables it). |
| `factor_weight_value` | float | `1.0` | Value factor weight (0 disables it). |
| `factor_weight_quality` | float | `1.0` | Quality factor weight (0 disables it). |
| `factor_weight_pead` | float | `0.0` | Post-earnings-drift factor weight (0 disables it). |
| `top_n` | int | `20` | Number of top-ranked names to hold. |
| `weighting` | str | `equal` | Position weighting: `equal` (1/N) or `score`-proportional. |
| `max_weight_per_name` | float | `0.10` | Maximum portfolio weight per holding (0–1). |
| `gross_exposure` | float | `1.0` | Total gross exposure to deploy (1.0 = fully invested). |
| `winsorize_pct` | float | `0.02` | Winsorize each factor's tails at this fraction before z-scoring. |
| `pead_drift_window_days` | int | `60` | Post-earnings drift window (days) for the PEAD factor. |
| `min_price` | float | `0.0` | Minimum share price liquidity guard (0 disables). |
| `sector_neutralize` | bool | `False` | Reserved — not applied in v1. |
| `min_dollar_volume` | float | `0.0` | Reserved — not enforced in v1. |
| `hard_stop_pct` | float | `0.0` | Reserved — optional per-name hard stop between rebalances. |

**Per-factor weights are separate float settings** (not a single JSON blob), so each renders as a plain number field.

### Screener universe settings (`universe_source = "screener"`)

FactorRanker reuses the platform's shared `screener_*` settings:
`screener_market_cap_min/max`, `screener_price_min/max`, `screener_volume_min/max`,
`screener_sort_metric` (`market_cap` | `volume` | `composite` | …), `screener_max_stocks`,
`screener_provider`.

> ⚠️ The screener's defaults (`screener_relative_volume_min = 1.05`, `screener_price_drop_pct = 15`)
> are penny-momentum oriented and **wrong for factor strategies** — set both to `0` and select on
> market cap / price / volume instead.

---

## Example configurations

- **Nasdaq-50 momentum** — `universe_source=static`, enabled_instruments = your Nasdaq list, `factor_weight_momentum=1`, others `0`, `top_n=15`, `weighting=equal`.
- **Multi-factor, equal weight** — static or screener, `factor_weight_momentum=value=quality=1`, `pead=0`, `top_n=20`.
- **Large-cap screener, all factors** — `universe_source=screener`, `screener_market_cap_min=10e9`, `screener_volume_min=1e6`, `screener_sort_metric=market_cap`, `screener_relative_volume_min=0`, `screener_price_drop_pct=0`, all four factor weights > 0.

---

## Implementation

- `modules/experts/FactorRanker/__init__.py` — the expert (settings, properties, `run_analysis`, `render_market_analysis`).
- `modules/experts/FactorRanker/factors.py` — pure factor calculators + z-score / composite / rank.
- `modules/experts/FactorRanker/construction.py` — long-only top-N target weights (water-filling caps).
- `modules/experts/FactorRanker/portfolio.py` — `rebalance_deltas` + `FactorPortfolioManager` (execution).
- `modules/experts/FactorRanker/data.py` — bulk FMP data adapters (per-symbol, drop-on-error).

The pure factor / combine / construction / rebalance logic is fully unit-tested; see `tests/test_factorranker_*.py`.
