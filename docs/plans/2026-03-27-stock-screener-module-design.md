# Stock Screener Module Design

**Date:** 2026-03-27

## Goal

Extract penny trader screening logic into a reusable `StockScreener` module, add a new `"screener"` instrument selection mode for all experts, and refactor the PennyMomentumTrader to use it.

## Architecture

### 1. `core/StockScreener.py`

Stateless utility class instantiated with a settings dict.

**`screen()` method** — main entry point, returns `List[Dict]`:
1. Calls the configured screener provider's `screen_stocks()` with basic filters (price, volume, market cap, float)
2. If RVOL filter enabled (`screener_relative_volume_min > 0`): batch-fetches quotes via FMP, calculates RVOL (`current_volume / avg_volume`), filters by threshold
3. If price drop filter enabled (`screener_price_drop_pct > 0`): fetches OHLCV data for lookback period, calculates % drop, filters by threshold
4. Ranks remaining stocks by chosen metric (`market_cap`, `volume`, `float_shares`, `relative_volume`, `composite`)
5. Returns top N stocks (configurable limit)

**Return format:** normalized dicts with keys: `symbol`, `company_name`, `price`, `volume`, `market_cap`, `float_shares`, `sector`, `industry`, `exchange`, `relative_volume` (if enriched), `price_drop_pct` (if enriched).

### 2. MarketExpertInterface Settings

New base settings, conditionally rendered when `instrument_selection_method == "screener"`:

| Setting Key | Type | Default | Description |
|---|---|---|---|
| `screener_provider` | str | `"fmp"` | Screener data provider |
| `screener_market_cap_min` | int | `1000000000` (1B) | Min market cap (0 = disabled) |
| `screener_market_cap_max` | int | `0` | Max market cap (0 = disabled) |
| `screener_volume_min` | int | `500000` | Min avg volume (0 = disabled) |
| `screener_volume_max` | int | `0` | Max avg volume (0 = disabled) |
| `screener_float_min` | int | `10000000` (10M) | Min share float (0 = disabled) |
| `screener_float_max` | int | `0` | Max share float (0 = disabled) |
| `screener_price_min` | float | `20.0` | Min price (0 = disabled) |
| `screener_price_max` | float | `0` | Max price (0 = disabled) |
| `screener_relative_volume_min` | float | `1.5` | Min relative volume (0 = disabled) |
| `screener_price_drop_pct` | float | `15.0` | Min price drop % (0 = disabled) |
| `screener_price_drop_days` | int | `1` | Lookback days for price drop |
| `screener_max_stocks` | int | `10` | Max stocks to select |
| `screener_sort_metric` | str | `"market_cap"` | Ranking: `market_cap`, `volume`, `float_shares`, `relative_volume`, `composite` |

Defaults target large-cap stocks. All descriptions note that 0 (or None) disables the filter.

### 3. JobManager Integration

- `get_enabled_instruments()` returns `["SCREENER"]` when mode is `"screener"`
- `JobManager._get_enabled_instruments()` recognizes `"SCREENER"`
- New `_execute_screener_analysis()` method:
  - Instantiates `StockScreener` with expert's screener settings
  - Calls `screener.screen()` to get stock list
  - Creates individual analysis jobs for each returned symbol (same pattern as dynamic/expert expansion)

### 4. PennyMomentumTrader Refactoring

Current Phase 1 does: FMP screener call → RVOL enrichment → filtering → sorting.

After refactoring:
- Phase 1 base screening delegates to `StockScreener.screen()` with penny trader settings mapped to screener params
- Penny trader keeps its extra layers on top: FMP gainers merge, StockTwits discovery (Phase 1c), LLM discovery (Phase 1b)
- RVOL sort metric hardcoded to `relative_volume` (current behavior)

### 5. UI Changes

**Instruments tab restructured:**
1. Top: `instrument_selection_method` dropdown (moved from General tab)
2. Below: dynamically rendered content based on selection:
   - `"static"` → instrument selector table
   - `"dynamic"` → AI prompt textarea, model selector, test button
   - `"expert"` → info banner
   - `"screener"` → screener settings (compact 2-column grid, same style as expert settings) + "Test Screener" button
3. General tab loses the instrument selection method dropdown

**Test Screener button:**
- Instantiates `StockScreener` with current (unsaved) form values
- Calls `screen()`, displays results in a dialog/table: symbol, company name, price, market cap, volume, float, RVOL, price drop %
- Spinner during loading, error notification on failure
