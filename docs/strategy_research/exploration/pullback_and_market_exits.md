# Pullback (long/short) and market-condition exits and TP/SL adjustments — design

Status: **implemented on feat/pullback-market-exits (2026-09-24).** Part of the
[strategy exploration grid](README.md). Implementation plan:
[2026-09-24-pullback-and-market-exits.md](../../plans/2026-09-24-pullback-and-market-exits.md).

Two additions:

- **A. A literal pullback expert:** long and short, with its own trend, structure and SMA5 exit.
  It needs no platform change.
- **B. Market-condition exits and TP/SL adjustments at the rule level:** a platform change that
  lets open-position rules use the `ohlcv-v1` / `ta-structure-v1` fields that entry rules already
  use, plus one stop-loss policy for every ruleset path. Every family in this grid can search
  them; the option grids still need their own profile wiring.

## Why: the 2026-09-24 probe

Probe: [pullback_feasibility_20260924.py](../../../test_files/pullback_feasibility_20260924.py).
Results: `reports/pullback_feasibility_20260924*.csv`.

- **Set-up:** 95 large caps from `tools/options_universe_top100.txt`, 10 slots of 10%,
  3 bps round trip, a 3×ATR resting stop, at most 10 days held, entry at the next open.
- **Periods:** parameters chosen on 2020–2022, reported on 2023–2026.

| 2023–2026 (not used to choose) | CAR | Max DD | Sharpe | Capital used | Corr SPY |
|---|---|---|---|---|---|
| Long: close > SMA200, RSI2 < 5, exit on close > SMA5 | +22.3% | −9.6% | 1.50 | 40% | 0.57 |
| Same, OR exit when swing structure turns bear | +20.7% | −7.5% | 1.56 | 37% | 0.57 |
| SPY buy and hold | +20.8% | −19.0% | 1.33 | 100% | 1.00 |

- **Long works.** In 2020–2022 it made +8.2% a year with a −8.9% drawdown, against SPY's +5.6% with −34%.
- **Short failed.** All 60 short variants lost money in 2023–2026. Gating shorts on SPY < SMA200
  also lost. They only paid in 2022.
- **The useful market-condition exit is protective.** A swing-structure flip (CHoCH) added to the
  SMA5 exit cut average drawdown from −12.9% to −10.1%. The channel-position exit held positions
  longer and roughly doubled drawdown.
- **The ohlcv-v1 trend slope was a worse long gate than a plain SMA200.**
- **Caveat: survivorship.** The probe used today's top 100 stocks, which flatters longs and
  punishes shorts. This grid fixes that: its large-cap screen is resolved point-in-time from the
  metric store.

**The existing `pullback` family is not this rule.** It is a DeterministicScorer blended score
(RSI weight 0.8, SMA200 distance 0.2) compared with `theta_buy`, as its own README row says. It
stays as it is, as a comparison.

## A. `PullbackReversion` expert and the `pullback_rsi` family

### Expert (`packages/experts/ba2_experts/PullbackReversion.py`)

Its shape follows ETFTrend: a pure, causal function over completed daily bars, plus a thin expert.
The expert never submits orders and never loops over prices checking whether to exit
(see the "no trade processing in expert code" rule).

| Setting | Values | Meaning |
|---|---|---|
| `direction` | `long` / `short` | One direction per instance, so BUY and SELL keep one meaning each. In a `short` job, a BUY is the exit signal and long entries are disabled. |
| `trend_gate` | `sma200` / `slope_ohlcv_v1` / `sma200_and_spy` | Entry trend filter. `sma200_and_spy` also requires SPY below/above its own SMA200 (short side). |
| `rsi_period` | 2–5 | Wilder RSI. |
| `entry_threshold` | 1–30 | Long: RSI below it. Short: RSI above 100 minus it. |
| `exit_mode` | `sma5` / `rsi` / `sma5_or_choch` / `time` | When to send the exit signal. |
| `rsi_exit` | 50–90 | Used only by `exit_mode=rsi`, mirrored for shorts. |

- **Entry:** a BUY (long) or SELL (short) recommendation with a set confidence, and an expected
  profit of 0: the rule has no price target.
- **Exit:** for a held symbol whose exit condition is met, the opposite recommendation, flagged
  `bearish`/`bullish`. The grid's close rule acts on that flag, exactly as the `mid_ds`
  `signal_reversal` job already does.
- **`sma5_or_choch`:** calls the platform's own `compute_chart_structure` over the same
  prior-session 128-bar window that the entry gates read. It does not use a separate
  implementation, so backtest and live share one function.
- **Stop:** a resting stop set by the entry rule's `adjust_stop_loss` action (−8% from the open
  price in `pullback_rsi`), as a rule parameter.
- **Maximum hold:** the ordinary `days_opened` close rule.

### Family `pullback_rsi`

- **Universe:** the grid's point-in-time large-cap screen (≥$10bn, up to 50 names), which answers
  the survivorship caveat.
- **Schedule:** daily entries, as in the existing `pullback` family.
- **Costs and fitness:** the new-idea defaults (5 bps plus 5 bps stress spread,
  `consistent_annual_return`).

| Job | Fixed | Searched |
|---|---|---|
| `long_sma5` | long, sma200 gate, sma5 exit | rsi 2/3, threshold 5/10/15, max hold 5/10 |
| `long_choch` | long, sma200 gate, sma5_or_choch exit | same |
| `long_rsi` | long, sma200 gate, rsi exit | same, plus `rsi_exit` 60/70 |
| `short_sma5` | short, sma200 gate | rsi 2/3, threshold 5/10/15, max hold 5/10 |
| `short_spy` | short, sma200_and_spy gate | same |

- **Size:** 5 jobs and 72 combinations, small enough to run as an exhaustive grid.
- **All 5 jobs (72 combinations) run.** Since short selling landed
  ([design](../../plans/2026-09-24-equity-short-selling-design.md)), a `sell` from a flat book
  opens a short when `enable_sell` is on (`enable_short` feeds it), so `short_sma5` and
  `short_spy` open real shorts: stop 8% above entry, covered by the BUY exit signal or the time
  limit, and charged the backtest's borrow cost (`short_borrow_rate_pa`, default 0.5%/yr).
- **Long and short in one account is a portfolio question.** It comes after both sides are
  measured separately, not before.

Tests (`packages/experts/tests/test_pullback_reversion.py`,
`testplatform/backend/tests/backtest/test_pullback_reversion_expert.py`,
`testplatform/backend/tests/test_research_pullback_rsi.py`):
- the causal function is pure (bar t+1 cannot change the signal at t);
- a short-job BUY only ever closes;
- `sma5_or_choch` equals the platform calculator on the same window;
- the live expert registry does not list PullbackReversion.

## B. Market-condition exits and TP/SL adjustments at the rule level (platform)

### What changed

Before this work, every exit rule with a market leaf was refused, because the live open-positions
pass had no market-condition context (a leaf read `no_context` there) while the backtest would
have evaluated it. Lifting the refusal alone would have broken backtest/live parity.

1. **Live scope.** `TradeManager.process_open_positions_recommendations` opens
   `market_condition_decision_scope(expert_instance_id=...)` per instance, as the entry pass does.
   Without a `market_condition_profile` it is a no-op.
2. **Action allow-list instead of the blanket refusal.** A rule that carries a market leaf may
   only exit or adjust TP/SL:
   - **tree form** (`assert_market_rule_actions`, applied by `rules_convert`, `backtests.py` and
     `strategies.py`): `close`, `close_option`, `adjust_stop_loss`, `adjust_take_profit`;
   - **live EventAction form** (`assert_market_rule_actions_live`, applied by the settings UI and
     `rules_export_import`): the same, plus `decrease_instrument_share`. The tree form refuses a
     reduce because the tree-to-live converter (`rule_builders.action_from_rule`) cannot carry it
     and would silently drop the rule at deploy.

   Everything else is refused with a message naming the rule, the offending actions and the
   allowed set: opening actions, `stop_processing`, rolls, option lifecycle and overlays.
   `assert_market_conditions_resolved` also runs on exit rules, so an unresolved template never
   leaves for live.
3. **Nesting.** A market leaf may sit only under AND groups, all the way up. A nested OR is
   flattened to AND on the live export, and a NOT would turn "unknown, does not fire" into
   "unknown, fires". Both are refused. Ordinary leaves may share the AND group.
4. **No fail-open rule.** `rule_builders.rule_triggers_from_tree` (used by the backtest seeder and
   the live export) refuses a rule whose leaves produce no trigger, since an empty trigger set is
   always true and a close rule would close every position. It also refuses a rule that lost a
   market leaf in conversion.
5. **Exit-pass guarantees.** A failure to open the market-condition scope never blocks the exit
   pass: the other exit rules still run. A leaf whose evaluation fails, or that is read outside an
   open scope, reads unknown (`no_context`, with its cause) and never raises. Unknown never fires.
6. **Max-loss stop.** At every equity market entry, live and backtest,
   `trade_cycle.record_max_loss_stop` records the stop the position was sized on as
   `Transaction.meta_data["max_loss_stop"]`: the RM safeguard stop when there is one, otherwise
   the ruleset stop. It is written once, as a single-column write, and never changes an order,
   price, size or fill. `position_sizing.max_loss_stop_of(transaction)` returns None when absent
   (older transactions, options), and None means "no bound known".
7. **One stop-loss policy on every ruleset path.** `TradeActions.ruleset_stop_policy` decides
   every ruleset stop, whatever condition fired it. Both the SL-only `AdjustStopLossAction` and
   the combined TP+SL branch of `TradeActionEvaluator` call it, so **the combined path now
   ratchets too** (it used to skip the ratchet). The TP half of a combined call is unaffected.
   - A tighter (or equal) request applies. A looser request keeps the existing stop.
   - The expert setting `allow_ruleset_sl_loosen` (bool, **default off**) lets a looser request
     apply, clamped at the recorded max-loss stop. Without a recorded bound nothing loosens.
     Two protections stop the SL min-distance floor from creating a loosen:
     `floor_would_loosen` (the rule tightened; only the floor loosened) and `floor_exceeds_rule`
     (the floor pushed past the rule's own price). Both keep the existing stop.
   - Manual UI edits and the SmartRM call the account directly and are outside this policy.

### Templates (`market_condition_templates.market_exit_rules`)

Up to four rules per job, each emitted only when its profile is selected:

| Rule | Profile | Leaves (AND) | Action | Continues |
|---|---|---|---|---|
| `<prefix>-mkt-exit-structure` | ta-structure-v1 | `structure_state` == against | `close` | no |
| `<prefix>-mkt-exit-slope` | ohlcv-v1 | trend slope against, threshold searched | `close` | no |
| `<prefix>-mkt-stop` | ta-structure-v1 | `structure_state` == against | `adjust_stop_loss` from open, −2..0% (0 = breakeven) | yes |
| `<prefix>-mkt-tp` | ohlcv-v1 | slope with the position AND ADX above, both searched | `adjust_take_profit` from open, +10..+30% | yes |

- Leaves are fixed and resolved: no mode gene, so no leaf can be "off" (an empty close rule would
  close every position). Only thresholds and percents are searched.
- Every rule is **off by default** behind a toggle gene. An all-off genome decodes to the job's
  original exit rules exactly.
- Direction is baked in (long: against = bear, slope below 0; short mirrors it), so the templates
  are valid for single-direction jobs only.
- Placement: after the job's exit rules, or immediately **before** the first terminal catch-all
  (a rule that matches every held position and stops processing), which would otherwise shadow
  them.
- The market **stop** is omitted before a catch-all that adjusts the stop-loss: a rule pass keeps
  only its last SL action, so the market stop would always be discarded.

### Exploration driver

- `--market-exit exit,stop,tp` attaches the templates. It requires a profile, `--search genetic`
  and `--market-condition-mode search`, and refuses a job that is not single-direction.
- `--allow-sl-loosen` sets `allow_ruleset_sl_loosen=True` on every job's experts, independent of
  `--market-exit`.
- Both enter the fingerprint, name and labels only when set, so the default manifests are
  byte-identical. Details: [market_conditions.md](market_conditions.md), driver items 6 and 7.
- **GA budget (A4):** in genetic mode each job is sized from its own gene count: population
  clamp(4 × genes, 24, 120), 25 generations (30 above 20 genes), early stop 8. `--population`,
  `--generations` and `--early-stop` override it. Grid mode is unchanged.

### Parity

`testplatform/backend/tests/backtest/test_market_exit_parity_engine.py` (real backtest engine) and
`tests/test_market_exit_live_parity.py` (live pass) check that a market exit fires on the same
session in both, and that all-off templates are byte-identical to no templates.

## Known gaps (operator)

- **UI reduce rules store `target_percent`, but the evaluator reads `value`.** A reduce authored in
  the settings UI does not carry its percent to `decrease_instrument_share`.
- **`TradeActionEvaluator` treats a trigger with an unknown `event_type` as passing.** The
  converters now refuse a rule that loses all its triggers or a market trigger, but the evaluator
  itself still fails open.
- **"The last SL action in a pass wins"** is platform behaviour (`TradeActionEvaluator.execute`).
  The driver works around it by omitting the market stop; the behaviour itself is unchanged.
- **The TP churn guard was not built.** `adjust_take_profit` has no minimum step, and backtests do
  not count TP changes per trade. Check for flapping before trusting a `tp` gene live.
- **A `tests/backtest` test leaks the in-memory store flag** into later tests in the same process.
