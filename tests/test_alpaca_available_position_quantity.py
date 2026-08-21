"""AlpacaAccount.get_available_position_quantity — the cancel-and-replace gate.

The gate (``TradeManager.replacement_blocked_by_qty``) defers a replacement stop
while the broker still holds the prior order's quantity, so the replacement isn't
rejected with Alpaca 40310000 "insufficient qty available" and hard-ERRORed,
which silently leaves the position with NO protective stop.

I9 hoisted the seam onto ``ReadOnlyAccountInterface`` with a default derived from
``get_positions()``. Alpaca keeps a FAST override — ``get_open_position(symbol)``
is one targeted call instead of the whole book — but it must honour the same
contract: NEVER ``None``, because ``None`` is the caller's "unknown -> do NOT
block" value and "we could not find out" must defer, not submit blind.

No live API call: the client is a MagicMock.
"""
from unittest.mock import MagicMock

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _bare_account():
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._margin_info_cache = {}
    return acct


def _position(qty="6", qty_available="6"):
    pos = MagicMock()
    pos.qty = qty
    pos.qty_available = qty_available
    return pos


def test_reads_qty_available_from_the_targeted_endpoint():
    acct = _bare_account()
    acct.client.get_open_position.return_value = _position(qty="10", qty_available="4")

    assert acct.get_available_position_quantity("AAPL") == 4.0
    acct.client.get_open_position.assert_called_once_with("AAPL")


def test_a_short_reports_the_magnitude():
    acct = _bare_account()
    acct.client.get_open_position.return_value = _position(qty="-100", qty_available="-100")

    assert acct.get_available_position_quantity("AAPL") == 100.0


def test_a_fully_encumbered_position_reports_zero():
    """The exact 40310000 shape: shares still held against the just-canceled order."""
    acct = _bare_account()
    acct.client.get_open_position.return_value = _position(qty="6", qty_available="0")

    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_no_position_at_broker_is_zero_not_unknown():
    """get_open_position raises 404 when the account holds nothing in the symbol.
    A protective stop for shares Alpaca does not have is a guaranteed rejection,
    so defer (0.0) rather than submit blind (None)."""
    acct = _bare_account()
    acct.client.get_open_position.side_effect = Exception("position does not exist")

    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_a_lookup_failure_blocks_rather_than_submitting_blind():
    acct = _bare_account()
    acct.client.get_open_position.side_effect = ConnectionError("getaddrinfo failed")

    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_a_position_without_qty_available_falls_back_to_the_held_qty():
    acct = _bare_account()
    acct.client.get_open_position.return_value = _position(qty="100", qty_available=None)

    assert acct.get_available_position_quantity("AAPL") == 100.0


def test_a_non_numeric_quantity_blocks_rather_than_guessing():
    acct = _bare_account()
    acct.client.get_open_position.return_value = _position(qty="n/a", qty_available="n/a")

    assert acct.get_available_position_quantity("AAPL") == 0.0


def test_never_returns_none_whatever_the_broker_does():
    """None means "don't block". The override must not be able to produce it."""
    acct = _bare_account()
    for setup in (
        lambda: setattr(acct.client.get_open_position, "side_effect", Exception("boom")),
        lambda: setattr(acct.client.get_open_position, "return_value", _position("0", "0")),
        lambda: setattr(acct.client.get_open_position, "return_value", _position(None, None)),
    ):
        acct.client.get_open_position.side_effect = None
        setup()
        assert acct.get_available_position_quantity("AAPL") is not None
