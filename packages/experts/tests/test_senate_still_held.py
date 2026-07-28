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
