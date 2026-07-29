"""Still-held gating for FMPSenateTraderWeight (2026-07-28).

The expert scored individual DISCLOSURES with no notion of an open position, so a buy that was
quietly sold two months later counted exactly like one still held. That is tolerable at a 60-day
window (little has been sold yet) and actively wrong at 9-12 months, which is why these land
together with the widened max_disclose_date_days / max_trade_exec_days ceilings.

Two DISTINCT settings, deliberately not folded into the existing consensus knobs:
  require_still_held  -- FILTER: drop a trade whose discloser has since sold out
  min_still_holders   -- CONSENSUS floor on OPEN positions, independent of min_traders (which
                         counts anyone who traded in the window, sold or not)
"""
from datetime import datetime, timezone

import pytest

from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)

import contextlib
import time as _time
import ba2_experts.FMPSenateTraderWeight as _mod_shadow   # noqa: F401  (see importlib note below)
import importlib as _importlib
_M = _importlib.import_module("ba2_experts.FMPSenateTraderWeight")


@contextlib.contextmanager
def _clock_advanced_past_ttl():
    """Jump the clock beyond the holdings TTL. Patches time.time globally because the module
    calls it via `time.time()`; the package __init__ re-exports the CLASS under the module's own
    name, so `import ba2_experts.FMPSenateTraderWeight as m` yields the class -- importlib is
    required to reach the real module."""
    orig = _time.time
    _time.time = lambda: orig() + FMPSenateTraderWeight._HOLDINGS_TTL_SECONDS + 10
    try:
        yield
    finally:
        _time.time = orig




def _t(who, sym, ttype, date, amount="$15,001 - $50,000"):
    return {"representative": who, "symbol": sym, "type": ttype,
            "transactionDate": date, "amount": amount}


def _expert():
    return FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)


# --------------------------------------------------------------------------- #
# netting
# --------------------------------------------------------------------------- #
def test_buy_with_no_sale_is_still_held():
    e = _expert()
    held = e._still_held_by([_t("Alice", "UBER", "purchase", "2024-01-10")], "UBER", now=NOW)
    assert held == {"Alice": True}


def test_buy_then_larger_sale_is_not_held():
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Alice", "UBER", "sale", "2024-03-10")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": False}


def test_any_sale_closes_the_position_even_a_small_one():
    """SIMPLIFYING RULE: disclosures are amount RANGES, not share counts, so a partial sale
    cannot be sized. Treating every sale as a full exit is the conservative direction -- it can
    only drop a candidate, never keep one the trader has actually exited."""
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10", "$50,001 - $100,000"),
              _t("Alice", "UBER", "sale", "2024-03-10", "$1,001 - $15,000")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": False}


def test_rebuy_after_selling_is_held_again():
    """Last action wins, so a re-entry re-opens the position."""
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Alice", "UBER", "sale", "2024-02-10"),
              _t("Alice", "UBER", "purchase", "2024-03-10")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": True}


def test_same_day_buy_and_sale_counts_as_exited():
    """Ambiguous ordering within a day -> assume the exit, never the entry."""
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Alice", "UBER", "sale", "2024-01-10")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": False}


def test_a_later_sale_does_not_retroactively_close_an_earlier_bar():
    """LOOKAHEAD GUARD. Netting is as-of `now`; a sale in the future of the bar being analysed
    must not mark the position closed at that bar."""
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Alice", "UBER", "sale", "2024-05-01")]
    early = datetime(2024, 2, 1, tzinfo=timezone.utc)
    assert e._still_held_by(trades, "UBER", now=early) == {"Alice": True}
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": False}


def test_only_the_requested_symbol_is_netted():
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Alice", "LYFT", "sale", "2024-02-10")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": True}


def test_traders_are_tracked_independently():
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Bob", "UBER", "purchase", "2024-01-11"),
              _t("Bob", "UBER", "sale", "2024-02-11", "$100,001 - $250,000")]
    held = e._still_held_by(trades, "UBER", now=NOW)
    assert held == {"Alice": True, "Bob": False}


def test_the_uber_case_six_bought_none_sold():
    """The public-tracker signal this exists to express."""
    e = _expert()
    names = ["Hickenlooper", "Boozman", "Trump", "Whitehouse", "Pelosi", "Carper"]
    trades = [_t(n, "UBER", "purchase", "2024-01-%02d" % (i + 5)) for i, n in enumerate(names)]
    held = e._still_held_by(trades, "UBER", now=NOW)
    assert sum(1 for v in held.values() if v) == 6


# --------------------------------------------------------------------------- #
# robustness -- bad data must not silently void a trader
# --------------------------------------------------------------------------- #
def test_amount_is_irrelevant_now():
    """Amounts are no longer parsed at all -- the rule is direction + recency only."""
    e = _expert()
    held = e._still_held_by([_t("Alice", "UBER", "purchase", "2024-01-10", "who knows")],
                            "UBER", now=NOW)
    assert held == {"Alice": True}


def test_malformed_date_is_dropped_not_fatal():
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "not-a-date"),
              _t("Bob", "UBER", "purchase", "2024-01-10")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Bob": True}


def test_non_buy_sell_rows_are_ignored():
    e = _expert()
    trades = [_t("Alice", "UBER", "exchange", "2024-01-10"),
              _t("Bob", "UBER", "purchase", "2024-01-10")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Bob": True}


def test_symbol_matching_is_case_insensitive():
    e = _expert()
    trades = [{"representative": "Alice", "ticker": "uber", "type": "purchase",
               "transactionDate": "2024-01-10", "amount": "$15,001 - $50,000"}]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": True}


def test_empty_trades_is_empty_not_an_error():
    assert _expert()._still_held_by([], "UBER", now=NOW) == {}


# --------------------------------------------------------------------------- #
# caching -- this runs per symbol PER BAR, ~78x/day on a 5min clock
# --------------------------------------------------------------------------- #
def test_repeat_calls_in_the_same_day_are_memoized():
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10")]
    first = e._still_held_by(trades, "UBER", now=NOW)
    again = e._still_held_by(trades, "UBER", now=NOW)
    assert again is first, "recomputed instead of serving the memo"


def test_memo_is_per_day_not_forever():
    """Holdings change across days, so a later day must recompute."""
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Alice", "UBER", "sale", "2024-03-10")]
    feb = e._still_held_by(trades, "UBER", now=datetime(2024, 2, 1, tzinfo=timezone.utc))
    jun = e._still_held_by(trades, "UBER", now=NOW)
    assert feb == {"Alice": True} and jun == {"Alice": False}


def test_intraday_bars_share_one_entry():
    """A day bucket is EXACT here: disclosures carry a date, not a time."""
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10")]
    a = e._still_held_by(trades, "UBER", now=datetime(2024, 6, 1, 9, 30, tzinfo=timezone.utc))
    b = e._still_held_by(trades, "UBER", now=datetime(2024, 6, 1, 15, 55, tzinfo=timezone.utc))
    assert a is b


def test_a_longer_history_invalidates_the_memo():
    """Otherwise a re-gathered/extended history would serve a stale answer."""
    e = _expert()
    t1 = [_t("Alice", "UBER", "purchase", "2024-01-10")]
    assert e._still_held_by(t1, "UBER", now=NOW) == {"Alice": True}
    t2 = t1 + [_t("Alice", "UBER", "sale", "2024-02-10")]
    assert e._still_held_by(t2, "UBER", now=NOW) == {"Alice": False}


def test_different_symbols_do_not_collide():
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10"),
              _t("Bob", "LYFT", "purchase", "2024-01-10")]
    assert e._still_held_by(trades, "UBER", now=NOW) == {"Alice": True}
    assert e._still_held_by(trades, "LYFT", now=NOW) == {"Bob": True}


def test_memo_is_bounded():
    e = _expert()
    trades = [_t("Alice", "UBER", "purchase", "2024-01-10")]
    for i in range(4300):
        e._still_held_by(trades, "S%d" % i, now=NOW)
    assert len(e._still_held_memo) <= 4096


def test_gate_uses_unwindowed_history_not_the_windowed_set():
    """REGRESSION: _window_trades filters on the DISCLOSURE window too (as short as 15 days),
    so a sale that CLOSED a position but was disclosed 100 days ago is absent from all_trades.
    Netting over the windowed set reported long-closed positions as still open -- biased toward
    'held', the opposite of the conservative direction this gate is for."""
    e = _expert()
    buy = _t("Alice", "UBER", "purchase", "2024-01-10")
    closing_sale = _t("Alice", "UBER", "sale", "2024-02-10")   # disclosed long ago -> windowed out
    assert e._still_held_by([buy], "UBER", now=NOW) == {"Alice": True}          # windowed view
    e2 = _expert()
    assert e2._still_held_by([buy, closing_sale], "UBER", now=NOW) == {"Alice": False}


# --------------------------------------------------------------------------- #
# the gate's own lookup key
# --------------------------------------------------------------------------- #
def _t_named(first, last, office, sym, ttype, date, amount="$15,001 - $50,000"):
    """A REAL-shaped feed row: firstName/lastName plus an `office` string that does NOT match
    them. 51 of 285 traders are like this in the live feed (20.9% of all rows) -- e.g.
    office='A. Mitchell McConnell' vs 'Mitch McConnell', 'Cory A Booker' vs 'Cory Booker'."""
    return {"firstName": first, "lastName": last, "office": office, "symbol": sym,
            "type": ttype, "transactionDate": date, "amount": amount}


def test_held_by_is_keyed_by_trader_name_not_office():
    """REGRESSION. held_by is keyed by _trader_name; the require_still_held FILTER looked the
    trader up by `office`. For any trader whose office string differs from firstName+lastName the
    lookup missed, defaulted to False, and dropped their trades as "sold out" whether or not they
    still held -- biasing the GA against require_still_held=1 for a pure string-matching reason,
    exactly as the ATR tz bug biased it against use_atr_stop."""
    e = _expert()
    trades = [_t_named("Mitch", "McConnell", "A. Mitchell McConnell", "UBER",
                       "purchase", "2024-01-10")]
    held = e._still_held_by(trades, "UBER", now=NOW)
    assert held == {"Mitch McConnell": True}
    assert "A. Mitchell McConnell" not in held, "office must not be the key"


def test_a_mismatched_office_trader_survives_the_require_still_held_filter():
    """The end-to-end consequence: the filter must find this trader and KEEP the trade."""
    e = _expert()
    trade = _t_named("Cory", "Booker", "Cory A Booker", "UBER", "purchase", "2024-01-10")
    held = e._still_held_by([trade], "UBER", now=NOW)
    # This is the exact expression the gate uses.
    kept = [t for t in [trade] if held.get(FMPSenateTraderWeight._trader_name(t), False)]
    assert kept == [trade], "still-holding trader was dropped by a name-key mismatch"


def test_a_mismatched_office_trader_who_sold_is_still_dropped():
    """And the gate must keep WORKING -- fixing the key must not make it a no-op."""
    e = _expert()
    buy = _t_named("Cory", "Booker", "Cory A Booker", "UBER", "purchase", "2024-01-10")
    sale = _t_named("Cory", "Booker", "Cory A Booker", "UBER", "sale", "2024-03-10")
    held = e._still_held_by([buy, sale], "UBER", now=NOW)
    kept = [t for t in [buy] if held.get(FMPSenateTraderWeight._trader_name(t), False)]
    assert kept == [], "trader who sold out should have been dropped"


# --------------------------------------------------------------------------- #
# live cache invalidation
# --------------------------------------------------------------------------- #
def _named(first, last, sym, ttype, date):
    return {"firstName": first, "lastName": last, "symbol": sym, "type": ttype,
            "transactionDate": date, "amount": "$15,001 - $50,000"}


_BASE = [_named("A", "a", "UBER", "purchase", "2024-01-10"),
         _named("B", "b", "UBER", "purchase", "2024-01-11")]
_GREW = _BASE + [_named("C", "c", "UBER", "purchase", "2024-02-01")]      # C bought
_AMEND = _BASE + [_named("C", "c", "UBER", "sale", "2024-02-01")]         # C sold - SAME LENGTH


def test_live_same_length_feed_change_is_picked_up_after_the_ttl():
    """REGRESSION. Both holdings caches keyed on len(trades) ALONE. The live `-latest` endpoints
    return a ROLLING WINDOW, so as new filings arrive old ones drop off and the content changes
    completely while the count stays identical -- a length-only key then pins a stale answer
    forever.

    The timeline had a TTL, but it rebuilt by calling _holdings_index_cached, which had NONE, so
    on expiry it faithfully reconstructed the same wrong answer from the same stale rows. Cache
    invalidation has to reach the layer that owns the data. Measured before the fix: 3 holders
    where the truth was 2, still 3 after the TTL elapsed."""
    e = _expert()
    assert e._holder_count_as_of(_GREW, "UBER", NOW, is_live=True) == 3
    with _clock_advanced_past_ttl():
        assert e._holder_count_as_of(_AMEND, "UBER", NOW, is_live=True) == 2, \
            "stale index survived TTL expiry -- invalidation did not reach _holdings_index_cached"


def test_live_still_held_map_also_expires():
    """_still_held_by memoised on (symbol, len, DAY), which is exact for a frozen backtest feed
    but pins a live answer for the whole day. Its key now carries a TTL window too."""
    e = _expert()
    assert e._still_held_by(_GREW, "UBER", now=NOW, is_live=True)["C c"] is True
    with _clock_advanced_past_ttl():
        assert e._still_held_by(_AMEND, "UBER", now=NOW, is_live=True)["C c"] is False


def test_backtest_never_pays_the_ttl():
    """A backtest feed is FROZEN for the whole run, so length is an exact content check there.
    Expiring on a timer would rebuild the index for nothing, per symbol, for millions of bars."""
    e = _expert()
    first = e._holder_count_as_of(_BASE, "UBER", NOW, is_live=False)
    with _clock_advanced_past_ttl():
        assert e._holder_count_as_of(_BASE, "UBER", NOW, is_live=False) == first
        assert e._holdings_index_cached(_BASE, False) is e._holdings_index_cached(_BASE, False), \
            "backtest rebuilt the index on a clock change"


def test_within_the_ttl_the_cache_is_deliberately_reused():
    """The bound is TTL-sized staleness, not zero staleness -- rebuilding per call would put an
    O(feed) scan back on the hot path, which is what the index exists to remove."""
    e = _expert()
    assert e._holder_count_as_of(_GREW, "UBER", NOW, is_live=True) == 3
    assert e._holder_count_as_of(_AMEND, "UBER", NOW, is_live=True) == 3  # same window -> cached
