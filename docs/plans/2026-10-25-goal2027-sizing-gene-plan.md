# goal2027 item 2.1: sizing-mode gene (implementation plan)

Design source: `docs/plans/2026-10-25-goal2027-grid-design.md` section 2.1. Branch `feat/goal2027-sizing-gene`.

## Decisions

- **Flag:** `ba2-test optimize --search-sizing-mode` (store_true; parallels `--search-sl-loosen`). `--sizing-mode
  notional|risk_atr` is unchanged. The two are mutually exclusive: the launcher exits with a message naming both.
  `optimize-batch` has no `--sizing-mode` today and gets no gene flag.
- **Gene:** `model:sizing_mode`, a `choice` gene over `["risk_atr", "notional"]` (that order is a persisted contract:
  the GA stores the index). `collect_param_space` already encodes choices as int indexes and `decode_individual` maps
  them back to the value, which flows `model:sizing_mode -> expert_overrides -> trial expert settings`.
- **Precedence (already correct, now pinned by test):** in `_build_daily_trial_config`, `expert_overrides` (genes) are
  merged over the spec settings. When the gene is on, the launcher also removes `sizing_mode` from the run's expert
  settings, so no pin can disagree with the gene and `expertFixedSettings` (persisted on each top-N row) carries no
  `sizing_mode`. An old row has no `model:sizing_mode` gene, so decode is unchanged for it; its pin still applies.
- **Canonicalisation:** one pure function, `strategy_param_space.neutralise_sizing_genes(flat)`. Under
  `model:sizing_mode == "notional"` it removes every gene in `NOTIONAL_INERT_MODEL_GENES` (`model:atr_risk_budget_pct`)
  and returns the same dict object when nothing changes. Canonical value = ABSENT, which is exactly what a fixed
  `--sizing-mode notional` run builds (`_rm_opt_for` drops the gene there), so the trial config is equal to the
  fixed-flag run's. It is called from `decode_params` (the one decode path: GA, top-N persist, re-run, export) and from
  `canonical_trial_params` (the memo key, both the in-process and the distributed path).
- **Scope:** classic S1-S7 only. Option kinds and bypass experts (FactorRanker) are refused loudly in the launcher
  (same convention as `_refuse_sl_loosen_on_bypass`). The capband driver forwards the flag only to classic jobs and
  prints a NOTE for the FactorRanker job (like the decision-time gene).
- **Deploy export:** `model:sizing_mode` is a `model:*` gene in the stored `strategy_params`; `backtest_export`
  already layers `model_overrides` over the base settings and the persisted fixed floor, so the chosen mode reaches
  the live expert settings with no package change. Pinned by a test. The raw (unneutralised)
  `atr_risk_budget_pct` stays in a notional row's `strategy_params` (needed so `distinct_topn` keeps matching the raw
  genome); live ignores it under notional.
- **No `packages/` change**, so only `TEST_APP_VERSION` is bumped.

## Gene audit (sizing code: `TradeRiskManagement.calculate_position_sizes`, `_ensure_safeguard_stop`,
`_risk_atr_quantity`; `position_sizing.resolve_sizing_risk_budget_pct`)

Mode switch: `risk_atr` sizes by `_risk_atr_quantity`; `notional` sizes by `min(per-instrument room / price, balance /
price)`. BOTH modes call `_ensure_safeguard_stop` (notional directly, risk_atr inside `_risk_atr_quantity`) when the
order carries no explicit stop.

| gene | risk_atr | notional | action |
|---|---|---|---|
| `atr_risk_budget_pct` | size budget (falls back to `risk_per_trade_pct`) | NEVER read | neutralised (dropped) |
| `risk_per_trade_pct` | safeguard-stop distance | safeguard-stop distance | keep (stop distance applies in both) |
| `atr_multiplier` | safeguard stop (only if `use_atr_stop`) and sizing floor | safeguard stop (only if `use_atr_stop`) | keep |
| `atr_period` | ATR window of the safeguard stop | same | keep |
| `min_stop_loss_pct` | stop floor, also the sizing stop fallback | stop floor | keep |
| `use_atr_stop` | stop synthesis toggle | stop synthesis toggle | keep (searched only under atr-searched) |
| `max_virtual_equity_per_instrument_percent` | ceiling (`max_position_value`) | the main sizer | keep in both (design) |
| `regime_*` | pinned off by policy (`regime_overlay_enabled`=0) | same | not mode-specific; unchanged |
| expert/rule/screener/schedule genes | no sizing read | no sizing read | keep |

Caveat: `risk_per_trade_pct`/`atr_*`/`min_stop_loss_pct` act only on an order WITHOUT an explicit ruleset stop; that
holds equally in both modes, so it is not a mode-dependent neutralisation. The `--sizing-mode` help text and the
`_expert_run_settings` docstring that call five ATR genes inert under notional pre-date `_ensure_safeguard_stop`
being applied in notional mode (only the budget is inert). The docstring is updated.

## Tasks (TDD, one commit each)

1. Failing tests + `neutralise_sizing_genes` + `decode_params`/`canonical_trial_params` wiring (tests b, d-decode).
2. Launcher: `--search-sizing-mode`, gene space, mutual exclusion, bypass/option refusal, `_effective_sizing_mode`
   returns None under the gene, `_expert_run_settings` drops the pin (tests a, c).
3. Capband driver passthrough, name token requirement `sizegene`, FactorRanker skip (driver tests).
4. Export/persistence test (d), versions, check_package_versions, regression runs.
