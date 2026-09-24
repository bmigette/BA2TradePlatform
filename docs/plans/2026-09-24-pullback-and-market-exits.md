# Pullback expert + market-condition exits/TP/SL — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Add (A) a research-only `PullbackReversion` expert with a `pullback_rsi` exploration family, and (B) rule-level market-condition exits and TP/SL adjustments for open positions, one consistent stop-loss ratchet on every ruleset path, and an opt-in expert setting (default off) that lets rules loosen a stop down to the trade's max-loss stop.

**Design:** [docs/strategy_research/exploration/pullback_and_market_exits.md](../strategy_research/exploration/pullback_and_market_exits.md). Read it first.

**Architecture:**
- A follows ETFTrend: a pure causal function plus a thin expert, registered only in `ba2_experts` and the backtest registry.
- B does four things: opens the market-condition decision scope in the live open-positions pass (a no-op without a profile); replaces the blanket exit refusal with an action allow-list; persists each trade's maximum-loss stop at entry; and sends every ruleset stop change, including the combined TP+SL path that skips the ratchet today, through one policy function. That function's opt-in expert setting `allow_ruleset_sl_loosen` (default off) allows loosening down to the max-loss stop.

**Tech stack:** Python, SQLModel, pytest. Shared code lives in `packages/` (ba2_common, ba2_experts), test platform code in `testplatform/backend`, and the research driver in `tools/strategy_research/exploration`.

---

## Hard constraints (every task)

1. **No change to current experts or rulesets.** Every existing ruleset, expert and stored backtest
   must behave byte-identically:
   - with `allow_ruleset_sl_loosen` at its default (off), stop-only adjustments behave exactly as
     today. The ONE intended live change is that the combined TP+SL path now applies the same
     ratchet; no prod transaction has ever hit that gap (log audit 2026-09-24);
   - the default exploration manifest keeps its frozen fingerprint
     (`test_research10_market_conditions.py:36`);
   - no current expert's settings change.
2. **Do not touch the live platform.**
   - No restart, API call or DB write against 8081 (prod) or 8080 (dev).
   - Work only in the worktree `C:\Users\basti\Documents\dev\BA2-pullback`
     (branch `feat/pullback-market-exits`).
   - Never edit `C:\Users\basti\Documents\dev\BA2TradePlatform`: prod runs from that clone.
3. **The live expert registry is not changed** (`ba2_trade_platform/modules/experts/__init__.py`).
   PullbackReversion is research-only, like ETFTrend.
4. **Tests run from the worktree:**
   - `packages/common/tests`, root `tests/` and `testplatform/backend/tests` are always separate
     invocations, never concurrent.
   - Backend: `cd testplatform/backend && ../../.venv/Scripts/python.exe -m pytest ...`. The venv
     lives in the main clone: use `C:\Users\basti\Documents\dev\BA2TradePlatform\.venv\Scripts\python.exe`.
   - Run `tests/backtest/...` separately from other backend tests.
   - Diff failure LISTS against the base commit `55c71536` before claiming a regression.
5. **Commit after every task** in the worktree with the session attribution lines. **No push.**
   Do not bump version files; the controller does that at merge.

---

## Part A — PullbackReversion

### Task A1: Pure causal signal function + unit tests

**Files:**
- Create: `packages/experts/ba2_experts/PullbackReversion.py` (the pure function only in this task).
- Test: `packages/experts/tests/test_pullback_reversion.py`.

**Function:** `pullback_signal(bars: pd.DataFrame, settings: Mapping, spy_bars: Optional[pd.DataFrame] = None) -> dict`.
- `bars` holds COMPLETED daily bars up to the decision session: columns
  Open/High/Low/Close/Volume, index ascending by date.
- Returns `{"action": "entry"|"exit"|"none", "rsi": float, "sma200": float, "sma5": float,
  "trend_ok": bool, "structure_state": Optional[str], "reason": str}`.

**Settings, validated with a ValueError on bad values:**
- `direction` ∈ {long, short}.
- `trend_gate` ∈ {sma200, slope_ohlcv_v1, sma200_and_spy}.
- `rsi_period` int 2..5, `entry_threshold` float 1..30.
- `exit_mode` ∈ {sma5, rsi, sma5_or_choch, time}; `rsi_exit` float 50..90.

**Semantics** (they mirror the probe, `test_files/pullback_feasibility_20260924.py`):
- **RSI:** Wilder, `ewm(alpha=1/n, adjust=False)`, as in the probe's `rsi()`.
- **Trend:** long needs close > SMA200; short needs close < SMA200.
  - `slope_ohlcv_v1` uses the sign of `compute_market_conditions(...)` `trend_slope` over the last
    128 bars. An unknown status means trend not ok.
  - `sma200_and_spy` additionally needs SPY close < SPY SMA200 (short) or > (long). A missing or
    short `spy_bars` raises ValueError. Never treat it as passing.
- **Entry:** long if trend ok and RSI < threshold; short if trend ok and RSI > 100 − threshold.
- **Exit signal** (evaluated only when not entry):
  - `sma5`: close > SMA5 (long) or close < SMA5 (short);
  - `rsi`: RSI > `rsi_exit` (long) or RSI < 100 − `rsi_exit` (short);
  - `sma5_or_choch`: sma5 OR `compute_chart_structure(last 128 bars).structure_state` equals the
    code for bear (long) or bull (short). Use `STRUCTURE_STATE_CODES`; unknown means no CHoCH;
  - `time`: never signals; the max-hold rule owns the exit.
- **Entry beats exit on the same bar.**
- **Insufficient history** (under 200 bars, or under 128 for the market-condition modes) raises
  ValueError. It never quietly returns "none".

**Tests** (write them first, then implement):
- Causality: appending a future bar never changes the result computed on the prefix.
- Hand-built series: an oversold dip in an uptrend gives long entry; the same dip in a downtrend
  gives none.
- Short mirror: an overbought rally in a downtrend gives short entry.
- Exit: after a long entry, close > SMA5 gives exit; `time` never gives exit.
- `sma5_or_choch` with a series whose platform `compute_chart_structure` is bear gives exit even
  when close < SMA5. Assert against the calculator itself; don't hard-code the state.
- The `sma200_and_spy` gate blocks a short when SPY is above its SMA200.
- Bad settings and short history raise ValueError.

Run: `...python.exe -m pytest packages/experts/tests/test_pullback_reversion.py -q` from the worktree root.
Commit: `feat(experts): PullbackReversion pure causal signal`.

### Task A2: Expert class and backtest registration

**Files:**
- Modify: `packages/experts/ba2_experts/PullbackReversion.py`. Add `class PullbackReversion(MarketExpertInterface)`,
  copying ETFTrend's shape: `description`, `get_settings_definitions`, `__init__`, `_analyze`,
  `analyze_as_of`, `run_analysis`, `render_market_analysis`.
- Modify: `packages/experts/ba2_experts/__init__.py` (import it and append it to `experts`).
- Modify: `testplatform/backend/app/services/backtest/daily_backtest_handler.py`: add
  `_SUPPORTED_EXPERTS["PullbackReversion"]` and `_EXPERT_WARMUP_BARS["PullbackReversion"] = 274`,
  next to ETFTrend's entries (~262/283).
- Test: `testplatform/backend/tests/backtest/test_pullback_reversion_expert.py`, modelled on
  `test_etf_trend.py`: `object.__new__` plus `BacktestContext`.

**Recommendation mapping** (expected_profit_percent = 0.0 always):

| Pure action | direction=long | direction=short |
|---|---|---|
| entry | BUY, confidence = 50 + 50·clamp((th − rsi)/th, 0, 1) | SELL, the same on (rsi − (100 − th))/th |
| exit | SELL, confidence 100 | BUY, confidence 100 |
| none | HOLD | HOLD |

- **OHLCV:** exactly as ETFTrend's `_analyze`: `providers.ohlcv().get_ohlcv_data(symbol, end_date=as_of, lookback_days=..., interval="1d")`.
  Use a lookback of about 420 calendar days so SMA200 and 128 bars are covered.
- **SPY:** fetched the same way only when `trend_gate == sma200_and_spy`.
- **Missing or short data:** raise `FMPHistoryCacheMiss`, as ETFTrend does.
- **Settings definitions:** the six settings from A1, with the defaults `long`, `sma200`, 2, 5,
  `sma5`, 70.

**Tests:**
- Long entry gives BUY; exit gives SELL; short entry gives SELL; short exit gives BUY; HOLD otherwise.
- A bad basket aborts with `FMPHistoryCacheMiss`.
- `get_expert_class("PullbackReversion")` resolves from `ba2_experts`.
- The live registry `ba2_trade_platform/modules/experts/__init__.py` does NOT list it. Pin this
  with a test so live never gets it silently.
- `packages/experts/tests/test_experts_import.py` still passes; update any expert-count assertion
  there if one exists.

Commit: `feat(experts): PullbackReversion expert, backtest-registered only`.

### Task A3: `pullback_rsi` exploration family (opt-in, default campaign untouched)

**Files:**
- Modify: `tools/strategy_research/exploration/profiles.py`.
- Modify: `tools/strategy_research/exploration/run_exploration.py`, only if `--families` choices are
  validated against `FAMILIES`.
- Test: `testplatform/backend/tests/test_research_pullback_rsi.py` (new).

**Rules:**
- Add `EXTENSION_FAMILIES = ("pullback_rsi",)` and `ALL_FAMILIES = FAMILIES + EXTENSION_FAMILIES`.
- `build_manifest`'s default stays `families=FAMILIES`. Validation accepts `ALL_FAMILIES`. The CLI's
  `--families` choices accept `ALL_FAMILIES`.
- **The default manifest fingerprint must not change.** `test_research10_market_conditions.py:36`
  and `test_research6_driver.py:37-40,151` must pass unmodified.

**Baseline:** `new_idea_baseline("pullback_rsi", large_ds reference)`.
- Keep `screener_opt`, which is the point-in-time large-cap screen, and set `cadence_days=1`.
- Daily `run_schedule_override`, as the `pullback` family has.
- Replace the expert with `{"class": "PullbackReversion", "settings": {...}}`.

**Rules per direction:**
- **Long entry:** `bullish is_true` AND `has_no_position` AND the existing `days_since_last_close`
  gate. Actions: `buy` and `adjust_stop_loss` at −8% from `order_open_price`, with **no** TP action.
- **Short entry:** the same with `bearish` and a `sell` action, and the stop at +8%. The implementer
  must find and set whatever expert/backtest setting enables short entries in the BT engine; ETFTrend
  notes shorts are disabled by default. Grep `allow_short`, `enable_sell`, `short` in
  `daily_engine.py` and the RM. Pin it in the job, and add a test that the short job actually opens
  a short.
- **Exit rules**, in first-match order:
  - the reverse-signal close: `bearish` for a long job, `bullish` for a short job; the `mid_ds`
    `signal_reversal` dict shape with `continue_processing: False`;
  - `time_exit()` on `days_opened`.

**Jobs** (a hard `ValueError` in `variants()` for anything else):

| Variant | Settings | `expert_params` | Other genes |
|---|---|---|---|
| `long_sma5` | long, sma200, sma5 | `rsi_period` 2..3 step 1, `entry_threshold` 5..15 step 5 | `condition_range(exit_rules, "days_opened", 5, 10, 5)` |
| `long_choch` | long, sma200, sma5_or_choch | same | same |
| `long_rsi` | long, sma200, rsi | same, plus `rsi_exit` 60..70 step 10 | same |
| `short_sma5` | short, sma200, sma5 | same as `long_sma5` | same |
| `short_spy` | short, sma200_and_spy, sma5 | same as `long_sma5` | same |

**Tests:**
- `build_manifest(families=("pullback_rsi",))` gives 5 jobs and 72 grid combinations.
- The default `build_manifest()` is unchanged: its job count and fingerprint equal the pinned values.
- Long and short rule shapes are as specified.
- `--families pullback_rsi --dry-run` works via the CLI with `--output-dir` in a tmp dir.
- `attach()` with `--market-condition-profile ta-structure-v1` gates the new family's entry rules too.

**Docs:** add a `pullback_rsi` row to `docs/strategy_research/exploration/README.md`'s families
table, marked "opt-in extension, not in the default 35 jobs", plus its CLI line.

Commit: `feat(research): pullback_rsi exploration family (opt-in)`.

---

## Part B — Market-condition exits and TP/SL adjustments

### Task B1: Live open-positions pass opens the market-condition decision scope

**Files:**
- Modify: `ba2_trade_platform/core/TradeManager.py` (`process_open_positions_recommendations`, ~3149).
- Test: `tests/test_open_positions_market_condition_scope.py` (root suite).

**Change:** wrap the per-instance evaluation in
`with market_condition_decision_scope(expert_instance_id=expert_instance_id):`, mirroring
`process_expert_recommendations_after_analysis` (~2428-2432). If that method opens
`_decision_capture_scope`, keep capture outermost; do not add capture here if the open-positions pass
never had it. `market_condition_decision_scope` yields None immediately when the instance has no
`market_condition_profile` (`market_condition_live.py:762`).

**Tests:**
- Monkeypatch `market_condition_decision_scope` with a recorder. Calling the method opens the scope
  once with the right `expert_instance_id`.
- Without a profile, the per-instance resolver returns None and evaluation proceeds unchanged. Use a
  stub `get_instance_resolver` with empty settings, as in existing
  `packages/common/tests/test_market_condition_live*.py` fixtures; reuse their helpers.

Commit: `feat(live): open the market-condition decision scope in the open-positions pass`.

### Task B2: Replace the blanket exit refusal with an action allow-list

**Files:**
- Modify: `packages/common/ba2_common/core/market_condition_rules.py`.
- Modify the call sites: `packages/common/ba2_common/core/rules_convert.py:502`,
  `testplatform/backend/app/api/backtests.py:~1424`, `testplatform/backend/app/api/strategies.py:~173`.
- Modify the live-format doors that use `assert_no_market_fields` on EXIT rulesets:
  `ba2_trade_platform/ui/pages/settings.py` (36, 4519, 6031, 6644) and
  `packages/common/ba2_common/core/rules_export_import.py` (345, 356). Read each one first. Only the
  exit/open-positions doors change; entry doors keep their current checks.
- Test: update `testplatform/backend/tests/test_deploy_payload_market_conditions.py`, and add
  `packages/common/tests/test_market_rule_actions.py`.

**New API:**
```python
MARKET_RULE_ACTIONS = frozenset({"close", "decrease_instrument_share",
                                 "adjust_stop_loss", "adjust_take_profit"})

def assert_market_rule_actions(rules, where: str) -> None:
    """Every rule that carries a market-condition leaf may only CLOSE, REDUCE, or ADJUST TP/SL.
    Refuses (ValueError) a market leaf in a rule with any other action (open, roll, lifecycle,
    overlay, stop_processing...), and a market leaf nested under OR/NOT (only a top-level AND tree
    of leaves is allowed: a nested OR silently flattens to AND, and NOT would turn 'unknown ->
    does not fire' into 'unknown -> fires')."""
```
- Evaluate it per rule: find the leaves with `iter_market_condition_leaves(rule["conditions"])`.
- Keep `assert_no_market_conditions` defined and unchanged, because other code may import it.
  Switch the exit call sites to `assert_market_rule_actions`.
- The error message names the rule id, the offending actions and the allowed set.
- For the live-format (EventAction) doors, write a sibling `assert_market_rule_actions_live(...)`
  over that format's triggers and actions, using the same allow-list. Read the current format in
  `rules_export_import.py` before writing it.

**Tests:**
- An exit rule with a market leaf and a `close` action is accepted by all three tree-form doors and
  both live-format doors.
- A market leaf with `buy`, with `stop_processing`, or with an option roll action is refused with
  the message.
- A market leaf under OR is refused; under a top-level AND it is accepted.
- An ordinary exit ruleset with no market leaves is untouched: rewrite
  `test_an_ordinary_exit_ruleset_is_untouched` accordingly.
- Update `test_a_market_leaf_in_an_exit_ruleset_is_refused` and
  `test_the_api_save_path_refuses_a_market_leaf_on_an_exit_rule` to the new contract: refused only
  with a disallowed action.
- `assert_market_conditions_resolved` still applies to exit rules too. Call it on exit rules as well,
  since an unresolved `mode_optimize` template must never leave for live. Test that.

Commit: `feat(rules): market-condition leaves allowed in exit rules that only close/reduce/adjust TP-SL`.

### Task B3: Persist each trade's maximum-loss stop at entry

**Files:**
- Modify: `packages/common/ba2_common/core/position_sizing.py` (`reconcile_protective_stop` ~295,
  or a new helper beside it).
- Modify the two entry sites that call it: `ba2_trade_platform/core/TradeManager.py:~1582-1589`
  (`_entry_submit_stop`) and `testplatform/backend/app/services/backtest/daily_engine.py:~1726, ~1782`.
- Test: `packages/common/tests/test_max_loss_stop_persisted.py`, plus a daily_engine test under
  `testplatform/backend/tests/backtest/`.

**Change:** when the entry stop is decided, record
`transaction.meta_data["max_loss_stop"] = <the stop price the position was SIZED on>`:
- use the safeguard stop when one exists;
- otherwise use the reconciled ruleset stop.

Add a helper `max_loss_stop_of(transaction) -> Optional[float]` in `position_sizing.py`, returning
None when absent (older transactions). This is additive metadata: no order, price or sizing changes.
Check that `meta_data` is persisted by `update_instance` in both live and BT (it's a JSON column;
assign a new dict, don't mutate in place, or SQLModel won't see the change).

**Tests:**
- After a backtest entry, the transaction's `meta_data["max_loss_stop"]` equals the submitted stop.
- Sizing, stop price and fills are identical to before on a fixed small backtest. Compare against a
  run with the metadata write disabled: same trades and same P&L.
- `max_loss_stop_of` returns None on a transaction without it.

Commit: `feat(risk): record the stop each position was sized on as its max-loss stop`.

### Task B4: One stop-loss policy for every ruleset path + opt-in loosening to the max-loss stop

**Operator decision (2026-09-24):**
- SL processing must be the SAME whatever kind of condition triggered it. No per-rule or per-action
  flag.
- The ratchet must be consistent: today the combined TP+SL path skips it.
- A new expert setting, **off by default**, lets rules loosen a stop down to the trade's max-loss
  stop. It is off for every current expert; the follow-up grid turns it on.

**Files:**
- Modify: `packages/common/ba2_common/core/TradeActions.py`: extract the policy out of
  `AdjustStopLossAction._call_broker` (~1416) into one function.
- Modify: `packages/common/ba2_common/core/TradeActionEvaluator.py` (~700, the "Phase 2 (merged)"
  branch that calls `account.adjust_tp_sl(transaction, tp_price, sl_price, source="ruleset")` and
  bypasses the ratchet): run `sl_price` through the same function before the call.
- Modify: `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py`: add the setting
  beside `regime_stop_scale` (~387).
- Test: `packages/common/tests/test_ruleset_stop_policy.py`.

**The one policy function** (in TradeActions.py, beside the action):
```python
def ruleset_stop_policy(transaction, requested: float, is_long: bool, expert) -> tuple[float, str]:
    """The stop a RULESET may set. Returns (price_to_apply, reason).

    - no existing stop, or a TIGHTER request -> requested
    - LOOSER request, expert setting allow_ruleset_sl_loosen is False (default) -> existing
      (the ratchet, exactly today's behaviour)
    - LOOSER request, setting True -> clamp at max_loss_stop_of(transaction): long
      max(requested, bound), short min(requested, bound); if the bound is ABSENT -> existing
      (never loosen without a recorded bound), logged
    Every non-trivial outcome is logged at INFO naming existing / requested / bound / applied."""
```
- `AdjustStopLossAction._call_broker` and the combined branch both call it. When it returns the
  existing stop in the combined branch, pass `new_sl_price` so the account treats it as unchanged,
  or pass None if the account API treats None as "don't adjust SL". Read `adjust_tp_sl` in
  `backtest_account.py:5073` and `AlpacaAccount._adjust_tpsl_internal` to choose. **The TP half of a
  combined call is unaffected.**
- Resolve `expert` the way `_regime_expert()` (~866) does, and read the setting with
  `get_setting_with_interface_default("allow_ruleset_sl_loosen", log_warning=False)` through
  `coerce_bool`. A string "1" must read True (see memory "deploy parity traps").
- **Scope stays the ruleset path only.** Manual UI edits and the SmartRM call `account.adjust_sl`
  directly and keep their current freedom.
- **Setting definition:** `"allow_ruleset_sl_loosen": {"type": "bool", "required": False,
  "default": False, "description": "Let ruleset rules move a stop-loss further away (up to the
  trade's max-loss stop). Off: stops only tighten."}`.
- Deploy parity: check whether `packages/common/ba2_common/core/deploy_parity.py` needs to know the
  setting, since a bool gene is stored as "1". It needs no pinning. It must round-trip through
  export/import (`expert_batch_export_import`, `rules/settings` export), which it will as an ordinary
  bool setting. Add one round-trip assertion.

**Tests:**
- **Consistency (the gap):** through the combined TP+SL branch, a looser SL on a transaction with a
  tighter existing stop is REFUSED with the setting off. It is the same outcome as the SL-only
  path, and the TP still applies. Use the backtest account, and also a fake account that records
  `adjust_tp_sl` arguments.
- The SL-only ratchet is unchanged with the setting off; existing ratchet tests stay green.
- Setting on, loosening within the bound: applied. Past the bound: clamped. No bound recorded:
  refused and logged.
- Setting on, short mirror.
- A string "1" reads True, and a missing setting reads False.
- **No-impact:** a stored backtest's trades are unchanged by this task, re-run with the default
  setting. Use the fixed small real-engine fixture from B3.

Commit: `fix(actions): one stop-loss policy for every ruleset path; opt-in loosening to the max-loss stop`.

### Task B5: Market exit / stop / TP rule templates (shared builder)

**Files:**
- Modify: `packages/common/ba2_common/core/market_condition_templates.py` (beside `market_condition_leaves`).
- Test: `packages/common/tests/test_market_exit_templates.py`.

**API:** `market_exit_rules(prefix: str, profiles, direction: str) -> list[dict]` returns up to three
rule dicts, one per kind, each OFF by default through a **rule-level toggle gene**. Use the same
toggle keys the option exit rules use in `testplatform/ba2test_launcher.py:_option_exit_rules`
(~4111-4254); read it for the exact key names (`toggle_optimize`, `enabled` or similar):

| id suffix | Leaves (top-level AND) | Action | continue_processing |
|---|---|---|---|
| `mkt-exit` | ONE leaf: structure_state == against (categorical, choices = the "against" code only) OR trend-slope against (numeric, `mode_choices` WITHOUT "off") — pick via a rule-level `kind` gene if the rule format supports it, else emit two separate toggled rules `mkt-exit-structure` and `mkt-exit-slope` | `close` | False |
| `mkt-stop` | structure_state against | `adjust_stop_loss`, reference `order_open_price`, percent searched −2..0 step 1 (0 = breakeven) | True |
| `mkt-tp` | trend slope with the position (numeric, above, threshold searched) AND ADX above (threshold searched) | `adjust_take_profit`, reference `order_open_price`, percent searched +10..+30 step 10 (short: negative) | True |

- **A leaf with mode "off" must never be possible inside these rules.** An exit rule whose only leaf
  is off has an empty condition tree, is always true, and would CLOSE EVERY POSITION. So:
  - `mode_choices` excludes "off" here;
  - `market_exit_rules` asserts no emitted leaf can resolve to off;
  - a test pins it.
- `direction` "long" means against = bear (code from `STRUCTURE_STATE_CODES`) and slope below 0;
  "short" mirrors it.

**Tests:**
- Every rule passes `assert_market_rule_actions` (B2).
- No leaf can be off.
- Each rule is toggled off by default.
- Adjustment rules have `continue_processing=True`.
- A decoded genome with all toggles off yields exactly the input exit rules (the no-impact control).

Commit: `feat(rules): shared market exit/stop/TP rule templates`.

### Task B6: `--market-exit` in the exploration driver

**Files:**
- Modify: `tools/strategy_research/exploration/market_conditions.py` (a new
  `attach_exits(job, bt, profiles, kinds)`), `profiles.py` (the `build_manifest` parameter
  `market_exit=()`) and `run_exploration.py` (the CLI flag
  `--market-exit exit,stop,tp`, which requires `--market-condition-profile` and `--search genetic`).
- Placement: append the templates **after** the existing exit rules (after stop and floor rules),
  because first match wins. Adjustment rules continue processing.
- Include `market_exit` in the job fingerprint, as `market_condition_profile` is.
- Add `--allow-sl-loosen` (default off). When on, set the expert setting `allow_ruleset_sl_loosen=True` on every job's experts. It is a separate flag from `--market-exit`, because the SL policy does not depend on the condition type. Include it in the fingerprint.
- Test: `testplatform/backend/tests/test_research_market_exits.py`.

**Tests:**
- Without the flag, the default manifest fingerprint is unchanged (pinned hash).
- With the flag, each job's exit rules end with the templates.
- The flag without a profile is refused.
- The fingerprint differs between with and without.
- Every generated exit rule passes `assert_market_rule_actions`.

Commit: `feat(research): --market-exit adds market exit/stop/TP rules to exploration jobs`.

### Task B7: BT/live parity of a market exit + real-engine test

**Files:**
- Test: extend `testplatform/backend/tests/test_research10_market_conditions.py` (its real-engine
  all-off parity test, `test_real_equity_engine_all_off_parity_and_active_entry_veto`, is the model)
  or add `test_research_market_exit_engine.py`.

**Tests:**
1. **Real engine, market exit on:** with a pinned tiny manifest fixture (reuse that test's fixture),
   a position whose structure flips against it is closed by `mkt-exit` on the session the reader
   reports. With the toggle off, the same position is not closed on that session.
2. **All-off exit templates:** the result is byte-identical (trades and equity) to no templates.
3. **Live pass:** a fake resolver installed via `set_market_condition_context_resolver` returning the
   same observation, plus `TradeManager.process_open_positions_recommendations` → the rule fires.
   Monkeypatch the account and DB pieces as existing TradeManager unit tests do; find the closest
   existing test of `process_open_positions_recommendations` in `tests/` and reuse its fixtures. If
   a full live-path test is infeasible, test at the `TradeActionEvaluator` level inside an opened
   `market_condition_decision_scope`, and say so in the commit message.

Commit: `test(parity): market exit fires on the same session in backtest and live`.

### Task B8: Docs + final verification

- Update `docs/strategy_research/exploration/pullback_and_market_exits.md`: Status becomes
  "implemented on feat/pullback-market-exits". Record the final allow-list, the max-loss stop
  mechanism and the templates.
- Update `docs/strategy_research/exploration/market_conditions.md`'s driver section with
  `--market-exit`.
- Update the design doc `docs/plans/2026-09-15-option-market-condition-genes-design.md` §6.0 with one
  sentence saying exits may now carry market leaves under the allow-list (link the new doc). Do not
  rewrite that section.
- **Full verification**, each as a separate invocation:
  - `packages/common/tests`, `packages/experts/tests` and `packages/providers/tests`;
  - root `tests` (split the portfolio_allocation glob);
  - `testplatform/backend/tests` excluding `tests/backtest`, then `tests/backtest`.
  - Compare failure lists against a baseline run at `55c71536` (the controller provides the
    baseline).

---

## Execution order

A1 → A2 → A3 → B1 → B2 → B3 → B4 → B5 → B6 → B7 → B8.

A and B are independent up to B6. B6 needs A3 only for its exploration tests. Run tasks strictly
in sequence: one implementer at a time.

### Task A4 (added 2026-09-24, operator): GA budget for the exploration grid

**Decision:** "20/30 generations with early stop is a better approach; fitness keeps climbing
after gen 20 on the option grid."

**Today:** `run_exploration.py` defaults to `--population 24 --generations 4`, and
`profiles.build_manifest` sets `earlyStoppingGenerations = generations`, so early stopping never
fires. The option grid uses `--early-stop 8` (`tools/stage1_run.sh`).

**Change:**
- **New flag:** `--early-stop N` (patience in generations).
- **Genetic mode only:** when `--search genetic` and the user passed neither `--generations` nor
  `--early-stop`, default to generations=25 and early_stop=8. Explicit values always win. Record the
  resolved values in `optimization_config.earlyStoppingGenerations` and in the job fingerprint.
- **Grid mode:** keeps today's config values, so the default (grid) campaign's manifest fingerprint
  stays byte-identical (`test_research10_market_conditions.py:36`).
- **Validation:** `early_stop >= 1` and `early_stop <= generations`.
- **Launch line:** print the resolved budget (population x generations, early stop).
- **Docs:** update README's genetic example and market_conditions.md's search-budget note.

**Tests:**
- genetic defaults resolve to 25/8;
- explicit flags win;
- grid-mode fingerprint unchanged;
- `early_stop > generations` is refused;
- the value reaches `optimization_config`.
