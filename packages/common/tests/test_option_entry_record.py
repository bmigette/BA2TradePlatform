"""The option entry_record is written by the SHARED submit path (BT/live parity, plan Part C2).

``_OptionEntryAction._submit_option_order`` -- the one choke point every entry builder reaches,
live and backtest -- builds ``data["entry_record"]`` (``option_trade_record_v1``) from the
legs' chosen chain contracts (``OptionLeg.quote``) and puts the same object on the ORDER ROW
(the entry-facts route) and in the ``TradeActionResult``.

The live-vs-backtest equality test (a real BacktestAccount beside an AlpacaAccount over the
same fixture) lives in testplatform/backend/tests/backtest/test_option_entry_record_parity.py.
"""
import json
import os

import pytest

import ba2_common
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_trade_record import (
    LEG_SNAPSHOT_FIELDS, OPTION_TRADE_RECORD_VERSION, STRUCTURE_SNAPSHOT_FIELDS,
)
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection
from tests.test_max_loss_persisted_at_submit import (  # noqa: F401
    BPS, EXPIRY, TODAY, FakeAccount, _own_db, act,
)


def test_worktree_code_is_under_test():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.abspath(ba2_common.__file__).startswith(here)


class RecordingFake(FakeAccount):
    OPTION_GREEKS_SOURCE = "fixture"


def _assert_record_shape(rec, n_legs):
    assert rec["version"] == OPTION_TRADE_RECORD_VERSION
    assert rec["legs_without_quote"] == []
    assert len(rec["legs"]) == n_legs
    for leg in rec["legs"]:
        assert tuple(leg) == LEG_SNAPSHOT_FIELDS
        assert leg["greeks_source"] == "fixture"
        assert leg["spot"] == 100.0
        assert leg["expiry"] == EXPIRY.isoformat()
        assert leg["dte"] == (EXPIRY - TODAY).days
    assert tuple(rec["structure"]) == STRUCTURE_SNAPSHOT_FIELDS
    json.dumps(rec, allow_nan=False)


def test_long_call_record_with_breakeven_and_unbounded_profit():
    acct = RecordingFake()
    res = act(acct, "buy_call", strike_method="percent_otm", strike_param=5.0,
              dte_min=10, dte_max=40, sizing=5.0).execute()
    assert res["success"], res["message"]
    rec = res["data"]["entry_record"]
    _assert_record_shape(rec, 1)
    sub = acct.submitted[-1]
    leg = rec["legs"][0]
    assert leg["contract_symbol"] == sub["legs"][0].contract_symbol
    assert leg["right"] == "call" and leg["moneyness_pct"] == pytest.approx(5.0)
    st = rec["structure"]
    assert st["strategy"] == "long_call"
    assert st["quantity"] == sub["quantity"] and st["multiplier"] == 100
    assert st["net_price"] == sub["limit_price"]
    assert st["max_loss"] == pytest.approx(sub["limit_price"] * 100)
    assert st["max_loss_state"] == "MEASURED"
    assert st["max_profit"] is None and st["max_profit_state"] == "UNBOUNDED"
    assert st["breakevens"] == [pytest.approx(105.0 + sub["limit_price"])]


def test_bull_put_spread_record_both_limits_measured():
    acct = RecordingFake()
    res = act(acct, "open_bull_put_spread", **BPS).execute()
    assert res["success"], res["message"]
    rec = res["data"]["entry_record"]
    _assert_record_shape(rec, 2)
    sub = acct.submitted[-1]
    assert [l["contract_symbol"] for l in rec["legs"]] == \
        [l.contract_symbol for l in sub["legs"]]
    for leg in rec["legs"]:
        assert leg["right"] == "put" and leg["moneyness_pct"] > 0   # both OTM puts
    st = rec["structure"]
    credit = -sub["limit_price"]
    assert st["max_profit"] == pytest.approx(credit * 100)
    assert st["max_loss"] == pytest.approx(res["data"]["max_loss_per_contract"])
    short = [l for l in sub["legs"] if l.side == OrderDirection.SELL][0]
    assert st["breakevens"] == [pytest.approx(short.strike - credit)]


def test_the_record_reaches_the_stored_order_row():
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    row_id = add_instance(TradingOrder(account_id=1, symbol="XYZ", quantity=1,
                                       side=OrderDirection.SELL, order_type=OrderType.MARKET,
                                       status=OrderStatus.PENDING))
    acct = RecordingFake()
    acct.next_order_id = row_id
    res = act(acct, "open_bull_put_spread", **BPS).execute()
    assert res["success"], res["message"]
    stored = get_instance(TradingOrder, row_id)
    assert stored.data["entry_record"] == res["data"]["entry_record"]


def test_a_leg_without_a_chain_contract_is_named_not_dropped_silently():
    acct = RecordingFake()
    a = act(acct, "open_bull_put_spread", **BPS)
    bare = OptionLeg(contract_symbol="XYZ100C", side=OrderDirection.SELL,
                     position_intent="sell_to_open", option_type=OptionRight.CALL,
                     strike=100.0, expiry=EXPIRY, underlying="XYZ")
    a._spot()
    res = a._submit_option_order([bare], 1, 3.0, "covered_call")
    assert res["success"], res["message"]
    rec = res["data"]["entry_record"]
    assert rec["legs"] == [] and rec["legs_without_quote"] == ["XYZ100C"]
    assert rec["structure"]["max_loss_state"] == "UNBOUNDED"


def test_spot_unreadable_refuses_the_entry_before_the_broker(monkeypatch):
    from ba2_common.core import TradeActions as ta
    errors = []
    monkeypatch.setattr(ta.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    class NoSpot(RecordingFake):
        def get_instrument_current_price(self, symbol, price_type=None):
            return None
    acct = NoSpot()
    chain_leg = acct.get_option_chain("XYZ", None, None, OptionRight.CALL)[4]
    a = act(acct, "open_bull_put_spread", **BPS)
    leg = OptionLeg(contract_symbol=chain_leg.symbol, side=OrderDirection.BUY,
                    position_intent="buy_to_open", option_type=OptionRight.CALL,
                    strike=chain_leg.strike, expiry=EXPIRY, underlying="XYZ", quote=chain_leg)
    res = a._submit_option_order([leg], 1, 3.0, "long_call")
    assert res["success"] is False
    assert "REFUSED before the broker" in res["message"] and "XYZ" in res["message"]
    assert "XYZ" in res["data"]["entry_record_refusal"]
    assert "entry_record" not in res["data"]
    assert acct.submitted == []
    assert any("XYZ" in m and "REFUSED" in m for m in errors)


def test_an_undeclared_greeks_source_is_logged_and_the_entry_proceeds(monkeypatch):
    """I3 (controller decision): the record is analysis data. An account with no declared
    greeks source is a loud ERROR (with the traceback) and an ``error`` record on the row --
    and the entry itself is NOT blocked."""
    from ba2_common.core import TradeActions as ta
    errors = []
    monkeypatch.setattr(ta.logger, "error",
                        lambda msg, *a, **k: errors.append((str(msg), k.get("exc_info"))))
    monkeypatch.setattr(OptionsAccountInterface, "OPTION_GREEKS_SOURCE", None)
    acct = FakeAccount()                 # declares none of its own
    res = act(acct, "buy_call", strike_method="percent_otm", strike_param=5.0,
              dte_min=10, dte_max=40, sizing=5.0).execute()
    assert res["success"], res["message"]
    assert len(acct.submitted) == 1
    rec = res["data"]["entry_record"]
    assert rec == {"version": OPTION_TRADE_RECORD_VERSION,
                   "error": rec["error"]}
    assert rec["error"].startswith("OptionGreeksSourceUndeclared: FakeAccount")
    assert any("XYZ" in m and exc for m, exc in errors)


def test_any_other_record_failure_is_an_error_record_and_the_entry_proceeds(monkeypatch):
    from ba2_common.core import TradeActions as ta
    errors = []
    monkeypatch.setattr(ta.logger, "error",
                        lambda msg, *a, **k: errors.append((str(msg), k.get("exc_info"))))

    def boom(*a, **k):
        raise KeyError("payoff exploded")
    monkeypatch.setattr(ta, "build_payoff_chart", boom)
    acct = RecordingFake()
    res = act(acct, "open_bull_put_spread", **BPS).execute()
    assert res["success"], res["message"]
    assert len(acct.submitted) == 1
    assert res["data"]["entry_record"] == {"version": OPTION_TRADE_RECORD_VERSION,
                                           "error": "KeyError: 'payoff exploded'"}
    assert any(exc for _, exc in errors), "logged at ERROR with exc_info"


@pytest.mark.parametrize("spot", [__import__("decimal").Decimal("100.0"),
                                  __import__("numpy").float64(100.0),
                                  __import__("numpy").int64(100)])
def test_a_numpy_or_decimal_spot_is_usable(spot):
    """The record's spot rule is ``usable_spot`` (``_num(spot) > 0``), the same one
    ``leg_snapshot`` applies -- a Decimal or numpy price is a price."""
    acct = RecordingFake()
    a = act(acct, "open_bull_put_spread", **BPS)
    chain_leg = acct.get_option_chain("XYZ", None, None, OptionRight.CALL)[4]
    leg = OptionLeg(contract_symbol=chain_leg.symbol, side=OrderDirection.BUY,
                    position_intent="buy_to_open", option_type=OptionRight.CALL,
                    strike=chain_leg.strike, expiry=EXPIRY, underlying="XYZ", quote=chain_leg)
    a._last_spot = spot
    res = a._submit_option_order([leg], 1, 3.0, "long_call")
    assert res["success"], res["message"]
    assert res["data"]["entry_record"]["legs"][0]["spot"] == 100.0


@pytest.mark.parametrize("spot", [0.0, -5.0, float("nan"), float("inf"), "100"])
def test_an_unusable_spot_refuses_like_a_missing_one(spot):
    acct = RecordingFake()
    a = act(acct, "open_bull_put_spread", **BPS)
    chain_leg = acct.get_option_chain("XYZ", None, None, OptionRight.CALL)[4]
    leg = OptionLeg(contract_symbol=chain_leg.symbol, side=OrderDirection.BUY,
                    position_intent="buy_to_open", option_type=OptionRight.CALL,
                    strike=chain_leg.strike, expiry=EXPIRY, underlying="XYZ", quote=chain_leg)
    a._last_spot = spot
    res = a._submit_option_order([leg], 1, 3.0, "long_call")
    assert res["success"] is False and "REFUSED before the broker" in res["message"]
    assert acct.submitted == []


def test_execute_resets_the_remembered_spot():
    acct = RecordingFake()
    a = act(acct, "buy_call", strike_method="percent_otm", strike_param=5.0,
            dte_min=10, dte_max=40, sizing=5.0)
    a._last_spot = 55.0               # a previous execute's value
    res = a.execute()
    assert res["success"], res["message"]
    assert res["data"]["entry_record"]["legs"][0]["spot"] == 100.0


def test_record_legs_carry_the_order_legs_side_ratio_and_intent():
    acct = RecordingFake()
    res = act(acct, "open_bull_put_spread", **BPS).execute()
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    got = [(l["side"], l["ratio_qty"], l["position_intent"])
           for l in res["data"]["entry_record"]["legs"]]
    want = [(l.side.value.lower(), l.ratio_qty, l.position_intent) for l in sub["legs"]]
    assert got == want
    assert {g[0] for g in got} == {"buy", "sell"}


def test_the_interface_declares_no_greeks_source_itself(monkeypatch):
    # The conftest declares one for this suite's fakes; undo it to read the real default.
    # The production accounts' declarations (Alpaca "broker", BacktestAccount
    # "bs_from_close") are pinned in the backend parity test.
    monkeypatch.undo()
    assert OptionsAccountInterface.OPTION_GREEKS_SOURCE is None


# --------------------------------------------------------------------------------------
# Every entry builder hands the submit path a chosen contract on EVERY leg
# --------------------------------------------------------------------------------------
from ba2_common.core.types import ExpertActionType, get_option_entry_action_values  # noqa: E402


#: open_pmcc needs a two-expiry chain the shared sweep fixture does not carry; it is swept by
#: ``test_the_pmcc_entry_quotes_both_legs`` below on the PMCC suite's own account.
_SWEPT = sorted(set(get_option_entry_action_values()) - {ExpertActionType.OPEN_PMCC.value})


def test_the_sweep_leaves_out_only_the_pmcc():
    assert set(get_option_entry_action_values()) - set(_SWEPT) == {"open_pmcc"}


@pytest.mark.parametrize("action_type", _SWEPT)
def test_every_entry_builder_quotes_every_leg(action_type, monkeypatch):
    from ba2_common.core import TradeActions as ta
    from ba2_common.core.interfaces.OptionsAccountInterface import CoverCapacity
    from tests.test_option_spot_basis_seam import _Acct, _run

    seen = []
    real = ta._OptionEntryAction._build_entry_record

    def spy(self, legs, **kw):
        seen.append(list(legs))
        return real(self, legs, **kw)
    monkeypatch.setattr(ta._OptionEntryAction, "_build_entry_record", spy)
    monkeypatch.setattr(OptionsAccountInterface, "check_cover_for_covered_call",
                        lambda self, legs, q, s: CoverCapacity(True))
    _, res = _run(action_type, _Acct(), monkeypatch=monkeypatch)
    assert seen, f"{action_type} never reached the shared submit path: {res['message']}"
    for legs in seen:
        assert all(leg.quote is not None for leg in legs), \
            [l.contract_symbol for l in legs if l.quote is None]
    rec = res["data"]["entry_record"]
    assert "error" not in rec, rec
    assert rec["legs_without_quote"] == []
    assert len(rec["legs"]) == len(seen[-1])


# --------------------------------------------------------------------------------------
# I1: the PMCC roll writes a record for its new short
# --------------------------------------------------------------------------------------
def test_the_pmcc_roll_writes_an_entry_record_for_the_new_short():
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import TradingOrder
    from tests.test_pmcc_lifecycle import (
        NEW_SHORT, OLD_SHORT, PMCCAccount, _arrive_at_roll_day, _open, _roll, submitted)

    acct, parent = _open(PMCCAccount())
    _arrive_at_roll_day(acct)
    res = _roll(acct, parent)
    assert res["success"], res["message"]
    rec = res["data"]["entry_record"]
    assert rec["version"] == OPTION_TRADE_RECORD_VERSION
    assert rec["legs_without_quote"] == [OLD_SHORT]
    assert [l["contract_symbol"] for l in rec["legs"]] == [NEW_SHORT]
    leg = rec["legs"][0]
    assert tuple(leg) == LEG_SNAPSHOT_FIELDS
    assert (leg["side"], leg["position_intent"]) == ("sell", "sell_to_open")
    assert rec["structure"]["strategy"] == "pmcc_roll"
    assert rec["structure"]["max_loss"] is None
    roll_parent, _, _ = submitted(acct)
    stored = get_instance(TradingOrder, roll_parent.id)
    assert stored.data["entry_record"] == rec


def test_the_pmcc_entry_quotes_both_legs():
    from tests.test_pmcc_lifecycle import PMCCAccount, run
    acct, res = run(PMCCAccount())
    assert res["success"], res["message"]
    rec = res["data"]["entry_record"]
    assert rec["legs_without_quote"] == [] and len(rec["legs"]) == 2
    assert {l["side"] for l in rec["legs"]} == {"buy", "sell"}
    # Two expiries: the combined expiration payoff is not a number, and says why.
    assert rec["structure"]["breakevens"] is None
    assert rec["structure"]["payoff_unavailable_reason"]
