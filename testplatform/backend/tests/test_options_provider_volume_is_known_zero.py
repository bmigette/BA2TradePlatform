"""A cached contract with no bar traded ZERO — that is a known value, not missing data.

WHY (2026-08-23): the new chain-level liquidity-gate probe
(``option_selector.check_liquidity_data_available``) turns "an enabled gate that NO contract
in the chain can answer" into a loud configuration error, because a gate whose field the data
source never publishes silently rejects 100% of candidates. ``open_interest`` genuinely is
such a field for this cache (``option_bar`` has no open_interest column at all, and
``option_chain.open_interest`` is NULL for every row), so that error is exactly right there.

``volume`` is NOT such a field, and must not be mistaken for one. ``option_chain.volume`` is
also NULL throughout, but the per-date ``option_bar`` supplies it — and a contract with NO bar
on or before the as-of date did not trade, i.e. its volume is a KNOWN 0. Emitting ``None``
there conflated "did not trade" with "the source cannot tell you", which (a) would make the
probe fire spuriously on a thin underlying whose whole DTE window happens to be untraded and
(b) left ``min_volume`` doing its stale-quote filtering by accident rather than by statement:
those no-bar rows are precisely the ones still carrying the cache build's start-date quotes.

Selection is UNCHANGED by this: any ``min_volume >= 1`` rejects 0 exactly as it rejected None.

EXACT SESSION SINCE 2026-09-22 (BT/live option parity B2). The volume is no longer derived
inside ``_to_contract`` from the clamped greeks bar; the reader computes it with
``_exact_session_volume`` (the shared ``option_session.session_volume`` rule) from the bar dated
EXACTLY the decision's data session and hands it in. The known-zero property above is unchanged
and is now pinned at that seam; the one deliberate change is that a bar from an EARLIER session
no longer counts (yesterday's volume is not today's liquidity).
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from app.services.backtest.options_provider import (                    # noqa: E402
    _BarHistory, _exact_session_volume, _to_contract,
)
from ba2_common.core.option_selector import (                            # noqa: E402
    check_liquidity_data_available, passes_liquidity,
)

CHAIN_ROW = {
    "occ_symbol": "BAC240405C00037000", "underlying": "BAC", "option_type": "call",
    "strike": 37.0, "expiry": "2024-04-05", "bid": 0.60, "ask": 0.60, "last": 0.60,
    "iv": 0.25, "delta": 0.40, "gamma": None, "theta": None, "vega": None,
    "open_interest": None, "volume": None,          # both NULL, as in the real cache
}


SESSION = date(2024, 3, 7)


def _bar(volume, on=SESSION):
    return {"date": on.isoformat(), "close": 0.75, "volume": volume, "iv": 0.26,
            "delta": 0.41, "gamma": None, "theta": None, "vega": None}


def _contract(bar):
    """What ``HistoricalOptionsProvider.get_chain`` builds for one row on ``SESSION``: prices
    from the clamped bar, volume from the session's exact bar."""
    bars = _BarHistory({} if bar is None else {bar["date"]: bar})
    return _to_contract(dict(CHAIN_ROW), bars.latest_on_or_before(SESSION.isoformat()),
                        _exact_session_volume(bars, SESSION))


def test_a_contract_with_no_bar_reports_zero_volume_not_unknown():
    assert _contract(None).volume == 0


def test_a_contract_with_a_bar_reports_the_bars_volume():
    assert _contract(_bar(1234)).volume == 1234


def test_a_bar_from_an_earlier_session_is_not_this_sessions_volume():
    """The 2026-09-22 change: before, the clamped bar's 1234 was reported here."""
    c = _contract(_bar(1234, on=date(2024, 3, 4)))
    assert c.volume == 0
    assert c.last == 0.75            # prices still come from that clamped bar


def test_open_interest_stays_unknown_because_the_cache_truly_has_none():
    """Do NOT paper over open_interest the same way: there is no column to read it from, so
    a rule that gates on it must get the loud error, not a fabricated 0 that rejects
    everything just as silently."""
    assert _contract(None).open_interest is None
    assert _contract(_bar(1234)).open_interest is None


def test_selection_is_unchanged_a_zero_volume_contract_is_still_rejected():
    c = _contract(None)
    assert passes_liquidity(c, None, None, 25) is False
    assert passes_liquidity(c, None, None, None) is True      # gate off -> unaffected


def test_the_volume_gate_stays_evaluable_on_an_entirely_untraded_window():
    """The whole point: an all-untraded chain is a real (and correct) 100% rejection, not a
    'the source does not publish volume' configuration error."""
    chain = [_to_contract(dict(CHAIN_ROW, occ_symbol=f"X{i}"), None,
                          _exact_session_volume(_BarHistory({}), SESSION)) for i in range(5)]
    check_liquidity_data_available(chain, min_volume=25, underlying="BAC")   # no raise
    assert all(not passes_liquidity(c, None, None, 25) for c in chain)
