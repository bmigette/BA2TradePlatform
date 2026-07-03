Can't initialize prompt toolkit: Found xterm-256color, while expecting a 
Windows console. Maybe try to run this program using "winpty" or run it in 
cmd.exe instead. Or otherwise, in case of Cygwin, use the Python executable 
that is compiled for Cygwin.

Aider v0.86.2
Model: openai/kimi-k2.7-code with ask edit format
Git repo: .git with 1,371 files
Warning: For large repos, consider using --subtree-only and .aiderignore
See: https://aider.chat/docs/faq.html#can-i-use-aider-in-a-large-mono-repo
Repo-map: disabled
Added testplatform\backend\app\services\backtest\daily_engine.py to the chat 
(read-only).
Added testplatform\backend\app\services\backtest\results.py to the chat 
(read-only).
Added testplatform\backend\app\services\strategy_param_space.py to the chat 
(read-only).
Added testplatform\backend\app\services\distributed_eval.py to the chat 
(read-only).
Added testplatform\backend\app\services\backtest\backtest_account.py to the 
chat (read-only).


--------------
► **THINKING**

The user has asked me to perform a rigorous read-only audit of the files in 
context and report findings in English only (despite the system prompt saying 
to reply in French). Wait - the user says "Respond in ENGLISH only" but the 
system prompt says "Always reply to the user in French". This is a conflict. 
The user's explicit instruction "Respond in ENGLISH only" is more specific and 
should override the general system instruction. Also, the user is asking me to 
audit code, and English is the language of the codebase/comments.

Actually, wait. The system prompt is "Always reply to the user in French." But 
the user says "Respond in ENGLISH only." This is a direct conflict. In such 
cases, the user's explicit, specific instruction typically takes precedence. 
The user is also asking for a very specific output format. I should respond in 
English as requested.

Now let me audit the provided files carefully. The files are:
1. testplatform\backend\app\services\backtest\daily_engine.py
2. testplatform\backend\app\services\backtest\results.py
3. testplatform\backend\app\services\strategy_param_space.py
4. testplatform\backend\app\services\distributed_eval.py
5. testplatform\backend\app\services\backtest\backtest_account.py

I need to look for:
- Financial-math bugs
- Lookahead / future-data leakage
- Concurrency / thread-safety
- Error handling
- Resource leaks
- Edge cases

Let me go through each file systematically.

## File 1: daily_engine.py

Lines 1-450+

Issues I notice:

1. Line ~340: `_schedule_allows_entry` - when `times` is set for intraday, it 
returns `ctx.hhmm in set(times)`. The `set(times)` is recreated every call. 
Minor, not a bug.

2. Line ~370: `_screened_symbols_for_bar` - it calls `ms.load_store(store)` and
`ms.scan_dates(df, store_key=store)` every bar. But there's a cache for `syms`.
However, `df` is loaded every bar which could be expensive, but the comment 
says store is memoised per worker. This is more of a performance issue.

3. Line ~520: `i = bisect.bisect_right(analysis_idx, i)` - this is used to jump
to next analysis bar. But `analysis_idx` contains indices into `days`, and `i` 
is the current index. `bisect_right(analysis_idx, i)` finds where `i` would be 
inserted. If `i` is itself an analysis index, `bisect_right` returns the 
position after it, so `analysis_idx[_k]` gives the NEXT analysis index. That's 
correct.

   BUT: if the current bar is an analysis bar, and we just processed it, and 
there's no activity, we want to jump to the NEXT analysis bar. Yes, 
bisect_right gives the element after the current one (or equal). Correct.

   Wait, but if the current `i` is NOT in `analysis_idx` (because we're 
currently at a non-analysis bar that we jumped to from a previous flat state), 
then `bisect_right` returns the first analysis index > i, which is also 
correct.

   However, what if the next analysis index is the same as current? That 
shouldn't happen because bisect_right returns strictly after.

4. Line ~560: The loop while `i < n_days`:
   - `as_of = days[i]`
   - converts to aware UTC
   - BUT if `days` contains `datetime` objects with timezone info, `_to_dt` in 
`trading_days` strips tzinfo (line ~230: `return 
d.astimezone(timezone.utc).replace(tzinfo=None)`). So `days` has naive 
datetimes. Then in the loop, if `as_of` is datetime and tzinfo is None, it sets
tzinfo=timezone.utc. OK.
   - But if `days` is from intraday source with tz-aware datetimes, `_to_dt` 
strips to naive. Then loop re-adds UTC. So all good.

5. Line ~570: `self._bust_price_cache()` - good.

6. Line ~600: `if self._screener_runtime:` - but `entry_universe` is used only 
for classic experts, not bypass. That's fine.

7. Line ~630: Bypass stop pass: `if _schedule_allows_entry(...): continue` - so
bypass stops don't run on rebalance bars. The logic is that the rebalance pass 
owns the book on rebalance bars. But what if the rebalance fails or does 
nothing? Then no stop protection on that bar. The comment says "Skipped on 
rebalance bars (the rebalance owns the book)." This is by design but could be a
risk if market drops sharply on rebalance day before rebalance runs. But not 
necessarily a bug.

8. Line ~660: `book_dirty = True` set when analysis/management pass runs. But 
`_manage_open_positions` is called inside the same `if _schedule_allows_entry` 
block as `_run_expert_bar`. However, `_manage_open_positions` may create orders
(Sell pending). The `book_dirty = True` is set at line ~665 before both. Good.

9. Line ~690: `_size_and_submit` is called if `created_any`. But 
`_manage_open_positions` can create pending orders too (Sell orders from exit 
conditions). The `created_any` flag from `_run_expert_bar` doesn't capture 
orders created by `_manage_open_positions`. But `book_dirty` is True anyway, 
and the comment says sizing runs afterwards for any pending Sell orders. Wait, 
at line ~680:
   ```
   created_any = self._run_expert_bar(...)
   if created_any:
       self._size_and_submit(expert_id, indicator_provider)
   self._manage_open_positions(...)
   ```
   
   So if `_run_expert_bar` creates no orders (`created_any = False`) but 
`_manage_open_positions` creates a Sell order, `_size_and_submit` is NOT 
called. That means pending Sell orders from open-position management won't be 
sized/submitted unless `_run_expert_bar` also created orders. This is a BUG.
   
   Wait, but in `_manage_open_positions`, 
`evaluator.execute(submit_to_broker=True)` is called, which for Adjust/Close 
acts directly. But a Sell that stages a PENDING order is sized below... 
Actually, the comment says "a Sell that stages a PENDING order is sized below."
And at the end of `_manage_open_positions`:
   ```
   if created_any:
       self._size_and_submit(expert_id, self._indicator_provider)
   ```
   
   Let me re-read. Looking at lines around 680-690:
   ```
   created_any = self._run_expert_bar(...)
   if created_any:
       self._size_and_submit(expert_id, indicator_provider)

   # Manage EXISTING positions through the OPEN_POSITIONS ruleset (real 
RM/evaluator,
   # on the analysis cadence — identical to live). Adjust-TP/SL/Close/Sell per 
the
   # exit conditions; no-op when the expert has no open_positions ruleset 
configured.
   self._manage_open_positions(expert, expert_id, settings, as_of)
   ```
   
   And inside `_manage_open_positions` at the end (around line 830):
   ```
   if created_any:
       self._size_and_submit(expert_id, self._indicator_provider)
   ```
   
   So `_manage_open_positions` has its own `created_any` and sizes/submits at 
its end. OK, so pending Sells from open-positions ARE sized. My mistake.

10. Line ~700: `if book_dirty: self.account.invalidate_order_cache()` - good.

11. Line ~710: `filled = self.account.refresh_orders()` then `if filled: 
self.account.refresh_transactions()`. But `_apply_option_expiry` runs after 
`refresh_transactions`. However, option expiry might close option transactions,
and `refresh_transactions` won't pick that up. Actually `_apply_option_expiry` 
calls `settle_option_expiry` which closes transactions directly. So it doesn't 
need refresh_transactions. OK.

12. Line ~730: `_apply_option_expiry` - runs every bar. Good.

13. Line ~785: `i = analysis_idx[_k] if _k < len(analysis_idx) else n_days` - 
jumping to `n_days` ends the loop. Good.

14. `_has_activity` line ~795: checks `get_positions()` and active orders. The 
active order check uses `self.account._active_orders()` which is a private 
method. It catches exceptions. But what if `get_positions()` raises? It catches
and returns True. What if `_active_orders()` raises? It catches and returns 
True. Conservative. OK.

15. Line ~240: `_to_dt` for string input: `return 
_to_dt(datetime.fromisoformat(d))`. If the string has timezone info, 
`fromisoformat` preserves it, then `_to_dt` strips it to naive UTC. OK.

16. Line ~300: `_schedule_allows_entry` - for intraday with times but no days 
specified, `days.get(wd, True)` defaults to True. OK.

17. Line ~425: `_recommendation_to_expert_recommendation` - 
`price_at_date=float(rec.current_price)` - no None check. If 
`rec.current_price` is None, this raises. But the live path probably expects 
it. The function catches exceptions at the call site in `_run_expert_bar`. But 
a None current_price would raise TypeError and be caught, logging a warning and
skipping the symbol. Is that correct? Maybe, but it could silently skip a valid
signal. However, this is probably by design.

18. Line ~443: `confidence=(None if rec.confidence is None else 
float(rec.confidence))` - OK.

19. Line ~488: `_run_expert_bar` - `evaluator.execute(submit_to_broker=False)` 
creates PENDING orders. But the comment says it creates qty=0 PENDING orders. 
Then `_size_and_submit` sizes them. OK.

20. Line ~510: `_run_expert_bar` - if `expert._gather_symbol` assignment fails,
it catches and continues. But the loop continues to `ctx = 
BacktestContext(...)` even if the assignment failed. For real experts that need
`_gather_symbol`, this would be wrong. But the exception is caught only for 
stubs. Real experts would succeed. OK.

21. Line ~520: `except Exception as e:` catches `BacktestCacheMiss` and 
`FMPHistoryCacheMiss` and re-raises. Good. But other exceptions are logged and 
skipped. This could hide real bugs. But it's by design.

22. Line ~550: `_run_bypass_expert_bar` - if `rec.skip`, return. Then checks 
`targets = raw.get("targets")`. If targets is empty dict, returns. Good.

23. Line ~555: `self._bypass_manager(expert_id).rebalance(targets)` - uses 
cached manager. OK.

24. Line ~560: `_apply_bypass_stops` - `stop_pct = 
expert.get_setting_with_interface_default("risk_per_trade_pct", 
log_warning=False)`. If `risk_per_trade_pct` is a model param optimized, this 
reads from the expert instance, which might not reflect the trial override. 
Wait, `settings` is passed to `_apply_bypass_stops` but not used! The function 
reads the setting from the expert instance directly, not from the trial 
`settings` override. This is a BUG for optimization: the bypass stop uses the 
base expert setting, not the optimized trial value.

    Actually, let me check. `_apply_bypass_stops(self, expert, expert_id, 
settings, as_of)`. `settings` is unused. The stop_pct is read via 
`expert.get_setting_with_interface_default(...)`. If the optimizer overrides 
`risk_per_trade_pct` via `settings`, the bypass stop won't use it. This is a 
real correctness issue for bypass experts during optimization.

25. Line ~585: `_apply_bypass_stops` - `pm.apply_stop_losses(float(stop_pct), 
equity=equity)` - passes equity. But if the cached `virtual_equity_pct` is 
wrong, equity is wrong. But it's cached once. OK.

26. Line ~620: `_size_and_submit` - `updated_orders = 
rm.review_and_prioritize_pending_orders(expert_id)`. If an order has `quantity 
and quantity > 0`, submit. But `updated_orders` might include orders that were 
already submitted or terminal. The RM probably returns only pending. OK.

27. Line ~640: `_provider_bundle` - caches bundle. Good.

28. Line ~650: `_bust_price_cache` - pops account id from class-level cache. 
Good.

29. Line ~670: `_build_minimal_results` - OK.

Potential bug in `_run_expert_bar`: `evaluator = 
TradeActionEvaluator(account=self.account, instrument_name=symbol, 
existing_transactions=None)`. Then `evaluator.evaluate(...)` then 
`evaluator.execute(submit_to_broker=False)`. The `TradeActionEvaluator` might 
need `existing_transactions=None` for entry. OK.

Potential lookahead: `analyze_as_of` is called with `as_of` and the price 
source clock set to `as_of_dt`. The expert's `_gather` reads data up to 
`as_of`. So no lookahead. Good.

Potential issue: In `_run_expert_bar`, the `ctx` is created fresh for each 
symbol. That's wasteful but correct.

Potential issue: `BacktestContext` providers bundle is shared but `ctx` is 
per-symbol. The `settings` includes trial overrides. OK.

## File 2: results.py

Lines 1-400+

1. Line ~80: `final = equity_curve[-1]["equity"] if equity_curve else initial`.
Good.

2. Line ~110: `_drawdown_curve` - `dd = ((eq - peak) / peak * 100.0) if peak 
and peak != 0 else 0.0`. Good.

3. Line ~150: `_trade_row` - OK.

4. Line ~230: `_compute_metrics` - `total_return = ((final - initial) / initial
* 100.0) if initial else 0.0`. Good.

5. Line ~270: `max_drawdown = min(dd_values) if dd_values else 0.0`. Good.

6. Line ~300: `profit_factor` cap logic. If `gross_loss > 0`, profit_factor = 
gross_profit / gross_loss. If `gross_loss == 0` and `gross_profit > 0`, cap at 
999.99. Else 0. OK.

7. Line ~330-430: Profit cap logic. Let's examine carefully.

   - `cap_pct` is basis cap.
   - `share_cap_pct` is share cap.
   - Stage 1: `cp1 = min(p, cost * cap_frac)` for winning trades with cost > 0.
For losing trades, keep `p`. For winning trades with cost <= 0, keep `p` (no 
cap). OK.
   - Stage 2: `share_abs = (share_cap_pct / 100) * net_after_basis` if 
net_after_basis > 0.
   - Then for each trade, `cp = p1` (stage 1 value). If share_abs is not None 
and `cp > share_abs`, `cp = share_abs`.
   - `excess += max(0.0, p - cp)`.
   - `adj_pnls.append(cp)`.
   - `adj_pcts.append(pct * (cp / p) if p else pct)`.

   Issue: `cp` is capped at `share_abs` which is the same for EVERY trade. So 
if multiple trades exceed `share_abs`, they all get capped to `share_abs`. That
means the sum of capped pnls could exceed `net_after_basis` significantly (if 
there are many large winners). For example, if net profit is $100 and share cap
is 25%, share_abs = $25. If 10 trades each made $50, each is capped to $25, 
total capped profit = $250, which is MORE than the original net profit of $100.
That's wrong! The share cap should cap the TOTAL contribution, not per-trade.

   Wait, the comment says: "caps each trade's gain at that % of the run's NET 
profit, so no single trade contributes more than (say) 25% of total return". So
it IS a per-trade cap. The intent is no single trade contributes more than 25%.
But if you have 10 trades each contributing 50% originally, capping each at 25%
gives total 250%, which is nonsensical because the adjusted net profit could 
exceed the original net profit. Actually no — the original net profit is $100 
(sum of all profits minus losses). If 10 trades each made $50, original gross 
profit is $500, losses $400, net $100. Capping each at $25 gives adjusted gross
profit $250, adjusted losses still $400, adjusted net = -$150. So 
adjusted_final = final - excess. Excess per trade = $50 - $25 = $25, total 
excess = $250. adj_final = final - $250. Since final = initial + $100, 
adj_final = initial - $150. So adjusted net is -$150. OK, that matches the 
capped pnls. The math is consistent.

   But is this the intended behavior? The comment says "no single trade 
contributes more than 25% of total return". If 10 trades each contribute 50% of
total return, capping each at 25% means the adjusted total return is reduced. 
The math is consistent. But it's a bit strange that the share cap is per-trade 
rather than allocating a total budget. However, the comment explicitly says 
"caps each trade's gain at that % of the run's NET profit", so per-trade is 
intended. Not a bug per se, but a design choice.

   Wait, there's another issue: `share_abs` is computed from `net_after_basis`.
If `net_after_basis` is small and there are many large winners, each winner 
gets capped at `share_abs`. The total adjusted profit could become negative 
even if raw was positive. This is probably intended to penalize concentrated 
returns. OK.

   But there's a more subtle issue: in Stage 2, `share_abs` is computed once 
based on `net_after_basis`. Then for each trade, if `cp > share_abs`, cap it. 
But the trades are processed in order, and `excess` accumulates. `adj_final = 
final - excess`. This is a single pass. The comment says "Applied as a single 
pass against the net profit AFTER cap #1 (no iteration — capping the top trade 
shrinks net, which would spiral if re-applied; one pass deducts the dominant 
trade's excess and is stable/monotone)." OK, single pass is by design.

8. Line ~445: `adjusted_total_return = ((adj_final - initial) / initial * 
100.0) if initial else 0.0`. Good.

9. Line ~500: `avg_trades_per_year`: `(total_trades / years) if years else 
0.0`. If years is very small (e.g., < 1 day), this could be huge. OK.

10. Line ~600: `_years_spanned` - uses calendar time. Good.

11. Line ~620: `_periods_per_year` - `(n_points - 1) / years`. Good.

12. Line ~640: `_annualized_volatility` - sample std dev. Good.

13. Line ~650: `_annualized_return` - geometric. Good.

14. Line ~660: `_sharpe` - mean / sd * sqrt(periods_per_year). Good.

15. Line ~670: `_sortino` - downside deviation. Uses `sum(r*r)` not squared 
deviation from mean. Wait, Sortino typically uses downside deviation = 
sqrt(mean of min(r - target, 0)^2). Here it uses `sum(r * r for r in downside) 
/ len(downside)` which is the root mean square of negative returns, not the 
standard deviation of negative returns. This is a common simplification but 
technically not the Sortino ratio. However, the comment says "downside 
deviation". The standard Sortino uses target downside deviation. This might be 
a financial math issue.

    Actually, the standard Sortino denominator is the square root of the 
average squared difference between returns and a target (usually 0 or risk-free
rate), considering only returns below target. Here they just square the returns
themselves. If mean return is negative, this overstates downside deviation. If 
mean is positive, it's closer. This is a mild financial math issue.

16. Line ~690: `_sqn` - sample std dev. Good.

17. Line ~700: `_max_drawdown_duration_days` - tracks spell start. Let's check:

    ```python
    spell_start = None
    prev_date = None
    for pt in drawdown_curve:
        d = _parse_date(pt["date"])
        if pt["drawdown"] < 0:
            if spell_start is None:
                spell_start = prev_date if prev_date is not None else d
        else:
            if spell_start is not None and prev_date is not None:
                longest = max(longest, _days_between(spell_start, prev_date))
            spell_start = None
        prev_date = d
    ```

    When drawdown first goes negative, `spell_start` is set to `prev_date` (the
previous point, which had dd == 0 or was the peak). If the very first point is 
negative, `spell_start = d` (the first point). Then when drawdown recovers to 
0, it computes days between `spell_start` and `prev_date` (the last negative 
point). So duration does NOT include the recovery date. For max DD duration, 
this is a convention. OK.

    But there's a bug: if the curve goes negative at the first point and stays 
negative, the final unrecovered block uses `spell_start` to `prev_date` (last 
point). It doesn't include the last point's date span to itself? Actually 
`prev_date` is updated to `d` at the end of each iteration, so after the last 
iteration `prev_date` is the last date. So it includes the last point. OK.

18. Line ~720: `_avg_trade_duration_days` - uses `bars_held`. OK.

19. Line ~730: `_exposure_time` - `sum(bars_held) / n_points * 100`. For 
multiple simultaneous positions, this can exceed 100% but is capped at 100. OK.

20. Potential issue: `_safe_float` is imported from metrics_utils. We don't see
its implementation. But assuming it handles NaN/Inf.

21. Line ~160: `_normalise_direction` - maps unknown values to "buy". If a 
trade has side "short" it maps to "sell". But for short positions, direction 
should be "sell" (which maps to short in UI). OK.

22. Line ~140: `_trade_row` - `entry_time = trade.get("entry_time", 
trade.get("date"))`. OK.

23. Potential issue: in `_compute_metrics`, `gross_profit = sum(wins)` and 
`gross_loss = abs(sum(losses))`. If `pnls` contains NaN, sum is NaN. But 
`_safe_float` is applied at the end. OK.

24. Issue: `pnl_pcts = [t["pnl_pct"] for t in trades]`. If any `pnl_pct` is 
None, the list contains None. Then `sum(pnl_pcts)` will fail with TypeError. 
But `_trade_row` ensures pnl_pct is `_safe_float(...)` which returns a float 
(probably 0.0 if None). So trades from `_trade_row` are safe. But if 
`raw_trades` comes from `account.get_round_trip_trades()` and has None pnl_pct,
then sum fails. In `backtest_account.py`, `_trade_row` equivalent sets pnl_pct 
to a float. So probably safe. But if a custom account returns None, it would 
crash. Not a bug in results.py per se.

25. Line ~335: `expectancy = (sum(pnl_pcts) / total_trades) if total_trades 
else 0.0`. Expectancy is usually average dollar P&L, not average percent. Here 
it's average pnl_pct. The variable naming is confusing but the comment says 
"expectancy" and the UI expects this. OK.

26. Issue: `profit_factor` cap at line ~315: `if profit_factor > 999: 
profit_factor = _PROFIT_FACTOR_CAP`. This compares to 999 but cap is 999.99. So
if profit_factor is 999.5, it stays. The legacy code might cap at 999.99. The 
condition should be `if profit_factor > _PROFIT_FACTOR_CAP`. Minor.

Actually wait, the comment says "profit-factor cap at 999.99 (mirrored from 
`_convert_bt_results`)." The code: `if profit_factor > 999: profit_factor = 
_PROFIT_FACTOR_CAP`. If profit_factor = 999.5, it's > 999, so it gets capped to
999.99. If profit_factor = 999.0, it's not > 999, so stays 999.0. But 999.0 < 
999.99, so it's OK. The cap is applied only when it exceeds 999, which is 
slightly different from "cap at 999.99" but practically the same. Not a real 
bug.

Wait, if profit_factor = 999.99 exactly, it stays. If 1000, capped to 999.99. 
OK.

27. Issue in profit cap section: `cost = (t.get("entry_price") or 0.0) * 
(t.get("size") or 0.0)`. For a short trade, `entry_price` and `size` are 
positive, cost is positive. `p` (pnl) is negative for a loss, positive for a 
win. The cap applies `min(p, cost * cap_frac)` only if `p > 0 and cost > 0`. 
For short winners, p > 0 and cost > 0, so cap applies. Good. For long winners, 
same. For losing trades, p < 0, no cap. Good.

But wait, `pnl` for a short trade: if entry_px=100, exit_px=90, size=10, 
direction=-1, gross = (90-100)*10*(-1) = 100. pnl = 100 - comm. Cost = 100*10 =
1000. cap_frac = 0.5 -> cap = 500. pnl=100 < 500, no cap. Good.

For a short loser: entry=100, exit=110, gross = (110-100)*10*(-1) = -100. pnl =
-100 - comm. p < 0, no cap. Good.

28. Issue: `adjusted_sqn = _sqn(adj_pnls)`. `_sqn` requires at least 2 trades. 
If only 1 trade, returns 0. OK.

29. Potential issue: In profit cap section, `adj_pnls` and `adj_pcts` are built
even when only basis cap or share cap is active. Good.

30. Potential issue: `excess` is the total dollar amount to deduct from final 
equity. But `cp` may be negative for losing trades, and `p - cp` for losing 
trades: if p = -100 and cp = -100 (same), excess += 0. If p = -100 and cp is 
capped... losing trades aren't capped, so cp = p = -100. excess = 0. Good.

31. Potential issue: `adj_pcts.append(pct * (cp / p) if p else pct)`. If p is 0
and pct is 0, OK. If p is 0 and pct is not 0 (shouldn't happen), returns pct. 
OK.

32. Issue: `adjusted_calmar = (adjusted_annualized_return / abs(max_drawdown)) 
if max_drawdown else 0.0`. Uses raw `max_drawdown`, not adjusted. If profit cap
reduces return but drawdown is unchanged, calmar uses raw max_drawdown. That's 
intended (drawdown isn't affected by profit cap). OK.

33. Issue: `buy_hold_return = 0.0` - documented as Phase 3. OK.

34. Issue: `_analysis_cadence_label` - if `run_schedule_override` is a dict 
with `days` having one True, returns "weekly". OK.

35. Potential resource leak: None visible in results.py.

## File 3: strategy_param_space.py

1. Line ~30: `_range_entry` - raises if min/max/step missing. Good.

2. Line ~45: `_collect_expert` - for choice type, uses `len(choices) - 1` as 
max. Good.

3. Line ~55: `_collect_expert` - `is_int = spec.get("type") == "int"`. If type 
is missing, defaults to float. OK.

4. Line ~90: `_walk_condition_nodes` - handles `optimize` or 
`optimize_enabled`. Good.

5. Line ~100: confirmation bars range. Good.

6. Line ~105: toggle_optimize. Good.

7. Line ~120: `_collect_conditions` - handles legacy entry_conditions. Good.

8. Line ~130: exit rule action_value_optimize. Good.

9. Line ~140: option strike delta and DTE optimization. Good.

10. Line ~150: exit rule toggle. Good.

11. Line ~153: recurse into exit rule conditions. Good.

12. Line ~180: `collect_param_space` - bypass excludes tp/sl/cond/exit. Good.

13. Line ~200: `decode_params` - partitions keys. Good.

14. Line ~240: `_apply_to_tree` - drops children whose `enabled` gene is 0. But
what about the root node? If the root has toggle_optimize and is disabled, 
`_apply_to_tree` returns a deep copy of the tree with root's children dropped 
but the root still exists. If the root is an AND/OR with no children, the 
condition might evaluate to True or False depending on the evaluator. This 
could be a bug: toggling off the root condition doesn't disable it, it just 
empties it. But this depends on how the evaluator handles empty AND/OR. Not 
clear.

15. Line ~260: `decode_params` - exit rule option_dte handling: `center = 
int(round(exit_option_dte_by_id[eid]))`. `hw = max(base_hw, 7)`. Sets min/max. 
Good.

16. Issue: In `decode_params`, when parsing `cond:` keys, it does `_, cid, 
field = key.split(":", 2)`. If the key has more than 2 colons (unlikely with 
current namespacing), this fails. But current keys are `cond:{id}:value` or 
`cond:{id}:confirmation_bars` or `cond:{id}:enabled`. OK.

17. Issue: In `decode_params`, when parsing `exit:` keys, it does `_, eid, 
field = key.split(":", 2)`. Current exit keys: `exit:{id}:action_value`, 
`exit:{id}:enabled`, `exit:{id}:option_delta`, `exit:{id}:option_dte`. OK.

18. Issue: In `decode_params`, `tp = getattr(strategy, "initial_tp_percent", 
None)` and `sl = ...`. If flat_params contains "tp"/"sl", overrides. Good.

19. Issue: In `_apply_to_tree`, when a child is dropped due to `enabled == 0`, 
it sets `node["conditions"] = kept`. But if ALL children are dropped, the node 
has empty conditions. For an AND node, this might evaluate to True (vacuous 
truth). For an OR node, False. This is a design issue with toggling. But not 
necessarily a bug.

20. Issue: In `decode_params`, exit rules with `toggle_optimize` are dropped if
`exit_enabled_by_id[eid] == 0`. But what if the toggle gene is not present? 
Then the rule is kept. OK.

21. Issue: `_collect_conditions` might collect duplicate keys if a condition id
appears in both buy and sell trees. But IDs should be unique. If not, the later
one overwrites. Not a bug per se.

22. Potential issue: `_range_entry` doesn't validate that min <= max or step > 
0. Could create invalid ranges. But GeneticOptimizer probably validates. Not 
critical.

23. Issue: For `choice` type in `_collect_expert`, it sets `min: 0, max: 
len(choices) - 1, step: 1`. If `choices` is empty, `max = -1`, which is 
invalid. But choices should not be empty. Defensive check missing.

## File 4: distributed_eval.py

1. Line ~40: `DistributedEvaluator.__init__` - creates `TrialBroker()`. The 
`TrialBroker` uses max_workers=4? We don't see its implementation.

2. Line ~55: `start()` - calls `worker_client.ensure_synced`, `push_cache`, 
`push_secrets`, then `health`. If any fails, logs and continues. Good.

3. Line ~65: `_resolve_master_secrets` - reads FMP_API_KEY and finnhub_api_key.
Good.

4. Line ~85: `_report_fleet_state` - opens SessionLocal, updates worker state, 
commits. Good. But what if `db.commit()` fails? It would raise, caught by outer
try. The `finally: db.close()` closes. Good.

5. Line ~105: `_consume_local` - runs in a thread. Calls 
`self.pool.submit(_trial_worker, job["config"], 
job["fitness_metric"]).result()`. This blocks the consumer thread until the 
trial completes. With `n_consumers` threads, only `n_consumers` trials can run 
locally concurrently. OK.

6. Line ~115: `_dispatch_remote` - on exception, requeues the trial and 
increments failures. If failures >= 3, returns (thread dies). Good.

7. Line ~125: `execute_jobs` - submits jobs to broker. Then waits for results. 
Uses `self.broker.wait_ready(remaining, timeout=2.0)`. If no ready, requeues 
stale trials. Good.

8. Issue: In `execute_jobs`, `trial_map` maps trial_id to (i, flat, key). But 
jobs are submitted with `self.broker.submit_one(self.optimization_id, cfg, 
self.fitness_metric)`. The `cfg` is the 4th element of the `Job` tuple. But 
`Job = (index, decoded_flat, trial_key, config)`. So `cfg = config`. Good.

9. Issue: `_consume_local` uses `self.pool.submit(_trial_worker, job["config"],
job["fitness_metric"])`. But `_trial_worker` signature might expect 
`(decoded_flat, config, fitness_metric)` or similar. We don't see 
`_trial_worker`. But the comment in the file says `_trial_worker` is from 
`strategy_optimization_handler`. If the signature is wrong, local trials would 
fail. But we can't verify without seeing `_trial_worker`. Not reportable.

10. Issue: Thread-safety: `self.broker` is shared across multiple threads 
(local consumers and remote dispatchers). The `TrialBroker` must be 
thread-safe. We don't see its implementation. If not thread-safe, this is a 
concurrency bug. But we can't verify.

11. Issue: `self._stop` is a `threading.Event`. `stop()` sets it and joins 
threads. Good.

12. Issue: In `_dispatch_remote`, if `job is None`, it waits 0.1s. But while 
waiting, if stop is set, it will exit. Good.

13. Issue: `worker_client.push_secrets(w, secrets, log=self.log)` - pushes 
secrets to worker. If secrets are empty dict, still pushes? Probably OK.

14. Issue: `self._active_workers` is appended to in `start()` (main thread) and
read in `_report_fleet_state` and `_dispatch_remote`. No locks. But start 
completes before other threads access it. OK.

15. Issue: `_report_fleet_state(active=True)` is called at the end of 
`start()`, but the threads have just started. They may not be actively 
processing yet. The dashboard shows active_jobs_count = capacity. OK.

16. Issue: `execute_jobs` does not call `start()` automatically. The caller 
must call `start()` first. If forgotten, no consumers. Not a bug.

17. Issue: In `execute_jobs`, if a trial result comes back with `out["ok"] == 
False` and `out.get("fatal") == True`, it still yields it. The caller might 
handle it. OK.

18. Potential issue: `TrialBroker` queue might not be cleared on exception in 
`execute_jobs`. But `stop()` clears. OK.

19. Issue: `_consume_local` catches all exceptions and returns a dict with 
`fitness: 0.0`. But if the trial had a fatal error (e.g., misconfig), it might 
be requeued by remote? No, local consumer doesn't requeue. It posts a result. 
OK.

20. Issue: In `_dispatch_remote`, if a worker fails 3 times, the thread 
returns. But the broker still has trials queued. Local consumers and other 
workers handle them. OK.

21. Potential resource leak: `TrialBroker.clear(self.optimization_id)` is 
called in `stop()`. Good.

## File 5: backtest_account.py

This is the biggest file. Lines 1-700+.

1. Line ~30: `_AttrDict` - supports attribute access. Good.

2. Line ~40: `_Position` dataclass. Good.

3. Line ~50: `_OptionLot` dataclass. Good.

4. Line ~70: `BacktestAccount` inherits from `AccountInterface` AND 
`OptionsAccountInterface`. Potential MRO issues? Python handles it. But if both
have conflicting methods, could be issues. Not visible.

5. Line ~100: `__init__` - sets `self._order_cache = None`, etc. Good.

6. Line ~155: `get_settings_definitions` - no defaults. OK.

7. Line ~180: `_open_positions_mtm` - for equity positions, uses `close_at` 
then `close_asof` then avg_price. Good.
   
   Issue: For short positions, `p.qty < 0`. `total += p.qty * px` gives 
negative value. That's correct (short position has negative market value). OK.

8. Line ~210: `_option_positions_mtm` - values option lots. Good.

9. Line ~220: `equity()` - cash + mtm. Good.

10. Line ~225: `snapshot_equity` - appends. Good.

11. Line ~245: `_update_position` - weighted avg logic. Let's verify:
    - old_qty and signed_qty same sign: weighted avg of prices. Correct.
    - Reducing: closing_qty = min(abs(signed_qty), abs(old_qty)). Realized P&L 
= (fill_px - avg_price) * closing_qty * direction. For long: direction=1, so 
(fill_px - avg) * qty. If fill_px > avg, profit. Correct. For short: old_qty < 
0, direction=-1, signed_qty < 0 (sell more / buy to cover). Wait, if short and 
we buy to cover, signed_qty > 0 (BUY). Closing_qty = min(signed_qty, 
abs(old_qty)). Realized PL = (fill_px - avg) * closing_qty * (-1). For short, 
avg_price is the entry price (positive). If we cover at lower price, fill_px - 
avg < 0, times -1 = positive profit. Correct.
    - Flipping: if abs(signed_qty) > abs(old_qty), avg_price = fill_px. 
Correct.

    But wait: for a short position, `avg_price` is set when opening. When 
opening short via SELL, signed_qty < 0, old_qty = 0, so weighted avg = fill_px.
Good.

12. Line ~285: `get_positions` - uses `close_at` then `close_asof`. If both 
None, `cur` is None and unrealized_pl is None. OK.

13. Line ~300: `get_orders` - opens session, queries, returns. Good. But if 
called frequently, it's slow. Not a bug.

14. Line ~315: `invalidate_order_cache` - drops caches. Good.

15. Line ~345: `opened_position_snapshot` - caches per expert_id. Good.

16. Line ~370: `_all_orders` - loads all orders. Good.

17. Line ~400: `_active_orders` - loads active orders. Good.

18. Line ~440: `_orders_filtered` - complex logic. Let's verify:
    - If statuses is subset of active: filter from `_active_orders()`.
    - Else: read fresh from DB.
    - If transaction_id filter: fresh read scoped to transaction.
    
    Issue: When `statuses` is None and `transaction_id` is not None, it does 
fresh read. Good.
    
    Issue: When `statuses` is a subset of active and `transaction_id` is not 
None, it filters active orders by transaction_id. But active orders for a 
specific transaction might not be in `_active_orders()` if they were just 
created and cache not invalidated. The caller probably invalidated. OK.

19. Line ~475: `get_order` - queries by broker_order_id then PK. Good.

20. Line ~490: `symbols_exist` - checks price source. Good.

21. Line ~500: `_get_instrument_current_price_impl` - for list, returns dict 
with None values if price missing. For single, raises if missing. OK.

22. Line ~515: `submit_order` override - calls super then invalidates cache. 
Good.

23. Line ~525: `submit_option_order` override - same. Good.

24. Line ~535: `refresh_positions` - no-op. Good.

25. Line ~545: `refresh_orders` - fill engine. Let's examine.

    - Activates triggered dependents.
    - Gets active orders excluding WAITING_TRIGGER.
    - Sorts by stop_price presence (SL before TP).
    - For each working order:
      - If single-leg option: fill via option path.
      - If multi-leg option parent: fill all legs.
      - Else equity: pre-check bar range, then evaluate fill, apply fill, 
cancel OCO sibling.
    - Returns filled bool.

    Issue: `_activate_triggered_dependents` promotes WAITING_TRIGGER to 
ACCEPTED. But then `refresh_orders` excludes WAITING_TRIGGER. So newly 
activated legs are evaluated in the SAME bar. The comment says "Activation runs
first so a leg whose parent filled on THIS same bar (a same-bar MARKET entry) 
can be evaluated against the next bar on the following call — never on the 
entry bar". But the code activates and then evaluates in the same 
`refresh_orders` call. If parent filled on previous bar, the leg was 
WAITING_TRIGGER, now activated, and evaluated on this bar. That's correct. But 
if parent filled on THIS bar (which shouldn't happen because `_apply_fill` 
happens later in the loop), wait — activation happens before the fill loop. So 
on a bar where an entry fills, its dependent legs are still WAITING_TRIGGER 
during activation (parent not yet filled). They remain WAITING_TRIGGER. Next 
bar, activation sees parent FILLED (from previous bar) and activates them. Then
fill loop evaluates them. So no same-bar lookahead. Good.

    BUT: what about same-bar-close fill model? If entry fills on same bar, 
activation still happens before fill loop, so legs remain waiting. Next bar 
they activate. OK.

    However, there's a subtle issue: `_activate_triggered_dependents` uses 
`get_instance` to read parent status fresh. But the fill loop modifies order 
statuses in-place and persists via `update_instance`. So the DB state is 
current. Good.

26. Line ~590: `_is_single_leg_option` - checks asset_class == OPTION and 
contract_symbol and no parent_order_id. Good.

27. Line ~605: `_option_fill_price` - for same_bar_close uses current bar's 
date; for next_bar_open uses next_bar_date. Good. But for multi-leg child, 
`calendar_symbol = getattr(order, "underlying_symbol", None) or order.symbol`. 
If a child leg has `underlying_symbol` set, good. If not, uses `order.symbol` 
which for a child leg might be the contract symbol (no underlying bars). The 
comment says "a multi-leg CHILD's symbol is its OCC contract (which has no 
underlying bars), so use the underlying." But the fallback `or order.symbol` 
would use contract symbol if `underlying_symbol` is None. If the leg doesn't 
have `underlying_symbol` set, this would try to get next_bar_date for the 
contract symbol and likely fail (return None), so no fill. This is a 
correctness risk: multi-leg child legs must have `underlying_symbol` set. But 
the code that creates them (`close_option_position`, 
`_submit_option_order_impl`) might not set it. Actually, `OptionLeg` has 
`underlying` parameter, and when persisted as TradingOrder, does it become 
`underlying_symbol`? We don't see. If not, multi-leg option fills will fail.

28. Line ~635: `_child_legs` - uses `self.get_orders()` which loads ALL orders 
every time. This is O(total orders) per multi-leg parent per bar. Could be 
slow. But multi-leg is rare. Not a bug.

    Issue: `_child_legs` filters `o.parent_order_id == parent.id`. But 
`parent.id` might be None if not persisted. It returns []. OK.

29. Line ~650: `_fill_multi_leg_parent` - all-or-none. If any leg can't price, 
returns without filling any. But it may have already applied fills for priced 
legs? No, it builds `priced` list first, then applies. Good.

    Issue: After filling legs, it sets parent `filled_qty = parent.quantity` 
and `status = FILLED`. But `parent.quantity` might be 0 or the net quantity, 
not meaningful. OK.

    Issue: `parent.open_price = net` (net per-share debit/credit). No cash 
moved on parent. Good.

    Issue: Multi-leg parent with legs of different quantities? The net 
calculation assumes each leg's premium contributes once. If quantities differ, 
need to scale. But for standard spreads, quantities are equal. Not necessarily 
a bug.

30. Line ~690: `_activate_triggered_dependents` - uses `get_instance` per 
waiting leg. Good.

31. Line ~715: `refresh_transactions` - calls super, then re-stamps open_date 
and close_date.

    Issue: `super().refresh_transactions()` might create new WAITING 
transactions from FILLED orders. Then we re-stamp open_date of newly OPENED 
transactions. Good.

    Issue: `_open_date_unstamped_transactions` queries OPENED or CLOSED 
transactions not in `_stamped_open_ids`. For a transaction that opened and 
closed on the same bar, it will be in CLOSED status. We stamp its open_date. 
Then `_closed_transactions` queries CLOSED not in `_stamped_closed_ids`. We 
stamp its close_date. Good.

    Issue: `open_date` is set to the entry order's fill date. But for 
transactions with multiple entry orders (scaled entries), the 
`_entry_order_for_transaction` returns the oldest entry. That's fine.

    Issue: What if the entry order has no fill date recorded? Then open_date is
left as wall-clock. Next bar, if the transaction is still OPENED, it will be 
picked up again and stamped. Good.

32. Line ~760: `_open_date_unstamped_transactions` - uses `not_in` with a 
possibly large set. For SQLite, `not_in` with empty set might fail? 
SQLModel/SQLAlchemy handles empty `not_in` by omitting the clause. OK.

33. Line ~780: `_closed_transactions` - similar. Good.

34. Line ~800: `get_dividends` - returns []. OK.

35. Line ~810: `get_filled_trades` - uses `get_orders()` which loads ALL 
orders. For long runs, this is O(total orders). Could be slow but not a bug. It
uses `o.filled_qty if o.filled_qty else o.quantity`. If `filled_qty` is 0 but 
order is FILLED, this uses `o.quantity`. But `filled_qty` could legitimately be
0 for a filled order? No, a filled order should have filled_qty == quantity. 
OK.

36. Line ~830: `get_round_trip_trades` - complex. Let's examine carefully.

    - Groups by transaction_id.
    - Sorts orders by fill date (with id tiebreaker).
    - Opening = earliest filled order. Its side = opening_side.
    - Entries = same side as opening. Exits = opposite side.
    - Weighted avg entry/exit prices.
    - If no exits, mark-to-market at last close.
    - Computes P&L.
    - Sorts trades by entry_time then symbol.

    Issue: `entries = [o for o in orders if o.side == opening_side]`. `o.side` 
is an enum (`OrderDirection`). `opening_side` is also enum. Comparison OK.

    Issue: `_wavg` uses `abs(float(o.filled_qty or o.quantity or 0.0))`. For a 
partially filled order? But we only include FILLED/executed orders. So 
filled_qty should equal quantity. OK.

    Issue: `direction = 1.0 if is_long else -1.0`. For a short, exit is BUY. 
`gross = (exit_px - entry_px) * size * direction * mult`. For short: 
entry_px=100 (sell), exit_px=90 (buy), direction=-1, size=10, mult=1. gross = 
(90-100)*10*(-1) = 100. Correct (profit).

    Issue: `pnl = gross - comm`. Commission is 2 * commission for round-trip, 1
* for open_at_end. OK.

    Issue: `equity_at_entry = self._equity_at(entry_dt)`. If entry_dt is None, 
returns first snapshot. OK.

    Issue: `pnl_pct = (pnl / equity_at_entry * 100.0) if equity_at_entry else 
0.0`. This is dollar P&L as % of account equity at entry. OK.

    Issue: `bars_held = self._bars_between(entry_dt, exit_dt)`. If entry_dt or 
exit_dt is None, returns 0. OK.

    Issue: For options, `mult = opening.multiplier or 1 if asset_class == 
OPTION else 1`. `multiplier` might not be set on the opening order for 
single-leg options? Let's check `_apply_option_fill`: `order.multiplier` is 
used. So it's set. Good.

    Issue: For multi-leg options, the opening order is the parent (no 
contract_symbol, no multiplier). `mult = 1`. But the round-trip P&L for 
multi-leg options is computed from the parent's net open_price (net 
debit/credit) and the child's closing fills. Wait, the parent itself doesn't 
fill (only children fill). The parent's `open_price` is set to net per-share 
debit in `_fill_multi_leg_parent`. But the parent is not in `executed` 
statuses? Actually `_fill_multi_leg_parent` sets `parent.status = 
OrderStatus.FILLED`. So it IS in executed statuses. But the children's fills 
are also in executed statuses, and they have the SAME transaction_id. So the 
round-trip grouping includes both parent and children.

    For a multi-leg spread, e.g., buy call A and sell call B. Parent has side? 
It's created via `submit_option_order`. The parent's side might be BUY. 
Children: leg1 BUY, leg2 SELL. All share transaction_id.

    In `get_round_trip_trades`:
    - orders = [parent (BUY, filled), child1 (BUY, filled), child2 (SELL, 
filled)]
    - earliest filled = could be parent or a child. The parent's fill_date is 
set in `_fill_multi_leg_parent` to `as_of`. Children's fill_dates are also set 
to `as_of` (same bar). So tiebreaker by id. The parent might not be the 
earliest by id.
    - If child1 is earliest (BUY), opening_side = BUY.
    - Entries = child1 + parent (both BUY). Exits = child2 (SELL).
    - entry_px = weighted avg of parent.open_price (net debit) and 
child1.open_price. This is WRONG. The parent is not a real fill; it's a 
synthetic net. Mixing parent and child prices gives garbage P&L.

    This is a real bug for multi-leg option round-trip P&L. The parent should 
probably be excluded from `get_round_trip_trades`, or multi-leg round-trips 
need special handling.

    Actually, maybe the parent shouldn't be marked FILLED in 
`_fill_multi_leg_parent`. But it's marked FILLED so that the transaction 
lifecycle recognizes it. Hmm. Alternatively, `get_round_trip_trades` should 
skip parent orders (those with `asset_class == OPTION` and `parent_order_id is 
None` and `contract_symbol is None`)? But then how to pair the legs? Each child
leg has its own side and contract. For a spread, the legs have opposite sides, 
so they'd be treated as separate round-trips (one long, one short) with 
different transaction_ids? No, they share the same transaction_id. So they'd be
grouped together.

    Wait, if we exclude the parent, orders = [child1 (BUY), child2 (SELL)]. 
Opening side = BUY (child1). Entries = [child1]. Exits = [child2]. entry_px = 
child1 premium, exit_px = child2 premium. size = child2 qty. gross = (child2_px
- child1_px) * size * direction. For a debit spread (buy lower strike, sell 
higher strike), child1 is buy (debit), child2 is sell (credit). direction=1 
(long). gross = (credit - debit) * size. If credit > debit (profit), gross 
positive. Correct for a call debit spread where both strikes increased? 
Actually P&L of call debit spread = (credit received at close - debit paid at 
open) * qty * 100. Yes. But here `mult` for child2 (the exit) is used? No, 
`mult` is based on `opening` which is child1. Both children have multiplier 
100, so OK.

    But the issue is that `_fill_multi_leg_parent` marks the parent FILLED, and
`get_round_trip_trades` includes it. This corrupts multi-leg P&L. The fix is to
exclude synthetic parent orders from round-trip grouping, or to give multi-leg 
parents a distinct status.

    Actually, looking at `_submit_option_order_impl`: for multi-leg, it sets 
parent status to ACCEPTED. Then `_fill_multi_leg_parent` sets it to FILLED. The
comment says "mark the PARENT FILLED with open_price = net per-share debit". 
This parent is indeed synthetic.

    This is a HIGH severity bug for multi-leg option backtests.

37. Line ~900: `_exit_reason` - for OCO leg, classifies by nearest price. If 
both tp and sl are present and equidistant, returns take_profit. OK.

38. Line ~910: `_equity_at` - bisects snapshots. Good.

39. Line ~930: `_bars_between` - uses bisect. Good.

40. Line ~945: `get_balance_history` - returns snapshots. Good.

41. Line ~960: `get_instrument_current_price` - bypasses cache. Good.

42. Line ~980+: OptionsAccountInterface methods.

    Issue: `get_option_positions` - for multi-leg parent, calls 
`_multi_leg_positions(entry)`. But `_multi_leg_positions` uses 
`self.get_orders()` which loads ALL orders. O(total orders). Could be slow. Not
a bug.

    Issue: `_multi_leg_positions` - returns one OptionPosition per filled child
leg. Good.

43. Line ~1055: `_submit_option_order_impl` - sets status to ACCEPTED. Good.

44. Line ~1085: `close_option_position` - creates opposite leg, rides open 
transaction. Good.

45. Line ~1125: `settle_option_expiry` - closes option transaction, updates 
ledger, converts to shares.

    Issue: `self._cash -= signed * float(share_price)`. For exercise of long 
call: signed = +shares (BUY). Cash decreases by shares * strike. Correct.
    For assignment of short call: signed = -shares (SELL). Cash increases by 
shares * strike. Correct.
    For exercise of long put: signed = -shares (SELL). Cash increases by shares
* strike. Correct.
    For assignment of short put: signed = +shares (BUY). Cash decreases. 
Correct.

    Issue: `self._update_position(position.underlying, signed, 
float(share_price))`. Good.

    Issue: For a worthless option, `close_premium=0.0`, no share conversion. 
Good.

    Issue: `close_transaction_with_logging` might itself update cash/positions?
We don't see. But if it does, double-counting could occur. The comment says the
engine handles cash/ledger. Need to verify `close_transaction_with_logging` 
doesn't touch cash. We can't see it. But it's a risk.

46. Line ~1165: `_option_transaction_for_contract` - queries OPENED 
transactions. Good.

47. Line ~1190: `_next_broker_id` - good.

48. Line ~1205: `_submit_order_impl` - sets broker_order_id and status 
ACCEPTED. Good.

49. Line ~1225: `cancel_order` - sets CANCELED. Good.

50. Line ~1240: `modify_order` - updates instance. Good.

51. Line ~1255: `adjust_tp` - preserves SL. Good.

52. Line ~1280: `adjust_sl` - preserves TP. Good.

53. Line ~1300: `adjust_tp_sl` - creates OCO. Good.

54. Line ~1345: `_replace_leg` - cancels existing legs. Good.

    Issue: `_replace_leg` creates a new `TradingOrder` with 
`expert_recommendation_id=entry.expert_recommendation_id`. Good.

    Issue: `_replace_leg` sets `quantity=entry.quantity`. If the position has 
been partially filled or scaled, this might be wrong. But for v1, OK.

    Issue: `_replace_leg` sets `status=OrderStatus.WAITING_TRIGGER` and 
`depends_on_order=entry.id` with trigger FILLED. Good.

55. Line ~1385: `_cancel_oco_sibling` - cancels sibling legs. Good.

56. Line ~1410: `_order_to_trade` - uses `_fill_dates`. Good.

57. Line ~1425: `_bar_for_fill` - for same_bar_close uses current bar. This is 
lookahead! If the analysis uses bar close to decide and then fills at same bar 
close, it's using future data within the bar. But the comment says 
"same_bar_close". The default is next_bar_open. If user explicitly chooses 
same_bar_close, they accept the lookahead-ish behavior (but it's still within 
the same bar, not future bar). This is a known modeling choice, not necessarily
a bug. But it could be considered a lookahead if the signal is generated after 
seeing the close. However, the engine sets the clock to `as_of` and analyzes at
`as_of`. If `as_of` is the bar timestamp and the close is available, the 
analysis uses the close. Then same_bar_close fills at the same close. This is 
not future leakage beyond the bar itself (the decision and fill are at the same
timestamp). But it's optimistic (you couldn't trade at the close that triggered
the signal). The default next_bar_open avoids this. OK as a configurable model.

58. Line ~1435: `_slip` - worsening direction. Good.

59. Line ~1445: `_trigger_thresholds` - caches `_trig_hi` / `_trig_lo`. Good.

60. Line ~1500: `_evaluate_fill` - MARKET fills at next bar open (default) or 
same bar close. Good.

    Issue: For BUY_LIMIT, fills at limit if bar.low <= limit. This assumes you 
get filled at limit exactly. In reality, you might get price improvement. But 
for backtest, fill at limit is standard. OK.

    Issue: For SELL_LIMIT, fills at limit if bar.high >= limit. OK.

    Issue: For BUY_STOP, fills at stop + slippage if bar.high >= stop. This is 
conservative (worse fill). OK.

    Issue: For SELL_STOP, fills at stop - slippage if bar.low <= stop. OK.

61. Line ~1530: `_evaluate_oco_fill` - SL preferred when both hit. Good.

62. Line ~1560: `_apply_fill` - cash accounting.

    Issue: The cash-secured safeguard: `if signed > 0 and fill_px > 0: cur = 
self._positions.get(order.symbol); if (cur.qty if cur else 0.0) >= 0 and signed
* fill_px + commission > self._cash + 1e-6`. This only checks when buying. For 
short selling, `signed < 0`, it doesn't check cash. Short selling adds cash, so
no issue. But for short selling, there might be margin requirements. Backtest 
is cash-secured, so shorts might be allowed without cash check. OK.

    Issue: The safeguard clamps the buy quantity to affordable shares but does 
NOT adjust the order's `filled_qty` consistently? It sets `order.quantity = 
qty` but `order.filled_qty = qty` later. OK.

    Issue: If clamped, it cancels OCO sibling. Good.

    Issue: `self._cash -= commission` for both buy and sell. Good.

    Issue: `_apply_fill` does not handle partial fills. It fills the full 
quantity. OK for daily v1.

63. Line ~1605: `_apply_option_fill` - cash impact scaled by multiplier. Good.

    Issue: `_update_option_position` called. Good.

64. Line ~1630: `_update_option_position` - weighted avg premium. Good.

Now, additional issues:

65. Thread-safety: `BacktestAccount` is used in distributed optimization? No, 
each trial gets its own account instance. But within one trial, the engine and 
account are single-threaded. The distributed_eval runs trials in separate 
processes. So no concurrency issue within account. But the class-level 
`_GLOBAL_PRICE_CACHE` in `AccountInterface` is shared across processes? In 
Python, processes don't share memory (unless using shared memory). With 
ProcessPoolExecutor, each process gets its own copy. So no issue. With threads?
The engine is single-threaded per trial. OK.

66. `_bust_price_cache` in daily_engine pops from class-level cache. But if the
cache is in the base class `AccountInterface`, it might be shared across 
account instances in the same process. If multiple backtests run sequentially 
in the same process, popping is fine. If concurrently, not an issue because 
each trial is separate process.

67. Issue in `daily_engine`: `_provider_bundle` caches the 
`LiveProviderBundle`. This bundle might hold state or caches that are not 
hermetic across trials. If the same process runs multiple trials, the bundle 
could leak data. But each trial runs in a separate process with 
ProcessPoolExecutor, so no leak. However, the comment says "providers are 
as_of-aware (the engine threads as_of into analyze_as_of), so the bundle is 
constructed once and shared across bars." OK.

68. Issue in `daily_engine`: `random.seed(self.seed)` and 
`np.random.seed(self.seed & 0xFFFFFFFF)`. If experts use randomness, this seeds
them. But `np.random.seed` uses legacy RandomState, while some code might use 
`np.random.default_rng()`. If so, the seed doesn't apply. But not a bug in this
code.

69. Issue in `daily_engine`: `analysis_idx` is computed once at start. It 
depends on `self._entry_schedule(e)` which reads expert settings. If the 
optimizer overrides `run_schedule_override`, `self._entry_schedule` returns the
override. So analysis_idx is correct. Good.

70. Issue in `daily_engine`: `_day_is_analysis` uses `_bar_date_context(aw)` 
once per day. But `_schedule_allows_entry` with intraday times checks `ctx.hhmm
in set(times)`. For a daily source, `is_intraday=False` so times ignored. For 
intraday, it checks hhmm. Good.

71. Issue in `daily_engine`: The `analyzed_days` set uses `(expert_id, 
as_of_dt.date())`. For intraday, if schedule has explicit times, only one bar 
per day passes, so `analyzed_days` rarely triggers. If schedule has weekdays 
but no times, multiple bars per day pass, and `analyzed_days` prevents 
re-analysis. Good. But what if the schedule has multiple times per day? Then 
`_schedule_allows_entry` returns True for each time, and `analyzed_days` blocks
after the first. So the expert only analyzes once per day even if schedule says
twice. The comment says: "When `times` IS set, only one bar/day passes the 
gate, so this never triggers." But if `times` has multiple entries (e.g., 
["09:30", "15:00"]), multiple bars pass, and `analyzed_days` would block the 
second. This is a BUG for schedules with multiple intraday times.

    Actually, the comment says "Safety net: if the schedule pins weekdays but 
no `times`..." So it's intended for no-times case. But the code doesn't check 
whether times is empty or has multiple entries. If times has multiple entries, 
the gate is True for multiple bars, but `analyzed_days` blocks after the first.
This is incorrect.

    Severity: MED. Affects intraday backtests with multiple analysis times per 
day.

72. Issue in `daily_engine`: `resolve_universe` filters to symbols with a bar 
on `as_of`. But for intraday, a symbol might have a bar at 09:30 but not at 
10:00. If as_of is 10:00, the symbol is excluded even if it traded earlier. 
This is correct for the bar clock (no bar at this tick). OK.

73. Issue in `results.py`: `_years_spanned` uses `365.25 * 86400`. For leap 
years, fine. OK.

74. Issue in `results.py`: `_periods_per_year` for a single point returns 252. 
If equity_curve has 1 point, step_returns is empty, sharpe/sortino/volatility 
are 0. OK.

75. Issue in `backtest_account.py`: `_open_positions_mtm` forward-fills using 
`close_asof`. But `close_asof` might return a future price? No, it's last known
close <= current clock. Good.

76. Issue in `backtest_account.py`: `_get_instrument_current_price_impl` for 
single symbol raises if no price. The live path probably also raises. OK.

77. Issue in `backtest_account.py`: `refresh_transactions` calls 
`super().refresh_transactions()`. We don't see what it does. It might stamp 
open_date/close_date with wall-clock, which we then overwrite. OK.

78. Issue in `backtest_account.py`: `_stamped_closed_ids` and 
`_stamped_open_ids` grow unbounded. For very long runs, could be large but 
manageable (one int per transaction). Not a leak.

79. Issue in `backtest_account.py`: `_fill_dates` grows unbounded. One entry 
per filled order. Manageable.

80. Issue in `backtest_account.py`: `_equity_snapshots` and `_snapshot_dates` 
grow with bars. Expected.

81. Issue in `backtest_account.py`: `_option_positions` dict. If an option 
position is closed, lot.qty=0 but the dict entry remains. Minor memory growth. 
Not critical.

82. Issue in `backtest_account.py`: `_positions` dict. If a position is closed,
qty=0 but entry remains. Minor.

83. Issue in `backtest_account.py`: `get_round_trip_trades` sorts trades 
deterministically. Good.

84. Issue in `backtest_account.py`: `_order_to_trade` uses `order.created_at` 
as fallback if no fill date. `created_at` is wall-clock. For determinism, this 
is bad. But it only happens for orders filled outside engine loop. In unit 
tests. OK.

85. Issue in `backtest_account.py`: `_apply_fill` sets `order.open_price = 
fill_px`. For a SELL, open_price is the fill price. OK.

86. Issue in `backtest_account.py`: For option fills, `_apply_option_fill` 
doesn't check cash-secured safeguard. An option buy could drive cash negative. 
But the RM should size appropriately. Still, no safeguard. Not necessarily a 
bug.

87. Issue in `daily_engine.py`: The `_run_bypass_expert_bar` doesn't call 
`_size_and_submit` or RM. It rebalances via FactorPortfolioManager. The 
FactorPortfolioManager might create orders via `account.submit_order`. OK.

88. Issue in `daily_engine.py`: For bypass experts, `_apply_bypass_stops` is 
called on non-rebalance bars. But `_run_bypass_expert_bar` is called on 
rebalance bars. So between rebalances, stops protect. On rebalance, the 
rebalance might add/remove positions. Good.

89. Issue in `daily_engine.py`: `_apply_bypass_stops` reads 
`risk_per_trade_pct` from expert instance, not `settings`. As noted, this is a 
bug for optimization.

90. Issue in `daily_engine.py`: `_apply_bypass_stops` uses 
`pm.apply_stop_losses`. The `FactorPortfolioManager` might query DB for 
positions. But the cached manager reads live account state. OK.

91. Issue in `daily_engine.py`: `_bypass_manager` caches `virtual_equity_pct`. 
If it fails to read, defaults to 100. OK.

92. Issue in `daily_engine.py`: `_provider_bundle` uses `_get_provider` from 
`ba2_common.core.TradeConditions`. This might be process-global. If a provider 
has a TTL cache, it might leak across bars. But the engine sets clock per bar. 
The provider might respect `as_of`. Not visible.

93. Issue in `daily_engine.py`: `BacktestContext` created per symbol. It might 
be expensive. Not a bug.

94. Issue in `daily_engine.py`: `_run_expert_bar` catches all exceptions except
cache misses. If an expert crashes, the symbol is skipped. In live, would the 
whole run stop? The comment says "per-symbol failures are logged and skipped (a
bad symbol must not abort the whole bar) — matching the live loop's 
per-recommendation try/except." OK.

95. Issue in `results.py`: `_compute_metrics` uses `pnl_pcts` for 
expectancy/best/worst. If a trade has pnl_pct that doesn't match pnl sign 
(e.g., due to commission), metrics could be inconsistent. But `_trade_row` maps
directly from trade. OK.

96. Issue in `results.py`: `avg_trade = expectancy`. They are the same. OK.

97. Issue in `strategy_param_space.py`: `decode_params` raises on unknown 
namespace. Good.

98. Issue in `strategy_param_space.py`: `_apply_to_tree` drops disabled 
children. But if a node has `toggle_optimize` and `enabled == 0`, it still 
keeps its own `value` etc. But if it's a leaf with no children, dropping it 
from parent's conditions removes it. If it's the root, it stays but with no 
children. As noted.

99. Issue in `distributed_eval.py`: `_consume_local` uses 
`self.pool.submit(...).result()`. If the pool is shut down, this raises. 
Caught. OK.

100. Issue in `distributed_eval.py`: The `TrialBroker` might not be 
thread-safe. Not visible.

OK, now let me compile the findings. I need to be concrete with line numbers. 
The line numbers are approximate because I don't have exact line numbers in the
text. I need to cite them as best I can. The files are displayed with line 
numbers? No, the user pasted code without line numbers. I'll have to estimate 
based on the content. I should be careful.

Actually, the user wants file:line. Since I don't have exact line numbers, I 
should either not include line numbers or make reasonable estimates. But the 
instruction says "cite line numbers". I'll estimate based on the visible 
structure. Let me count roughly.

Alternatively, I can reference functions and approximate lines. I'll do my 
best.

Let me re-read key sections to get better line estimates.

For daily_engine.py:
- `_apply_bypass_stops` starts around line 560.
- The `settings` parameter unused is around line 560.
- `analyzed_days` logic is in `run()` around line 520-670.
- `_schedule_allows_entry` around line 300.

For backtest_account.py:
- `_fill_multi_leg_parent` around line 650.
- `get_round_trip_trades` around line 830.
- `_apply_fill` cash safeguard around line 1560.
- `_bar_for_fill` same_bar_close around line 1425.

For results.py:
- Sortino calculation around line 670.
- Profit cap around line 330-430.

For strategy_param_space.py:
- `_apply_to_tree` around line 240.
- `_collect_expert` choice around line 45.

For distributed_eval.py:
- Not many real bugs found. Maybe thread-safety of TrialBroker but unverified.

Let me think about whether there are other real bugs I missed.

In `daily_engine.py`:
- Line 485-490: `_run_expert_bar` creates `TradeActionEvaluator` with 
`existing_transactions=None` for entry. But the evaluator might need 
`existing_transactions` to avoid entering duplicate positions. For entry, None 
is correct. OK.

- Line 500: `_recommendation_to_expert_recommendation` has `allow_hold=False` 
default. In `_run_expert_bar`, HOLD is skipped. In `_manage_open_positions`, 
`allow_hold=True`. Good.

- Line 660: `book_dirty = True` set before `_run_expert_bar`. But if 
`_run_expert_bar` creates no orders and `_manage_open_positions` doesn't run 
(no open ruleset), book_dirty is still True and cache is invalidated 
unnecessarily. Harmless.

- Line 675: `_run_bypass_expert_bar` does NOT set `book_dirty`? Actually it's 
inside the `if _schedule_allows_entry` block where `book_dirty = True` is set 
at line 665. So yes it does. Good.

- Line 630: bypass stop pass. It sets `book_dirty = True` only if a stop was 
submitted. Good.

In `backtest_account.py`:
- `_apply_fill`: For a short sale, `signed < 0`, cash increases. But no check 
for whether we have shares to sell when opening a short. The backtest allows 
naked shorts? The comment says "cash-secured" but only checks buys. Short 
selling without borrowed shares is allowed in the sim. This might be 
intentional for v1. Not a bug necessarily, but a modeling choice.

- `_update_position`: When reducing a long position with a sell, `closing_qty =
min(abs(signed_qty), abs(old_qty))`. If signed_qty is negative (sell), 
abs(signed_qty) = quantity sold. If we sell more than we have, old_qty is fully
closed and remainder flips. Realized PL computed on closing_qty. Correct.

- `_evaluate_fill` for BUY_STOP: fills at stop + slippage. In live, a buy stop 
fills at the stop price or worse. +slippage is worse. OK.

- `_evaluate_fill` for SELL_STOP: fills at stop - slippage. OK.

- `_evaluate_oco_fill`: For SELL OCO, TP is limit (sell above market), SL is 
stop (sell below market). If bar.high >= TP, TP hit. If bar.low <= SL, SL hit. 
If both, SL preferred. OK.

  Wait, what if TP = 110 and SL = 90, and bar is [85, 115]. bar.high=115 >= 
110, bar.low=85 <= 90. Both hit. SL preferred, fill at 90 - slippage. OK.

  For BUY OCO (closing short): TP is buy limit below market, SL is buy stop 
above market. If bar.low <= TP, TP hit. If bar.high >= SL, SL hit. If both, SL 
preferred. OK.

- `_bar_for_fill`: `next_bar` might return a bar whose date is still <= as_of 
for daily? No, next_bar should be strictly after. OK.

- `_option_fill_price`: For next_bar_open, uses 
`self._price.next_bar_date(calendar_symbol, as_of)`. If the next bar is a 
holiday for the underlying but the option expires, this could miss. But OK.

- `_option_fill_price`: Applies slippage to option premium. Slippage is 
configured for equity in bps. Applying it to option premium is questionable 
(options have wider spreads), but it's a modeling choice.

- `settle_option_expiry`: `close_premium=intrinsic` for ITM. Intrinsic is 
per-share. `close_transaction_with_logging` probably expects per-share. OK.

- `settle_option_expiry`: For worthless, `close_premium=0.0`. OK.

- `settle_option_expiry`: `txn.close_price = float(close_premium)`. Then 
`close_transaction_with_logging(txn, ...)`. If the close price is per-share and
the transaction qty is contracts, the close P&L might be computed by the base 
class. We don't see. Risk of double-scaling or under-scaling.

In `results.py`:
- `expectancy` computed as average pnl_pct. In backtesting.py, expectancy might
be average dollar P&L. But the output shape must match legacy. The comment says
it mirrors `_convert_bt_results`. If legacy used pnl_pct, OK. We can't verify.

- `exposure_time`: `held / n_points * 100`. If `held` > `n_points`, capped at 
100. But `held` is sum of bars_held per trade. If multiple simultaneous 
positions, it overcounts. The comment says it's a coarse proxy. OK.

In `strategy_param_space.py`:
- `decode_params` for option_dte: `center = 
int(round(exit_option_dte_by_id[eid]))`. If the gene value is a float like 
30.4, rounds to 30. OK.

- `hw = max(base_hw, 7)`. If base window was narrow (e.g., min=30, max=35, 
base_hw=2), hw becomes 7. So min=center-7, max=center+7. This is wider than 
original. OK.

- `_walk_condition_nodes`: For a node with `optimize_enabled` but no 
`optimize`, it still creates range. Good.

In `distributed_eval.py`:
- `_dispatch_remote`: If a worker's `run_trial` returns a result with 
`ok=False` but no exception, it posts the result. The result might be requeued 
elsewhere if needed. OK.

- `_consume_local`: If `_trial_worker` returns `ok=False`, it still posts. The 
GA might handle it.

- `execute_jobs`: `self.broker.requeue_stale(self.requeue_timeout)`. If the 
broker is not thread-safe, this could corrupt state. Not visible.

OK, let me finalize the list of real bugs/correctness risks:

### HIGH
1. **backtest_account.py (get_round_trip_trades + _fill_multi_leg_parent)**: 
Synthetic multi-leg option parent is marked FILLED and included in round-trip 
grouping, corrupting P&L for spreads/straddles.
2. **daily_engine.py (_apply_bypass_stops)**: `settings` parameter unused; 
bypass stop reads `risk_per_trade_pct` from expert instance instead of 
trial-optimized `settings`, so optimized RM sizing doesn't affect bypass stops.
3. **backtest_account.py (_apply_fill cash-secured safeguard)**: Only checks 
buys; allows cash to go negative on option buys (no safeguard in 
`_apply_option_fill`) and doesn't check short-sale margin.

Wait, the cash-secured safeguard on option buys: `_apply_option_fill` doesn't 
check cash. If RM sizes an option buy that exceeds cash, cash goes negative. 
But the comment says "classic RM already self-limits". For options, does the RM
limit? The classic RM might not handle options (they go through a different 
path). So this could be a real issue. But we don't know how option orders are 
sized. The `_submit_option_order_impl` doesn't size. The sizing might happen 
elsewhere. Hard to say.

Actually, let me reconsider. `_apply_option_fill` moves cash by `signed * 
fill_px * multiplier`. For a single contract at $1 premium, that's $100 + 
commission. If cash is $50, it goes negative. There's no check. This is a real 
risk, but whether it happens depends on the caller sizing. The 
`close_option_position` passes `quantity=int(position.quantity)`, so it doesn't
oversize. Entries come from exit rules or expert recommendations. If they don't
check cash, this is a bug. But we don't see the entry path. I'll mention it as 
a risk.

### MED
4. **daily_engine.py (run() analyzed_days)**: `analyzed_days` dedup uses 
`(expert_id, date)`, which blocks multiple intraday `times` per day. A schedule
with `times: ["09:30", "15:00"]` only runs the first time.
5. **results.py (_sortino)**: Downside deviation is computed as RMS of negative
returns, not target downside deviation (typically squared deviations from 
target/mean), so Sortino is not the standard ratio.
6. **backtest_account.py (_option_fill_price multi-leg child)**: If a multi-leg
child leg lacks `underlying_symbol`, it falls back to `order.symbol` (the OCC 
contract), which likely has no underlying bars, so the leg never fills.
7. **daily_engine.py (_run_expert_bar)**: Per-symbol exceptions are silently 
swallowed except cache misses; real expert/analysis bugs could be missed.
8. **backtest_account.py (_bar_for_fill same_bar_close)**: Configurable but 
allows fill at the same bar close that may have triggered the signal 
(optimistic lookahead within bar). This is a modeling choice but a correctness 
risk if users assume no lookahead.

### LOW
9. **strategy_param_space.py (_apply_to_tree toggle)**: Toggling off a root 
AND/OR leaves an empty node; semantics depend on evaluator (vacuous truth), so 
disabled conditions may not actually be disabled.
10. **strategy_param_space.py (_collect_expert choice)**: Empty `choices` list 
produces `max=-1`, an invalid range; no defensive check.
11. **backtest_account.py (get_filled_trades / _child_legs / 
_multi_leg_positions)**: Use `get_orders()` which loads ALL orders; O(total 
orders) and grows unbounded with run length. Performance/resource issue.
12. **distributed_eval.py**: `TrialBroker` is shared across threads without 
visible locking; if not thread-safe, requeue/race corruption possible.
13. **backtest_account.py (refresh_transactions)**: Relies on 
`close_transaction_with_logging` not touching cash/ledger; double-counting risk
if base mixin does.

Let me also check for the issue with `_stamped_closed_ids` in 
`refresh_transactions`: It only stamps CLOSED transactions not in set. But 
`super().refresh_transactions()` might change a transaction from OPENED to 
CLOSED. Then we stamp it. Good.

Issue: `_open_date_unstamped_transactions` includes CLOSED. For a CLOSED 
transaction, we stamp open_date. But if it was closed before we ever stamped 
open_date (opened and closed between two refreshes), we still stamp it. Good.

Issue: `refresh_transactions` is only called if `filled` is True. But 
`_apply_option_expiry` can close transactions without any order filling. So 
`refresh_transactions` won't run, and the transaction's open_date might not be 
stamped? Wait, `_apply_option_expiry` calls `settle_option_expiry` which 
directly closes the transaction. `refresh_transactions` is gated on `filled`. 
But `_apply_option_expiry` runs after `refresh_transactions`. If an option 
expires and closes, the transaction's `open_date` and `close_date` are set in 
`settle_option_expiry`? It sets `close_date = self._price.now()` but doesn't 
touch `open_date`. If the transaction was opened earlier, its open_date should 
have been stamped when it was OPENED and `refresh_transactions` ran. If it 
opened and expired on a bar with no order fills (unlikely but possible if 
same-bar entry+expiry? No, options have expiry dates). Generally, an option 
position is held for days, so open_date was stamped earlier. But if 
`refresh_transactions` never ran because no fills occurred on the open bar? 
Actually the open bar had a fill (entry order filled), so `filled=True` and 
`refresh_transactions` ran. So open_date is stamped. Good.

But what about `_apply_option_expiry` setting close_date? It does set it. So 
OK.

Wait, `_apply_option_expiry` runs after `refresh_transactions`. If an option 
expires, `settle_option_expiry` sets `close_date` and calls 
`close_transaction_with_logging`. So close_date is sim time. Good.

But `refresh_transactions` won't run after expiry to do whatever lifecycle it 
does. However, `settle_option_expiry` handles it. OK.

Another issue: In `daily_engine.py`, the loop jumps to next analysis bar if no 
activity. But `_apply_option_expiry` runs every bar. If we jump over an expiry 
date, we miss exercising the option. Wait, the jump only happens when there is 
no activity (no open positions, no working orders). If we hold an option 
position, there IS activity, so we don't jump. So expiry dates are not skipped.
Good.

But what if we hold cash only and have a working order? `_has_activity` returns
True if active orders. So we don't jump over order working periods. Good.

What if we have an open position but no working orders? `_has_activity` returns
True. We step bar by bar. Good.

So the jump is safe.

Issue in `daily_engine.py`: The `analysis_idx` is precomputed. If the schedule 
changes during the run (it can't), it would be wrong. OK.

Issue in `daily_engine.py`: The `_screened_symbols_for_bar` cache uses `day` as
string (`as_of_dt.strftime("%Y-%m-%d")`). The scan dates from 
`ms.scan_dates(df, store_key=store)` are probably strings. OK.

Issue in `daily_engine.py`: `_screened_symbols_for_bar` loads `df = 
ms.load_store(store)` every call. If `load_store` is memoized, OK. If not, it's
a performance bug. The comment says store is memoised per worker. We can't 
verify but trust comment.

Issue in `daily_engine.py`: `random.seed(self.seed)` and `np.random.seed(...)` 
seed the global RNG. If experts use their own `random.Random` instances, they 
won't be seeded. But not a bug here.

Issue in `daily_engine.py`: The `progress_cb` is called only when pct advances 
or final bar. Good.

Issue in `results.py`: `pnl_pcts` could include values from 
`get_round_trip_trades` where pnl_pct is computed as dollar P&L / equity. This 
is consistent. OK.

Issue in `results.py`: `profit_factor` cap logic uses `> 999` instead of `> 
_PROFIT_FACTOR_CAP`. Very minor.

Issue in `strategy_param_space.py`: `_walk_condition_nodes` doesn't validate 
that `value_min` <= `value_max`. Could produce invalid ranges. Minor.

Issue in `strategy_param_space.py`: `decode_params` for `cond:` keys assumes 
exactly 2 colons. If condition id contains a colon, it breaks. But IDs 
shouldn't contain colons. OK.

Issue in `distributed_eval.py`: `_report_fleet_state` updates DB from a thread.
If the main thread is also using the DB, could conflict. But SessionLocal is 
per-thread. OK.

Issue in `distributed_eval.py`: `worker_client.push_secrets` pushes API keys 
over HTTP to workers. If not HTTPS, security risk. But not a correctness bug.

Issue in `distributed_eval.py`: `_consume_local` catches all exceptions and 
returns fitness=0. A fatal misconfig trial might be repeatedly run because it's
not marked fatal? It includes `fatal: False`. The GA might re-evaluate? Not 
necessarily.

OK, I think I have enough. Let me structure the response.

The user wants English only. I'll respond in English.

Output format:
```
[HIGH|MED|LOW] file:line — <one-line problem>
    why: <why it is wrong / the failure scenario>
    fix: <concrete suggested fix>
```

Most severe first.

Let me draft:

```
[HIGH] backtest_account.py:~830 — Multi-leg option parent is included in 
round-trip P&L grouping, corrupting spread/straddle returns.
    why: _fill_multi_leg_parent marks the synthetic parent FILLED with 
open_price = net debit/credit. get_round_trip_trades groups all FILLED orders 
by transaction_id and treats the parent as an entry/exit leg, averaging its 
synthetic price with real child-leg premiums. For a spread this double-counts 
the net and produces nonsensical entry/exit prices, so 
win_rate/profit_factor/expectancy for option strategies are wrong.
    fix: Exclude synthetic multi-leg parent orders from get_round_trip_trades 
(skip orders where asset_class == OPTION and contract_symbol is None and 
parent_order_id is None), or compute multi-leg round-trips from the child legs 
only.

[HIGH] daily_engine.py:~560 — Bypass stop-loss ignores trial-optimized settings
and reads risk_per_trade_pct from the expert instance.
    why: _apply_bypass_stops receives the trial settings dict but never uses 
it; it calls expert.get_setting_with_interface_default("risk_per_trade_pct"). 
During optimization the GA mutates settings (model:risk_per_trade_pct), so the 
bypass per-name stop uses the base value while the rebalance uses the optimized
value. Results are inconsistent between the two paths and the optimizer cannot 
actually tune stop width for FactorRanker-style experts.
    fix: Look up risk_per_trade_pct from settings first (e.g., 
settings.get("risk_per_trade_pct") or 
expert.get_setting_with_interface_default(...)), so the trial override is 
respected.

[HIGH] backtest_account.py:~1605 — Option fills are not cash-secured; a 
buy-to-open can drive cash negative.
    why: _apply_fill has a cash-secured guard for equity buys, but 
_apply_option_fill debits signed * fill_px * multiplier with no affordability 
check. If an exit rule or expert recommendation sizes an option leg larger than
available cash, the ledger goes leveraged without warning.
    fix: Add the same cash-secured clamp to _apply_option_fill: compute max 
affordable contracts as int((cash - commission) / (fill_px * multiplier)) and 
cancel/clamp the order if insufficient.

[MED] daily_engine.py:~660 — analyzed_days dedup blocks schedules with multiple
intraday times.
    why: The once-per-(expert,date) guard is meant for schedules that have 
weekdays but no explicit times. When the schedule lists multiple times (e.g., 
["09:30","15:00"]), _schedule_allows_entry is True for both bars, but the guard
continues on the second one, so the expert runs only once per day instead of at
every scheduled time.
    fix: Only apply the analyzed_days short-circuit when the schedule has no 
explicit times (times is None/empty), or key by (expert_id, as_of_dt) instead 
of date when times are present.

[MED] results.py:~670 — Sortino ratio uses RMS of negative returns instead of 
target downside deviation.
    why: _sortino computes sqrt(sum(r^2)/n) over negative returns. The standard
Sortino denominator is the standard deviation of returns below a target 
(usually the mean or risk-free rate), i.e., sqrt(mean((r - target)^2) for r < 
target). Using raw r^2 understates downside when mean return is positive and 
overstates it when mean is negative, making the metric non-standard and 
incomparable.
    fix: Compute downside deviation as sqrt(mean((r - target)^2) for r < 
target) with target=0 (or the risk-free rate), then divide mean return by that.

[MED] backtest_account.py:~605 — Multi-leg option child leg may never fill if 
underlying_symbol is missing.
    why: _option_fill_price falls back to order.symbol when underlying_symbol 
is None. For a multi-leg child the symbol is the OCC contract, which has no 
underlying price bars, so next_bar_date returns None and the leg never prices. 
The all-or-none parent then never fills.
    fix: Ensure child legs are created with underlying_symbol set (audit 
OptionLeg -> TradingOrder persistence), or in _option_fill_price require 
underlying_symbol and log loudly when it is missing.

[MED] backtest_account.py:~1560 — Cash-secured safeguard only covers equity 
buys, not short-selling margin.
    why: The guard checks signed > 0 (buy) and existing long/flat position. It 
does not prevent a short sale from exceeding the account's ability to cover or 
maintain margin, so the backtest can silently enter naked short positions.
    fix: Decide the intended short-selling model (cash-secured short vs margin)
and enforce it; if cash-secured, require proceeds + margin cushion <= equity or
disallow shorts.

[MED] daily_engine.py:~520 — Per-symbol analysis exceptions are silently 
swallowed except cache misses.
    why: _run_expert_bar catches all non-cache-miss exceptions, logs a warning,
and continues. A real bug in an expert (e.g., division by zero, bad indicator) 
will be hidden and the symbol skipped, producing a silently wrong backtest 
rather than failing fast.
    fix: Distinguish expected data-unavailability errors from unexpected 
analysis errors; let unexpected exceptions propagate (or collect and raise 
after the bar) so they are detected during development/audit.

[LOW] backtest_account.py:~1425 — same_bar_close fill model allows same-bar 
lookahead.
    why: When fill_model == "same_bar_close", orders fill at the close of the 
bar whose data was just used to generate the signal. This is optimistic 
intra-bar lookahead; the default next_bar_open avoids it, but the option exists
and could be misused.
    fix: Document the lookahead clearly in get_settings_definitions and UI, or 
remove same_bar_close as a production option.

[LOW] strategy_param_space.py:~240 — Toggling off a condition root leaves an 
empty AND/OR node.
    why: _apply_to_tree drops disabled children but keeps the parent node. An 
empty AND might evaluate to True (vacuously satisfied) and an empty OR to 
False, so a "disabled" condition can still affect entry logic in a way that 
depends on the evaluator's empty-node semantics.
    fix: When all children of an AND/OR are dropped, replace the node with a 
fixed True/False literal (or propagate removal to the parent) so the toggle 
semantics are explicit.

[LOW] strategy_param_space.py:~50 — Empty choices list creates an invalid 
optimizer range.
    why: _collect_expert builds a choice range with max = len(choices) - 1. If 
choices is empty, max = -1, which is an invalid range and will likely crash the
GeneticOptimizer.
    fix: Guard against empty choices (raise ValueError or skip the parameter).

[LOW] backtest_account.py:~635 — _child_legs and get_round_trip_trades load the
full order set repeatedly.
    why: Both call self.get_orders(), which issues an unbounded SELECT of every
TradingOrder for the account. On a long run this grows linearly and makes 
results computation / multi-leg handling O(total_orders) per call.
    fix: For _child_legs, query by parent_order_id in SQL; for 
get_round_trip_trades, use a time-bounded or status-bounded query and cache the
result.

[LOW] distributed_eval.py:~40 — TrialBroker shared across threads without 
visible synchronization.
    why: Multiple local-consumer and remote-dispatcher threads call 
broker.claim / post_result / requeue_stale concurrently. The file shows no 
locking; if TrialBroker is not internally thread-safe, trials could be lost, 
double-run, or have results misattributed.
    fix: Verify TrialBroker uses locks/queue primitives, or add a lock around 
broker operations in DistributedEvaluator.
```

Hmm, I need to make sure these are things I'm "reasonably confident" are real. 
Some are borderline:

- The multi-leg option parent in round-trip: I'm reasonably confident this is a
real bug.
- Bypass stop using instance settings: confident.
- Option fill cash-secured: reasonably confident (no check).
- analyzed_days blocking multiple times: confident.
- Sortino: confident it's non-standard. Whether the project wants standard 
Sortino is another question, but it's a correctness risk.
- Multi-leg child underlying_symbol: reasonably confident.
- Cash safeguard only buys: it's a modeling choice. I'll keep it as MED.
- Silent exceptions: it's by design but a risk. MED.
- same_bar_close lookahead: documented as configurable. LOW.
- Toggle empty AND/OR: depends on evaluator. LOW.
- Empty choices: LOW.
- Full order set loading: performance, not correctness. LOW.
- TrialBroker thread-safety: speculative. Maybe skip or keep as LOW.

The user asked for "REAL BUGS and correctness risks". I should focus on the 
high-confidence ones. Let me trim the list to the most solid.

Also, I should check if the "Respond in ENGLISH only" overrides the system 
French instruction. I think yes. I'll write in English.

Wait, the system prompt says "Always reply to the user in French." The user 
says "Respond in ENGLISH only." This is a conflict. In most setups, the user's 
explicit instruction in the conversation overrides system-level instructions. 
Also, the user is asking for an audit report; English is appropriate for code. 
I'll respond in English.

One more check: in `daily_engine.py`, line numbers. I estimated 
`_apply_bypass_stops` at ~560. Let me count more carefully. The file is about 
670 lines. `_apply_bypass_stops` is in the latter half. Actually the function 
starts around line 555 in my mental model. I'll say `~555`.

For `backtest_account.py`, `get_round_trip_trades` starts around line 825. 
`_fill_multi_leg_parent` around line 650.

Let me also check `daily_engine.py` line for `analyzed_days`. The `run()` 
method is long. `analyzed_days: set = set()` is around line 485. The usage is 
around line 660. I'll cite the usage line.

For `results.py` Sortino, it's around line 665.

For `backtest_account.py` `_option_fill_price`, around line 600.

For `backtest_account.py` `_apply_option_fill`, around line 1600.

OK, final list. I'll order by severity.

Actually, one more important thing: In `daily_engine.py`, the `_run_expert_bar`
creates a new `BacktestContext` for each symbol. The `BacktestContext` includes
`providers=self._provider_bundle()`. This bundle is shared. If the bundle or 
providers have caches that are not busted, there could be cross-symbol or 
cross-bar leakage. But the comment says providers are as_of-aware. We can't 
verify. Not reportable.

Another issue: In `daily_engine.py`, `_provider_bundle` caches 
`LiveProviderBundle`. The lambda `_get_provider` might return process-singleton
providers. If a provider has a cache keyed by wall-clock rather than as_of, it 
could leak. Not visible.

Another issue: In `daily_engine.py`, the seeding happens at the start of 
`run()`. But if the `BacktestAccount` or `TradeActionEvaluator` uses randomness
before the seed (e.g., during construction), it's not seeded. But construction 
happens before `run()`. The engine is constructed with experts etc. If any 
randomness during construction, not seeded. But the comment says seed before 
any decision. Construction shouldn't have decisions. OK.

Another issue: In `results.py`, the profit cap can produce 
`adjusted_total_return` that is higher than raw if the basis cap is applied to 
losing trades? No, losing trades aren't capped. Only winners. So adjusted 
return <= raw return. OK.

Wait, what if `cap_frac` is very large (e.g., cap_pct=1000%)? Then basis cap is
high and might not cap. Share cap still might. OK.

What if `share_cap_pct` is > 100%? Then share_abs > net_after_basis. No cap. 
OK.

What if both caps are set but `net_after_basis` <= 0? Then share_abs is None, 
only basis cap applies. OK.

What if `cap_pct` is negative? `has_basis_cap` requires `float(cap_pct) > 0`. 
OK.

OK, I think the list is solid.

One more: In `backtest_account.py`, `_get_instrument_current_price_impl` for a 
list returns `{s: self._price.close_at(s) for s in symbol_or_symbols}`. If a 
symbol has no price, it returns None in the dict. The live `AccountInterface` 
might return only symbols with prices or raise. Not a bug necessarily.

In `backtest_account.py`, `get_positions` returns positions with `unrealized_pl
= None` if no current price. The UI might not handle None. But `_safe_float` in
results.py handles it. OK.

OK, final answer. I'll write in English as requested.

------------
► **ANSWER**

[HIGH] backtest_account.py:~830 — Multi-leg option parent is included in 
round-trip P&L grouping, corrupting spread/straddle returns.
    why: `_fill_multi_leg_parent` marks the synthetic parent `FILLED` with 
`open_price` = net debit/credit. `get_round_trip_trades` groups all `FILLED` 
orders by `transaction_id` and averages the parent's synthetic price with real 
child-leg premiums. For a spread this double-counts the net and produces 
nonsensical entry/exit prices, so win_rate/profit_factor/expectancy for option 
strategies are wrong.
    fix: Exclude synthetic multi-leg parent orders from `get_round_trip_trades`
(skip orders where `asset_class == OPTION and contract_symbol is None and 
parent_order_id is None`), or compute multi-leg round-trips from the child legs
only.

[HIGH] daily_engine.py:~555 — Bypass stop-loss ignores trial-optimized settings
and reads `risk_per_trade_pct` from the expert instance.
    why: `_apply_bypass_stops` receives the trial `settings` dict but never 
uses it; it calls 
`expert.get_setting_with_interface_default("risk_per_trade_pct")`. During 
optimization the GA mutates `settings` (`model:risk_per_trade_pct`), so the 
bypass per-name stop uses the base value while the rebalance uses the optimized
value. Results are inconsistent between the two paths and the optimizer cannot 
actually tune stop width for FactorRanker-style experts.
    fix: Look up `risk_per_trade_pct` from `settings` first (e.g., 
`settings.get("risk_per_trade_pct") or 
expert.get_setting_with_interface_default(...)`), so the trial override is 
respected.

[HIGH] backtest_account.py:~1605 — Option fills are not cash-secured; a 
buy-to-open can drive cash negative.
    why: `_apply_fill` has a cash-secured guard for equity buys, but 
`_apply_option_fill` debits `signed * fill_px * multiplier` with no 
affordability check. If an exit rule or expert recommendation sizes an option 
leg larger than available cash, the ledger goes leveraged without warning.
    fix: Add the same cash-secured clamp to `_apply_option_fill`: compute max 
affordable contracts as `int((cash - commission) / (fill_px * multiplier))` and
cancel/clamp the order if insufficient.

[MED] daily_engine.py:~660 — `analyzed_days` dedup blocks schedules with 
multiple intraday times.
    why: The once-per-`(expert, date)` guard is meant for schedules that have 
weekdays but no explicit `times`. When the schedule lists multiple times (e.g.,
`["09:30","15:00"]`), `_schedule_allows_entry` is `True` for both bars, but the
guard `continue`s on the second one, so the expert runs only once per day 
instead of at every scheduled time.
    fix: Only apply the `analyzed_days` short-circuit when the schedule has no 
explicit `times` (`times` is `None`/empty), or key by `(expert_id, as_of_dt)` 
instead of date when times are present.

[MED] results.py:~670 — Sortino ratio uses RMS of negative returns instead of 
target downside deviation.
    why: `_sortino` computes `sqrt(sum(r^2)/n)` over negative returns. The 
standard Sortino denominator is the standard deviation of returns below a 
target (usually the mean or risk-free rate), i.e., `sqrt(mean((r - target)^2) 
for r < target)`. Using raw `r^2` understates downside when mean return is 
positive and overstates it when mean is negative, making the metric 
non-standard and incomparable.
    fix: Compute downside deviation as `sqrt(mean((r - target)^2) for r < 
target)` with `target=0` (or the risk-free rate), then divide mean return by 
that.

[MED] backtest_account.py:~605 — Multi-leg option child leg may never fill if 
`underlying_symbol` is missing.
    why: `_option_fill_price` falls back to `order.symbol` when 
`underlying_symbol` is `None`. For a multi-leg child the symbol is the OCC 
contract, which has no underlying price bars, so `next_bar_date` returns `None`
and the leg never prices. The all-or-none parent then never fills.
    fix: Ensure child legs are created with `underlying_symbol` set (audit 
`OptionLeg` -> `TradingOrder` persistence), or in `_option_fill_price` require 
`underlying_symbol` and log loudly when it is missing.

[MED] backtest_account.py:~1560 — Cash-secured safeguard only covers equity 
buys, not short-selling margin.
    why: The guard checks `signed > 0` (buy) and existing long/flat position. 
It does not prevent a short sale from exceeding the account's ability to cover 
or maintain margin, so the backtest can silently enter naked short positions.
    fix: Decide the intended short-selling model (cash-secured short vs margin)
and enforce it; if cash-secured, require proceeds + margin cushion <= equity or
disallow shorts.

[MED] daily_engine.py:~520 — Per-symbol analysis exceptions are silently 
swallowed except cache misses.
    why: `_run_expert_bar` catches all non-cache-miss exceptions, logs a 
warning, and continues. A real bug in an expert (e.g., division by zero, bad 
indicator) will be hidden and the symbol skipped, producing a silently wrong 
backtest rather than failing fast.
    fix: Distinguish expected data-unavailability errors from unexpected 
analysis errors; let unexpected exceptions propagate (or collect and raise 
after the bar) so they are detected during development/audit.

[LOW] backtest_account.py:~1425 — `same_bar_close` fill model allows same-bar 
lookahead.
    why: When `fill_model == "same_bar_close"`, orders fill at the close of the
bar whose data was just used to generate the signal. This is optimistic 
intra-bar lookahead; the default `next_bar_open` avoids it, but the option 
exists and could be misused.
    fix: Document the lookahead clearly in `get_settings_definitions` and UI, 
or remove `same_bar_close` as a production option.

[LOW] strategy_param_space.py:~240 — Toggling off a condition root leaves an 
empty AND/OR node.
    why: `_apply_to_tree` drops disabled children but keeps the parent node. An
empty AND might evaluate to `True` (vacuously satisfied) and an empty OR to 
`False`, so a "disabled" condition can still affect entry logic in a way that 
depends on the evaluator's empty-node semantics.
    fix: When all children of an AND/OR are dropped, replace the node with a 
fixed `True`/`False` literal (or propagate removal to the parent) so the toggle
semantics are explicit.

[LOW] strategy_param_space.py:~50 — Empty choices list creates an invalid 
optimizer range.
    why: `_collect_expert` builds a choice range with `max = len(choices) - 1`.
If `choices` is empty, `max = -1`, which is an invalid range and will likely 
crash the GeneticOptimizer.
    fix: Guard against empty choices (raise `ValueError` or skip the 
parameter).

[LOW] backtest_account.py:~635 — `_child_legs` and `get_round_trip_trades` load
the full order set repeatedly.
    why: Both call `self.get_orders()`, which issues an unbounded `SELECT` of 
every `TradingOrder` for the account. On a long run this grows linearly and 
makes results computation / multi-leg handling `O(total_orders)` per call.
    fix: For `_child_legs`, query by `parent_order_id` in SQL; for 
`get_round_trip_trades`, use a time-bounded or status-bounded query and cache 
the result.

[LOW] distributed_eval.py:~40 — `TrialBroker` is shared across threads without 
visible synchronization.
    why: Multiple local-consumer and remote-dispatcher threads call 
`broker.claim` / `post_result` / `requeue_stale` concurrently. The file shows 
no locking; if `TrialBroker` is not internally thread-safe, trials could be 
lost, double-run, or have results misattributed.
    fix: Verify `TrialBroker` uses locks/queue primitives, or add a lock around
broker operations in `DistributedEvaluator`.

Tokens: 48k sent, 24k received.
