"""The concrete broker seams on ReadOnlyAccountInterface / AccountInterface.

They are CONCRETE, never @abstractmethod: ReadOnlyAccountInterface already has 12
abstract methods, and adding a 13th would break instantiation of IBKRAccount,
TastyTradeAccount and every stub in the test suite.

_DictAccount below is the IBKR / TastyTrade shape: get_account_info() returns a
plain dict. AlpacaAccount's pydantic shape is covered in tests/test_alpaca_account_snapshot.py.
"""
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _DictAccount(ReadOnlyAccountInterface):
    """A broker whose get_account_info() returns a dict (IBKR / TastyTrade shape)."""

    def __init__(self, id, info):
        self.id = id
        self._info = info
        self._settings_cache = None

    def get_account_info(self):
        return self._info

    def get_balance(self):
        return None

    def get_positions(self):
        return []

    def get_orders(self, status=None):
        return []

    def symbols_exist(self, symbols):
        return {}

    def _get_instrument_current_price_impl(self, *a, **k):
        return None

    def get_balance_history(self, *a, **k):
        return []

    def get_dividends(self, *a, **k):
        return []

    def get_filled_trades(self, *a, **k):
        return []

    def get_order(self, *a, **k):
        return None

    def refresh_orders(self, *a, **k):
        return True

    def refresh_positions(self, *a, **k):
        return True


def test_snapshot_from_a_dict_broker_reads_the_tastytrade_field_names():
    """TastyTrade names them cash_balance / net_liquidating_value / equity_buying_power."""
    acct = _DictAccount(1, {
        "cash_balance": "12000.25",
        "net_liquidating_value": "48000.00",
        "equity_buying_power": "96000.00",
        "margin_multiplier": "2",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 12000.25
    assert snap.net_liquidation == 48000.00
    assert snap.buying_power == 96000.00
    assert snap.margin_multiplier == 2.0


def test_snapshot_from_a_dict_broker_reads_the_plain_field_names():
    acct = _DictAccount(1, {
        "cash": "500.00",
        "equity": "10000.00",
        "buying_power": "10000.00",
        "long_market_value": "9500.00",
        "short_market_value": "0",
        "multiplier": "1",
    })
    snap = acct.get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 10000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == 0.0


def test_a_broker_publishing_only_portfolio_value_gets_both_equity_and_net_liquidation():
    """AccountSnapshot's contract (account_types.py): "An adapter whose broker
    publishes only one MUST set BOTH to that value rather than leave one None."

    ``portfolio_value`` was in the equity chain but not the net_liquidation one,
    so such a broker got equity set and net_liquidation None -- and
    net_liquidation is the headline total value the page reports. The base
    mirrors whichever one it found rather than making every adapter remember.
    """
    snap = _DictAccount(1, {"portfolio_value": "48000.00"}).get_account_snapshot()

    assert snap.equity == 48000.00
    assert snap.net_liquidation == 48000.00


def test_a_broker_publishing_only_net_liquidation_gets_equity_too():
    """The mirror runs both ways -- it is one rule, not a portfolio_value patch."""
    snap = _DictAccount(1, {"net_liquidation": "31000.00"}).get_account_snapshot()

    assert snap.net_liquidation == 31000.00
    assert snap.equity == 31000.00


def test_the_two_are_left_alone_when_the_broker_publishes_both():
    """They legitimately DIVERGE where liquidation value is not the mark; the
    mirror must only fill a hole, never overwrite."""
    snap = _DictAccount(1, {"equity": "50000", "net_liquidation": "49500"}).get_account_snapshot()

    assert snap.equity == 50000.0
    assert snap.net_liquidation == 49500.0


def test_a_positive_short_magnitude_is_negated_to_the_platform_convention():
    """AccountSnapshot: short_market_value is NEGATIVE while shorts are held.

    TastyTrade publishes ``short-equity-value`` as a positive MAGNITUDE. Left
    unnegated it flips the sign of gross exposure, so the base normalises it and
    no adapter has to remember.
    """
    snap = _DictAccount(1, {"short_market_value": "2500.00"}).get_account_snapshot()

    assert snap.short_market_value == -2500.00


def test_an_already_negative_short_value_is_untouched():
    """Alpaca's convention is already negative -- do not double-negate it."""
    snap = _DictAccount(1, {"short_market_value": "-2500.00"}).get_account_snapshot()

    assert snap.short_market_value == -2500.00


def test_a_flat_account_reports_a_zero_short_value_not_minus_zero():
    snap = _DictAccount(1, {"short_market_value": "0"}).get_account_snapshot()

    assert snap.short_market_value == 0.0


def test_derivative_buying_power_is_the_last_resort_for_buying_power():
    """The third name in the chain, and the only one an options-only TastyTrade
    account may publish. It had no coverage at all."""
    snap = _DictAccount(1, {"derivative_buying_power": "7500.00"}).get_account_snapshot()

    assert snap.buying_power == 7500.00


def test_the_plain_buying_power_name_wins_over_the_derivative_one():
    snap = _DictAccount(1, {"buying_power": "9000",
                            "derivative_buying_power": "7500"}).get_account_snapshot()

    assert snap.buying_power == 9000.0


def test_pending_transfer_in_is_read_on_the_dict_path_too():
    """Only the attribute path asserted it (as None); an incoming ACH is money
    the allocation page must see."""
    snap = _DictAccount(1, {"pending_transfer_in": "1000.00"}).get_account_snapshot()

    assert snap.pending_transfer_in == 1000.00


def test_snapshot_multiplier_above_one_marks_the_account_as_margin():
    assert _DictAccount(1, {"multiplier": "4"}).get_account_snapshot().is_margin_account is True


def test_snapshot_multiplier_of_one_is_a_cash_account():
    assert _DictAccount(1, {"multiplier": "1"}).get_account_snapshot().is_margin_account is False


def test_snapshot_of_a_broker_returning_none_is_all_unknown_not_all_zero():
    """None means "the broker told us nothing". A 0.0 here would let a caller
    plan against an account it cannot see."""
    snap = _DictAccount(1, None).get_account_snapshot()
    assert snap == AccountSnapshot()
    assert snap.buying_power is None


def test_snapshot_leaves_a_non_numeric_field_as_none_rather_than_guessing():
    snap = _DictAccount(1, {"buying_power": "n/a", "cash": "100"}).get_account_snapshot()
    assert snap.buying_power is None
    assert snap.cash == 100.0


def test_snapshot_from_an_attribute_broker_uses_the_getattr_branch():
    """The other half of the tolerant probe: an object, not a dict.

    This is the shape that broke TradeActions.py:1493 -- ``.get()`` on a pydantic
    TradeAccount raises AttributeError. Task 31 tests AlpacaAccount's OVERRIDE, so
    without this the base's ``getattr`` branch would have no coverage at all.
    ``raw`` stays {} because only a dict can be copied into it.
    """
    class _Attrs:
        cash = "500.00"
        equity = "10000.00"
        buying_power = "20000.00"
        long_market_value = "9500.00"
        short_market_value = "-250.00"
        multiplier = "2"

    snap = _DictAccount(1, _Attrs()).get_account_snapshot()
    assert snap.cash == 500.0
    assert snap.equity == 10000.0
    assert snap.buying_power == 20000.0
    assert snap.long_market_value == 9500.0
    assert snap.short_market_value == -250.0
    assert snap.margin_multiplier == 2.0
    assert snap.is_margin_account is True
    assert snap.raw == {}
    # A field the object simply does not carry stays unknown, never 0.0.
    assert snap.pending_transfer_in is None
    assert snap.non_marginable_buying_power is None


def test_snapshot_survives_a_broker_that_raises():
    class _Boom(_DictAccount):
        def get_account_info(self):
            raise RuntimeError("connection reset")

    assert _Boom(1, None).get_account_snapshot() == AccountSnapshot()


def test_get_cash_transfers_defaults_to_empty_for_a_broker_that_does_not_implement_it():
    """[] by default so no existing broker breaks. Alpaca and TastyTrade override it."""
    assert _DictAccount(1, {}).get_cash_transfers() == []


def test_get_cash_transfers_accepts_a_date_window_without_complaining():
    from datetime import date
    acct = _DictAccount(1, {})
    assert acct.get_cash_transfers(start_date=date(2026, 8, 1),
                                   end_date=date(2026, 8, 31)) == []


def test_get_symbol_margin_info_defaults_to_empty_so_the_caller_falls_back():
    """A symbol the broker cannot describe is OMITTED, never defaulted here -- the
    caller substitutes the conservative bp_factor = account multiplier."""
    assert _DictAccount(1, {}).get_symbol_margin_info(["AAPL", "MSFT"]) == {}


# ---------------------------------------------------------------------------
# get_available_position_quantity -- the cancel-and-replace gate (I9)
#
# It used to exist ONLY on AlpacaAccount, so TradeManager's
# `except Exception: available_qty = None` fired on every other broker and
# replacement_blocked_by_qty(None) returned False -- the guard that stops a
# 40310000 "insufficient qty" rejection from dropping a position's protective
# stop was a permanent no-op everywhere but Alpaca.
#
# The base DERIVES it from get_positions() (the mandatory seam every broker
# implements), the same way get_account_snapshot() derives from
# get_account_info(). It NEVER returns None: "cannot answer" is 0.0, which
# BLOCKS (defer, retry next refresh) rather than submitting blind.
# ---------------------------------------------------------------------------

class _BookAccount(_DictAccount):
    """A broker whose get_positions() returns whatever the test sets."""

    def __init__(self, book):
        super().__init__(1, {})
        self._book = book

    def get_positions(self):
        if isinstance(self._book, Exception):
            raise self._book
        return self._book


class _Pos:
    """The attribute-shaped position (Alpaca / IBKR return a Position model)."""

    def __init__(self, symbol, qty, qty_available=None):
        self.symbol = symbol
        self.qty = qty
        self.qty_available = qty_available


def test_available_qty_reads_the_brokers_qty_available_for_a_dict_position():
    acct = _BookAccount([{"symbol": "AAPL", "qty": 10.0, "qty_available": 4.0}])
    assert acct.get_available_position_quantity("AAPL") == 4.0


def test_available_qty_reads_the_brokers_qty_available_for_an_object_position():
    acct = _BookAccount([_Pos("AAPL", 10.0, 4.0)])
    assert acct.get_available_position_quantity("AAPL") == 4.0


def test_available_qty_is_absolute_so_a_short_works_too():
    """A short holds -100; the buy-to-cover replacement needs 100 of them."""
    acct = _BookAccount([_Pos("AAPL", -100.0, -100.0)])
    assert acct.get_available_position_quantity("AAPL") == 100.0


def test_a_fully_encumbered_position_reports_zero_and_therefore_blocks():
    """The exact 40310000 shape: the broker still holds the shares against the
    just-canceled order, so none are available yet."""
    acct = _BookAccount([_Pos("AAPL", 6.0, 0.0)])
    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_a_symbol_the_broker_does_not_hold_is_zero_not_unknown():
    """A protective stop for shares the broker does not have is a GUARANTEED
    rejection. 0.0 defers it (WAITING_TRIGGER, retried next refresh) instead of
    submitting it into a hard ERROR."""
    acct = _BookAccount([_Pos("MSFT", 10.0, 10.0)])
    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_a_flat_book_is_zero():
    assert _BookAccount([]).get_available_position_quantity("AAPL") == 0.0


def test_a_positions_FETCH_FAILURE_blocks_rather_than_submitting_blind():
    """get_positions() returns None when the fetch FAILED (tri-state contract).
    An unverified book must never be read as "the qty is free" -- that is the
    direction that gets the replacement rejected and the stop dropped. 0.0 defers
    and the next refresh retries, so this is self-healing."""
    assert _BookAccount(None).get_available_position_quantity("AAPL") == 0.0


def test_a_broker_that_raises_blocks_too():
    assert _BookAccount(RuntimeError("connection reset")
                        ).get_available_position_quantity("AAPL") == 0.0


def test_a_broker_that_publishes_no_qty_available_falls_back_to_the_held_qty():
    """IBKR builds Position without qty_available. The broker CONFIRMS it holds
    the shares; only the transient encumbrance is unknown. Blocking such a broker
    forever would convert a seconds-long race into a PERMANENT, silent absence of
    protection -- strictly worse than the rejection this gate avoids. The gate
    still bites where it matters: a stale leg asking for more than the position's
    total size is deferred instead of submitted into a rejection."""
    acct = _BookAccount([_Pos("AAPL", 100.0, None)])
    assert acct.get_available_position_quantity("AAPL") == 100.0


def test_a_stale_leg_larger_than_the_whole_position_still_blocks_on_such_a_broker():
    acct = _BookAccount([_Pos("AAPL", 100.0, None)])
    available = acct.get_available_position_quantity("AAPL")
    # 181-share leg vs a 100-share position -> the broker would reject it.
    assert available is not None and available < 181.0


def test_a_non_numeric_quantity_blocks_rather_than_guessing():
    acct = _BookAccount([_Pos("AAPL", "n/a", "n/a")])
    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_available_qty_never_returns_none():
    """None means "don't block" to replacement_blocked_by_qty. The base must not
    be able to produce it -- that is the whole point of the hoist."""
    for book in (None, [], RuntimeError("boom"), [_Pos("MSFT", 1.0, 1.0)],
                 [_Pos("AAPL", "n/a", None)]):
        assert _BookAccount(book).get_available_position_quantity("AAPL") is not None


def test_symbol_matching_is_case_and_whitespace_insensitive():
    acct = _BookAccount([_Pos("AAPL", 10.0, 7.0)])
    assert acct.get_available_position_quantity(" aapl ") == 7.0
