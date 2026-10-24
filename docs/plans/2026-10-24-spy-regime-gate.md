# SPY Benchmark Regime Entry Gate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.
> Work ONLY in the worktree `C:\Users\basti\Documents\dev\BA2-spyregime` (branch `feat/spy-regime-gate`, based on dev `82d4e074`).
> **DO NOT push to dev.** Merge only at a job boundary (masters are mid-job; the raised `ba2_common` minimum in Task 15 would re-sync workers).

**Goal:** Option strategies get two new CATEGORICAL entry gates, `spy-trend` and `spy-vol`, that read the MARKET (SPY) regime instead of the instrument. Each is ONE set-membership leaf (`field == mode code`, mode searched by the GA incl. `off`). The same pure classifier and the same "newest session whose close is knowable" rule serve backtest and live. Opt-in: with no `--benchmark-regime-gates` flag every existing rule, gene, digest, job name and stored config is byte-for-byte unchanged.

**Design (user-approved 2026-10-24):**
- `spy-trend` mode in `off|up|pullback|bear_rally|down|above_200|below_200`: SPY close vs SMA200 and SMA50 slope (`SMA50[t] - SMA50[t-10] > 0` = rising). up = above & rising; pullback = above & falling; bear_rally = below & rising; down = below & falling; above_200 = up|pullback; below_200 = bear_rally|down.
- `spy-vol` mode in `off|calm|normal|stressed|not_stressed|not_calm`: the EXISTING `market_regime.classify_volatility_regime` (20d realised vol, percentile over 504 sessions; calm < 30, stressed > 70).
- Unknown (too little history, no SPY data, no regime published) NEVER passes and says why (`last_status`/`last_reason`, like `MarketConditionCompare`).

**Architecture:** (1) pure 4-state trend classifier in `market_regime.py`; (2) new `ba2_common/core/benchmark_regime.py`: mode/code tables, `BenchmarkRegime` value, `classify_benchmark_regime(closes)` (pure, the LIVE path), `BenchmarkRegimeCalendar` (precomputed, the BACKTEST path, asserted equal to the pure function for EVERY day), and a publish/read seam; the existing `regime_overlay.set_stressed/get_stressed/StressedCalendar` stay untouched so overlay results cannot move; (3) two hand-written condition classes (`SpyTrendRegimeCondition`, `SpyVolRegimeCondition`) reading the seam, registered as two new `ExpertEventType` members plus a small benchmark-field registry that `field_spec`/`field_codes` fall back to, so the GA decode (`strategy_param_space`), trigger catalog and rule converters work without a market-condition PROFILE/manifest; (4) launcher flag + leaves appended LAST in each option entry rule; (5) backtest handler builds and the engine publishes the calendar per bar (and REFUSES when a gated run has none); (6) live `TradeManager` publishes the regime from FINISHED SPY sessions before entry and exit passes.

**Tech Stack:** Python 3.11, pytest, pydantic rule models, `ba2_common` package (`packages/common`), testplatform backend (`testplatform/backend`), NiceGUI trade app. Test commands (one pytest process at a time; see memory "Worktree test-running quirks"):
- packages/common: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/common/tests/<file> -q` from the worktree root
- testplatform backend: `cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/<file> -q`
- root `tests/` (trade app): `.venv\Scripts\python.exe -m pytest tests/<file> -q` from the worktree root (if the worktree has no `.venv`, use the main clone's interpreter with `PYTHONPATH` pointing at the worktree)

**Investigation results this plan rests on (all read in the tree):**
- `MarketConditionCompare` (`packages/common/ba2_common/core/TradeConditions.py` L4213-4390) reads a per-INSTRUMENT feature row via `ctx.reader.observe(symbol, session)`; classes are generated per registered `FieldSpec`; categorical fields compare `==` against a float code (`OPERATORS_BY_KIND`, `market_conditions.py` L498-520). Its fields come from `market_conditions.PROFILES`, which also drives `parse_profile_setting`, manifests, `served_fields`, the launcher's per-profile manifest demand and the warm tools. Putting a benchmark pseudo-profile into `PROFILES` would drag in manifests and coverage checks that make no sense for a market-wide value. So: separate benchmark registry, NOT a profile.
- Registry consumers that must learn the new names (grep-verified): `strategy_param_space.py` L82/L260/L894 (`field_spec`, `field_codes` -> GA mode gene + decode), `market_condition_templates.py` L15/L39/L75, `rule_builders.FIELD_EVENT` (generated from PROFILES at import; a name missing there is silently DROPPED by `triggers_from_condition_tree`), `TradeConditions.CONDITION_MAP`, `market_condition_rules.STRICT_FIELD_NAMES`/`market_condition_fields()`/`served_fields()` (an unserved field is refused at deploy), `trigger_catalog.py` (kind/codes/profile requirement), `ExpertEventType` (`types.py` L594-624).
- Backtest: `daily_backtest_handler._build_regime_calendar` (L729) builds `StressedCalendar` from the run's `raw_ohlcv` (alpaca or fmp, the job stamp) with `cached_only=True`, memoised on `(benchmark, start, end)` -- NOTE the memo key has no history source (latent; the new memo includes it). `daily_engine.py` L819 publishes `set_stressed(calendar.at(price.scan_cutoff_date(as_of_dt)))`; `_check_regime_calendar` (L659) refuses an overlay-enabled run without a calendar. `run_daily_backtest` passes the calendar at L1089.
- Live: `TradeManager._publish_market_regime` (L246) is called ONLY from `refresh_accounts` (L334), reads `get_provider("ohlcv","fmp")` hard-coded with `end_date=now` (may include a partial TODAY bar after the open) and caches per calendar day. Entry rules are evaluated in `_process_expert_recommendations_after_analysis` (L2505) and exits in `process_open_positions_recommendations` (L3351), neither of which publishes. The live app has NO history-source setting yet (alpaca-history plan Phase 2).
- Launcher: `_option_entry_rule` (L6108) appends `*_market_condition_gates(m)` LAST in the AND list ("appending keeps every existing leaf at its current index"); append the new leaves after that. Smoke mode (`--gates-off`) strips any leaf with `toggle_optimize` or `mode_optimize`, so the new mode leaves are stripped too, as intended.
- `tools/run_options_matrix.py`: optional flags are forwarded in `build_cmd` only when given; `discovery_name` digests the built command, so a forwarded token enters the identity; boolean flags need listing in the `("--fitness-trade-scale", ...)` tuple of `discovery_name` (mirror commit `82d4e074`, tests in `testplatform/backend/tests/test_run_options_matrix_history_source.py`).

**Mode/code tables (a PERSISTED contract: the GA choice-gene index follows code order; never reorder, only append):**
```
spy_trend_regime : up=1 pullback=2 bear_rally=3 down=4 above_200=5 below_200=6
spy_vol_regime   : calm=1 normal=2 stressed=3 not_stressed=4 not_calm=5
```
Gene keys: `cond:<member>-spy-trend:mode`, `cond:<member>-spy-vol:mode` (choices `["off", *modes]`). Leaf ids `<member>-spy-trend`, `<member>-spy-vol`; ExpertEventType values/field names `spy_trend_regime`, `spy_vol_regime`.

---

## Task 0: Golden baseline BEFORE any change (opt-in guarantee)

**Files:**
- Create: `testplatform/backend/tests/fixtures/spy_regime_golden_baseline.json` (generated once, on the untouched branch)
- Create: `testplatform/backend/tests/test_spy_regime_opt_in_golden.py`

**Step 1 -- generator + test (passes on the baseline, must keep passing):**
```python
"""Without --benchmark-regime-gates nothing about an option job moves (rules, gene keys, name digest)."""
import hashlib, importlib.util, json, os
import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "spy_regime_golden_baseline.json")
_STRATS = ["O_BULLPS", "O_CSP", "O_LC", "O_IC", "O_WHEEL", "OS1"]   # single, composite, neutral, group


def _launcher():
    spec = importlib.util.spec_from_file_location("l_spy_golden", os.path.join(_ROOT, "testplatform", "ba2test_launcher.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _snapshot(kind):
    L = _launcher()
    s = L._build_strategy(kind) if hasattr(L, "_build_strategy") else None   # use the real builder entry (see Step 2)
    rules = json.dumps(s.entry_rules, sort_keys=True, default=str)
    from app.services.strategy_param_space import collect_param_space   # name verified in Step 2
    genes = sorted(collect_param_space(s))
    return {"rules_sha": hashlib.sha256(rules.encode()).hexdigest(), "genes": genes}


@pytest.mark.parametrize("kind", _STRATS)
def test_existing_jobs_unchanged_without_the_flag(kind):
    golden = json.load(open(_FIX))
    assert _snapshot(kind) == golden[kind]
```
**Step 2:** the two helper names above are placeholders: read `testplatform/backend/tests/test_launcher_market_condition_profile.py` (it already builds option strategies and collects gene names -- copy its exact calls) and use those. Add a `if __name__ == "__main__"` / `--regen` path that writes the fixture. Generate the fixture ON THE UNCHANGED BRANCH, then add a second test asserting `discovery_name(...)` for `O_BULLPS`/`O_CSP` with `_GRID_ARGV` (copy from `test_run_options_matrix_history_source.py`) equals the golden name stored in the same fixture.
**Run:** `cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_spy_regime_opt_in_golden.py -q` -> PASS (baseline).
**Commit:** `test(spy-regime): golden baseline of option rules/genes/job names (opt-in guarantee)`.
Never regenerate this fixture in a later task; a failing golden test means the change leaked.

---

## Task 1: Pure 4-state trend classifier

**Files:** Modify `packages/common/ba2_common/core/market_regime.py` (append after `classify_trend_regime`, ~L119); Modify `packages/common/tests/test_market_regime.py`.

**Step 1 -- failing tests:**
```python
from ba2_common.core import market_regime as mr

def _ramp(n, start=100.0, step=0.1):
    return [start + step * i for i in range(n)]

def test_trend_state_up_pullback_bear_rally_down():
    up = _ramp(260)                                   # above SMA200, SMA50 rising
    assert mr.classify_trend_state(up)["state"] == mr.TREND_UP
    down = list(reversed(_ramp(260)))                 # below SMA200, SMA50 falling
    assert mr.classify_trend_state(down)["state"] == mr.TREND_DOWN
    pull = _ramp(250) + [x for x in _ramp(250)[-1:] ] # build explicit shapes below
    # explicit shapes: long uptrend then 12 sessions of decline that stays above SMA200
    base = _ramp(300); pull = base + [base[-1] - 0.5 * (i + 1) for i in range(12)]
    assert mr.classify_trend_state(pull)["state"] == mr.TREND_PULLBACK
    # long downtrend then 12 sessions of bounce that stays below SMA200
    dbase = list(reversed(_ramp(300))); rally = dbase + [dbase[-1] + 0.5 * (i + 1) for i in range(12)]
    assert mr.classify_trend_state(rally)["state"] == mr.TREND_BEAR_RALLY

def test_trend_state_insufficient_history_is_none_with_reason():
    out = mr.classify_trend_state(_ramp(209))         # needs 200 closes for SMA200 AND 10 sessions of SMA50 history
    assert out["state"] is None and "insufficient" in out["reason"]
    assert mr.classify_trend_state(_ramp(210))["state"] is not None   # boundary: exactly enough

def test_trend_state_close_equal_sma200_is_below_like_classify_trend_regime():
    # price > sma strict in classify_trend_regime; the 4-state must agree on the 'above' axis
    closes = [100.0] * 260
    assert mr.classify_trend_regime(closes)["regime"] == mr.RISK_OFF
    assert mr.classify_trend_state(closes)["state"] in (mr.TREND_DOWN, mr.TREND_BEAR_RALLY)

def test_slope_exactly_zero_is_falling():            # spec: "> 0 = rising"
    closes = [100.0] * 260
    assert mr.classify_trend_state(closes)["slope_rising"] is False

def test_above_axis_matches_classify_trend_regime_every_prefix():
    import random
    r = random.Random(3); px = 100.0; closes = []
    for _ in range(500):
        px *= 1 + r.gauss(0, 0.01); closes.append(px)
    for i in range(209, 500):
        above = mr.classify_trend_state(closes[:i + 1])["state"] in (mr.TREND_UP, mr.TREND_PULLBACK)
        assert above == (mr.classify_trend_regime(closes[:i + 1])["regime"] == mr.RISK_ON)
```
(Clean the duplicated `pull =` scratch line when writing the file; keep only the explicit shapes.)
**Run:** `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/common/tests/test_market_regime.py -q` -> FAIL (`classify_trend_state` missing).

**Step 2 -- implement (minimal):** constants `TREND_UP="up"`, `TREND_PULLBACK="pullback"`, `TREND_BEAR_RALLY="bear_rally"`, `TREND_DOWN="down"`, `_SLOPE_SMA=50`, `_SLOPE_LAG=10`; `classify_trend_state(closes, sma_period=200, slope_sma=50, slope_lag=10) -> {"state","sma","price","sma50","sma50_lag","slope_rising","reason"}`. Needs `max(sma_period, slope_sma + slope_lag)` closes (=200; but the SMA50 of `t-10` needs 60, so the binding need is 200). Arithmetic MUST use the module's `_sma` on slices (`_sma(vals, 50)` and `_sma(vals[:-10], 50)`) and `price > sma200` strict, so Task 2's calendar can reproduce it bit for bit. Docstring: states the risk-regime caveat from the module docstring (this is an entry FILTER the GA searches, not a direction forecast).
**Run:** same -> PASS. **Commit:** `feat(regime): 4-state SPY trend classifier (SMA200 x SMA50 slope)`.

---

## Task 2: `benchmark_regime` module -- modes, value, pure classifier, calendar, seam

**Files:**
- Create: `packages/common/ba2_common/core/benchmark_regime.py`
- Create: `packages/common/tests/test_benchmark_regime.py`
- Leave `regime_overlay.py` UNCHANGED (back-compat is by not touching it; Task 9 publishes both).

**Contents of the module (spec):**
- `BENCHMARK_SYMBOL = "SPY"`; `TREND_FIELD = "spy_trend_regime"`, `VOL_FIELD = "spy_vol_regime"`.
- `TREND_MODE_CODES`, `VOL_MODE_CODES` (tables above, `OrderedDict`/dict in code order) and `MODE_STATES`: `{"above_200": {up,pullback}, "below_200": {bear_rally,down}, "not_stressed": {calm,normal}, "not_calm": {normal,stressed}, plus identity modes}`.
- `mode_for_code(field, code) -> str` (ValueError on unknown code), `state_matches(field, code, state) -> bool` (`state is None` -> False; callers decide the reason).
- `@dataclass(frozen=True) BenchmarkRegime(trend: Optional[str], vol: Optional[str], trend_reason: str = "", vol_reason: str = "", session: Optional[date] = None)`.
- `classify_benchmark_regime(closes, session=None) -> BenchmarkRegime`: PURE; calls `market_regime.classify_trend_state` and `classify_volatility_regime` on `closes` (oldest first, already cut at the knowable session). This is THE LIVE PATH.
- `knowable_closes(dates, closes, now_utc) -> (dates, closes)`: drops every row whose session has not FINISHED at `now_utc` using `market_calendar.regular_session_close_utc(session)` (so a partial TODAY bar after the open is dropped; today's bar is kept only once the session close has passed). Pure given the calendar; raises `ValueError` on non-increasing dates.
- `class BenchmarkRegimeCalendar(dates, closes)`: precomputes per-day `trend`/`vol` strings (None until enough history), `at(as_of) -> BenchmarkRegime` using the SAME bisect-on-date rule and `_as_date` coercion as `StressedCalendar.at`; `None` benchmark day before the first bar -> `BenchmarkRegime(None, None, reason)`. Same None-close filtering as the pure functions. Trend per day: `sum(vals[i-199:i+1])/200` and the two SMA50 slices with the SAME slice arithmetic as `_sma`; vol per day: copy `StressedCalendar._build`'s rank loop but keep the 3-way label (`rank < _CALM_PCTL` calm, `> _STRESSED_PCTL` stressed, else normal; the flat-series degenerate case -> normal), reusing `mr._realized_vol`, `mr._VOL_WINDOW`, `mr._VOL_RANK_LOOKBACK`.
- Seam (process-global, same rationale as `regime_overlay`): `set_benchmark_regime(r: Optional[BenchmarkRegime])`, `get_benchmark_regime()`, `reset_benchmark_regime()`, plus `set_benchmark_regime_failure(reason: Optional[str])` / `get_benchmark_regime_failure()` (live sets it on a fetch failure so the condition can say why; reset clears both). Default None = "nothing published".

**Step 1 -- failing tests (`test_benchmark_regime.py`):**
```python
import random, math
from datetime import date, timedelta
import pytest
from ba2_common.core import market_regime as mr
from ba2_common.core import benchmark_regime as br

def _series(n, seed=7):
    """Mirrors test_regime_overlay._synthetic_closes but with a drifting trend phase so ALL FOUR trend states and all
    THREE vol states occur."""
    rng = random.Random(seed); px = 100.0; d = date(2015, 1, 1); dates, closes = [], []
    for i in range(n):
        drift = 0.0012 if i < 400 else (-0.0015 if i < 800 else 0.0010)
        sigma = 0.035 if 600 <= i < 700 else 0.007
        px *= math.exp(rng.gauss(drift, sigma)); d += timedelta(days=1)
        dates.append(d); closes.append(px)
    return dates, closes

def test_calendar_equals_pure_classifier_every_day():
    dates, closes = _series(1100)
    cal = br.BenchmarkRegimeCalendar(dates, closes)
    for i, d in enumerate(dates):
        pure = br.classify_benchmark_regime(closes[:i + 1])
        got = cal.at(d)
        assert (got.trend, got.vol) == (pure.trend, pure.vol), f"diverged on day {i} ({d})"

def test_calendar_vol_stressed_equals_the_overlay_calendar():
    from ba2_common.core.regime_overlay import StressedCalendar
    dates, closes = _series(1100)
    new, old = br.BenchmarkRegimeCalendar(dates, closes), StressedCalendar(dates, closes)
    for d in dates:
        assert (new.at(d).vol == mr.STRESSED) == bool(old.at(d))

def test_sample_is_not_vacuous():
    dates, closes = _series(1100)
    cal = br.BenchmarkRegimeCalendar(dates, closes)
    assert {cal.at(d).trend for d in dates} >= {"up", "pullback", "bear_rally", "down", None}
    assert {cal.at(d).vol for d in dates} >= {"calm", "normal", "stressed", None}

def test_calendar_is_causal():
    dates, closes = _series(1100)
    full = br.BenchmarkRegimeCalendar(dates, closes)
    cut = br.BenchmarkRegimeCalendar(dates[:900], closes[:900])
    for d in dates[:900]:
        assert (full.at(d).trend, full.at(d).vol) == (cut.at(d).trend, cut.at(d).vol)

def test_calendar_holds_between_days_and_none_before_first():
    dates, closes = _series(700); cal = br.BenchmarkRegimeCalendar(dates, closes)
    assert cal.at(dates[0] - timedelta(days=5)).trend is None
    assert cal.at(dates[650] + timedelta(hours=7)) == cal.at(dates[650]) or cal.at(dates[650] + timedelta(hours=7)).trend == cal.at(dates[650]).trend

def test_modes_cover_exactly_the_spec_and_codes_are_stable():
    assert list(br.TREND_MODE_CODES) == ["up", "pullback", "bear_rally", "down", "above_200", "below_200"]
    assert list(br.TREND_MODE_CODES.values()) == [1, 2, 3, 4, 5, 6]
    assert list(br.VOL_MODE_CODES) == ["calm", "normal", "stressed", "not_stressed", "not_calm"]
    assert list(br.VOL_MODE_CODES.values()) == [1, 2, 3, 4, 5]

@pytest.mark.parametrize("mode,members", [("above_200", {"up", "pullback"}), ("below_200", {"bear_rally", "down"})])
def test_trend_set_modes_partition_the_states(mode, members):
    code = br.TREND_MODE_CODES[mode]
    for st in ("up", "pullback", "bear_rally", "down"):
        assert br.state_matches(br.TREND_FIELD, code, st) == (st in members)

def test_vol_set_modes():
    c = br.VOL_MODE_CODES
    for st, ns, nc in (("calm", True, False), ("normal", True, True), ("stressed", False, True)):
        assert br.state_matches(br.VOL_FIELD, c["not_stressed"], st) is ns
        assert br.state_matches(br.VOL_FIELD, c["not_calm"], st) is nc

def test_unknown_state_never_matches_any_mode():
    for code in br.TREND_MODE_CODES.values():
        assert br.state_matches(br.TREND_FIELD, code, None) is False

def test_unknown_code_raises():
    with pytest.raises(ValueError):
        br.mode_for_code(br.TREND_FIELD, 99)

def test_knowable_closes_drops_the_partial_today_bar():
    from datetime import datetime, timezone
    dates = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]   # Wed..Fri sessions
    closes = [1.0, 2.0, 3.0]
    during = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)            # 11:00 NY: Friday unfinished
    after = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc)            # 17:30 NY: finished
    assert br.knowable_closes(dates, closes, during)[1] == [1.0, 2.0]
    assert br.knowable_closes(dates, closes, after)[1] == [1.0, 2.0, 3.0]
    sat = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
    assert br.knowable_closes(dates, closes, sat)[1] == [1.0, 2.0, 3.0]

def test_seam_default_set_reset():
    br.reset_benchmark_regime(); assert br.get_benchmark_regime() is None
    r = br.BenchmarkRegime("up", "calm"); br.set_benchmark_regime(r); assert br.get_benchmark_regime() is r
    br.reset_benchmark_regime(); assert br.get_benchmark_regime() is None
```
(Tidy the one over-clever `or` assertion in `test_calendar_holds_between_days...` into a plain `.trend`/`.vol` equality when writing the file.)
**Run:** `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/common/tests/test_benchmark_regime.py -q` -> FAIL (module missing), implement, -> PASS. Also run `test_regime_overlay.py` and `test_market_regime.py` to prove they are untouched.
**Commit:** `feat(regime): benchmark regime calendar + pure classifier + seam (SPY gates, opt-in)`.

---

## Task 3: Benchmark field registry, event types, condition classes

**Files:**
- Modify `packages/common/ba2_common/core/market_conditions.py`: after `PROFILES` (L~650) add `BENCHMARK_FIELDS: Dict[str, FieldSpec]` built from two categorical `FieldSpec`s (`short="spy-trend"` / `"spy-vol"`, `searched=True`, `codes=` the Task 2 tables -- import the tables from `benchmark_regime` lazily or define the tables HERE and have `benchmark_regime` import them; pick ONE owner: define in `benchmark_regime.py` and build the specs in `market_conditions.py` with a function-local import to avoid a cycle). Make `field_spec`, `field_codes`, `_known_fields` fall back to `BENCHMARK_FIELDS` (so `strategy_param_space` needs NO change). `field_codes`'s lru cache clear paths already exist; benchmark specs are immutable.
- Modify `packages/common/ba2_common/core/types.py` (after L624): `N_SPY_TREND_REGIME = "spy_trend_regime"`, `N_SPY_VOL_REGIME = "spy_vol_regime"` with a comment pointing at this plan and "enum members cannot be added at runtime".
- Modify `packages/common/ba2_common/core/TradeConditions.py` (after `MarketConditionCompare` machinery, before `CONDITION_MAP`, L~4465): `class BenchmarkRegimeCompare(CompareCondition)` base + `SpyTrendRegimeCondition`, `SpyVolRegimeCondition`; add both to `CONDITION_MAP` explicitly (not via `register_market_condition_conditions`, which iterates PROFILES only).
- Modify `packages/common/ba2_common/core/rule_builders.py` (after `register_market_condition_field_events()`, L~125): `FIELD_EVENT["spy_trend_regime"]`/`["spy_vol_regime"]` for `BENCHMARK_FIELDS`.
- Modify `packages/common/ba2_common/core/market_condition_rules.py`: add both names to `STRICT_FIELD_NAMES` (L60) (a deployable name is permanent); `served_fields()` (L420) ALWAYS includes the benchmark names (they need no profile); `market_condition_fields()` keeps them (so exit-rule action restrictions, the `assert_market_conditions_resolved` mode guard and `assert_market_fields_mappable` apply to them exactly as to other market leaves).
- Modify `packages/common/ba2_common/core/trigger_catalog.py`: the two fields in the `market` category, kind `categorical`, codes via `categorical_codes_for` (reads `_market_field_specs()`; extend it to merge `BENCHMARK_FIELDS`), `requires_profile = None`, description line "SPY market regime, a CODE compared with '=='".
- Tests: Create `packages/common/tests/test_benchmark_regime_conditions.py`; extend `packages/common/tests/test_condition_registry_coverage.py` only if its enumeration needs the new members (run it first; it should pass with no edit because both mappings exist).

**Condition semantics (spec):**
- Constructor: `operator_str` must be `"=="` else `ValueError` (same message style as `MarketConditionCompare`); `value` must be a code in the field's table else `ValueError` (a deployed rule with a stale code must fail at construction, not read as "never passes").
- `evaluate()`: `r = get_benchmark_regime()`. `None` -> `_unknown("no_regime", "no benchmark regime is published for this decision: <why>")` where `<why>` comes from a second seam field `get_benchmark_regime_failure()` (set by live on fetch failure; the backtest never has it) else a generic sentence. `r.<axis>` is None -> `_unknown("insufficient_history", r.<axis>_reason)`. Else `calculated_value = float(code of the state)`, `last_status = "valid"`, result = `state_matches(...)`.
- `get_description()`: `"Check if the SPY <trend|volatility> regime is <mode> (<members>)"`. `get_actual_value_display()`: the state name (`"pullback"`), or `"unknown (<status>)"` -- never a bare number.
- NO `ctx`/reader/recorder involvement; no exception from here ever aborts an exit pass (there is nothing to raise except construction errors).

**Step 1 -- failing tests:**
```python
import pytest
from ba2_common.core import benchmark_regime as br
from ba2_common.core.types import ExpertEventType
from ba2_common.core.TradeConditions import CONDITION_MAP, SpyTrendRegimeCondition, SpyVolRegimeCondition
from ba2_common.core import rule_builders as rb
from ba2_common.core.market_conditions import field_codes, field_spec
from ba2_common.core.market_condition_rules import market_condition_fields, served_fields, STRICT_FIELD_NAMES

def _cond(cls, mode_code, op="=="):
    return cls(account=None, instrument_name="AAPL", expert_recommendation=None, operator_str=op, value=float(mode_code))

@pytest.fixture(autouse=True)
def _clean():
    br.reset_benchmark_regime(); yield; br.reset_benchmark_regime()

def test_registered_everywhere():
    assert CONDITION_MAP[ExpertEventType.N_SPY_TREND_REGIME] is SpyTrendRegimeCondition
    assert rb.FIELD_EVENT["spy_trend_regime"] is ExpertEventType.N_SPY_TREND_REGIME
    assert {"spy_trend_regime", "spy_vol_regime"} <= STRICT_FIELD_NAMES <= market_condition_fields()
    assert {"spy_trend_regime", "spy_vol_regime"} <= served_fields([])        # needs no profile
    assert dict(field_codes("spy_trend_regime")) == br.TREND_MODE_CODES
    assert field_spec("spy_vol_regime").kind == "categorical"

@pytest.mark.parametrize("op", ["<", ">", "!=", ">="])
def test_only_equality_is_accepted(op):
    with pytest.raises(ValueError):
        _cond(SpyTrendRegimeCondition, 1, op)

def test_unknown_mode_code_refused_at_construction():
    with pytest.raises(ValueError):
        _cond(SpyVolRegimeCondition, 99)

def test_nothing_published_never_passes_and_says_why():
    c = _cond(SpyTrendRegimeCondition, br.TREND_MODE_CODES["above_200"])
    assert c.evaluate() is False
    assert c.last_status == "no_regime" and "regime" in c.last_reason
    assert c.get_actual_value_display().startswith("unknown")

def test_unknown_axis_never_passes_and_carries_the_axis_reason():
    br.set_benchmark_regime(br.BenchmarkRegime(None, "calm", trend_reason="insufficient history (150 bars, need 200)"))
    c = _cond(SpyTrendRegimeCondition, br.TREND_MODE_CODES["below_200"])
    assert c.evaluate() is False and c.last_status == "insufficient_history" and "150 bars" in c.last_reason
    assert _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["calm"]).evaluate() is True    # other axis unaffected

@pytest.mark.parametrize("state,mode,expected", [
    ("up", "up", True), ("up", "pullback", False), ("pullback", "above_200", True),
    ("bear_rally", "below_200", True), ("down", "above_200", False)])
def test_trend_leaf_truth_table(state, mode, expected):
    br.set_benchmark_regime(br.BenchmarkRegime(state, "normal"))
    c = _cond(SpyTrendRegimeCondition, br.TREND_MODE_CODES[mode])
    assert c.evaluate() is expected and c.get_actual_value_display() == state

def test_vol_leaf_truth_table():
    br.set_benchmark_regime(br.BenchmarkRegime("up", "stressed"))
    assert _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["not_stressed"]).evaluate() is False
    assert _cond(SpyVolRegimeCondition, br.VOL_MODE_CODES["not_calm"]).evaluate() is True
```
**Run:** `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/common/tests/test_benchmark_regime_conditions.py packages/common/tests/test_condition_registry_coverage.py packages/common/tests/test_market_condition_conditions.py packages/common/tests/test_trigger_catalog.py packages/common/tests/test_market_condition_registry.py -q` -> new file FAIL first, then PASS; the four existing files must still pass (this is the "no PROFILES leakage" check: `test_market_condition_registry.py` enumerates PROFILES -- benchmark fields must NOT appear there).
**Commit:** `feat(conditions): spy_trend_regime / spy_vol_regime leaf conditions + registry fallback`.

---

## Task 4: Rule validation and converters accept the gate (entry), keep every refusal

**Files:** Modify `packages/common/ba2_common/core/market_condition_rules.py` only if Task 3's tests below fail; Create `packages/common/tests/test_benchmark_regime_rules.py`.

**Step 1 -- failing tests** (use the tree shape from `packages/common/tests/test_rules_import_market_gates.py`; copy its helper that builds an entry tree and runs `trade_rules_to_live_export`):
1. A decoded leaf `{"id":"o_csp-spy-trend","field":"spy_trend_regime","op":"==","comparison":"==","mode":"above_200","value":2.0}` survives `trade_rules_to_live_export` as a trigger `{"event_type":"spy_trend_regime","operator":"==","value":5.0}` -- NOT dropped (`rule_triggers_from_tree` raises when a market leaf is dropped, so the test just calls it).
2. An UNRESOLVED template leaf (`mode_optimize=True`, no `mode`) is refused by `assert_market_conditions_resolved` (existing guard; assert it fires for the new fields -- proves they are in `market_condition_fields()`).
3. `assert_market_fields_served(tree, profiles=[])` does NOT raise for a tree whose only market leaves are the benchmark ones, and DOES raise for an ohlcv leaf with empty profiles (unchanged behaviour).
4. A benchmark leaf in an exit ruleset obeys `assert_market_rule_actions` (allowed with `close`/`adjust_*`, refused with `stop_processing`) exactly like other market leaves.
5. A leaf placed under a nested OR group is refused (existing `_MARKET_LEAF_GROUP` rule) -- documents the "one leaf, never an OR tree" requirement.
**Run:** `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/common/tests/test_benchmark_regime_rules.py packages/common/tests/test_rules_import_market_gates.py packages/common/tests/test_rule_triggers_fail_open.py -q`.
**Commit:** `test(rules): benchmark gates survive the export/import converters and keep the market-leaf refusals`.

---

## Task 5: Template leaves (shared by launcher and any research driver)

**Files:** Modify `packages/common/ba2_common/core/market_condition_templates.py` (new function after `market_condition_leaves`); Create `packages/common/tests/test_benchmark_regime_templates.py`.

**Spec:** `benchmark_regime_leaves(prefix: str) -> list` returns, in this order, `[spy-trend leaf, spy-vol leaf]`:
`{"id": f"{prefix}-spy-trend", "field": "spy_trend_regime", "field_type": "numeric", "mode_optimize": True, "op": "==", "mode_choices": ["off", *TREND_MODE_CODES]}` (the same shape `market_condition_leaves` emits for a categorical spec -- reuse that branch via a helper to avoid a second copy of the leaf shape). No `optimize`, no `value`, no `toggle_optimize`.

**Failing tests:** shape equals `market_condition_leaves` categorical leaf for `structure_state` modulo id/field/choices; ids are `<prefix>-spy-trend`/`<prefix>-spy-vol`; `ConditionLeaf` validation accepts it (`from ba2_common.core.rule_models import ConditionLeaf; ConditionLeaf(**leaf)`); `strategy_param_space._collect_mode_gene` (import from `app.services` is testplatform-only -> put this assertion in the Task 8 test instead) -- here only the pydantic round trip.
**Run:** `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/common/tests/test_benchmark_regime_templates.py -q`. **Commit:** `feat(templates): benchmark_regime_leaves (spy-trend, spy-vol mode genes)`.

---

## Task 6: Launcher flag, gates in option entry rules, preflight

**Files:** Modify `testplatform/ba2test_launcher.py`; Create `testplatform/backend/tests/test_launcher_benchmark_regime_gates.py`.

**Changes (all additive; line refs on current file):**
1. Module global next to `_MARKET_CONDITION_PROFILES` (~L5226): `_BENCHMARK_REGIME_GATES: bool = False`, plus `_resolve_benchmark_regime_gates(raw, command)` (sets/returns it; mirror `_resolve_market_condition_mode`).
2. `_benchmark_regime_gates(m) -> list`: `[]` unless the global is on; else `benchmark_regime_leaves(m)`.
3. `_option_entry_rule` (L6108): append `*_benchmark_regime_gates(m)` AFTER `*_market_condition_gates(m)` (last in the AND list; comment: appending keeps every existing leaf index).
4. `_append_market_condition_gates` (O_CC/O_PP equity-entry overlays, ~L5297): NOT extended in this delivery (the gate is for the PURE option strategies in `_PURE_OPTION_STRATEGIES`; the flag is REFUSED for any other key, see 6).
5. `_rename_market_condition_gates` (O_WHEEL reuses the CSP builder, ~L5336): extend its id re-prefix to also rename `-spy-trend`/`-spy-vol` leaves (match on FIELD in `{spy_trend_regime, spy_vol_regime}`, as the existing function matches on field), so `o_wheel-spy-trend` exists and a bare CSP can differ.
6. `_apply_benchmark_regime(command, backtest_block, strat, kind)` called wherever `_apply_market_conditions` is called (grep `_apply_market_conditions(`): no-op returning `{}` when the flag is off (stored config UNCHANGED -- golden test); when on: `sys.exit` for `kind not in _PURE_OPTION_STRATEGIES`; requires `backtest_block["ohlcv_history_source"]`/the resolved `history_source` (so the SPY series source is the job stamp -- print it); writes `backtest_block["benchmark_regime_gates"] = True` and `backtest_block["benchmark_regime"] = {"symbol": "SPY", "history_source": <stamp>, "trend": "sma200+sma50slope10", "vol": "rv20-rank504", "genes": [...], "gene_count": n}`; prints one line `"<cmd>: benchmark regime gates on (SPY, <source>); N added genes; population and generations are NOT scaled"`.
7. CLI: `--benchmark-regime-gates` (`action="store_true"`) on `optimize` and `optimize-batch` (same parsers that take `--market-condition-profile`; grep `"--market-condition-profile"`), resolved next to `_resolve_market_condition_profiles`. Help text: opt-in, option strategies only, adds two mode genes per member, folds into the job identity in `run_options_matrix`.
8. Interaction rules (refuse loudly, `sys.exit`): with `--gates-off` the leaves are stripped by the existing filter (assert in test, no refusal); `--market-condition-mode all-off` does not touch these genes (document; the control arm for the SPY gates is "flag absent").

**Failing tests (`test_launcher_benchmark_regime_gates.py`, patterned on `test_launcher_market_condition_profile.py`):**
```python
def test_flag_off_rule_is_identical(...):                       # for O_BULLPS, O_CSP, OS1, O_WHEEL
    # build with flag off twice, once after a flag-on build in the same process: no state leakage
def test_flag_on_adds_exactly_two_leaves_last_per_member(...):
    ids = [leaf["id"] for leaf in entry_leaves("O_CSP")]
    assert ids[-2:] == ["o_csp-spy-trend", "o_csp-spy-vol"]
    assert ids[:-2] == baseline_ids("O_CSP")                    # appended, nothing moved
def test_group_members_get_their_own_genes(...):                # OS1: cond:<member>-spy-trend:mode for every member, distinct
def test_gene_names_and_choices(...):
    genes = collect_param_space(strategy)
    assert genes["cond:o_csp-spy-trend:mode"]["choices"] == ["off","up","pullback","bear_rally","down","above_200","below_200"]
    assert genes["cond:o_csp-spy-vol:mode"]["choices"] == ["off","calm","normal","stressed","not_stressed","not_calm"]
    assert not any(k.endswith("-spy-trend:value") for k in genes)       # categorical: no value gene
def test_gene_count_added_is_two_per_member(...):
def test_wheel_leaves_renamed(...):
def test_refused_for_non_option_kinds(...):                     # S1, O_CC, O_PP, O_STK -> SystemExit mentioning the kind
def test_gates_off_strips_them(...)
def test_decode_off_removes_leaf_and_nonoff_resolves_to_code(...):
    # decode a genome with mode=above_200 -> leaf has op "==", value 5.0, mode "above_200"; mode=off -> leaf removed
def test_apply_records_block_only_when_on(...):
```
Use the same fixtures/helpers the sibling test file uses for building a strategy and decoding (`strategy_param_space.decode`/`apply`). The decode assertion proves the GA path works with NO profile and NO manifest.
**Run:** `cd testplatform/backend && C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tests/test_launcher_benchmark_regime_gates.py tests/test_launcher_market_condition_profile.py tests/test_spy_regime_opt_in_golden.py tests/test_gene_to_artefact_audit.py -q`.
**Commit:** `feat(launcher): --benchmark-regime-gates adds spy-trend/spy-vol mode genes to option entries`.

---

## Task 7: `run_options_matrix` forwards the flag only when given (digest only when given)

**Files:** Modify `tools/run_options_matrix.py` (parser near L322; `build_cmd` boolean block near L610; `discovery_name` boolean tuple near L672); Create `testplatform/backend/tests/test_run_options_matrix_benchmark_regime.py` (copy the structure of `test_run_options_matrix_history_source.py`).

**Implementation:** `ap.add_argument("--benchmark-regime-gates", action="store_true", help=...)`; in `build_cmd`: `if getattr(args, "benchmark_regime_gates", False): cmd += ["--benchmark-regime-gates"]` (next to the Task 11 `--option-size-within-fill-volume` block); add `"--benchmark-regime-gates"` to the tuple of value-less flags in `discovery_name` (without it the parser would swallow the next token as the flag's value). Also check `tools/run_options_matrix.py` code that lists "gene counts"/budgets (grep `market_condition` in it, ~L264-270 and L453-530) and refuse the combination with anything incompatible.

**Failing tests:**
```python
def test_absent_forwards_nothing_and_keeps_the_name():  # == history-source test; ALSO compares to the golden name from Task 0
def test_given_is_forwarded_and_changes_the_name():
    legacy, _ = _name_and_cmd(mod); name, cmd = _name_and_cmd(mod, "--benchmark-regime-gates")
    assert "--benchmark-regime-gates" in cmd and name != legacy
def test_flag_does_not_swallow_the_next_token():        # build with flag + --ohlcv-history-source alpaca; both present, name stable across runs
def test_flag_and_market_profile_get_distinct_names():
```
**Run:** `cd testplatform/backend && ...python.exe -m pytest tests/test_run_options_matrix_benchmark_regime.py tests/test_run_options_matrix_history_source.py tests/test_option_matrix_market_condition_flag.py tests/test_stage1_run_sh.py -q`.
**Commit:** `feat(option-grids): stage-1 driver forwards --benchmark-regime-gates (only when given)`.

---

## Task 8: Trial-config pass-through (the whitelist trap)

**Files:** Modify `testplatform/backend/app/services/strategy_optimization_handler.py` (`_build_daily_trial_config`, L2777; add next to `**_market_condition_trial_pins(backtest_cfg)`, L~3183); Create `testplatform/backend/tests/test_benchmark_regime_trial_config.py`.

**Spec:** `"benchmark_regime_gates": bool(backtest_cfg.get("benchmark_regime_gates", False))` written ONLY when truthy (`**({...} if backtest_cfg.get("benchmark_regime_gates") else {})`) so trial configs of every existing run stay key-for-key identical (existing tests compare trial configs). The calendar itself is NOT shipped (workers build it from their local cache, like `StressedCalendar`). Verify the stored-row paths (`stored_row=True`: re-run, top-N persist, robustness variants, `_build_standalone_rerun_config`) get the key through the SAME function -- read the docstring at L2788 and `test_trial_config_whitelist*` tests; add the key to any second whitelist they reveal.

**Failing tests:**
```python
def test_flag_reaches_the_trial_config():
    cfg = _build_daily_trial_config({**BASE, "benchmark_regime_gates": True}, DECODED, option_trade_records=False)
    assert cfg["benchmark_regime_gates"] is True
def test_absent_flag_adds_no_key():
    assert "benchmark_regime_gates" not in _build_daily_trial_config(BASE, DECODED, option_trade_records=False)
def test_stored_row_rerun_keeps_the_flag():
    cfg = _build_daily_trial_config({**BASE, "benchmark_regime_gates": True}, DECODED, option_trade_records=False, stored_row=True)
    assert cfg["benchmark_regime_gates"] is True
```
(`BASE`/`DECODED`: copy the minimal fixtures from `test_strategy_optimization_handler_mode_anchor.py`.) Also add a gene-reach test: the launcher-built strategy's `cond:*-spy-*:mode` genes appear in the trial's decoded rules (guard against "knob parsed upstream but inert"; use the helper of `test_gene_to_artefact_audit.py`).
**Run:** `cd testplatform/backend && ...python.exe -m pytest tests/test_benchmark_regime_trial_config.py tests/test_strategy_optimization_handler_mode_anchor.py tests/test_gene_to_artefact_audit.py -q`.
**Commit:** `feat(optimizer): benchmark_regime_gates survives the trial-config whitelist`.

---

## Task 9: Backtest wiring -- calendar, per-bar publish, refusal

**Files:**
- Modify `testplatform/backend/app/services/backtest/daily_backtest_handler.py` (after `_build_regime_calendar`, L~770; call site L~1089)
- Modify `testplatform/backend/app/services/backtest/daily_engine.py` (ctor L531/L544; `_check_regime_calendar` L659; run init L708; per-bar L819; end-of-run reset L~1015)
- Create `testplatform/backend/tests/backtest/test_benchmark_regime_engine.py`

**Spec:**
1. Handler: factor the SPY daily read out of `_build_regime_calendar` into `_benchmark_daily_series(raw_ohlcv, start_date, end_date)` returning `(dates, closes)` or None (SAME `MemoizedOHLCVProvider(..., interval="1d", cached_only=True)` read, so the overlay calendar's inputs are byte-identical; keep `_build_regime_calendar`'s behaviour and memo untouched). New `_build_benchmark_regime_calendar(raw_ohlcv, start_date, end_date, history_source)` -> `BenchmarkRegimeCalendar | None`, memoised on `(BENCHMARK_SYMBOL, history_source, str(start), str(end))` (the source is in the key; note in a comment that the pre-existing overlay memo lacks it -- a worker that serves an fmp and an alpaca job in one process could reuse a stale calendar; fix THAT memo key in the same task with a regression test, since it is a one-line, behaviour-neutral change for single-source workers).
2. Handler call site: build the benchmark calendar ONLY when `config.get("benchmark_regime_gates")` OR any expert ruleset carries a benchmark leaf (`_config_uses_benchmark_regime(config)`: walk `experts[*]` rules / the seeded EventAction triggers for `event_type in {"spy_trend_regime","spy_vol_regime"}` -- copy `seam_wiring.market_condition_leaves_in` and its JSON-decoding handling), so a stored row re-run without the config key still gets the calendar and any other run pays nothing. Pass `benchmark_calendar=` to `DailyBacktestEngine`.
3. Engine: ctor kwarg `benchmark_calendar=None`; `_check_regime_calendar` also raises `RuntimeError` naming the expert instances when `self._uses_benchmark_regime` (computed once from the same config walk, passed from the handler as `benchmark_regime_required: bool`) and `self._benchmark_calendar is None` -- message mirrors the overlay one ("...no SPY daily history could be read from the <source> cache; the gates would read unknown everywhere and the GA would search dead genes. Pre-cache SPY daily bars and re-run"). Also refuse when the calendar exists but its first knowable regime is later than the run's first decision day by more than the warm-up (`calendar.at(first_day).trend is None`): the first N bars would be "unknown" -> log one WARNING with the first date a regime exists (do not refuse: warm-up is legitimate and unknown never passes), but the TOTAL absence is fatal.
4. Per bar (next to L819): `set_benchmark_regime(self._benchmark_calendar.at(self.price.scan_cutoff_date(as_of_dt)) if self._benchmark_calendar else None)` using the SAME `scan_cutoff_date` argument as `set_stressed` (daily clock: the bar's own date = decide on D's close; intraday clock: last session finished). End of run / start of run: `reset_benchmark_regime()` next to `reset_stressed()` (L708/L1015) so one GA trial cannot inherit another's bar.

**Failing tests (engine test, patterned on the existing overlay engine tests -- find with `grep -rl regime_calendar testplatform/backend/tests`):**
```python
def test_engine_refuses_gated_run_without_calendar():
    eng = make_engine(rules_with_leaf("spy_trend_regime"), benchmark_calendar=None, benchmark_regime_required=True)
    with pytest.raises(RuntimeError, match="benchmark regime"):
        eng.run()
def test_engine_without_gates_does_not_need_a_calendar():           # required=False, calendar None -> runs, seam stays None
def test_published_regime_is_the_knowable_session_daily_clock():    # bar D reads calendar.at(D)
def test_published_regime_intraday_uses_last_finished_session():    # 09:30 decision on D reads D-1's regime, not D (no same-day-close look-ahead)
def test_seam_reset_between_runs():                                 # a second run starts with get_benchmark_regime() is None
def test_overlay_calendar_still_published_identically():           # set_stressed values unchanged with and without gates
def test_handler_builds_calendar_only_when_needed(monkeypatch):    # counts _build_benchmark_regime_calendar calls: 0 for a plain run
def test_memo_key_includes_history_source():                        # two sources, same window -> two calendars
```
**Run:** `cd testplatform/backend && ...python.exe -m pytest tests/backtest/test_benchmark_regime_engine.py tests/backtest/test_market_condition_intraday_clock.py tests/backtest/test_market_condition_all_off_matches_baseline.py -q` plus the existing overlay engine tests found above.
**Commit:** `feat(backtest): publish the SPY benchmark regime per bar; refuse gated runs without SPY history`.

---

## Task 10: Live wiring -- publish from FINISHED SPY sessions before entry and exit passes

**Files:**
- Modify `ba2_trade_platform/core/TradeManager.py` (new `_publish_benchmark_regime`, near `_publish_market_regime` L246; calls at the top of `_process_expert_recommendations_after_analysis` L2505 and `process_open_positions_recommendations` L3351, and in `refresh_accounts` L334 after `_publish_market_regime()`)
- Create `tests/test_trademanager_benchmark_regime.py` (root suite)

**Behaviour (spec):**
1. `_publish_benchmark_regime(now=None)`: `now` injectable for tests (UTC). Fetch SPY daily bars (>= 1200 calendar days, same window as the overlay path) via the history provider (see "Source" below), call `knowable_closes(dates, closes, now)` (drops an unfinished TODAY bar; the backtest at D's close / at the 09:30 decision of D+1 sees exactly the same last close), `classify_benchmark_regime(closes, session=last_date)`, `set_benchmark_regime(...)`.
2. Cache key = the newest FINISHED session date (NOT the calendar day): after 16:00 NY the key changes and the new close is picked up the same evening; before it the cached value is reused. A successful classification is cached under that key; any non-None axis counts as success only if BOTH axes are classified or the reason is "insufficient history" (both are legitimate answers and are published as such).
3. FAILURE (exception, no bars, empty after `knowable_closes`, stale: last finished session older than the previous NYSE session -> data lag): publish NOTHING usable -- `set_benchmark_regime(None)` plus `set_benchmark_regime_failure(reason)`; `logger.error(..., exc_info=True)` inside the `except`, one ERROR per failure episode and WARNING for repeats; retry on the next call once `_REGIME_RETRY_SECONDS` (300 s) elapsed; never cached (same contract as `_publish_market_regime`). The gate then reads `no_regime` -> never passes -> the sleeve does not enter, which is loud (ERROR) not silent. Staleness check matters: FMP serving yesterday-1 would otherwise classify off an old close and DIFFER from the backtest.
4. Never raises into the caller (entry/exit pass must continue), but `process_open_positions_recommendations` and entry pass are the only consumers, so a failure there only blocks gated entries (and gated safe-action exit rules read unknown = do not fire).
5. Only fetch when needed: skip the whole thing (no network) unless some enabled expert's rules carry a benchmark leaf OR the platform setting is on -- use `market_condition_fields_in_ruleset`-style detection (see `market_condition_live.py` L412-430; extend it to understand the two benchmark names, or add `benchmark_leaves_in_ruleset(ruleset_id)`), memoised per expert instance. A platform with no gated rulesets must make ZERO new provider calls (test with a call-counting fake provider).
6. Source (DECISION NEEDED, see Open question 1): default and only implemented source today is the live app's history provider `get_provider("ohlcv", "fmp")`, exactly what `_publish_market_regime` uses; factor the fetch into `_fetch_benchmark_daily(now)` so the alpaca-history Phase 2 switch is one line. The deploy payload records the job's `ohlcv_history_source` (Task 12) and the importer WARNS when it differs from the source live will use.

**Failing tests (`tests/test_trademanager_benchmark_regime.py`, fake provider returning a DataFrame with `Date`/`Close`):**
```python
def test_unfinished_today_bar_is_not_used(): # now = 11:00 NY on D; df has a D row; published regime == classify(closes[:-1])
def test_finished_today_bar_is_used_after_close(): # now = 17:00 NY
def test_cache_is_keyed_by_newest_finished_session(): # two calls same session -> 1 fetch; call after close -> 2nd fetch
def test_failure_publishes_none_logs_error_and_retries(caplog): # provider raises -> get_benchmark_regime() is None, ERROR logged,
        # second call within 300 s makes no fetch, call after 300 s retries; recovery publishes and clears the failure
def test_stale_data_is_a_failure(): # last bar 3 sessions old -> failure, not a silently old regime
def test_no_gated_rulesets_means_no_fetch():
def test_insufficient_history_is_published_as_unknown_with_reason(): # 150 bars
def test_entry_and_exit_passes_publish_first(monkeypatch): # _process_expert_recommendations_after_analysis / process_open_positions_recommendations call the publisher before rule evaluation
```
**Run:** `.venv\Scripts\python.exe -m pytest tests/test_trademanager_benchmark_regime.py tests/test_market_condition_live_context.py tests/test_market_exit_live_parity.py -q`.
**Commit:** `feat(live): publish the SPY benchmark regime from finished sessions before entry/exit passes`.

---

## Task 11: BT-vs-live parity test (same closes -> same leaf result)

**Files:** Create `testplatform/backend/tests/test_benchmark_regime_bt_live_parity.py` (testplatform backend can import `ba2_trade_platform`? If not, put the live half in the root `tests/` file from Task 10 and keep this file BT-only plus the shared pure function; decide at implementation by trying the import).

**The invariant ("one shared function, two callers"):** BT publishes `calendar.at(scan_cutoff_date(as_of))`; live publishes `classify_benchmark_regime(knowable_closes(...))`. Test over a real-shaped series (use the cached SPY parquet if present, else the synthetic `_series` from Task 2, `pytest.skip` never -- the synthetic must always run):
```python
@pytest.mark.parametrize("cls,field", [(SpyTrendRegimeCondition, br.TREND_FIELD), (SpyVolRegimeCondition, br.VOL_FIELD)])
def test_every_mode_every_day_same_result_bt_and_live(cls, field):
    dates, closes = _series(1100); cal = br.BenchmarkRegimeCalendar(dates, closes)
    for i in range(0, len(dates), 7):                       # stride keeps it fast; Task 2 covers every day
        br.set_benchmark_regime(cal.at(dates[i]))           # BT path
        bt = {m: _cond(cls, c).evaluate() for m, c in codes(field).items()}
        br.set_benchmark_regime(br.classify_benchmark_regime(closes[:i + 1]))   # live path
        live = {m: _cond(cls, c).evaluate() for m, c in codes(field).items()}
        assert bt == live, (i, dates[i])

def test_intraday_decision_sees_the_same_close_as_a_live_run_at_that_instant():
    # Decision 2026-10-23 09:30 NY: BT intraday clock -> price_source.scan_cutoff_date -> 2026-10-22;
    # live now=09:30 NY -> knowable_closes drops 10-23 -> last close 10-22. Assert both give the same BenchmarkRegime.
    # Decision 2026-10-23 16:05 NY: both include 10-23. Half-day (2026-11-27 13:00 close) and holiday (2026-11-26) cases.
def test_weekend_and_holiday_decisions_use_last_session():
```
For the BT half call `PriceSource.scan_cutoff_date` (or `_scan_cutoff`) on a real intraday `PriceSource` fixture from `test_market_condition_intraday_clock.py`.
**Run:** `cd testplatform/backend && ...python.exe -m pytest tests/test_benchmark_regime_bt_live_parity.py -q`. **Commit:** `test(parity): SPY regime leaf gives the same answer in backtest and live for the same closes`.

---

## Task 12: Deploy parity / export / import keeps the gate

**Files (inspect first, then modify only what the new tests prove is needed):** `packages/common/ba2_common/core/deploy_parity.py`, `packages/common/ba2_common/core/rules_export_import.py` (`_assert_market_gates_served_by_linked_experts` L359), `packages/common/ba2_common/core/market_condition_live.py` (`assert_market_leaves_served_by_linked_experts`, `market_condition_fields_in_ruleset` L412), `tools/export_*deploy_payload*.py` / `tools/import_*deploy_payload*.py` (find with `grep -rl "market_condition" tools | grep -i deploy`), `testplatform/backend/tests/test_deploy_payload_market_conditions.py` as the model, Create `testplatform/backend/tests/test_deploy_payload_benchmark_regime.py`.

**Spec / tests:**
1. Export of a genome with `cond:o_csp-spy-trend:mode = above_200` produces an entry ruleset trigger `spy_trend_regime == 5.0` (NOT dropped, NOT left as a template); genome with mode `off` produces no such trigger; payload JSON round-trips.
2. Import onto an expert with `market_condition_profile == ''` SUCCEEDS (benchmark leaves need no profile), and an ohlcv leaf with the same empty setting still FAILS (existing refusal intact).
3. Import onto a server whose `ba2_common` predates the field refuses (STRICT name -> `assert_market_fields_mappable`), proved by monkeypatching `FIELD_EVENT` without the names.
4. The payload carries `benchmark_regime: {"symbol": "SPY", "history_source": "<job stamp>"}` when the backtest block had `benchmark_regime_gates`; the importer logs a WARNING (not a refusal) when `history_source` differs from the live source (fmp today). Add the fact to `BacktestRunFacts` / `deploy_parity` the same way `ohlcv_history_source` is handled (read how it is carried; do not invent a second mechanism).
5. Deployed-rule parity: build the live `EventAction` from the exported trigger via `TradeActionEvaluator`/condition factory (the code path at `TradeConditions.py` L4580 `condition_class = CONDITION_MAP.get(event_type)`), publish a regime on the seam and assert it evaluates like the BT leaf.
**Run:** `cd testplatform/backend && ...python.exe -m pytest tests/test_deploy_payload_benchmark_regime.py tests/test_deploy_payload_market_conditions.py tests/test_deploy_decision_time_gene.py -q` and `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest packages/common/tests/test_rules_import_market_gates.py -q`.
**Commit:** `feat(deploy): benchmark regime gates export/import with the genome (no profile needed)`.

---

## Task 13: Source-disagreement measurement (investigation, not a pytest)

**Files:** Create `test_files/probe_spy_regime_source_disagreement.py` (ad-hoc, NOT collected by pytest).
Reads SPY daily from `FMPOHLCVProvider` and `AlpacaHistoryOHLCVProvider` caches (cache-only), builds `BenchmarkRegimeCalendar` for each over 2020-01-01..2026-10-23, prints: days compared, days where trend differs, days where vol differs, longest run of disagreement, and the max abs close difference. Set `logging.disable` (memory: standalone scripts are 10x slower otherwise). Record the numbers in Open question 1 of this file. If trend disagreement > ~1% of days, escalate (the SMA200 boundary is sensitive to a 1-cent split/dividend-basis difference). **Commit:** `chore(probe): SPY regime fmp-vs-alpaca disagreement`.

---

## Task 14: UI / docs surface

**Files:** Modify `ba2_trade_platform/ui/pages/settings.py` only if the rule editor lists market fields by profile (it imports `market_condition_fields`, L38 -- verify the benchmark names do not trip the "needs profile" note or the UI guard `tests/test_market_condition_ui_guard.py`); Modify `packages/common/ba2_common/core/rules_documentation.py` (documentation dict entries for the two events; there is a test requiring every `ExpertEventType` to be documented -- run `grep -rn "rules_documentation" packages/common/tests tests | head`); add the two events' `get_actual_value_display` strings to whatever the activity log shows (nothing to do if it calls the condition method).
**Tests:** extend `tests/test_market_condition_ui_guard.py` and `tests/test_rule_trigger_picker.py` (categorical picker offers the six/five modes, `==` only); `packages/common/tests/test_trigger_catalog.py` (entries present in category `market`, kind `categorical`, no required profile).
**Run:** `.venv\Scripts\python.exe -m pytest tests/test_market_condition_ui_guard.py tests/test_rule_trigger_picker.py tests/test_ruleset_rule_picker.py -q`.
**Commit:** `feat(ui): SPY regime triggers in the rule editor`.

---

## Task 15: Versioning (per CLAUDE.md) -- LAST commit on the branch

Current values: `PACKAGE_VERSION` ba2_common `2026.10.00014`, `TEST_APP_VERSION 2026.09.0149`, `APP_VERSION 2026.09.1248`, `REQUIRED_PACKAGE_VERSIONS = {ba2_common 2026.10.00014, ba2_providers 2026.10.00015, ba2_experts 2026.10.00005}`. (Re-read them at execution time; dev may have moved -- bump from the CURRENT value.)
1. `packages/common/ba2_common/version.py` -> `2026.10.00015`; same string as `version` in `packages/common/pyproject.toml`.
2. GA-affecting? The change cannot alter any job without the flag, BUT it touches `ba2_common` code that backtests import and a worker below this version cannot decode a job that carries the flag (it would drop the leaf or fail at `field_spec`). Therefore RAISE: `testplatform/required_package_versions.py` `"ba2_common": "2026.10.00015"` (no TEST bump needed for that alone). Do not add a neutral glob.
3. `testplatform/version.py` -> `2026.09.0150` (testplatform files changed: launcher, handlers, engine).
4. `ba2_trade_platform/version.py` -> `2026.09.1249` (TradeManager, UI).
5. Run `python tools/check_package_versions.py --base origin/dev --include-worktree` (use the test venv interpreter) -> clean; then the unit test that wraps it: `C:\Users\basti\ba2-venvs\test\Scripts\python.exe -m pytest tools/ -k package_versions -q` (find the actual test with `grep -rl check_package_versions tests testplatform/backend/tests packages/common/tests`).
6. Full regression sweep, one pytest at a time: `packages/common/tests` (test venv), `testplatform/backend` (`tests/`), root `tests/` (app venv). Compare failures against a baseline run on `82d4e074` (memory: backend suite has pre-existing untriaged failures; a failure is only ours if it also passes on the baseline).
7. **Commit** `chore: SPY regime gate versions (ba2_common 00015 required, TEST 0150, APP 1249)`. **Do not push.** The merge checklist below applies when the owner schedules it.

**Merge-boundary checklist (CLAUDE.md "Boundary checklist"):** merge only between jobs; all masters (main clone, remote150 lanes, remote227) move together and long-lived `ba2-test serve` masters are restarted; push, then confirm `unsyncable_reason` is silent; check every worker's `GET /version` shows `ba2_common >= 2026.10.00015`; rollback only by reverting forward.

---

## Task 16: Stage-2 (O_STYLE) note -- documentation only, no code

The stage-2 O_STYLE expert lives on `origin/plan/option-stage2` (unmerged). When it merges, its member entry-rule builder must append `*_benchmark_regime_gates(m)` after its market-condition gates exactly as `_option_entry_rule` does, and its `_PURE_OPTION_STRATEGIES`-style membership must include the style members so `_apply_benchmark_regime` accepts them. Because the gate is registry- and template-driven (`benchmark_regime_leaves(prefix)`), nothing in `ba2_common` changes. Add one line to the stage-2 plan doc after both are on dev.

---

## Open questions / risks

1. **Live SPY source parity (DECISION NEEDED).** Backtests default to `alpaca` history (job stamp); live has no history-source setting and reads FMP (the overlay path hard-codes `get_provider("ohlcv","fmp")`). The SMA200/SMA50-slope sign and the 504-day vol percentile can flip on small close differences. Proposal: (a) implement live on FMP now (Task 10), (b) measure disagreement (Task 13) before the first deploy, (c) when alpaca-history Phase 2 lands, read the job's stamped source for the benchmark (the payload already records it, Task 12), (d) until then the importer WARNS on a mismatch. Alternative: a setting `benchmark_regime_history_source` per expert. Needs an owner call if Task 13 shows > ~1% differing days.
2. **Partial / stale live bars.** `knowable_closes` drops an unfinished TODAY bar and Task 10 treats a lagging provider as a failure. The existing overlay path (`end_date=now`, no such filter) can classify off an intraday bar -- a latent live-vs-backtest difference for the OVERLAY that this plan deliberately does not touch (changing it would move overlay behaviour live). Flag for a follow-up.
3. **5-minute decision clocks.** On an intraday clock the regime is the newest FINISHED session (`scan_cutoff_date`), so a 09:30 decision sees yesterday's close. SPY intraday bars are never used; the regime does not update within the day by design (matches the overlay).
4. **Holidays / half days / weekends.** Handled by `market_calendar.regular_session_close_utc` and the BT `_finished_session_end`; Task 11 pins Friday close, weekend, the 2026-11-26 holiday and the 2026-11-27 13:00 half day. An exchange-calendar outage (`MarketCalendarUnavailable`) must read as a live failure (ERROR, unknown), not fall back to a guess.
5. **Warm-up.** Trend needs 200 closes (210 for a defined slope sample), vol needs 524; with the 1200-calendar-day lookback (~825 sessions) a 2020 start has regimes from the first decision, but a run starting inside the first ~2 years of the SPY cache reads `unknown` (never passes) for the early bars. The engine logs the first date a regime exists (Task 9); a gated job whose window starts before that is silently shorter in effective trading days -- consider refusing windows with > N% unknown days.
6. **Overlay memo key.** `_REGIME_CALENDAR_MEMO` lacks the history source (pre-existing). Task 9 fixes it for the new calendar and adds a regression test for the old one; confirm the owner is fine touching it (it is behaviour-neutral for single-source workers).
7. **Gene-space growth.** +2 categorical genes per member (OS1 groups: +2 x members). Budgeting/POP is not scaled (launcher prints it). Statistically, 6- and 5-way modes give the GA more ways to overfit one SPY path (2020-2025 holds only a handful of independent bear/stressed episodes -- see the `market_regime.py` docstring warning about n=8). Consider reporting concentration per regime in the grid report before trusting a winner.
8. **Set modes vs one leaf.** `above_200`/`below_200`/`not_*` are encoded as extra codes, not as an OR tree; any future mode must be a new CODE appended to the table (never a reorder). Nested-OR is refused by the live export (`_MARKET_LEAF_GROUP`), verified in Task 4.
9. **Exit rules.** The conditions are also valid on exit rules with the safe action set; `process_open_positions_recommendations` publishes first (Task 10). No launcher exit template is added (entry gates only).
10. **Process-global seam.** Same soundness argument as `regime_overlay` (one market, one regime; one GA trial per process). If a future live path evaluates rulesets for two different `now`s concurrently, the seam would need a ContextVar like `market_condition_live`.
