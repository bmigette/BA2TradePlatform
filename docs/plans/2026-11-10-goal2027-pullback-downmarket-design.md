# goal2027: down-market behaviour, pullback entries, per-year activity (design)

Date: 2026-11-10. Status: DESIGN, not built. Owner decisions recorded below.

## Why

Phase A walk-forward (2026-11-10) showed FMPRating S2/S6 genomes that scored well on 2022-2023
and opened ZERO trades in the 2024 test year: market-condition gates fitted to a falling market never
opened in a rising one. The fitness lets this through because it gates the AVERAGE trades per year
over the whole run (`_consistent_annual_return`: 30 for full score, under 12 rejected), not each year.

Owner requirements (2026-11-10):

* an expert that opens nothing for a year is not acceptable; a minimum must hold in down markets too;
* long-only in a down market: trade smaller/tighter, and enter on pullbacks;
* keep the existing volatility overlay; keep every current expert;
* try the Pullback expert in goal2027 (reverses the 2026-10-26 "not promoted" decision,
  `2026-10-25-goal2027-grid-design.md`);
* larger populations.

## The four changes

### 1. Per-calendar-year trade floor (fitness)

`strategy_fitness._consistent_annual_return` keeps its average gate (30 / hard 12) and gains a
per-year factor:

    year_gate = clamp(min over calendar years of trades_opened_in_year / FLOOR, 0, 1)
    fitness   = base x dd_guard x consistency x trade_gate x year_gate      (positive base only)

* `FLOOR = 12` (`_CAR_MIN_TRADES_EACH_YEAR`), per-expert override `car_min_trades_each_year`
  resolved like `car_min_trades_per_year` (`daily_backtest_handler.py:1341-1366`).
* Trades are counted as STRUCTURES by entry date (`results["trades"]`, `entry_time`, the
  `_structure_count` rule), not legs.
* Partial years: a year covering under ~6 months of the window is merged into its neighbour, the same
  rule `_calendar_year_returns` uses, and the floor is pro-rated to the covered fraction of a year.
* A soft cut, not a rejection: a genome is not pushed into forced trades to clear a cliff.
* Written to the results blob (`fitness_year_gate`, `trades_by_year`) so a row is decomposable.

No regime definition is needed: 2022 is a down year in every training window, so a genome must trade
through it. The same factor closes the 2024 case.

Known consequence: an entry gate that silences a whole year (for example spy-trend `above_200`) now
costs fitness. Intended.

### 2. Down-trend trigger on the regime overlay

Today the overlay scales only on STRESSED bars (SPY 20-day realized vol in its top 30%,
`market_regime.classify_volatility_regime`). Add a second, independent trigger:

* trigger: SPY below its 200-day average = `get_benchmark_regime().trend in {bear_rally, down}`
  (`benchmark_regime.TREND_MODE_CODES` `below_200`). Fixed constants, not genes. Already published per
  bar in the backtest (`daily_engine.py:~876`) and live (`TradeManager.py:450-548`): one function,
  two callers.
* settings (MarketExpertInterface, next to the existing four): `downtrend_overlay_enabled`,
  `downtrend_risk_scale`, `downtrend_stop_scale`, `downtrend_tp_scale`; genes 0.5-2.0 step 0.25 under
  the same `atr-regime-searched` policy as the volatility ones.
* `regime_overlay.regime_scale` returns the PRODUCT of the active triggers' scales (stressed and
  below-200 can both hold). Consumers (`TradeRiskManagement.py:1416, 1907, 1971`,
  `TradeActions.py:1276`) are unchanged: they already call `regime_scale`.
* Unknown trend (insufficient SPY history) = trigger off, as `is_stressed` does.

Live effect: none until an instance sets `downtrend_overlay_enabled`; default OFF.

### 3. Per-symbol pullback entry conditions (all current experts)

Optional, GA-searchable leaves appended to every S1-S7 entry tree by one shared function
`_append_equity_pullback_gates` (same shape as `_append_equity_market_condition_gates`,
`ba2test_launcher.py:7313`), behind a matrix flag `--pullback-gates` and a required `pullback` name
suffix (job identity).

| leaf id | condition | genes |
|---|---|---|
| `<kind>-pb-rsi` | NEW `RsiCondition`: Wilder RSI(period) of finished daily closes | mode off/below/above, value 5-50 step 5, period 2-5 (choice) |
| `<kind>-pb-below-high` | existing `N_PERCENT_BELOW_RECENT_HIGH` (20-day) | mode off/above/below, value 2-20 step 2 |
| `<kind>-pb-vs-sma200` | NEW `PriceVsSmaCondition`: (price - SMA200)/SMA200 % | mode off/above/below, value -10..10 step 2.5 |

* `mode = off` removes the leaf, so the current behaviour is inside the search space.
* New conditions live in `ba2_common/core/TradeConditions.py`, registered in `CONDITION_MAP`,
  `rule_builders.FIELD_EVENT` and `types.py` (a leaf without the mapping is silently dropped today:
  add a test that every pullback leaf resolves).
* Data: finished daily sessions through the clamped OHLCV provider (`_as_of_fetch_end`), "now" from
  the account price. `_wilder_rsi` moves from `PullbackReversion.py` to a shared helper in
  `ba2_common` and both use it (one function, two callers). Update the `OHLCV_SITES` count in
  `test_no_bar_price_in_decision_code.py`; no `.iloc[-1]` price reads.
* Live: the conditions are ordinary rule conditions, usable in live rulesets unchanged.

### 4. PullbackReversion as a goal2027 expert

* Launcher gene space for `PullbackReversion` (long only): `rsi_period` 2-5, `entry_threshold` 5-30
  step 5, `trend_gate` (3 choices), `exit_mode` (4 choices), `rsi_exit` 50-90 step 10, plus the
  shared sizing/ATR/overlay genes and the standard exit rules.
* Matrix: add it to `ALL_EXPERTS`; goal2027 steps `pb-large pb-mid pb-small` appended to `ALL_STEPS`.
* It stays research-only for live until it passes walk-forward and a forward test; promotion to the
  live registry is a separate decision.

## Grid budget

`grid_goal2027.sh`: `POPULATION` 200 -> 300 (all folds and the refit), `WF_NEXT_GENERATIONS` 15 -> 20,
`WF_FIRST_GENERATIONS` 30 unchanged, early stop unchanged (patience 5, 1%).
Cost: about 1.5x per job from population; more genes (pullback leaves, down-trend scales) are the
reason for the larger population.

## Versions and rollout

* `ba2_common` (conditions, overlay, shared RSI helper) and `ba2_experts` (PullbackReversion helper
  move): PACKAGE_VERSION bump, minimum raised (GA-affecting). TEST bump for the launcher/fitness.
* The fitness change makes every earlier CAR score non-comparable: record the date in the memo like
  the 2026-08-04 CAR change.
* goal2027 restarts from scratch under a new name suffix (`pullback`, plus a fitness tag); phase A's
  current run stays as the baseline and is not resumed after the change.

## Tests

* fitness: year gate with a silent year, a partial first/last year, structures vs legs, negative base
  unfactored, override setting.
* overlay: stressed only, below-200 only, both (product), unknown trend, disabled; backtest calendar
  and live path return the same flag for the same date.
* conditions: RSI against a hand-computed series; SMA200 distance; no look-ahead on the 5-minute clock
  (decision at 10:00 reads only finished sessions); every pullback leaf resolves to a condition.
* launcher: gates appended to all three S1 tiers with shared genes; `off` reproduces the tree without
  the leaf; flag without the name suffix is refused.
* PullbackReversion: gene space decodes to valid settings; one short optimize smoke run.

## Open points

* RSI value range and whether `above` is useful for a pullback gate (kept for symmetry; drop if it
  only adds noise).
* Whether small caps need a different per-year floor (fewer signals for some experts): decide from
  the first run's `trades_by_year`.
