"""A mostly-warm store must be topped up by asking for what is MISSING, not for everything.

MEASURED 2026-09-11. The backfill's `--wide` shape asks the vendor for every expiration in
the window (``expiration="*"``) and discards on arrival everything it does not owe:

    for bar in provider.fetch_underlying_eod_bars(underlying, start=start, end=end):
        if expiry is None or expiry not in pending:
            continue

For a FIRST fill -- 350 partitions owed -- that is the right trade by a distance: one request
instead of 350. For a TAIL pass it is the wrong one by 50-80x. IBM and JPM each owed a single
straggler expiry; fetched per-expiry they took 18s and 29s, against the 10-34 MINUTES per
symbol the wide pass was spending to re-stream ~6M rows and keep ~3,000. The whole 857-symbol
pass had fallen to 1,341 units/h with a 216-hour ETA, nearly all of it spent re-downloading
history already on disk.

The fix is not a new fetch path -- both already existed -- it is choosing between them PER
SYMBOL instead of once per run.
"""
import importlib.util
import os
import sys
from datetime import date

import pytest


_MODNAME = "warm_options_history_under_test"


def _mod():
    """Load the tool as a module. Absolute path: another suite's conftest moves the CWD."""
    if _MODNAME in sys.modules:
        return sys.modules[_MODNAME]
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "tools", "warm_options_history.py")
    spec = importlib.util.spec_from_file_location(_MODNAME, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = m
    try:
        spec.loader.exec_module(m)
    except Exception:
        sys.modules.pop(_MODNAME, None)
        raise
    return m


def _plan(**owed):
    """A Plan owing ``{symbol: n_expiries}``."""
    m = _mod()
    plan = m.Plan()
    for symbol, n in owed.items():
        for i in range(n):
            expiry = date(2020 + i // 12, 1 + i % 12, 15)
            contract = type("C", (), {"expiry": expiry, "occ_symbol": f"{symbol}{i}"})()
            plan.units.append(m.WorkUnit(symbol, expiry, [contract]))
    plan.units_pending = len(plan.units)
    return plan


def test_a_symbol_owing_its_whole_ladder_goes_wide():
    """One request for 350 expiries beats 350 requests, and that is why --wide exists."""
    m = _mod()
    wide, tail = m.split_by_pending(_plan(AAPL=350), 25)
    assert [u.underlying for u in wide] == ["AAPL"]
    assert tail.units == []


def test_a_symbol_owing_one_straggler_goes_per_expiry():
    """THE DEFECT. IBM owed exactly one expiry and was costing a full-chain re-stream."""
    m = _mod()
    wide, tail = m.split_by_pending(_plan(IBM=1), 25)
    assert wide == []
    assert [u.underlying for u in tail.units] == ["IBM"]
    assert tail.units_pending == 1


def test_the_two_shapes_are_chosen_per_symbol_within_one_chunk():
    """A chunk mixes a cold symbol with warm ones; each must get the shape that suits it."""
    m = _mod()
    wide, tail = m.split_by_pending(_plan(COLD=340, IBM=1, JPM=2), 25)
    assert [u.underlying for u in wide] == ["COLD"]
    assert sorted({u.underlying for u in tail.units}) == ["IBM", "JPM"]
    assert tail.units_pending == 3


def test_every_owed_partition_survives_the_split():
    """The split may only ROUTE work. Losing a unit here would silently skip a partition and
    leave the store looking complete -- the same failure shape as a false 'empty'."""
    m = _mod()
    plan = _plan(COLD=40, WARM=3, TEPID=25)
    wide, tail = m.split_by_pending(plan, 25)
    routed = sum(len(u.pending_expiries) for u in wide) + len(tail.units)
    assert routed == plan.units_pending == 68
    # The threshold is inclusive: 25 pending is enough to earn a wide request.
    assert sorted(u.underlying for u in wide) == ["COLD", "TEPID"]


def test_zero_restores_the_old_all_wide_behaviour():
    """The escape hatch, and the proof the routing is what changes the outcome."""
    m = _mod()
    wide, tail = m.split_by_pending(_plan(IBM=1, JPM=2), 0)
    assert sorted(u.underlying for u in wide) == ["IBM", "JPM"]
    assert tail.units == []
