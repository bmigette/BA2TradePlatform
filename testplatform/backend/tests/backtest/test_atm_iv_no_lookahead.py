"""``get_atm_iv`` must never read an IV inverted from a price the engine cannot have seen
(OPT-C8).

WHY THE CHAIN ROW IS NOT AS-OF SAFE. ``_compute_atm_iv`` clamps the chain SNAPSHOT
(``latest_as_of``) and clamps the BAR (``latest_on_or_before``), and then falls back from the
bar to the snapshot row for ``iv``/``delta``. The snapshot row's greeks are not clamped by
that snapshot's own date. ``fetch_options.build_cache`` writes one chain row per contract
stamped ``as_of = <build start>``, and inverts its IV from::

    on_start = next((r for r in bar_rows if r["date"] == start_iso), None)
    as_of_premium = (on_start or bar_rows[0]).get("close")

-- so when the contract did not trade on the build's start date the premium comes from
``bar_rows[0]``, the FIRST bar anywhere in the fetch window, which can be months later. The
row records no trace of which date its IV came from.

The fallback is at its worst exactly where it fires. It fires when the contract has NO bar on
or before the engine clock -- and in that case every bar the contract has is LATER than the
clock, so the chain row's IV is inverted from a future price by construction. There is no
version of that read which is not lookahead.

WHY DROPPING THE FALLBACK RATHER THAN STAMPING THE ROW. The alternative was to record the
date each row's IV was inverted from and refuse rows dated after ``as_of``. That needs a new
column on ``option_chain``; no existing cache has one (the shared 10.9 GB file predates even
the greek columns), so every row would read "provenance unknown" and be refused anyway --
identical behaviour to this, plus a schema migration and a second thing to keep correct. The
information is not recoverable retrospectively, so the honest read is that it is absent.

This matters more since 2026-08-26, when ``iv_rank`` became a live searched gene: the
statistic being corrupted is the only cross-sectionally comparable option number the platform
has, and it is now inside the GA's search.
"""
from datetime import date

import pytest

from app.services.backtest.options_cache import OptionsHistoryCache
from app.services.backtest.options_provider import (
    HistoricalOptionsProvider, clear_worker_options_cache,
)

SNAPSHOT = "2024-02-01"     # the cache build's start date, and the chain row's as_of
AS_OF = date(2024, 3, 1)    # the engine clock
EXPIRY = "2024-04-01"       # 31 days from AS_OF -> inside the 20-45 DTE band
OCC = "XYZ240401C00100000"


@pytest.fixture(autouse=True)
def _fresh_caches():
    """The chain/bar/ATM-IV memos are module-level and keyed on the db path."""
    clear_worker_options_cache()
    yield
    clear_worker_options_cache()


def _cache(tmp_path, *, chain_iv, chain_delta, bars):
    db = str(tmp_path / "opt.sqlite")
    c = OptionsHistoryCache(db)
    c.write_chain_rows("XYZ", SNAPSHOT, [{
        "occ_symbol": OCC, "option_type": "call", "strike": 100.0, "expiry": EXPIRY,
        "bid": 2.0, "ask": 2.0, "last": 2.0,
        "iv": chain_iv, "delta": chain_delta,
        "gamma": None, "theta": None, "vega": None,
        "open_interest": None, "volume": None,
    }])
    if bars:
        c.write_bar_rows([{
            "occ_symbol": OCC, "date": d, "open": px, "high": px, "low": px, "close": px,
            "volume": 10, "underlying": "XYZ", "option_type": "call", "strike": 100.0,
            "expiry": EXPIRY, "iv": iv, "delta": delta,
            "gamma": None, "theta": None, "vega": None,
        } for d, px, iv, delta in bars])
    return HistoricalOptionsProvider(db)


# --------------------------------------------------------------------------- #
# THE LOOKAHEAD
# --------------------------------------------------------------------------- #
def test_a_chain_row_whose_only_bar_is_in_the_future_yields_no_iv(tmp_path):
    """The contract's ONLY bar is 2024-03-15, two weeks after the engine clock, so the
    0.55 on the chain row was inverted from a price that has not happened yet. The correct
    answer is "no measurable ATM IV today", not 0.55."""
    p = _cache(tmp_path, chain_iv=0.55, chain_delta=0.50,
               bars=[("2024-03-15", 3.0, 0.55, 0.50)])
    assert p.get_atm_iv("XYZ", AS_OF) is None


def test_the_same_cache_answers_once_the_clock_reaches_the_bar(tmp_path):
    """DISCRIMINATOR. Same cache, same contract, clock moved past the bar: now the value is
    measurable and must be returned. Without this, "return None" would pass the test above
    by disabling the function outright.

    2024-03-15 + 20..45 days spans 2024-04-04..2024-04-29, so the clock is 2024-03-10 --
    still on or before the bar? No: it must be ON OR AFTER the bar for the bar to clamp in,
    and the expiry must stay in band. 2024-03-15 with a 2024-04-01 expiry is 17 days, below
    the 20-day floor, so the reader would skip it -- use 2024-03-05, one day after a bar
    dated 2024-03-04, which is 28 days from expiry."""
    p = _cache(tmp_path, chain_iv=0.55, chain_delta=0.50,
               bars=[("2024-03-04", 3.0, 0.42, 0.50)])
    assert p.get_atm_iv("XYZ", date(2024, 3, 5)) == pytest.approx(0.42)


def test_a_clamped_bar_with_no_iv_of_its_own_does_not_borrow_the_chain_rows(tmp_path):
    """The second way the fallback fires: the bar IS on or before the clock but its own
    inversion failed (no underlying close that day, or a non-invertible price). The chain
    row's IV still has no provenance, so unknown stays unknown."""
    p = _cache(tmp_path, chain_iv=0.55, chain_delta=0.50,
               bars=[("2024-03-04", 3.0, None, None)])
    assert p.get_atm_iv("XYZ", date(2024, 3, 5)) is None


def test_a_contract_with_no_bars_at_all_yields_no_iv(tmp_path):
    """A cache built before the greek columns existed has chain rows and no usable bars.
    Reporting the frozen row's number would be the whole defect in its purest form."""
    p = _cache(tmp_path, chain_iv=0.55, chain_delta=0.50, bars=[])
    assert p.get_atm_iv("XYZ", AS_OF) is None


def test_the_delta_used_to_pick_the_contract_is_also_the_bars(tmp_path):
    """``delta`` is not incidental -- it is the selection key (nearest |delta| to 0.50), so a
    frozen delta chooses WHICH contract's iv is reported. Two calls: the chain rows claim the
    110 strike is the ATM one, the clamped bars say it is the 100. The bars must win."""
    db = str(tmp_path / "opt.sqlite")
    c = OptionsHistoryCache(db)
    rows = []
    for occ, strike, chain_delta in ((OCC, 100.0, 0.90),
                                     ("XYZ240401C00110000", 110.0, 0.50)):
        rows.append({"occ_symbol": occ, "option_type": "call", "strike": strike,
                     "expiry": EXPIRY, "bid": 2.0, "ask": 2.0, "last": 2.0,
                     "iv": 0.99, "delta": chain_delta, "gamma": None, "theta": None,
                     "vega": None, "open_interest": None, "volume": None})
    c.write_chain_rows("XYZ", SNAPSHOT, rows)
    c.write_bar_rows([
        {"occ_symbol": OCC, "date": "2024-03-04", "open": 3.0, "high": 3.0, "low": 3.0,
         "close": 3.0, "volume": 10, "underlying": "XYZ", "option_type": "call",
         "strike": 100.0, "expiry": EXPIRY, "iv": 0.31, "delta": 0.51,
         "gamma": None, "theta": None, "vega": None},
        {"occ_symbol": "XYZ240401C00110000", "date": "2024-03-04", "open": 1.0,
         "high": 1.0, "low": 1.0, "close": 1.0, "volume": 10, "underlying": "XYZ",
         "option_type": "call", "strike": 110.0, "expiry": EXPIRY, "iv": 0.28,
         "delta": 0.20, "gamma": None, "theta": None, "vega": None},
    ])
    p = HistoricalOptionsProvider(db)
    assert p.get_atm_iv("XYZ", date(2024, 3, 5)) == pytest.approx(0.31)
