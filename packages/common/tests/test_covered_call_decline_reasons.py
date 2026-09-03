"""Every way ``SellCoveredCallAction`` can decline says so, once, by name.

THE SILENT STEADY STATE this closes. On a wheel bar holding assigned shares with no written
call, ``cc_sell`` MATCHES — its trigger is ``has_assigned_shares``, not "a call can be
written" — so ``wheel_stock_guard`` halts the ruleset behind it and no other rule can act on
the position. Meanwhile the assignment liquidation is a no-op under ``hold_assigned_stock``,
the assigned lot carries no bracket, and the engine has no end-of-run flatten. So if this
action declines and says nothing, the sleeve holds naked-long stock for as long as the
decline lasts, with every log quiet and every metric normal.

Six paths could do that. Each now returns a NAMED reason on the result data and emits exactly
ONE warning naming the symbol, the share count and the cause — formatted in one place
(``_decline``) rather than at each site, because the reason is a value two different readers
consume: the backtest counts consecutive uncovered-assigned bars into its results payload, and
the live status path shows the reason on the action result.

The action does NOT auto-liquidate at any threshold. Selling assigned shares is a strategy
decision the operator owns.
"""

from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import (
    COVERED_CALL_DECLINE_COVER_SHORT,
    COVERED_CALL_DECLINE_EMPTY_CHAIN,
    COVERED_CALL_DECLINE_KEY,
    COVERED_CALL_DECLINE_MARKER,
    COVERED_CALL_DECLINE_NO_BID,
    COVERED_CALL_DECLINE_NO_CONTRACT,
    COVERED_CALL_DECLINE_NO_OPTIONS,
    COVERED_CALL_DECLINE_SUB_LOT,
    COVERED_CALL_DECLINE_UNMEASURABLE_SHARES,
    SellCoveredCallAction,
)
from ba2_common.core.types import OptionRight, OrderRecommendation

SYMBOL = "WHLX"

#: Every reason the action can produce. Enumerated so a NEW decline path added without a name
#: is caught by ``test_every_declared_reason_is_exercised`` rather than shipping silent.
ALL_REASONS = {
    COVERED_CALL_DECLINE_NO_OPTIONS,
    COVERED_CALL_DECLINE_UNMEASURABLE_SHARES,
    COVERED_CALL_DECLINE_SUB_LOT,
    COVERED_CALL_DECLINE_EMPTY_CHAIN,
    COVERED_CALL_DECLINE_NO_CONTRACT,
    COVERED_CALL_DECLINE_NO_BID,
    COVERED_CALL_DECLINE_COVER_SHORT,
}

_exercised: set = set()


class _Contract:
    def __init__(self, bid=1.0):
        self.symbol = "WHLX240719C00021000"
        self.underlying = SYMBOL
        self.option_type = OptionRight.CALL
        self.strike = 21.0
        self.expiry = None
        self.bid = bid
        self.ask = 1.1
        self.last = 1.05


def _action(monkeypatch, *, supports=True, held=200.0, chain=(), contract=..., bid=1.0,
            cover_refusal=None):
    """A bare action with exactly the collaborators each decline path consults stubbed.

    Built with ``__new__`` and wired attribute by attribute rather than through the real
    constructor: every one of these paths returns BEFORE any broker call, and the point is to
    reach each of them deliberately rather than to engineer six market states.
    """
    a = SellCoveredCallAction.__new__(SellCoveredCallAction)
    a.instrument_name = SYMBOL
    a.expert_recommendation = SimpleNamespace(instance_id=1, data=None,
                                              price_at_date=20.0,
                                              expected_profit_percent=10.0,
                                              recommended_action=OrderRecommendation.BUY)
    a.strike_method = "percent_otm"
    a.strike_param = 5.0
    a.dte_min, a.dte_max = 25, 45
    a.selection_policy = None

    monkeypatch.setattr(a, "_supports_options", lambda: supports, raising=False)
    monkeypatch.setattr(a, "_held_equity_shares", lambda: held, raising=False)
    monkeypatch.setattr(a, "_chain", lambda ot: list(chain), raising=False)
    monkeypatch.setattr(a, "_liq", lambda ch: {}, raising=False)
    monkeypatch.setattr(a, "_spot", lambda: 20.0, raising=False)
    monkeypatch.setattr(a, "_today", lambda: None, raising=False)
    monkeypatch.setattr(a, "_pick_refusal_message",
                        lambda *args, **kw: kw.get("generic_message", "no contract"),
                        raising=False)
    monkeypatch.setattr(a, "_refuse_if_cover_is_short",
                        lambda *args, **kw: cover_refusal, raising=False)
    monkeypatch.setattr(a, "_submit_option_order",
                        lambda *args, **kw: {"success": True, "data": {}}, raising=False)
    # ``_result`` is the persistence seam; here it just hands the dict back.
    monkeypatch.setattr(a, "_result",
                        lambda ok, message, data=None: {"success": ok, "message": message,
                                                        "data": data or {}},
                        raising=False)
    if contract is not ...:
        import ba2_common.core.TradeActions as TA
        monkeypatch.setattr(TA, "select_single", lambda *args, **kw: contract)
    return a


@pytest.fixture
def warnings(monkeypatch):
    """Capture ``logger.warning`` at the module the action logs from.

    Not ``caplog``: the platform logger is configured with its own handlers and does not
    propagate to the root logger pytest attaches to, so caplog sees nothing and the pin would
    pass while the operator's log stayed empty -- the exact failure this file exists to stop.
    """
    import ba2_common.core.TradeActions as TA

    seen = []
    monkeypatch.setattr(TA.logger, "warning", lambda msg, *a, **k: seen.append(str(msg)))
    return seen


def _assert_declined(result, warnings, reason):
    """One named reason on the data, and exactly ONE warning naming symbol + shares + cause."""
    _exercised.add(reason)
    assert result["success"] is False
    assert result["data"][COVERED_CALL_DECLINE_KEY] == reason, result["data"]

    lines = [m for m in warnings if COVERED_CALL_DECLINE_MARKER in m]
    assert len(lines) == 1, f"expected exactly one decline warning, got {lines}"
    assert SYMBOL in lines[0]
    assert "shares held:" in lines[0], f"the share count is not on the line: {lines[0]}"
    assert reason in lines[0], f"the reason is not on the line: {lines[0]}"


def test_a_non_options_account_declines_by_name(monkeypatch, warnings):
    a = _action(monkeypatch, supports=False)
    _assert_declined(a.execute(), warnings, COVERED_CALL_DECLINE_NO_OPTIONS)


def test_an_unmeasurable_share_count_declines_by_name(monkeypatch, warnings):
    """UNKNOWN is not zero: an executed equity order with no filled_qty."""
    a = _action(monkeypatch, held=None)
    _assert_declined(a._build_and_submit(), warnings,
                     COVERED_CALL_DECLINE_UNMEASURABLE_SHARES)


def test_a_sub_lot_share_count_declines_by_name(monkeypatch, warnings):
    """99 shares is no contract; the overlay would silently no-op into a plain equity run."""
    a = _action(monkeypatch, held=99.0)
    _assert_declined(a._build_and_submit(), warnings, COVERED_CALL_DECLINE_SUB_LOT)


def test_an_empty_chain_declines_by_name(monkeypatch, warnings):
    a = _action(monkeypatch, chain=())
    _assert_declined(a._build_and_submit(), warnings, COVERED_CALL_DECLINE_EMPTY_CHAIN)


def test_no_eligible_contract_declines_by_name(monkeypatch, warnings):
    """Nothing at the target %, or everything filtered by volume/OI/spread, or no expiry in
    the window — one reason, because the position is uncovered either way."""
    a = _action(monkeypatch, chain=(_Contract(),), contract=None)
    _assert_declined(a._build_and_submit(), warnings, COVERED_CALL_DECLINE_NO_CONTRACT)


def test_a_contract_with_no_bid_declines_by_name(monkeypatch, warnings):
    """Selling at a bid of 0 collects nothing while capping the upside."""
    a = _action(monkeypatch, chain=(_Contract(),), contract=_Contract(bid=0.0))
    _assert_declined(a._build_and_submit(), warnings, COVERED_CALL_DECLINE_NO_BID)


def test_a_short_account_wide_cover_declines_by_name(monkeypatch, warnings):
    """The seam's own refusal, given the same name and the same one line as the other five."""
    a = _action(monkeypatch, chain=(_Contract(),), contract=_Contract(),
                cover_refusal={"success": False, "message": "COVER REFUSAL: already pledged"})
    result = a._build_and_submit()
    _assert_declined(result, warnings, COVERED_CALL_DECLINE_COVER_SHORT)
    assert "already pledged" in result["message"], (
        "the seam's own wording was lost; only the NAME and the log line are added here")


def test_every_declared_reason_is_exercised():
    """A new decline path that forgets to declare a reason ships silent. This is the ratchet.

    Ordered last in the file so the per-path tests above have run and filled the set.
    """
    assert _exercised == ALL_REASONS, (
        f"declared but never exercised: {sorted(ALL_REASONS - _exercised)}; "
        f"exercised but not declared: {sorted(_exercised - ALL_REASONS)}")
