"""RESULTS-IDENTITY GUARD FOR AN OPTION KEY: a pinned fingerprint of an O_LEAP run.

WHAT THIS IS AND IS NOT
-----------------------
``test_equity_golden_run.py`` pins EQUITY results and carries a cross-check proving its value
is byte-identical to the pre-options reference tree. Option results have had no equivalent:
every option test asserts a BEHAVIOUR (the mark is answered on a barless day, the DTE exit
fires, the roll happens) and none pins the NUMBERS a finished option run produces. So an
option-side change that moved every premium by 1e-9, or shifted one exit by a bar, would pass
the whole suite.

This file closes that. It runs the O_LEAP chain -- the launcher's own emitted entry/exit rules,
a fixture options cache at the ~50% bar density design section 1 measured at LEAPS range, so
the Black-Scholes mark fallback is exercised on roughly half the bars -- through the REAL
``DailyBacktestEngine.run()``, and pins every round-trip at full float precision INCLUDING the
option-only columns (``contract_symbol``, ``underlying_symbol``, ``multiplier``) that the
equity fingerprint deliberately drops.

**THIS IS A NEW BASELINE, NOT A PROOF OF IDENTITY WITH PRE-BRANCH BEHAVIOUR.** The equity
golden could claim identity because the same harness was replayed against a pre-options
reference tree and compared. Nothing comparable is claimed here: the value committed alongside
this file is HEAD's, recorded on ``options-grid2`` at final review, and it says only "from here
on, this run's numbers do not move without someone saying so". In particular the branch's own
results-comparability note already records a BASELINE SPLIT on the Black-Scholes mark fallback,
so an option run on this branch is NOT expected to reproduce a pre-branch number and this
fingerprint must not be read as evidence that it does.

The fixture is reused from ``test_grid2_engine_paths.py`` rather than copied: one options-cache
fixture with one bar-density rationale, two consumers. A change to that fixture legitimately
moves this golden, and the regeneration path below is how that is recorded.

IF THIS TEST FAILS
------------------
Assume an option results regression until proven otherwise. Read the printed per-field diff,
explain the moved trades, and only then regenerate DELIBERATELY:

    BA2_REGEN_OPTION_GOLDEN=1 python -m pytest tests/backtest/test_option_golden_run.py

Run from the backend dir:
    python -m pytest tests/backtest/test_option_golden_run.py -v
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

# The precision convention is SHARED with the equity golden -- ``repr(float(x))``, never a
# rounded form -- so the two pins cannot silently drift apart on how a number is written down.
from tests.backtest.test_equity_golden_run import _diff_lines, _r, _ts

GOLDEN_PATH = Path(__file__).with_name("golden") / "option_leap_golden_run.json"

#: The floor that makes "identical" mean "identical AND still trading". The O_LEAP fixture
#: opens ONE LEAPS position and the DTE exit closes it, so one closed round-trip is the whole
#: run; an engine change that stopped submitting the entry would otherwise reproduce an empty
#: fingerprint and only fail once someone also regenerated the golden.
MIN_ROUND_TRIPS = 1

#: The DTE floor the ``opt_dte`` exit is set to. The fixture expiry starts at 416 DTE and the
#: run is long enough to walk it down through 300, so the exit FIRES INSIDE the run -- the close
#: path is pinned, not just the open. Once DTE is below 300 the entry rule's own 300-420 window
#: no longer admits the contract, so the run is one long-held round-trip rather than the
#: open/close/reopen churn a floor above the starting DTE produces.
_DTE_FLOOR = 300
_ACCOUNT_ID = 9401

_SYMBOL = "GOLDX"
_START = datetime(2024, 1, 2)
_END = datetime(2024, 7, 26)
_EXPIRY = _START.date() + timedelta(days=416)
_STRIKE = 80.0
_CONTRACT = "GOLDX250222C00080000"
_IV = 0.30
_DELTA = 0.80


def _trading_days() -> List[date]:
    d, out = _START.date(), []
    while d <= _END.date():
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


_DAYS = _trading_days()

#: ~50% BAR DENSITY in CONSECUTIVE PAIRS, the density design section 1 measured at LEAPS range.
#: Pairs, not alternate days: the ``next_bar_open`` fill model needs the bar AFTER the one the
#: order was placed on, and a strict every-other-day pattern gives it that exactly never. Half
#: the days carry no premium bar at all, so the mark falls through to Black-Scholes on those --
#: which is the point of pinning an OPTION golden rather than trusting the equity one.
_BAR_DAYS = [d for i, d in enumerate(_DAYS) if i % 4 in (0, 1)]


def _wave(n: int, base: float, amplitude: float, period: int) -> List[float]:
    """A deterministic triangular wave from exact rational arithmetic -- no RNG, no sin, no
    wall clock. Same construction as the equity golden's fixture, for the same reason: the
    fingerprint must depend on nothing outside this file."""
    half = period / 2.0
    out: List[float] = []
    for t in range(n):
        pos = t % period
        tri = pos / half if pos < half else (period - pos) / half
        out.append(round(base * (1.0 + amplitude * (tri - 0.5)), 4))
    return out


#: The UNDERLYING moves (12% peak-to-trough over a 22-day cycle). It feeds the Black-Scholes
#: fallback on every barless day, so a change anywhere in that pricing path moves the golden.
#: A flat underlying would leave the fallback returning one constant and pin nothing about it.
_UNDER_CLOSES = _wave(len(_DAYS), 100.0, 0.12, 22)
#: The PREMIUM moves on its own, slower cycle, so the round-trip carries real non-zero P&L and
#: the pin covers the fill/marking arithmetic rather than only the schedule. The period is 13
#: (coprime with the ~42-bar hold this fixture produces) ON PURPOSE: at period 14 the exit bar
#: landed on the wave's own zero phase, so entry and exit filled at the SAME premium and the
#: golden recorded pnl == 0.0 for every field -- a fingerprint that hashes stably while pinning
#: nothing about the P&L arithmetic. A golden whose numbers are all zero is not a pin.
_PREM_CLOSES = _wave(len(_BAR_DAYS), 25.0, 0.30, 13)


def _underlying_rows():
    rows = []
    for t, d in enumerate(_DAYS):
        c = _UNDER_CLOSES[t]
        o = _UNDER_CLOSES[t - 1] if t else c
        rows.append((d, o, round(max(o, c) * 1.004, 4), round(min(o, c) * 0.996, 4), c))
    return rows


def _chain_rows():
    return [{"occ_symbol": _CONTRACT, "option_type": "call", "strike": _STRIKE,
             "expiry": _EXPIRY.isoformat(), "bid": 25.0, "ask": 25.0, "last": 25.0,
             "iv": _IV, "delta": _DELTA, "open_interest": 5000}]


def _bar_rows():
    rows = []
    for t, d in enumerate(_BAR_DAYS):
        c = _PREM_CLOSES[t]
        o = _PREM_CLOSES[t - 1] if t else c
        rows.append({"occ_symbol": _CONTRACT, "date": d.isoformat(), "open": o,
                     "high": round(max(o, c) * 1.01, 4), "low": round(min(o, c) * 0.99, 4),
                     "close": c, "volume": 500, "underlying": _SYMBOL,
                     "option_type": "call", "strike": _STRIKE,
                     "expiry": _EXPIRY.isoformat(), "iv": _IV, "delta": _DELTA})
    return rows


def run_option_golden_backtest() -> Dict[str, Any]:
    """The O_LEAP chain -- the launcher's own emitted rules -- through the real engine."""
    from tests.backtest.test_grid2_engine_paths import (
        _PlainBuyExpert, _harness, _launcher, _leap_rules)

    m = _launcher()
    # The LAUNCHER'S OWN rules, not a hand-written pair: a golden built on rules this grid does
    # not actually emit would pin a configuration nothing runs.
    entry_rules, exit_rules = _leap_rules(m, dte_floor=_DTE_FLOOR)
    engine, account, ctx = _harness(
        symbol=_SYMBOL, underlying_rows=_underlying_rows(), chain_rows=_chain_rows(),
        bar_rows=_bar_rows(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, _SYMBOL),
        start=_START, end=_END, account_id=_ACCOUNT_ID)
    try:
        engine.run()
        return fingerprint(account)
    finally:
        ctx.__exit__(None, None, None)


def fingerprint(account) -> Dict[str, Any]:
    """Full-precision fingerprint of a finished OPTION run.

    Superset of the equity fingerprint's fields: the three option-only columns are included
    here precisely because they are what an option result IS. ``transaction_id`` stays out (it
    is a DB row id, not a result).
    """
    rows: List[Dict[str, Any]] = []
    for t in account.get_round_trip_trades():
        rows.append({
            "symbol": str(t["symbol"]),
            "direction": str(t["direction"]),
            "entry_time": _ts(t["entry_time"]),
            "exit_time": _ts(t["exit_time"]),
            "size": _r(t["size"]),
            "entry_price": _r(t["entry_price"]),
            "exit_price": _r(t["exit_price"]),
            "pnl": _r(t["pnl"]),
            "pnl_pct": _r(t["pnl_pct"]),
            "bars_held": t["bars_held"],
            "exit_reason": str(t["exit_reason"]),
            # OPTION-ONLY. ``.get`` is correct here and is NOT the forbidden config-default
            # pattern: this is a RESULT-ROW shape probe (an equity row genuinely has no
            # contract symbol), not configuration access, and the absent case is recorded as
            # None in the pin rather than being replaced by a made-up value.
            "contract_symbol": (str(t["contract_symbol"])
                                if t.get("contract_symbol") is not None else None),
            "underlying_symbol": (str(t["underlying_symbol"])
                                  if t.get("underlying_symbol") is not None else None),
            "multiplier": _r(t["multiplier"]) if t.get("multiplier") is not None else None,
        })
    rows.sort(key=lambda r: (r["entry_time"] or "", r["symbol"], r["contract_symbol"] or "",
                             r["exit_time"] or "", r["entry_price"], r["exit_price"]))

    hist = account.get_balance_history()
    curve = [[_ts(h["date"]), _r(h["net_liquidating_value"]),
              _r(h["cash_balance"]), _r(h["equity_value"])] for h in hist]
    payload: Dict[str, Any] = {
        "n_trades": len(rows),
        "trades": rows,
        "final_equity": _r(account.equity()),
        "equity_curve_len": len(curve),
        "equity_first": curve[0] if curve else None,
        "equity_last": curve[-1] if curve else None,
        # THE WHOLE CURVE, unlike the equity golden's first/last. This is what actually pins the
        # per-bar MARK -- including the Black-Scholes fallback, which runs on roughly half the
        # bars of this fixture and touches the result nowhere else: the round-trip's entry and
        # exit both fill off real premium bars, and equity_first/equity_last both fall outside
        # the hold, so with first/last alone every BS mark in the run could move freely without
        # changing the hash. 149 points is cheap; the equity golden omits it only because its
        # own fixture is 6 symbols x 120 bars.
        "equity_curve": curve,
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


_CACHE: Dict[str, Any] = {}


def _cached_run() -> Dict[str, Any]:
    if "fp" not in _CACHE:
        _CACHE["fp"] = run_option_golden_backtest()
    return _CACHE["fp"]


def _load_golden() -> Dict[str, Any]:
    with GOLDEN_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_golden(fp: Dict[str, Any], metadata: Dict[str, Any]) -> None:
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GOLDEN_PATH.open("w", encoding="utf-8") as fh:
        json.dump({"_metadata": metadata, "fingerprint": fp}, fh, indent=2, sort_keys=True)
        fh.write("\n")


def test_option_golden_run_matches_pinned_fingerprint():
    """The real-engine O_LEAP run reproduces the committed golden EXACTLY, trade for trade."""
    fp = _cached_run()

    if os.environ.get("BA2_REGEN_OPTION_GOLDEN") == "1":
        _write_golden(fp, {
            "key": "O_LEAP",
            "dte_floor": _DTE_FLOOR,
            "note": "NEW BASELINE recorded on options-grid2 at final review. NOT a claim of "
                    "identity with pre-branch option results -- the branch's "
                    "results-comparability note records a baseline split on the BS mark "
                    "fallback. Regenerate only with a written justification.",
        })
        return

    golden = _load_golden()["fingerprint"]
    assert fp["sha256"] == golden["sha256"], (
        "OPTION BACKTEST RESULTS MOVED.\n" + _diff_lines(golden, fp) +
        f"\n  golden sha256={golden['sha256']}\n  now    sha256={fp['sha256']}")


def test_the_option_golden_run_actually_trades():
    """'Identical' must mean 'identical AND still trading'. See MIN_ROUND_TRIPS."""
    fp = _cached_run()
    assert fp["n_trades"] >= MIN_ROUND_TRIPS, (
        f"the O_LEAP fixture closed {fp['n_trades']} round-trips (floor {MIN_ROUND_TRIPS}) -- "
        "the entry or the DTE exit stopped firing; the fingerprint pin is meaningless until "
        "this is explained")


def test_the_golden_carries_a_NON_ZERO_round_trip():
    """A golden of all-zeros hashes just as stably as a real one and pins nothing.

    The first draft of this fixture did exactly that: a 14-bar premium wave against a ~42-bar
    hold put the exit on the wave's own zero phase, so entry and exit filled at the identical
    premium and every P&L field was ``'0.0'``. Every other test here passed. This is the guard
    that makes the fingerprint's numbers mean something.
    """
    fp = _cached_run()
    closed = [t for t in fp["trades"] if t["exit_reason"] != "open_at_end"]
    assert closed, "no CLOSED round-trip: the opt_dte exit never fired"
    assert any(float(t["pnl"]) != 0.0 for t in closed), (
        "every closed round-trip has pnl == 0.0 -- entry and exit are filling at the same "
        f"premium, so the pricing path is unpinned: {closed}")
    assert any(t["entry_price"] != t["exit_price"] for t in closed)


def test_the_curve_actually_MOVES_while_the_option_is_held():
    """The BS-fallback guard. A curve that is flat through the hold pins no mark at all."""
    fp = _cached_run()
    held = [p for p in fp["equity_curve"] if float(p[3]) != 0.0]
    assert len(held) > 40, f"only {len(held)} bars carried a marked option position"
    assert len({p[3] for p in held}) > 10, (
        "the held-option equity value takes almost no distinct values -- the per-bar mark is "
        "not varying, so the fallback pricing path is unpinned")


def test_the_pinned_rows_carry_the_option_only_columns():
    """The pin's REASON to exist over the equity one.

    If ``contract_symbol``/``multiplier`` came back None the fingerprint would still be stable
    and still hash -- it would just have stopped pinning anything option-shaped. Assert the
    columns are populated so a silent shape regression cannot hollow the golden out.
    """
    fp = _cached_run()
    assert fp["trades"], "no round-trips to inspect"
    for row in fp["trades"]:
        assert row["contract_symbol"], f"round-trip carries no contract symbol: {row}"
        assert row["multiplier"] is not None, f"round-trip carries no multiplier: {row}"
