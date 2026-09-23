"""BT/live option parity B5: the grid's volume gate sees the SAME liquidity in both paths.

Plan: ``docs/plans/2026-09-22-bt-live-option-parity.md`` step B5.

THE BUG THIS GATES (review P1). The options grid stamps ``option_min_volume=25`` onto every
option strategy (``ba2test_launcher._apply_option_min_volume``). ``AlpacaAccount
.get_option_chain`` never set ``volume``, so ``check_liquidity_data_available`` refused every
live chain (``OptionLiquidityDataUnavailable``): a grid option strategy deployed live never
traded, while its backtest traded happily.

THE RULE BOTH PATHS NOW SHARE. A backtest bar D decides with data through D's close and is the
live decision made during the NEXT session N(D); both read data session D
(``market_calendar.decision_data_session``), and a contract's volume is what it traded in
exactly that session (``option_session.session_volume``), else 0.

What is compared: the REAL ``BuyCallAction._resolve`` (the grid's O_LC entry, config built by
the launcher exactly as the grid builds it) run once against a ``BacktestAccount`` reading a
tiny parquet option store at bar D, and once against an ``AlpacaAccount`` reading a raw Alpaca
snapshot fixture at N(D) 09:35 ET. No network: the Alpaca raw client is a fake.

The fixture is built so that each divergence this plan closed picks a DIFFERENT contract:
  * a contract that traded heavily three sessions before D but not on D (the BT's old
    carried-forward volume would admit it),
  * a contract whose PARTIAL bar of N(D) is heavy but whose bar of D is thin (a live read of
    ``dailyBar`` instead of the data session's bar would admit it -- lookahead),
and the 2%-OTM target strike sits on exactly those two.

Run from testplatform/backend:
    python -m pytest tests/backtest/test_option_liquidity_bt_live_parity.py -q
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
from ba2_common.core.market_calendar import backtest_decision_label, decision_data_session
from ba2_common.core.option_selector import (
    OptionLiquidityDataUnavailable,
    check_liquidity_data_available,
)
from ba2_common.core.rule_builders import _option_action_config
from ba2_common.core.types import OptionRight, OrderRecommendation

from app.services.backtest.parquet_options_provider import (
    ParquetOptionsProvider,
    clear_worker_parquet_options_cache,
)

# ``ba2test_launcher`` resolves from THIS checkout via tests/backtest/conftest.py.
import ba2test_launcher as L

_UNDER = "ZZ"
_EXP = date(2023, 2, 17)
_SPOT = 100.0
#: Bar D (a Tuesday), the session three before it, and N(D) -- the live session it is.
_D = date(2023, 1, 10)
_D_MINUS_3 = date(2023, 1, 5)
_N_D = date(2023, 1, 11)
#: 09:35 ET on N(D); January is EST (UTC-5).
_LIVE_AT = datetime(2023, 1, 11, 14, 35, tzinfo=timezone.utc)

#: strike -> (volume on D-3, volume on D or None for no bar, volume of N(D)'s partial bar)
_BOOK = {
    100.0: (40, 80, 2),       # liquid on D: the right answer
    102.0: (900, None, 0),    # the 2%-OTM target: traded on D-3 only (stale-volume trap)
    103.0: (5, 10, 700),      # thin on D, heavy this morning (lookahead trap)
    106.0: (30, 30, 1),       # liquid but further from target
}


def _occ(strike: float) -> str:
    return f"{_UNDER}230217C{int(round(strike * 1000)):08d}"


def _grid_o_lc_action_config() -> dict:
    """The O_LC entry exactly as the grid builds it: the launcher's strategy row (which
    carries ``option_min_volume`` from ``_apply_option_min_volume``), mapped to the action
    config the evaluator hands the action ctor by the shared ``rule_builders`` map."""
    rule = L._option_entry_action_for("O_LC")
    cfg = _option_action_config(rule["action_type"], rule)
    assert cfg["min_volume"] == L._OPTION_MIN_VOLUME_DEFAULT == 25
    return cfg


def _buy_call(account, cfg):
    from ba2_common.core.TradeActions import BuyCallAction

    kwargs = {k: v for k, v in cfg.items() if k != "action_type"}
    return BuyCallAction(_UNDER, account, OrderRecommendation.BUY, **kwargs)


# --------------------------------------------------------------------------- #
# Backtest side: a BacktestAccount over a parquet option store, on bar D
# --------------------------------------------------------------------------- #
@pytest.fixture
def bt_account(tmp_path):
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    from app.services.backtest.backtest_account import BacktestAccount

    rows = []
    for strike, (vol_d3, vol_d, _today) in _BOOK.items():
        rows.append(OptionEodBar(occ_symbol=_occ(strike), bar_date=_D_MINUS_3, open=3.0,
                                 high=3.2, low=2.9, close=3.1, volume=vol_d3, bid=3.0, ask=3.2,
                                 open_interest=None))
        if vol_d is not None:
            rows.append(OptionEodBar(occ_symbol=_occ(strike), bar_date=_D, open=3.0, high=3.2,
                                     low=2.9, close=3.1, volume=vol_d, bid=3.0, ask=3.2,
                                     open_interest=None))
    root = str(tmp_path / "ThetaDataOptionsProvider")
    OptionHistoryParquetStore(root=root).write_partition(
        _UNDER, _EXP, rows, start=date(2023, 1, 1), end=date(2023, 3, 31))
    clear_worker_parquet_options_cache()
    provider = ParquetOptionsProvider(root, spot_source=lambda u, on: _SPOT,
                                      risk_free_rate=0.045, spot_scope="test")

    # The REAL BacktestAccount option read (get_option_chain -> _option_data_session), on a
    # double carrying only what that read touches: the option reader and the simulated clock.
    acct = BacktestAccount.__new__(BacktestAccount)
    acct._options = provider
    acct._price = SimpleNamespace(now=lambda: datetime(2023, 1, 10, 16, 0))
    acct.get_instrument_current_price = lambda symbol, price_type=None: _SPOT
    yield acct
    clear_worker_parquet_options_cache()


# --------------------------------------------------------------------------- #
# Live side: an AlpacaAccount over a raw snapshot fixture, at N(D) 09:35 ET
# --------------------------------------------------------------------------- #
def _bar(day: date, volume: int) -> dict:
    # Alpaca stamps a daily bar at New York midnight: 05:00Z under EST.
    return {"t": f"{day.isoformat()}T05:00:00Z", "o": 3.0, "h": 3.2, "l": 2.9, "c": 3.1,
            "v": volume, "n": 5, "vw": 3.1}


def _raw_snapshot(vol_d3, vol_d, vol_today) -> dict:
    """What Alpaca returns at 09:35 ET on N(D) once a contract has traded this morning:
    ``dailyBar`` is N(D)'s PARTIAL bar and ``prevDailyBar`` the latest completed session it
    traded in -- D, or D-3 for a contract that did not trade on D."""
    snap = {"latestQuote": {"t": "2023-01-11T14:34:59Z", "bp": 3.0, "ap": 3.2, "bs": 10,
                            "as": 10},
            "latestTrade": {"t": "2023-01-11T14:30:00Z", "p": 3.1, "s": 1},
            "greeks": {"delta": 0.5, "gamma": 0.05, "theta": -0.03, "vega": 0.1, "rho": 0.04},
            "impliedVolatility": 0.4}
    last_completed = _bar(_D, vol_d) if vol_d is not None else _bar(_D_MINUS_3, vol_d3)
    if vol_today:
        snap["dailyBar"] = _bar(_N_D, vol_today)
        snap["prevDailyBar"] = last_completed
    else:
        # No trade yet this morning: dailyBar is still the last completed bar, and
        # prevDailyBar the one before it.
        snap["dailyBar"] = last_completed
        snap["prevDailyBar"] = _bar(date(2023, 1, 4), 50)
    return snap


@pytest.fixture
def live_account(monkeypatch):
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    snapshots = {_occ(k): _raw_snapshot(*v) for k, v in _BOOK.items()}
    metas = {_occ(k): SimpleNamespace(symbol=_occ(k), underlying_symbol=_UNDER,
                                      root_symbol=_UNDER, type=SimpleNamespace(value="call"),
                                      strike_price=k, expiration_date=_EXP,
                                      open_interest=None, size="100")
             for k in _BOOK}

    class FakeRawClient:
        def get_option_chain(self, request):
            assert request.underlying_symbol == _UNDER
            return snapshots

    acct = AlpacaAccount.__new__(AlpacaAccount)        # no __init__: no DB, no broker
    acct.id = 9002
    acct._settings_cache = {"api_key": "k", "api_secret": "s"}
    acct._option_data_client_raw = FakeRawClient()
    monkeypatch.setattr(acct, "_get_option_contracts_meta", lambda *a, **k: metas,
                        raising=False)
    monkeypatch.setattr(acct, "get_instrument_current_price",
                        lambda symbol, price_type=None: _SPOT, raising=False)
    # THE live clock, and only it: both the chain's data session and the action's DTE label
    # derive from it (OptionsAccountInterface.decision_label).
    _set_live_clock(monkeypatch, _LIVE_AT)
    return acct


def _set_live_clock(monkeypatch, instant: datetime) -> None:
    from ba2_common.core import option_session
    monkeypatch.setattr(option_session, "live_decision_time", lambda: instant)


def _expiry_window(today: date, cfg: dict):
    from datetime import timedelta
    return today + timedelta(days=cfg["dte_min"]), today + timedelta(days=cfg["dte_max"])


# --------------------------------------------------------------------------- #
# The DTE label: the session the decision's orders execute in, on both paths
# --------------------------------------------------------------------------- #
def test_backtest_bar_d_labels_its_option_entry_with_the_next_session(bt_account):
    """Bar D's orders fill on the next bar, so its DTE window is anchored on N(D), not D."""
    action = _buy_call(bt_account, _grid_o_lc_action_config())
    assert action._today() == _N_D


@pytest.mark.parametrize("instant", [
    _LIVE_AT,                                                  # 09:35 ET on N(D)
    datetime(2023, 1, 12, 4, 30, tzinfo=timezone.utc),         # 23:30 ET on N(D): UTC is D+2
], ids=["0935_et", "2330_et_utc_next_day"])
def test_live_labels_its_option_entry_with_the_new_york_date(live_account, monkeypatch, instant):
    _set_live_clock(monkeypatch, instant)
    assert _buy_call(live_account, _grid_o_lc_action_config())._today() == _N_D


def test_live_on_a_saturday_labels_the_saturday(live_account, monkeypatch):
    """The label is the New York CALENDAR date, never rolled to a session: DTE counts
    calendar days from it (``expiry - label``), whatever day of the week it is."""
    saturday = date(2023, 1, 14)
    _set_live_clock(monkeypatch, datetime(2023, 1, 14, 15, 0, tzinfo=timezone.utc))
    action = _buy_call(live_account, _grid_o_lc_action_config())
    assert action._today() == saturday
    assert action._dte_for(_EXP) == (_EXP - saturday).days


def _dte_exit_reference(account, created_at):
    """The date ``DaysToExpiryCondition`` (the ``opt_dte`` exit) counts remaining life from."""
    from ba2_common.core.TradeConditions import DaysToExpiryCondition

    cond = DaysToExpiryCondition(
        account=account, instrument_name=_UNDER,
        expert_recommendation=SimpleNamespace(created_at=created_at, instance_id=1),
        operator_str="<=", value=21, existing_order=None)
    return cond._as_of_date()


def test_backtest_entry_and_exit_count_dte_from_the_same_next_session(bt_account):
    """Bar D: the entry's window and the exit's remaining life both count from N(D). The exit
    used to count from the recommendation's created_at (D), one session behind the entry."""
    entry = _buy_call(bt_account, _grid_o_lc_action_config())._today()
    exit_ = _dte_exit_reference(bt_account, datetime(2023, 1, 10, 21, 0, tzinfo=timezone.utc))
    assert entry == exit_ == _N_D


def test_live_evening_entry_and_exit_count_dte_from_the_new_york_date(live_account,
                                                                      monkeypatch):
    """21:00 ET on N(D) is already the next day in UTC; both count from the NY date N(D)."""
    evening = datetime(2023, 1, 12, 2, 0, tzinfo=timezone.utc)
    _set_live_clock(monkeypatch, evening)
    entry = _buy_call(live_account, _grid_o_lc_action_config())._today()
    exit_ = _dte_exit_reference(live_account, evening)
    assert entry == exit_ == _N_D


def test_bt_and_live_count_the_same_dte_for_the_same_contract(bt_account, live_account):
    cfg = _grid_o_lc_action_config()
    assert _buy_call(bt_account, cfg)._dte_for(_EXP) == _buy_call(live_account, cfg)._dte_for(_EXP)


# --------------------------------------------------------------------------- #
# The parity
# --------------------------------------------------------------------------- #
def test_bar_d_and_live_at_next_session_morning_read_the_same_data_session():
    assert decision_data_session(backtest_decision_label(_D)) == _D
    from ba2_common.core.market_calendar import live_decision_label
    assert decision_data_session(live_decision_label(_LIVE_AT)) == _D


def test_every_contract_has_the_same_volume_in_both_chains(bt_account, live_account):
    cfg = _grid_o_lc_action_config()
    bt = {c.symbol: c.volume for c in bt_account.get_option_chain(
        _UNDER, *_expiry_window(_N_D, cfg), OptionRight.CALL)}
    live = {c.symbol: c.volume for c in live_account.get_option_chain(
        _UNDER, *_expiry_window(_N_D, cfg), OptionRight.CALL)}
    assert bt == live == {_occ(100.0): 80, _occ(102.0): 0, _occ(103.0): 10, _occ(106.0): 30}


def test_the_live_chain_passes_the_grids_volume_gate(live_account):
    """THE P1 regression: before B3 no live contract carried a volume, and this raised
    ``OptionLiquidityDataUnavailable`` for every grid option strategy."""
    cfg = _grid_o_lc_action_config()
    chain = live_account.get_option_chain(_UNDER, *_expiry_window(_N_D, cfg), OptionRight.CALL)
    check_liquidity_data_available(chain, min_volume=cfg["min_volume"], underlying=_UNDER,
                                   source="AlpacaAccount")
    stripped = [dataclasses.replace(c, volume=None) for c in chain]
    with pytest.raises(OptionLiquidityDataUnavailable):
        check_liquidity_data_available(stripped, min_volume=cfg["min_volume"],
                                       underlying=_UNDER, source="parity-no-volume-source")


def test_the_grid_long_call_picks_the_same_contract_in_backtest_and_live(bt_account,
                                                                         live_account):
    cfg = _grid_o_lc_action_config()
    bt = _buy_call(bt_account, cfg)._resolve()
    live = _buy_call(live_account, cfg)._resolve()
    assert not isinstance(bt, dict), bt
    assert not isinstance(live, dict), live
    bt_pick = [leg.contract_symbol for leg in bt.legs]
    live_pick = [leg.contract_symbol for leg in live.legs]
    # 2% OTM of 100 targets 102: 102 traded only on D-3 and 103 only thinly on D (its heavy
    # bar is N(D)'s, which neither path may read), so both paths fall back to 100.
    assert bt_pick == live_pick == [_occ(100.0)]


def test_the_backtest_chain_request_is_anchored_on_the_next_session(bt_account):
    """The expiry window the action ASKS the backtest chain for starts at N(D) + dte_min,
    exactly as live at N(D) asks: the fetch itself, not only the in-memory DTE filter."""
    from datetime import timedelta

    from app.services.backtest.backtest_account import BacktestAccount

    cfg = _grid_o_lc_action_config()
    requests = []

    def recording_chain(underlying, expiry_min, expiry_max, option_type=None, **kw):
        requests.append((expiry_min, expiry_max))
        return BacktestAccount.get_option_chain(bt_account, underlying, expiry_min,
                                                expiry_max, option_type, **kw)

    bt_account.get_option_chain = recording_chain
    _buy_call(bt_account, cfg)._resolve()
    assert requests == [(_N_D + timedelta(days=cfg["dte_min"]),
                         _N_D + timedelta(days=cfg["dte_max"]))]
