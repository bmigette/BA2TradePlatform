# Option grid: configurable max underlying stock price via screener entry gate

Date: 2026-07-29
Status: approved design (pre-plan)

## Goal

The options strategy matrix (`tools/run_options_matrix.py`) runs on $20k starting capital, but
its static top-100 large-cap universe contains underlyings priced far above what a $20k account
can structure options on. Add a **configurable maximum underlying stock price** (default $100,
targeted at the $20k account) that blocks option entries while the underlying trades above the
cap, **per strategy** where needed.

## Chosen approach: gate-only screener mode (approved)

Reuse the existing screener machinery instead of inventing a new filter:

- The metric store already supports a point-in-time `price_max` filter
  (`packages/providers/ba2_providers/screener/metric_store.py` `screen_universe_for_day`,
  `_le("price", "price_max")`).
- The backtest engine already supports `screener_runtime`: a per-bar entry gate restricting
  entries to the per-day screened universe (wired per-trial by
  `testplatform/backend/app/services/strategy_optimization_handler.py:_build_daily_trial_config`).
- Today that gate only exists in full `--screener` mode, which also replaces the run universe
  with the store's symbols and adds `screener:*` GA genes. Full `--screener` mode was rejected
  for the option grid: the screened universe can select symbols outside the offline options
  cache (OptionsCacheMiss risk) and grows the GA search space.

**Gate-only mode** keeps the static `--universe` (top-100, options-cache-covered) and attaches
the store purely as an entry gate: no universe switch, no screener genes, no candidate-bound
universe restriction.

Rejected alternative (earlier draft): a new `option_max_underlying_price` option-action param
plumbed through `TradeActions`/`TradeActionEvaluator`/`rule_builders`. Correct, but duplicates
an existing filter; the user directed the screener-filter approach instead.

## Changes

### 1. `testplatform/ba2test_launcher.py`

- New CLI flag `--screener-gate-store <parquet path>` on the `optimize` command. May be combined
  with the existing `--screener-base-json` and `--screener-cadence-days` flags. When set WITHOUT
  `--screener`:
  - the run universe stays the static `--universe`;
  - no `screener:*` genes are merged into `expert_params`;
  - the `backtest` block gains `screener_opt` with `"gate_only": true`, the store path, base
    settings, and cadence.
  - `--screener-gate-store` together with `--screener` is rejected (pick one mode).
- New CLI flag `--max-stock-price <float>` (default `100.0`): sets `price_max` in the gate base
  settings. `0` disables the price cap (gate becomes a no-op; still allows future base-json
  criteria to ride along).
- Default gate base settings: everything most-admitting except the price cap —
  `market_cap_min: 0`, `relative_volume_min: 0`, `price_drop_pct: 0`, `max_stocks` at a high
  ceiling, `price_max: <--max-stock-price>` — so only the price filter bites.
  `--screener-base-json` merges OVER this default (explicit keys win).
- Per-strategy override: `_OPTION_STRATS` entries accept an optional `screener_gate_base` dict
  (e.g. `{"price_max": 60.0}` for a full-notional structure). Precedence, highest first:
  per-strategy `screener_gate_base` > `--screener-base-json` > `--max-stock-price` default block.
  This is the "depending on strategy" knob.

### 2. `testplatform/backend/app/services/strategy_optimization_handler.py`

- `_build_hoisted_state`: accept `screener_opt` with `"gate_only": true` — warm/memo the store,
  hoist `screener_store`, `screener_base`, `screener_cadence_days` exactly as today. Never set
  `screener_apply_to_expert_settings` for gate-only runs.
- `_build_daily_trial_config`: when the hoisted state is gate-only, build `screener_runtime`
  from the hoisted base settings (there are no decoded screener overrides — no genes exist),
  skip the candidate-bound (`screened_symbol_union`) universe restriction, and leave the
  static instrument list and expert settings untouched. Non-gate-only behavior is unchanged
  byte-for-byte.

### 3. `tools/run_options_matrix.py`

- New flags `--screener-gate-store` and `--max-stock-price` (default 100), passed through to
  every `ba2-test optimize` invocation so matrix jobs run with the gate.

## Semantics and caveats

- The gate is point-in-time: an underlying above the cap on a bar has its entries skipped that
  bar; if it later trades back below the cap, entries resume. This is why a static universe-file
  filter (today's price) was rejected.
- Store rows exist per scan date (weekly by default); `cadence_days` (default 7) holds the
  screened set between scans, so the price check can be up to `cadence_days` stale. For a
  tighter price gate, build the store with daily scans and/or pass `--screener-cadence-days 1`.
- **Prerequisite:** the metric store must cover the top-100 options universe over the backtest
  window. A symbol with no store row is not in the screened set, so its entries are blocked.
  Implementation adds a startup warning when the store's coverage of the run universe looks thin
  (e.g. < 90% of universe symbols present in the store) so a bad store fails loud instead of
  silently starving trades.
- The engine gate sits at entry level, so it covers option entries (and the O_CC/O_PP equity
  legs) — to be confirmed by the engine test below.

## Tests

- Handler unit test (`testplatform/backend/tests/`): a gate-only hoisted state yields a trial
  config whose `screener_runtime.settings` carries `price_max`, with the instrument list and
  expert settings untouched and no candidate-bound restriction.
- Launcher test: `--max-stock-price` lands in the gate base settings; per-strategy
  `screener_gate_base` overrides it; `--screener` + `--screener-gate-store` exits with an error.
- Engine test: with a store row pricing the underlying above the cap on a bar, an option entry
  on that bar is blocked; below the cap it proceeds. Extend existing screener_runtime gate tests
  if present.

## Out of scope

- GA-optimizing the cap (no `screener_price_max` gene in gate-only mode).
- Live-app changes.
- The pre-existing `FIELD_EVENT` gap in `packages/common/ba2_common/core/rule_builders.py`
  (price-vs-target and other condition leaves silently dropped when seeding backtest rulesets) —
  a separate bug worth its own fix.
