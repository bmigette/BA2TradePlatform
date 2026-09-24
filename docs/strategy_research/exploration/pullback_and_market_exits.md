# Pullback (long/short) and market-condition exits — design

Status: **proposed 2026-09-24, not implemented.** Part of the
[strategy exploration grid](README.md).

Two additions, in delivery order:

- **A. A literal pullback expert:** long and short, with its own trend, structure and SMA5 exit.
  It needs no platform change.
- **B. Market-condition exits at the rule level:** a platform change that lets exit rules use the
  `ohlcv-v1` / `ta-structure-v1` fields that entry rules already use. After that, every family in
  this grid, and later the option grids, can search them.

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
| `entry_threshold` | 3–20 | Long: RSI below it. Short: RSI above 100 minus it. |
| `exit_mode` | `sma5` / `rsi` / `sma5_or_choch` / `time` | When to send the exit signal. |
| `rsi_exit` | 50–80 | Used only by `exit_mode=rsi`, mirrored for shorts. |

- **Entry:** a BUY (long) or SELL (short) recommendation with a set confidence, and an expected
  profit of 0: the rule has no price target.
- **Exit:** for a held symbol whose exit condition is met, the opposite recommendation, flagged
  `bearish`/`bullish`. The grid's close rule acts on that flag, exactly as the `mid_ds`
  `signal_reversal` job already does.
- **`sma5_or_choch`:** calls the platform's own `compute_chart_structure` over the same
  prior-session 128-bar window that the entry gates read. It does not use a separate
  implementation, so backtest and live share one function.
- **Stop:** a resting ATR stop, set by the entry rule's `adjust_stop_loss` action, as a rule
  parameter.
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

- **Size:** 5 jobs and about 72 candidates, small enough to run as an exhaustive grid.
- **The short jobs test whether the probe's failure survives a point-in-time universe.** The
  probe's null result is the expectation.
- **Long and short in one account is a portfolio question.** It comes after both sides are
  measured separately, not before.

Tests:
- the causal function is pure (bar t+1 cannot change the signal at t);
- a short-job BUY only ever closes;
- `sma5_or_choch` equals the platform calculator on the same window;
- the controls reproduce the probe's trade count on a fixed symbol set, within documented
  execution differences (5-minute fills against the probe's next-open fills).

## B. Market-condition exits at the rule level (platform)

### Why exits are refused today

Exits are refused at three points:
- `assert_no_market_conditions` (`ba2_common/core/market_condition_rules.py`), called by
  `rules_convert.py` on every deploy/export;
- `testplatform/backend/app/api/backtests.py`;
- `testplatform/backend/app/api/strategies.py`.

The stated reason is correct but narrow. **Live**, the market-condition context exists only
inside `market_condition_decision_scope`. `TradeManager.process_expert_recommendations_after_analysis`
opens that scope, but `process_open_positions_recommendations` does not, so an exit leaf would
read `no_context` and never fire. **The backtest resolver** has no such boundary: it would evaluate
the leaf. Lifting the refusal alone would therefore make backtests trade exits that live never
fires, which the backtest/live parity rule forbids.

The design doc's other argument is that a gate could delay an exit. That argument applies only to
**adding** a market leaf to an existing exit rule: under AND, an unknown observation silences
that exit. A rule that exists *only* to exit on a market condition can only add exits.

### Change

1. **Live scope.** Wrap `process_open_positions_recommendations`' per-instance evaluation in
   `market_condition_decision_scope(expert_instance_id=...)`, the same call the entry pass uses,
   with one decision clock per pass.
2. **Narrow the refusal.** Replace the blanket exit refusal with a *market-exit rule* contract:
   - A rule may contain market leaves only if every action is `close` (or `reduce`).
   - It may never adjust a take-profit or stop-loss, or carry a lifecycle/roll/overlay action.
   - `assert_no_market_conditions` stays as it is for protective and lifecycle rules, and gains
     one sibling `assert_market_exit_rule_shape`.
   - All three entry points apply the same pair.
3. **Unknown means "does not fire".** It is never a pass, and never a failure of the other exits.
   Every other exit rule still runs, because the market exit is its own rule. Unknown-by-reason
   counts are reported, as for entry gates.
4. **Placement and precedence.** A generated market-exit rule is inserted **after** the stop-loss
   and floor rules, because first match wins, so it can never pre-empt a protective rule. It is
   not a nested OR group, since a nested OR flattens to AND.
5. **Genes.** One rule per job:
   - a toggle, off by default;
   - a field mode: `structure_against` (swing state flips against the position),
     `slope_against` (EMA50 slope past a threshold against it), or `channel_far_side`
     (`channel_pos` beyond a threshold, which is profit-taking);
   - a threshold where the field needs one.

   That is 2–3 genes per family. A frozen all-off control must be byte-identical to today's rules
   (the no-impact gate the entry profile already passes).
6. **Parity.** One test replays the same bars through the live pass and the backtest pass and
   asserts the same exit fires on the same session. It extends the existing
   `test_research10_market_conditions.py` real-engine parity test to cover exits.

### Where it applies first

- **Exploration grid:** a `--market-exit` flag beside `--market-condition-profile`, searched per
  family with matched seeds against the ungated control, as the market-condition page's comparison
  rules require.
- **Then the option follow-ups** (`run_options2_matrix.py` O_LEAP/O_PMCC first). They hold for
  months and their exits look only at option P&L, time and days to expiry. That grid's driver
  still needs its own profile wiring first.

## Open questions

1. Can a short-side job rely on the existing `bullish` flag in the close rule, or does the rule
   vocabulary need an explicit mirror? This must be checked in `TradeConditions` before Part A.
2. Should the market exit also be allowed to `reduce` (partial exit)? The proposal allows it;
   dropping it keeps the contract to a single action.
3. Should Part B ship behind an AppSetting kill-switch for live (default on only after the
   parity test passes on replay)?
