"""option_trade_record (BT/live option parity, plan Part C1): the shared snapshot builders."""
import json
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core.option_trade_record import (
    LEG_SNAPSHOT_FIELDS, OPTION_TRADE_RECORD_VERSION, STRUCTURE_SNAPSHOT_FIELDS, entry_record,
    leg_snapshot, moneyness_pct, quote_mid_and_spread_pct, structure_snapshot,
)
from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection

LABEL = date(2024, 6, 3)
SESSION = date(2024, 5, 31)
QT = datetime(2024, 6, 3, 13, 35, tzinfo=timezone.utc)


def _full(right=OptionRight.CALL, strike=105.0):
    return OptionContract(
        symbol="XYZ240719C00105000", underlying="XYZ", option_type=right, strike=strike,
        expiry=date(2024, 7, 19), bid=2.0, ask=2.2, last=2.1, implied_volatility=0.31,
        delta=0.42, gamma=0.05, theta=-0.03, vega=0.12, open_interest=1500, volume=320,
        rho=0.04, quote_time=QT)


def _snap(contract, spot=100.0, quote_time=QT, greeks_source="broker",
          side=OrderDirection.BUY, ratio_qty=1, position_intent="buy_to_open"):
    return leg_snapshot(contract, side=side, ratio_qty=ratio_qty,
                        position_intent=position_intent, spot=spot, data_session=SESSION,
                        decision_label=LABEL, greeks_source=greeks_source,
                        quote_time=quote_time)


def test_version_constant():
    assert OPTION_TRADE_RECORD_VERSION == "option_trade_record_v1"


def test_full_contract_every_field():
    s = _snap(_full())
    assert tuple(s) == LEG_SNAPSHOT_FIELDS
    assert s["contract_symbol"] == "XYZ240719C00105000"
    assert s["side"] == "buy" and s["ratio_qty"] == 1
    assert s["position_intent"] == "buy_to_open"
    assert s["right"] == "call"
    assert s["strike"] == 105.0
    assert s["expiry"] == "2024-07-19"
    assert s["dte"] == (date(2024, 7, 19) - LABEL).days == 46
    assert s["data_session"] == "2024-05-31"
    assert s["spot"] == 100.0
    assert s["moneyness_pct"] == pytest.approx(5.0)
    assert s["bid"] == 2.0 and s["ask"] == 2.2 and s["last"] == 2.1
    assert s["mid"] == pytest.approx(2.1)
    assert s["spread_pct"] == pytest.approx(0.2 / 2.1 * 100)
    assert (s["iv"], s["delta"], s["gamma"], s["theta"], s["vega"], s["rho"]) == \
        (0.31, 0.42, 0.05, -0.03, 0.12, 0.04)
    assert s["open_interest"] == 1500 and s["volume"] == 320
    assert s["greeks_source"] == "broker"
    assert s["quote_time"] == QT.isoformat()


def test_sparse_contract_unknowns_stay_none():
    c = OptionContract(symbol="XYZ240719P00095000", underlying="XYZ",
                       option_type=OptionRight.PUT, strike=95.0, expiry=date(2024, 7, 19))
    s = _snap(c, quote_time=None)
    assert tuple(s) == LEG_SNAPSHOT_FIELDS
    for key in ("bid", "ask", "mid", "spread_pct", "last", "iv", "delta", "gamma", "theta",
                "vega", "rho", "open_interest", "volume", "quote_time"):
        assert s[key] is None, key          # unknown is None, never 0
    assert s["moneyness_pct"] == pytest.approx(5.0)


def test_nan_greek_is_unknown_not_a_number():
    c = _full()
    c.delta = float("nan")
    s = _snap(c)
    assert s["delta"] is None
    json.dumps(s, allow_nan=False)


@pytest.mark.parametrize("right,strike,expected", [
    ("call", 105.0, 5.0), ("call", 95.0, -5.0),     # OTM call +, ITM call -
    ("put", 95.0, 5.0), ("put", 105.0, -5.0),       # OTM put +, ITM put -
])
def test_moneyness_positive_is_otm_for_both_rights(right, strike, expected):
    assert moneyness_pct(right, strike, 100.0) == pytest.approx(expected)
    c = _full(OptionRight(right), strike)
    assert _snap(c)["moneyness_pct"] == pytest.approx(expected)


@pytest.mark.parametrize("bid,ask", [(None, 2.0), (2.0, None), (0.0, 1.0), (2.0, 2.0),
                                     (2.2, 2.0), (float("nan"), 2.0)])
def test_mid_and_spread_only_for_a_valid_two_sided_quote(bid, ask):
    assert quote_mid_and_spread_pct(bid, ask) == (None, None)


def test_iso_serialisation_round_trips_through_json():
    s = _snap(_full())
    back = json.loads(json.dumps(s, allow_nan=False))
    assert back == s
    assert datetime.fromisoformat(back["quote_time"]).tzinfo is not None


def test_naive_quote_time_refused():
    with pytest.raises(ValueError):
        _snap(_full(), quote_time=datetime(2024, 6, 3, 13, 35))


@pytest.mark.parametrize("spot", [None, 0.0, -1.0, float("nan")])
def test_spot_missing_refused_never_zero(spot):
    with pytest.raises(ValueError):
        _snap(_full(), spot=spot)


def test_structure_snapshot_keys_and_json():
    legs = [_snap(_full())]
    st = structure_snapshot(legs, strategy="long_call", quantity=3, multiplier=100,
                            net_price=2.2, max_loss=220.0, max_profit=None,
                            breakevens=[107.2], max_loss_state="MEASURED",
                            max_profit_state="UNBOUNDED")
    assert tuple(st) == STRUCTURE_SNAPSHOT_FIELDS
    assert st["leg_count"] == 1 and st["quantity"] == 3 and st["multiplier"] == 100
    assert st["breakevens"] == [107.2] and st["max_profit"] is None
    rec = entry_record(legs, st, [])
    assert rec["version"] == OPTION_TRADE_RECORD_VERSION
    json.dumps(rec, allow_nan=False)


def test_structure_snapshot_refuses_a_short_leg_snapshot():
    leg = _snap(_full())
    del leg["rho"]
    with pytest.raises(ValueError):
        structure_snapshot([leg], strategy="x", quantity=1, multiplier=100, net_price=1.0,
                           max_loss=None, max_profit=None, breakevens=None)


def test_option_leg_quote_is_in_memory_only():
    c = _full()
    a = OptionLeg(contract_symbol=c.symbol, side=OrderDirection.BUY, quote=c)
    b = OptionLeg(contract_symbol=c.symbol, side=OrderDirection.BUY)
    assert a == b                              # excluded from equality
    assert "quote" not in repr(a)


def test_order_leg_fields_are_recorded():
    s = _snap(_full(), side=OrderDirection.SELL, ratio_qty=2, position_intent="sell_to_open")
    assert (s["side"], s["ratio_qty"], s["position_intent"]) == ("sell", 2, "sell_to_open")
    s = _snap(_full(), position_intent=None)
    assert "position_intent" in s and s["position_intent"] is None


@pytest.mark.parametrize("ratio", [0, -1, None])
def test_bad_ratio_refused(ratio):
    with pytest.raises(ValueError):
        _snap(_full(), ratio_qty=ratio)


def test_the_rows_own_greeks_source_wins_over_the_accounts():
    c = _full()
    c.greeks_source = "chain_snapshot"
    assert _snap(c, greeks_source="bs_from_close")["greeks_source"] == "chain_snapshot"
    c.greeks_source = None
    assert _snap(c, greeks_source="bs_from_close")["greeks_source"] == "bs_from_close"


def test_no_greeks_source_anywhere_is_refused():
    with pytest.raises(ValueError, match="greeks source"):
        _snap(_full(), greeks_source=None)


def test_numpy_and_decimal_quote_sides_are_numbers():
    import decimal
    import numpy as np
    mid, sp = quote_mid_and_spread_pct(np.float64(2.0), decimal.Decimal("2.2"))
    assert mid == pytest.approx(2.1) and sp == pytest.approx(0.2 / 2.1 * 100)
    assert quote_mid_and_spread_pct("2.0", 2.2) == (None, None)     # a string is not a price
    assert quote_mid_and_spread_pct(True, 2.2) == (None, None)
