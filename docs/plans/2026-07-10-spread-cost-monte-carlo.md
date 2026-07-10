# Spread-Cost Monte Carlo Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a user stress-test how sensitive a saved backtest's edge is to bid-ask spread cost, via the existing Monte-Carlo robustness pass — without touching the GA optimization grid or the backtest engine's fill simulation.

**Architecture:** The backtest engine currently has NO spread cost model at all (only `commission_per_trade` + `slippage_bps`, applied at fill time in `backtest_account.py`). Rather than adding `spread_bps` as a real fill-time cost (which would require re-running full backtests per spread level — expensive, and would tempt the GA to "optimize around" a friction cost it shouldn't exploit), this plan adds spread as a **post-hoc, deterministic cost deduction** applied to a saved backtest's already-persisted per-trade `pnl_pct`, inside the existing pure-function `app/services/backtest/monte_carlo.py` module. This mirrors the module's existing `mc_jitter` approximation (works entirely off persisted trade percentages, no DB, no re-simulation, numpy only) and slots into the same `run_monte_carlo(trades, initial, years, cfg)` entrypoint the robustness API already calls inline (sub-second, no task queue).

Two pieces ride on one new primitive, `apply_spread_cost`:
1. **Baseline `spread_bps`** (single value) — folded into the existing `bootstrap`/`shuffle`/`jitter`/`drop_k` pipeline as a one-time cost haircut applied to every trade before those methods run, so "how robust is this strategy at a realistic 8bp spread" becomes a checkbox-adjacent number field.
2. **`spread_sweep_bps`** (list of values) — a new deterministic table (same shape/spirit as the existing `drop_k` table) showing `{spread_bps, annualized_return, max_drawdown, calmar}` at each level over the ORIGINAL (unresampled) trade sequence — "how does the edge degrade as spread widens, and where does it break."

**Tech Stack:** Python (numpy, no new deps), FastAPI/Pydantic (`app/api/backtests.py`), React/TypeScript (`RobustnessDialog.tsx`, `RobustnessResults.tsx`, `btApi.ts`).

---

## Design notes (read before starting)

**Why not model spread at the engine/fill level?** `backtest_account.py._slip()` already worsens fill price by `slippage_bps` in the trade direction. A real spread model would do the same (`spread_bps/2` each way) — straightforward to bolt on — but then "playing with spread" means re-running the FULL backtest per spread value, which is minutes per point instead of milliseconds, and (per the user's own steer) risks the GA learning to route around a friction cost it should instead be judged robust against. Keep it out of `backtest_account.py` and the GA gene space entirely for this plan.

**How the cost is computed per trade.** A persisted trade (see `results.py:_trade_row`, the exact shape every `Backtest.trades` row has) carries `entry_price`, `size`, `pnl`, `pnl_pct` — but NOT the account equity at entry, which `pnl_pct` (equity-relative) is normalized against. `monte_carlo.py` already reconstructs a synthetic equity path via `equity_path_from_trade_pcts` (sequential multiplicative compounding — the module's own documented approximation, see its docstring). Reuse that SAME reconstructed equity value as "equity at entry" for trade `i` (`path[i]`, the point BEFORE trade `i` is applied) to convert a price-space `spread_bps` into an equity-relative pct deduction:

```
notional_i          = entry_price_i * size_i
round_trip_cost_i   = notional_i * (spread_bps / 10_000.0) * 2      # cross the spread entering AND exiting
spread_pct_deduct_i = (round_trip_cost_i / equity_at_entry_i) * 100.0
adjusted_pnl_pct_i  = pnl_pct_i - spread_pct_deduct_i
```

This avoids dividing by `pnl` or `pnl_pct` (which breaks on scratch/zero trades) and stays consistent with the module's existing approximation philosophy — it's the same equity path the rest of the module already treats as ground truth.

**Trades with missing/zero `entry_price` or `size`.** Some engine paths may not populate these (e.g. very old rows). Treat missing as `0.0` (via `.get(..., 0.0) or 0.0`, matching the module's existing `t.get("pnl_pct") or 0.0` style) — a trade with zero notional gets zero spread deduction rather than crashing. This is a pre-existing data-quality concern, not something this plan needs to fix.

---

### Task 1: `apply_spread_cost` core primitive

**Files:**
- Modify: `testplatform/backend/app/services/backtest/monte_carlo.py`
- Test: `testplatform/backend/tests/backtest/test_monte_carlo.py`

**Step 1: Write the failing test**

Add to `tests/backtest/test_monte_carlo.py` (reuse the existing `_trades` helper, but note it needs `entry_price`/`size` too now — extend it rather than duplicating):

```python
def _trades_priced(rows):
    """rows: list of (pnl_pct, entry_price, size) tuples."""
    return [{"pnl_pct": p, "entry_price": ep, "size": sz,
             "exit_time": f"2023-0{1+i%9}-15T00:00:00"}
            for i, (p, ep, sz) in enumerate(rows)]

def test_apply_spread_cost_deducts_round_trip_bps_from_pnl_pct():
    from app.services.backtest.monte_carlo import apply_spread_cost
    # Single trade: $10,000 equity, notional = 100 * 100 = $10,000 (fully invested),
    # so round-trip cost at 10bps = 10_000 * 0.0010 * 2 = $20 = 0.20% of the $10,000 entry equity.
    trades = _trades_priced([(5.0, 100.0, 100.0)])
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=10.0)
    assert len(adjusted) == 1
    assert abs(adjusted[0] - (5.0 - 0.20)) < 1e-6

def test_apply_spread_cost_zero_bps_is_a_noop():
    from app.services.backtest.monte_carlo import apply_spread_cost
    trades = _trades_priced([(5.0, 100.0, 100.0), (-3.0, 50.0, 40.0)])
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=0.0)
    assert adjusted == [5.0, -3.0]

def test_apply_spread_cost_tolerates_missing_notional_fields():
    from app.services.backtest.monte_carlo import apply_spread_cost
    trades = [{"pnl_pct": 5.0, "exit_time": "2023-01-15T00:00:00"}]  # no entry_price/size
    adjusted = apply_spread_cost(trades, initial=10_000.0, spread_bps=10.0)
    assert adjusted == [5.0]  # zero notional -> zero deduction, no crash
```

**Step 2: Run test to verify it fails**

Run (from `testplatform/backend`): `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -k spread_cost -v`
(Use whichever venv the repo's `pytest.ini`/CLAUDE.md points at — same one every other backend test in this session ran under.)

Expected: FAIL with `ImportError: cannot import name 'apply_spread_cost'`.

**Step 3: Write minimal implementation**

Add to `monte_carlo.py`, directly after `equity_path_from_trade_pcts` (same "Equity path" section):

```python
def apply_spread_cost(trades: List[Dict[str, Any]], initial: float, spread_bps: float) -> List[float]:
    """Deduct an assumed round-trip bid-ask spread cost from each trade's equity-relative
    ``pnl_pct``, returning the ADJUSTED pct list (same length/order as ``trades``).

    ``spread_bps`` is crossed TWICE per trade (once entering, once exiting) — the standard
    round-trip convention. Converts the price-space cost to an equity-relative pct using the
    SAME sequential-compounding equity reconstruction ``equity_path_from_trade_pcts`` already
    uses elsewhere in this module (equity-at-entry for trade i = path[i], the point BEFORE
    trade i is applied) — see the module's approximation-philosophy docstring. A trade with
    missing/zero ``entry_price``/``size`` gets zero deduction rather than raising.

    ``spread_bps == 0`` is an exact no-op (returns the original pcts unchanged).
    """
    pcts = [float(t.get("pnl_pct") or 0.0) for t in trades]
    if not spread_bps:
        return pcts
    path = equity_path_from_trade_pcts(pcts, initial)  # path[i] = equity BEFORE trade i
    bps_frac = float(spread_bps) / 10_000.0
    out = []
    for i, t in enumerate(trades):
        notional = float(t.get("entry_price") or 0.0) * float(t.get("size") or 0.0)
        equity_at_entry = float(path[i])
        if notional <= 0 or equity_at_entry <= 0:
            out.append(pcts[i])
            continue
        round_trip_cost = notional * bps_frac * 2.0
        deduct_pct = (round_trip_cost / equity_at_entry) * 100.0
        out.append(pcts[i] - deduct_pct)
    return out
```

**Step 4: Run test to verify it passes**

Run: `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -k spread_cost -v`
Expected: 3 passed.

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/monte_carlo.py testplatform/backend/tests/backtest/test_monte_carlo.py
git commit -m "feat(robustness): add apply_spread_cost primitive to Monte Carlo module"
```

---

### Task 2: `spread_sweep` deterministic table

**Files:**
- Modify: `testplatform/backend/app/services/backtest/monte_carlo.py`
- Test: `testplatform/backend/tests/backtest/test_monte_carlo.py`

**Step 1: Write the failing test**

```python
def test_spread_sweep_returns_one_row_per_level_with_monotonic_degradation():
    from app.services.backtest.monte_carlo import spread_sweep
    # A run of alternating small wins/losses, priced so notional ~= equity (spread bites).
    trades = _trades_priced([(3.0, 100.0, 100.0), (-1.0, 100.0, 100.0)] * 20)
    rows = spread_sweep(trades, initial=10_000.0, years=3.0, spread_bps_list=[0, 10, 50])
    assert [r["spread_bps"] for r in rows] == [0, 10, 50]
    # Wider spread -> strictly worse (or equal) annualized_return, monotonically.
    ann = [r["annualized_return"] for r in rows]
    assert ann[0] >= ann[1] >= ann[2]

def test_spread_sweep_empty_list_returns_empty():
    from app.services.backtest.monte_carlo import spread_sweep
    trades = _trades_priced([(3.0, 100.0, 100.0)])
    assert spread_sweep(trades, initial=10_000.0, years=3.0, spread_bps_list=[]) == []
```

**Step 2: Run test to verify it fails**

Run: `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -k spread_sweep -v`
Expected: FAIL with `ImportError: cannot import name 'spread_sweep'`.

**Step 3: Write minimal implementation**

Add directly after `drop_k_best` (same "Drop-K best" section — this is the same "deterministic table over the original trade order" family):

```python
def spread_sweep(trades: List[Dict[str, Any]], initial: float, years: float,
                  spread_bps_list: List[float]) -> List[Dict[str, Any]]:
    """DETERMINISTIC table: for each ``spread_bps`` in ``spread_bps_list``, apply
    ``apply_spread_cost`` to the ORIGINAL (unresampled) trade order and report the resulting
    path metrics. Answers "how much of the edge survives as spread widens" — a curve, not a
    distribution (no randomness, mirrors ``drop_k_best``'s determinism).

    Returns one row per level: ``{"spread_bps": X, **_path_metrics(...)}``, in the SAME order
    as ``spread_bps_list``. Empty input -> empty output.
    """
    rows = []
    for bps in spread_bps_list:
        adjusted = apply_spread_cost(trades, initial, float(bps))
        path = equity_path_from_trade_pcts(adjusted, initial)
        rows.append({"spread_bps": float(bps), **_path_metrics(path, initial, years)})
    return rows
```

**Step 4: Run test to verify it passes**

Run: `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -k spread -v`
Expected: 5 passed (3 from Task 1 + 2 from this task).

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/monte_carlo.py testplatform/backend/tests/backtest/test_monte_carlo.py
git commit -m "feat(robustness): add spread_sweep deterministic table to Monte Carlo module"
```

---

### Task 3: Wire baseline `spread_bps` into `run_monte_carlo`'s existing pipeline

**Files:**
- Modify: `testplatform/backend/app/services/backtest/monte_carlo.py`
- Test: `testplatform/backend/tests/backtest/test_monte_carlo.py`

**Step 1: Write the failing test**

```python
def test_run_monte_carlo_spread_bps_haircuts_bootstrap_and_drop_k():
    from app.services.backtest.monte_carlo import run_monte_carlo
    trades = _trades_priced([(5.0, 100.0, 100.0), (-2.0, 100.0, 100.0), (4.0, 100.0, 100.0)] * 5)
    cfg_no_spread = {"methods": ["bootstrap"], "n_paths": 200, "seed": 1, "drop_k": [1]}
    cfg_spread = {**cfg_no_spread, "spread_bps": 20.0}
    r0 = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg_no_spread)
    r1 = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg_spread)
    # Same seed/paths -> spread strictly worsens the median annualized return.
    assert r1["methods"]["bootstrap"]["annualized_return"]["p50"] < r0["methods"]["bootstrap"]["annualized_return"]["p50"]
    # drop_k_best also runs over the spread-adjusted trades now.
    assert r1["drop_k"][0]["annualized_return"] < r0["drop_k"][0]["annualized_return"]

def test_run_monte_carlo_spread_bps_defaults_to_zero_noop():
    from app.services.backtest.monte_carlo import run_monte_carlo
    trades = _trades_priced([(5.0, 100.0, 100.0)] * 10)
    cfg = {"methods": ["bootstrap"], "n_paths": 50, "seed": 1, "drop_k": []}
    r = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg)  # no spread_bps key at all
    assert r["methods"]["bootstrap"]["n_paths"] == 50  # ran fine, no KeyError
```

**Step 2: Run test to verify it fails**

Run: `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -k run_monte_carlo_spread -v`
Expected: first test FAILs the assertion (spread_bps currently has no effect since `run_monte_carlo` never reads that key); second test currently PASSES already (harmless — confirms the no-op default before the change, keep it as a regression pin).

**Step 3: Write minimal implementation**

In `run_monte_carlo`, apply the spread haircut to `pcts` once, and feed the SAME adjusted trades into `drop_k_best` (which reads `pnl_pct` off the trade dicts, not off `pcts` directly — build a shallow-copied trades list with `pnl_pct` overwritten so `drop_k_best`'s existing signature/behavior is untouched):

```python
def run_monte_carlo(trades: List[Dict[str, Any]], initial: float, years: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    ...
    spread_bps = float(cfg.get("spread_bps") or 0.0)
    pcts = apply_spread_cost(trades, initial, spread_bps) if spread_bps else \
        [float(t.get("pnl_pct") or 0.0) for t in trades]
    # drop_k_best reads pnl_pct straight off each trade dict -- give it the SAME spread-adjusted
    # values via shallow copies (never mutate the caller's trades) so the "was it luck" table
    # reflects the same cost assumption as bootstrap/shuffle/jitter above.
    trades_for_drop_k = trades
    if spread_bps:
        trades_for_drop_k = [{**t, "pnl_pct": p} for t, p in zip(trades, pcts)]
    exit_dates = [t.get("exit_time") for t in trades]
    ...
```

Replace the existing `pcts = [float(t.get("pnl_pct") or 0.0) for t in trades]` line (module-level, near the top of the function body) with the block above, and change the `drop_k_rows` loop's `drop_k_best(trades, ...)` call to `drop_k_best(trades_for_drop_k, ...)`.

**Step 4: Run test to verify it passes**

Run: `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -v`
Expected: all tests in the file pass (existing + new).

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/monte_carlo.py testplatform/backend/tests/backtest/test_monte_carlo.py
git commit -m "feat(robustness): thread optional spread_bps haircut through run_monte_carlo"
```

---

### Task 4: Wire `spread_sweep_bps` into `run_monte_carlo`'s output

**Files:**
- Modify: `testplatform/backend/app/services/backtest/monte_carlo.py`
- Test: `testplatform/backend/tests/backtest/test_monte_carlo.py`

**Step 1: Write the failing test**

```python
def test_run_monte_carlo_includes_spread_sweep_table_when_configured():
    from app.services.backtest.monte_carlo import run_monte_carlo
    trades = _trades_priced([(5.0, 100.0, 100.0), (-2.0, 100.0, 100.0)] * 5)
    cfg = {"methods": [], "n_paths": 10, "seed": 1, "drop_k": [],
           "spread_sweep_bps": [0, 10, 25]}
    r = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg)
    assert [row["spread_bps"] for row in r["spread_sweep"]] == [0, 10, 25]

def test_run_monte_carlo_omits_spread_sweep_key_when_not_configured():
    from app.services.backtest.monte_carlo import run_monte_carlo
    trades = _trades_priced([(5.0, 100.0, 100.0)])
    cfg = {"methods": [], "n_paths": 10, "seed": 1, "drop_k": []}
    r = run_monte_carlo(trades, initial=10_000.0, years=3.0, cfg=cfg)
    assert r["spread_sweep"] == []  # present but empty, not a KeyError either way
```

**Step 2: Run test to verify it fails**

Run: `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -k spread_sweep_table -v`
Expected: FAIL — `KeyError: 'spread_sweep'` (the return dict doesn't have that key yet).

**Step 3: Write minimal implementation**

In `run_monte_carlo`'s `return` statement, add the new key (computed via Task 2's `spread_sweep`, over the ORIGINAL `trades`/`years` — the sweep is deliberately independent of the baseline `spread_bps` haircut from Task 3, so a user can see the degradation curve regardless of whether they've also set a baseline assumption):

```python
    return {
        "methods": out_methods,
        "drop_k": drop_k_rows,
        "spread_sweep": spread_sweep(trades, initial, years, cfg.get("spread_sweep_bps") or []),
        "n_trades": len(trades),
        "years": float(years),
    }
```

**Step 4: Run test to verify it passes**

Run: `../../.venv/Scripts/python.exe -m pytest tests/backtest/test_monte_carlo.py -v`
Expected: all pass.

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/backtest/monte_carlo.py testplatform/backend/tests/backtest/test_monte_carlo.py
git commit -m "feat(robustness): expose spread_sweep table in run_monte_carlo output"
```

---

### Task 5: API surface — `MonteCarloConfig` gains `spread_bps` / `spread_sweep_bps`

**Files:**
- Modify: `testplatform/backend/app/api/backtests.py:824-833` (`MonteCarloConfig`), `:892-899` (`mc_cfg` dict build)
- Test: `testplatform/backend/tests/test_robustness_api.py`

**Step 1: Read the existing test file's pattern first**

Run: `cat testplatform/backend/tests/test_robustness_api.py` (or open it) to match its fixture/client style before writing new assertions — every other task in this plan showed you the pattern inline, this one doesn't because the file wasn't read during planning; read it now rather than guessing the request-building helper's name.

**Step 2: Write the failing test**

Add a test (naming/fixtures matched to whatever the file already uses for POST `/api/backtests/robustness` with `monte_carlo.enabled=True`) asserting that a request body containing `"spread_bps": 15.0, "spread_sweep_bps": [0, 10, 30]` results in a `RobustnessRun.params` dict that carries both keys (query the created run via the existing GET route or DB session the file already uses), e.g.:

```python
def test_robustness_launch_threads_spread_config_into_run_params(...):
    # ... reuse whatever backtest-with-trades fixture the existing MC tests in this file use ...
    resp = client.post("/api/backtests/robustness", json={
        "backtest_ids": [bt.id],
        "monte_carlo": {"enabled": True, "methods": ["bootstrap"], "n_paths": 50, "seed": 1,
                         "drop_k": [], "jitter_bp": 0.0,
                         "spread_bps": 15.0, "spread_sweep_bps": [0, 10, 30]},
    })
    assert resp.status_code == 200
    run_id = resp.json()["runs"][0]["robustness_run_id"]
    run = db.query(RobustnessRun).get(run_id)  # or however the file already fetches rows
    assert run.params["spread_bps"] == 15.0
    assert run.params["spread_sweep_bps"] == [0, 10, 30]
    assert "spread_sweep" in run.results  # confirms it flowed through to monte_carlo.run_monte_carlo
```

**Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_robustness_api.py -k spread_config -v`
Expected: FAIL — Pydantic ignores unknown fields silently by default in this codebase's `BaseModel` usage UNLESS `extra="forbid"` is set (check `MonteCarloConfig`'s base — if it's plain `pydantic.BaseModel` with no `Config`, sending `spread_bps` today is silently dropped, not a 422), so the failure will show as `run.params` missing the keys, not a request error.

**Step 4: Write minimal implementation**

```python
class MonteCarloConfig(BaseModel):
    enabled: bool = False
    n_paths: int = 1000
    seed: int = 42
    methods: List[str] = ["bootstrap", "shuffle"]
    drop_k: List[int] = [1, 2, 3]
    jitter_bp: float = 0.0
    spread_bps: float = 0.0
    spread_sweep_bps: List[float] = []
```

And in `launch_robustness`'s `mc_cfg` dict build:

```python
            mc_cfg = {
                "n_paths": request.monte_carlo.n_paths,
                "seed": request.monte_carlo.seed,
                "methods": request.monte_carlo.methods,
                "drop_k": request.monte_carlo.drop_k,
                "jitter_bp": request.monte_carlo.jitter_bp,
                "spread_bps": request.monte_carlo.spread_bps,
                "spread_sweep_bps": request.monte_carlo.spread_sweep_bps,
            }
```

**Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_robustness_api.py -v`
Expected: all pass (existing + new).

**Step 6: Run the FULL backend suite** (this plan touches a shared API model — cheap insurance)

Run (from `testplatform/backend`): `../../.venv/Scripts/python.exe -m pytest -q`
Expected: same pass count as before this task, plus the new tests.

**Step 7: Commit**

```bash
git add testplatform/backend/app/api/backtests.py testplatform/backend/tests/test_robustness_api.py
git commit -m "feat(robustness): expose spread_bps/spread_sweep_bps on the robustness API"
```

---

### Task 6: Frontend — types + dialog controls + results table

**Files:**
- Modify: `testplatform/frontend/src/lib/btApi.ts:266-313`
- Modify: `testplatform/frontend/src/components/RobustnessDialog.tsx`
- Modify: `testplatform/frontend/src/components/RobustnessResults.tsx`
- Test: `testplatform/frontend/src/lib/btApi.test.ts` if one exists for this file (check first — `ls testplatform/frontend/src/lib/*.test.ts`); if none, skip a dedicated type-test (TS types have no runtime behavior to unit test) and rely on `tsc`/build as the verification step below.

**Step 1: Extend the TypeScript types**

In `btApi.ts`, extend `RobustnessRequestBody.monte_carlo`:

```ts
export interface RobustnessRequestBody {
  backtest_ids: number[];
  monte_carlo: {
    enabled: boolean;
    n_paths: number;
    seed: number;
    methods: string[];
    drop_k: number[];
    jitter_bp: number;
    spread_bps: number;
    spread_sweep_bps: number[];
  };
  schedule: { ... };  // unchanged
}
```

Add a new row type next to `McDropKRow` and thread it into `McResults`:

```ts
export interface McSpreadSweepRow {
  spread_bps: number;
  final_equity: number;
  annualized_return: number;
  max_drawdown: number;
  calmar: number;
}
export interface McResults {
  methods: Record<string, McMethodSummary>;
  drop_k: McDropKRow[];
  spread_sweep: McSpreadSweepRow[];
  n_trades: number;
  years: number;
}
```

**Step 2: Add dialog controls** (mirror the existing `jitterBp`/`dropKText` state + inputs in `RobustnessDialog.tsx`)

State additions (near the other MC state, `~line 42`):

```ts
const [spreadBps, setSpreadBps] = useState(0);
const [spreadSweepText, setSpreadSweepText] = useState('0,5,10,20,50');
```

Parsed value (near `parsedDropK`):

```ts
const parsedSpreadSweep = spreadSweepText
  .split(',')
  .map(s => parseFloat(s.trim()))
  .filter(n => Number.isFinite(n) && n >= 0);
```

Body wiring (in `handleSubmit`'s `monte_carlo` object):

```ts
spread_bps: spreadBps,
spread_sweep_bps: parsedSpreadSweep,
```

UI (new fields inside the existing `grid grid-cols-2 gap-3` block, right after the "Jitter σ (bp)" `<label>`):

```tsx
<label className="flex flex-col gap-1">
  <span className={label}>Spread (round-trip, bp)</span>
  <input type="number" min={0} step={1} value={spreadBps}
    onChange={e => setSpreadBps(Math.max(0, parseFloat(e.target.value) || 0))} className={inputClass} />
</label>
<label className="flex flex-col gap-1">
  <span className={label}>Spread sweep (bp, comma list)</span>
  <input type="text" value={spreadSweepText} placeholder="0,5,10,20,50"
    onChange={e => setSpreadSweepText(e.target.value)} className={inputClass} />
</label>
```

Update the explanatory `<p>` right below the grid to mention spread alongside jitter.

**Step 3: Render the sweep table** (mirror `DropKTable` in `RobustnessResults.tsx`)

```tsx
function SpreadSweepTable({ rows }: { rows: McSpreadSweepRow[] }) {
  if (!rows.length) return null;
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
      <h5 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Spread sensitivity (original trade order)
      </h5>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 dark:text-gray-400 text-right">
            <th className="text-left py-1">spread (bp)</th>
            <th className="py-1">ann return</th>
            <th className="py-1">max DD</th>
            <th className="py-1">calmar</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.spread_bps} className="border-t border-gray-100 dark:border-gray-700/50 text-right">
              <td className="text-left py-1 text-gray-700 dark:text-gray-300">{r.spread_bps}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{fmtPct(r.annualized_return)}</td>
              <td className="py-1 text-red-600 dark:text-red-400">{fmtPct(r.max_drawdown)}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{fmtNum(r.calmar)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

Import `McSpreadSweepRow` at the top of the file (next to the existing `McDropKRow` import), and add `<SpreadSweepTable rows={res.spread_sweep || []} />` right after the existing `<DropKTable rows={res.drop_k || []} />` line in `MonteCarloResults`.

**Step 4: Verify it builds**

Run (from `testplatform/frontend`): `npm run build` (or `npx tsc --noEmit` if the repo has that script — check `package.json` first).
Expected: no type errors.

**Step 5: Manual smoke test** (per CLAUDE.md: UI changes need a real browser check, not just a type-check)

Run the dev server, open a saved backtest with persisted trades, launch a robustness run with Monte-Carlo enabled + a non-empty spread sweep list, and confirm:
- The "Spread (round-trip, bp)" and "Spread sweep" fields appear and accept input.
- After launch, the results panel shows a "Spread sensitivity" table with one row per configured level, return/DD visibly degrading as spread increases.

**Step 6: Commit**

```bash
git add testplatform/frontend/src/lib/btApi.ts testplatform/frontend/src/components/RobustnessDialog.tsx testplatform/frontend/src/components/RobustnessResults.tsx
git commit -m "feat(robustness): spread-cost controls + sensitivity table in the robustness UI"
```

---

### Task 7: Docs + version bump

**Files:**
- Modify: `ba2_trade_platform/version.py`
- Modify: `docs/plans/2026-07-02-backtest-robustness-suite.md` (append a short note pointing at this plan — keep the original suite doc as the index of what robustness supports, don't duplicate content into it)

**Step 1:** Bump `APP_VERSION` by one build number (see `CLAUDE.md`'s versioning rule — check the current value first, don't assume).

**Step 2:** Add one paragraph to the original robustness-suite plan doc noting spread-cost sensitivity was added, with a link to this file's filename — future readers of that doc are the ones who'll go looking for "does robustness cover spread."

**Step 3: Commit**

```bash
git add ba2_trade_platform/version.py docs/plans/2026-07-02-backtest-robustness-suite.md
git commit -m "chore: version bump + docs pointer for spread-cost Monte Carlo"
```

---

## Out of scope (explicitly, so it isn't silently re-litigated mid-implementation)

- **No engine/fill-level `spread_bps`.** Not added to `backtest_account.py`, not a GA-optimizable gene, not part of `strategy_optimization_handler.py`'s gene space. This plan is entirely the post-hoc `monte_carlo.py` approximation.
- **No re-running of full backtests per spread level.** Everything in this plan operates on already-persisted `Backtest.trades` — sub-second, matching the existing Monte-Carlo run's cost profile (the API already runs MC inline in the request, not on a task queue).
- **`jitter` interaction left alone.** `spread_bps` and `jitter_bp` both haircut/perturb `pcts` but are independent knobs (spread is a deterministic haircut applied once before any method runs; jitter is per-path gaussian noise inside `mc_jitter`) — no attempt to model them as correlated in this plan.
