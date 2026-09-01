# packages/common/tests/test_option_close_concession.py
"""Review 2026-08-30 F7 — exit fills were a FILTER, not a cost.

Entries pay the modelled spread through the ``option_entry_cross`` concession
(``_submit_option_order`` -> ``entry_limit_with_concession``); closes quoted
``quote.bid``/``quote.ask`` raw. In LIVE those are the real touch — already the crossing
side — but the backtest's historical store synthesizes ``bid == ask == close`` (the MID),
so a close's limit sat AT the mid and the fill rule (``_option_cross``: cross first, then
re-test) required the fill-day mid to move half a spread in the position's favor before
anything filled. TP/SL/DTE exits slipped days and migrated to the spread-free
expiry-settlement path.

The fix sits at the SAME seam the entry concession uses — the account's
``option_modelled_half_spread`` duck-type, which only a simulator that models a spread
implements — so a live account's close quote is byte-identical (pinned below):

  * a DISCRETIONARY close (TP, time) concedes the SAME fraction the position's ENTRY
    conceded (read from the entry order's persisted ``data['entry_cross']`` — no new
    gene);
  * a FORCED close (SL stop, DTE/roll — classified off the firing rule's TRIGGERS, not
    its name) crosses the modelled spread FULLY: a risk exit pays up.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import CloseOptionAction, create_action
from ba2_common.core.option_types import OptionPosition, OptionQuote
from ba2_common.core.types import ExpertActionType, OptionRight, OrderDirection


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    """Order-independence: sibling DB-seam tests repoint the global DB without restoring."""
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "close_concession.sqlite"))
    db.init_db()
    yield


MID = 10.0
HALF = 0.5           # modelled half-spread per share
ENTRY_FRACTION = 0.4  # what the entry conceded (persisted on the entry order)

_OCC = "XYZ240621C00100000"


class _QuoteAccount:
    """Bare quote-only account double for the _close_limit_price unit tests."""

    def __init__(self, bid, ask, modelled_half=None):
        self._quote = OptionQuote(symbol=_OCC, bid=bid, ask=ask, last=bid)
        if modelled_half is not None:
            # Duck-typed backtest seam: only a simulator that models a spread has it.
            self.option_modelled_half_spread = lambda cs: modelled_half

    def get_option_quote(self, contract_symbol):
        return self._quote


def _close_action(account, *, forced=False):
    action = CloseOptionAction.__new__(CloseOptionAction)
    action.instrument_name = "XYZ"
    action.account = account
    action.forced_exit = forced
    return action


def _position(side):
    return OptionPosition(
        contract_symbol=_OCC, underlying="XYZ", option_type=OptionRight.CALL,
        strike=100.0, expiry=date(2024, 6, 21), side=side, quantity=1.0,
        avg_entry_price=3.0)


def _entry_order(entry_cross=ENTRY_FRACTION):
    data = {} if entry_cross is None else {"entry_cross": entry_cross}
    return SimpleNamespace(open_price=3.0, limit_price=3.0, data=data,
                           parent_order_id=None)


def test_discretionary_close_concedes_the_entrys_cross_fraction():
    """On a synthetic bid==ask==MID quote, a TP close pays fraction x half-spread in the
    adverse direction — the SAME fraction its entry conceded — instead of quoting the mid
    and waiting for favorable drift."""
    acct = _QuoteAccount(bid=MID, ask=MID, modelled_half=HALF)
    # SHORT position -> close is a BUY: pay UP.
    buy_back = _close_action(acct)._close_limit_price(
        _position(OrderDirection.SELL), _entry_order())
    assert buy_back == pytest.approx(MID + ENTRY_FRACTION * HALF)   # 10.20
    # LONG position -> close is a SELL: receive LESS.
    sell = _close_action(acct)._close_limit_price(
        _position(OrderDirection.BUY), _entry_order())
    assert sell == pytest.approx(MID - ENTRY_FRACTION * HALF)       # 9.80


def test_discretionary_close_without_an_entry_concession_quotes_the_mid():
    """An entry that conceded nothing persisted nothing: its discretionary close keeps
    today's mid quote exactly (no invented cost)."""
    acct = _QuoteAccount(bid=MID, ask=MID, modelled_half=HALF)
    px = _close_action(acct)._close_limit_price(
        _position(OrderDirection.SELL), _entry_order(entry_cross=None))
    assert px == pytest.approx(MID)


def test_forced_close_crosses_the_modelled_spread_fully():
    """SL/DTE (forced) closes pay the WHOLE modelled half-spread — a risk exit pays up —
    regardless of what the entry conceded."""
    acct = _QuoteAccount(bid=MID, ask=MID, modelled_half=HALF)
    buy_back = _close_action(acct, forced=True)._close_limit_price(
        _position(OrderDirection.SELL), _entry_order())
    assert buy_back == pytest.approx(MID + HALF)                    # 10.50
    sell = _close_action(acct, forced=True)._close_limit_price(
        _position(OrderDirection.BUY), _entry_order())
    assert sell == pytest.approx(MID - HALF)                        # 9.50


def test_live_close_quote_is_byte_identical():
    """A live account has REAL bid != ask and no modelled spread: the close quotes the
    crossing side exactly as before, forced or not, entry concession or not."""
    acct = _QuoteAccount(bid=9.7, ask=10.3)   # no option_modelled_half_spread
    for forced in (False, True):
        buy_back = _close_action(acct, forced=forced)._close_limit_price(
            _position(OrderDirection.SELL), _entry_order())
        assert buy_back == 10.3               # the ask, untouched
        sell = _close_action(acct, forced=forced)._close_limit_price(
            _position(OrderDirection.BUY), _entry_order())
        assert sell == 9.7                    # the bid, untouched


# ---------------------------------------------------------------------------
# forced-vs-discretionary classification: off the firing rule's TRIGGERS
# ---------------------------------------------------------------------------
def _event_action(triggers):
    return SimpleNamespace(name="rule", triggers=triggers, actions={})


def test_dte_and_loss_side_exits_classify_as_forced():
    from ba2_common.core.TradeActionEvaluator import forced_option_exit

    dte = _event_action({"c0": {"event_type": "days_to_expiry", "operator": "<=", "value": 21}})
    sl_pct = _event_action({"c0": {"event_type": "profit_loss_percent", "operator": "<", "value": -100}})
    sl_amt = _event_action({"c0": {"event_type": "profit_loss_amount", "operator": "<=", "value": -500}})
    tp = _event_action({"c0": {"event_type": "profit_loss_percent", "operator": ">", "value": 50}})
    time_exit = _event_action({"c0": {"event_type": "days_opened", "operator": ">", "value": 28}})
    flag_only = _event_action({"c0": {"event_type": "bearish"}})

    assert forced_option_exit(dte) is True
    assert forced_option_exit(sl_pct) is True
    assert forced_option_exit(sl_amt) is True
    assert forced_option_exit(tp) is False
    assert forced_option_exit(time_exit) is False
    assert forced_option_exit(flag_only) is False


def test_the_grid_emitted_exit_rules_classify_per_the_review_table():
    """Review 2026-08-30 dev-merge FIX 1 — the four exit rules the option grid actually
    emits (``_option_exit_rules``), classified as the review's table requires.

    ``opt_sl_ml`` is the one that regressed: ``loss_pct_of_max_loss`` is a loss MAGNITUDE
    (positive while losing, S8.2), so its stop operator is ``>`` — the inverse of the
    ``profit_loss_*`` convention. Enumerating only the P&L convention made it match no
    test and classify like a take-profit, and on the 9 DEBIT kinds it is the ONLY stop
    emitted (``opt_sl`` is credit-only), so every one of its firings took the
    discretionary mid-quote path F7 exists to remove."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit

    # Exactly the triggers ba2test_launcher._option_exit_rules emits, defaults included.
    opt_sl_ml = _event_action(
        {"c0": {"event_type": "loss_pct_of_max_loss", "operator": ">", "value": 50}})
    opt_sl = _event_action(
        {"c0": {"event_type": "profit_loss_percent", "operator": "<", "value": -100}})
    opt_dte = _event_action(
        {"c0": {"event_type": "days_to_expiry", "operator": "<=", "value": 21}})
    opt_tp = _event_action(
        {"c0": {"event_type": "profit_loss_percent", "operator": ">", "value": 50}})
    # Task 4 (+ follow-up 2026-09-01): the debit-structure take-profit multiple.
    # ``>=`` is the profit side (like opt_tp) -- discretionary. ``<`` is a de-facto
    # loss-side stop (worth less than paid, affine-identical to profit_loss_percent < 0)
    # -- registered in _LOSS_SIDE_STOP_OPERATORS under the SIGNED-RESULT convention, so
    # it must classify forced, exactly like opt_sl.
    opt_tp_mult = _event_action(
        {"c0": {"event_type": "profit_multiple_of_premium", "operator": ">=", "value": 3.0}})
    opt_tp_mult_loss_side = _event_action(
        {"c0": {"event_type": "profit_multiple_of_premium", "operator": "<", "value": 1.0}})

    assert forced_option_exit(opt_sl_ml) is True    # loss-side of an INVERTED-sign field
    assert forced_option_exit(opt_sl) is True       # loss-side of a signed-P&L field
    assert forced_option_exit(opt_dte) is True      # the DTE/roll exit
    assert forced_option_exit(opt_tp) is False      # a take-profit is discretionary
    assert forced_option_exit(opt_tp_mult) is False  # profit-multiple TP (>=) is discretionary
    assert forced_option_exit(opt_tp_mult_loss_side) is True  # profit-multiple stop (<) is forced


def test_loss_pct_of_max_loss_take_profit_side_stays_discretionary():
    """The inverted convention cuts BOTH ways: ``loss_pct_of_max_loss < N`` reads
    "the loss is SHALLOWER than N% of max loss" — a profit-side gate, not a stop."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit

    assert forced_option_exit(_event_action(
        {"c0": {"event_type": "loss_pct_of_max_loss", "operator": "<", "value": -25}})) is False


def test_profit_multiple_of_premium_below_one_classifies_as_forced():
    """The SIGNED-RESULT convention cuts BOTH ways too: ``profit_multiple_of_premium``
    below 1.0 reads "worth less than what was paid" -- a de-facto loss-side stop,
    affine-identical to ``profit_loss_percent < 0`` (``multiple = 1 + percent / 100``).
    Mirrors ``test_loss_pct_of_max_loss_take_profit_side_stays_discretionary`` above, but
    for the OTHER direction: there the inverted field's ``<`` side stays discretionary,
    here the signed-result field's ``<`` side is the stop."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit

    assert forced_option_exit(_event_action(
        {"c0": {"event_type": "profit_multiple_of_premium", "operator": "<", "value": 1.0}})) is True


def test_an_opt_sl_ml_close_pays_the_full_modelled_spread():
    """End to end from the firing rule to the quote: the grid's ``opt_sl_ml`` rule must
    reach ``_close_limit_price`` as a FORCED exit and pay the whole modelled half-spread
    (``ENTRY_CROSS_FULL``), not the entry's ``entry_cross`` fraction."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.types import OrderRecommendation

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = _QuoteAccount(bid=MID, ask=MID, modelled_half=HALF)
    rec = SimpleNamespace(id=1, instance_id=None,
                          recommended_action=OrderRecommendation.SELL)
    sl_ml_rule = _event_action(
        {"c0": {"event_type": "loss_pct_of_max_loss", "operator": ">", "value": 50}})

    action = ev._create_trade_action(
        ExpertActionType.CLOSE_OPTION, {"action_type": "close_option"}, "XYZ",
        OrderRecommendation.SELL, None, rec, event_action=sl_ml_rule)
    assert isinstance(action, CloseOptionAction) and action.forced_exit is True

    # The entry conceded only ENTRY_FRACTION; a forced close must ignore that.
    buy_back = action._close_limit_price(_position(OrderDirection.SELL), _entry_order())
    assert buy_back == pytest.approx(MID + HALF)                    # 10.50, not 10.20
    sell = action._close_limit_price(_position(OrderDirection.BUY), _entry_order())
    assert sell == pytest.approx(MID - HALF)                        # 9.50, not 9.80


def test_the_evaluator_threads_the_forced_flag_into_the_close_action():
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator
    from ba2_common.core.types import OrderRecommendation

    ev = TradeActionEvaluator.__new__(TradeActionEvaluator)
    ev.account = _QuoteAccount(bid=MID, ask=MID, modelled_half=HALF)
    rec = SimpleNamespace(id=1, instance_id=None,
                          recommended_action=OrderRecommendation.SELL)
    dte_rule = _event_action(
        {"c0": {"event_type": "days_to_expiry", "operator": "<=", "value": 21}})
    tp_rule = _event_action(
        {"c0": {"event_type": "profit_loss_percent", "operator": ">", "value": 50}})

    forced = ev._create_trade_action(
        ExpertActionType.CLOSE_OPTION, {"action_type": "close_option"}, "XYZ",
        OrderRecommendation.SELL, None, rec, event_action=dte_rule)
    assert isinstance(forced, CloseOptionAction) and forced.forced_exit is True

    tp = ev._create_trade_action(
        ExpertActionType.CLOSE_OPTION, {"action_type": "close_option"}, "XYZ",
        OrderRecommendation.SELL, None, rec, event_action=tp_rule)
    assert isinstance(tp, CloseOptionAction) and tp.forced_exit is False


def test_create_action_accepts_and_defaults_the_forced_flag():
    acct = _QuoteAccount(bid=MID, ask=MID)
    rec = SimpleNamespace(id=1, instance_id=None)
    a = create_action(ExpertActionType.CLOSE_OPTION, "XYZ", acct,
                      SimpleNamespace(), None, rec, forced_exit=True)
    assert a.forced_exit is True
    b = create_action(ExpertActionType.CLOSE_OPTION, "XYZ", acct,
                      SimpleNamespace(), None, rec)
    assert b.forced_exit is False


# ---------------------------------------------------------------------------
# the entry persists its concession fraction for the close to read
# ---------------------------------------------------------------------------
def test_the_entry_order_persists_its_cross_fraction():
    """The close reuses the ENTRY's fraction, so the entry must write it to the order row
    whenever it concedes (not only into the TradeActionResult)."""
    from tests.test_new_option_actions import FakeAccount, _mk
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.types import OrderStatus, OrderType

    class PersistingAccount(FakeAccount):
        """FakeAccount whose submit_option_order writes a REAL TradingOrder row, so the
        post-submit data persist in _submit_option_order has a row to update."""

        def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                                option_strategy, expert_recommendation_id=None,
                                transaction_id=None):
            o = TradingOrder(account_id=1, symbol="AAPL", quantity=quantity,
                             side=OrderDirection.SELL, order_type=OrderType.MARKET,
                             status=OrderStatus.NEW, data={})
            oid = add_instance(o)
            # Detached namespace: _submit_option_order only reads .id off the return.
            return SimpleNamespace(id=oid, data={})

    acct = PersistingAccount()
    rec = SimpleNamespace(id=1, instance_id=None)
    act = create_action(ExpertActionType.OPEN_SHORT_STRANGLE, "AAPL", acct,
                        SimpleNamespace(), None, rec,
                        strike_method="percent_otm", strike_param=10.0,
                        dte_min=20, dte_max=40, sizing=10.0, entry_cross=0.4)
    act.submit_to_broker = True
    res = act.execute()
    assert res["success"], res["message"]
    order = get_instance(TradingOrder, res["data"]["order_id"])
    assert order is not None
    assert order.data.get("entry_cross") == pytest.approx(0.4)
