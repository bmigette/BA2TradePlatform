"""Plan Part E (E1-E4) in the backtest: the option path works in the AS-TRADED basis.

The fixture is REAL data: NFLX's ThetaData chain for 2024-04-24..2024-05-01 (expiries
2024-05-03 and 2024-06-21, strikes 525-600 around the money; ``fixtures/nflx_chain_20240424_20240501.csv``,
copied read-only from the local cache) against NFLX's FMP closes, which are back-adjusted for
the 10:1 split of 2025-11-17. On 2024-05-01 the FMP close is $55.17 and the chain's
put-call-parity spot is ~$553.
"""
from __future__ import annotations

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.split_basis import CalendarSplit, SplitBasisRefused, SymbolSplitBasis
from ba2_common.core.types import OptionRight, OrderDirection


def _in_this_checkout(module_file) -> bool:
    """The module under test is THIS checkout's copy, not a stale editable install elsewhere
    (the venv's editable installs point at the main checkout; a worktree must not test that)."""
    from pathlib import Path as _P
    repo_root = _P(__file__).resolve().parents[4]
    try:
        _P(module_file).resolve().relative_to(repo_root)
        return True
    except ValueError:
        return False

FIXTURE = Path(__file__).parent / "fixtures" / "nflx_chain_20240424_20240501.csv"

#: The real FMP NFLX closes (adjusted) for the fixture window.
_NFLX_FMP = [
    (date(2024, 4, 22), 55.07, 55.86, 54.20, 55.46),
    (date(2024, 4, 23), 55.65, 57.92, 55.53, 57.78),
    (date(2024, 4, 24), 57.43, 57.69, 55.13, 55.51),
    (date(2024, 4, 25), 54.95, 56.65, 54.57, 56.48),
    (date(2024, 4, 26), 55.82, 56.29, 55.32, 56.12),
    (date(2024, 4, 29), 55.92, 55.96, 55.42, 55.95),
    (date(2024, 4, 30), 56.00, 56.00, 54.94, 55.06),
    (date(2024, 5, 1), 54.78, 56.04, 54.43, 55.17),
]
_NFLX_CALENDAR = {"symbol": "NFLX", "historical": [
    {"date": "2025-11-17", "label": "November 17, 25", "numerator": 10, "denominator": 1},
    {"date": "2015-07-15", "label": "July 15, 15", "numerator": 7, "denominator": 1},
    {"date": "2004-02-12", "label": "February 12, 04", "numerator": 2, "denominator": 1}]}

CFG = {**_LEGACY_ZERO_SPREAD, "starting_cash": 1_000_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
       "fill_model": "next_bar_open"}


def test_the_backend_under_test_is_the_worktree_copy():
    import app.services.backtest.option_split_basis as m
    import ba2_common.core.split_basis as sb
    assert _in_this_checkout(m.__file__) and _in_this_checkout(sb.__file__), (m.__file__, sb.__file__)


# ---- fixtures -----------------------------------------------------------------------------------
def _write_fmp_daily(path: Path, through: date = date(2026, 1, 30)) -> None:
    """The fixture's real closes, then an ADJUSTED (no-step) continuation across the split, so
    the file is on one basis with the 2025-11-17 split inside it -- as the real cache is."""
    rows = [{"Date": pd.Timestamp(d), "Open": o, "High": h, "Low": l, "Close": c, "Volume": 1e6}
            for d, o, h, l, c in _NFLX_FMP]
    d = _NFLX_FMP[-1][0] + timedelta(days=1)
    while d <= through:
        if d.weekday() < 5:
            rows.append({"Date": pd.Timestamp(d), "Open": 56.0, "High": 56.5, "Low": 55.5,
                         "Close": 56.0, "Volume": 1e6})
        d += timedelta(days=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path)


class _Ohlcv:
    """The run's OHLCV reader, as far as the split basis needs it: where the file is."""

    def __init__(self, paths):
        self.paths = paths

    def cached_path(self, symbol, interval):
        return self.paths.get(symbol)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    from app.services.backtest.option_split_basis import clear_split_basis_memo

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    clear_split_basis_memo()
    fmp = tmp_path / "FMPOHLCVProvider" / "NFLX_1d.parquet"
    _write_fmp_daily(fmp)
    cal = tmp_path / "fmp_history" / "mc_stock_split__NFLX.json"
    cal.parent.mkdir(parents=True, exist_ok=True)
    cal.write_text(json.dumps(_NFLX_CALENDAR))
    yield tmp_path, _Ohlcv({"NFLX": str(fmp)})
    clear_split_basis_memo()


@pytest.fixture
def basis(cache):
    from app.services.backtest.option_split_basis import build_run_split_basis
    return build_run_split_basis(["NFLX"], cache[1])


def _chain_store(root: str) -> None:
    from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    df = pd.read_csv(FIXTURE)

    def f(v):
        return None if pd.isna(v) else float(v)

    store = OptionHistoryParquetStore(root=root)
    for exp, g in df.groupby("expiry"):
        bars = [OptionEodBar(occ_symbol=r.occ_symbol, bar_date=date.fromisoformat(r.bar_date),
                             open=f(r.open), high=f(r.high), low=f(r.low), close=f(r.close),
                             volume=None if pd.isna(r.volume) else int(r.volume),
                             bid=f(r.bid), ask=f(r.ask),
                             open_interest=None if pd.isna(r.open_interest) else int(r.open_interest))
                for r in g.itertuples()]
        store.write_partition("NFLX", date.fromisoformat(exp), bars,
                              start=date(2024, 4, 24), end=date(2024, 5, 1))


def _price_source(clock=datetime(2024, 5, 1)):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("NFLX", [{"Date": datetime(d.year, d.month, d.day), "Open": o, "High": h,
                           "Low": l, "Close": c, "Volume": 1e6} for d, o, h, l, c in _NFLX_FMP])
    ps.set_clock(clock)
    return ps


@pytest.fixture
def run(tmp_path, basis):
    """(account, provider, ps) over the fixture chain with the run's split basis wired."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_store import build_options_provider
    from app.services.backtest.parquet_options_provider import clear_worker_parquet_options_cache
    from app.services.backtest.option_basis_guard import clear_basis_guard_cache

    root = str(tmp_path / "ThetaDataOptionsProvider")
    _chain_store(root)
    clear_worker_parquet_options_cache()
    clear_basis_guard_cache()
    ps = _price_source()
    cfg = {"options_cache_db": "unused", "options_store": "thetadata",
           "options_parquet_root": root, "enabled_instruments": ["NFLX"],
           "start_date": date(2024, 4, 24), "end_date": date(2024, 5, 1),
           "options_risk_free_rate": 0.045}
    provider = build_options_provider(cfg, price_source=ps, split_basis=basis)
    account = BacktestAccount(1, ps, CFG, options_provider=provider, split_basis=basis)
    yield account, provider, ps, root
    clear_worker_parquet_options_cache()
    clear_basis_guard_cache()


# ---- E1 wiring: the run's basis -----------------------------------------------------------------
def test_run_basis_nflx_2024_is_ten_and_after_the_split_is_one(basis):
    assert basis.factor("NFLX", date(2024, 5, 1)) == 10.0
    assert basis.factor("NFLX", date(2025, 11, 17)) == 1.0


def test_a_symbol_outside_the_verified_universe_refuses_never_one(basis):
    with pytest.raises(SplitBasisRefused, match="not in this run's verified split basis"):
        basis.factor("AAPL", date(2024, 5, 1))


def test_a_missing_calendar_refuses_the_run_naming_every_symbol(cache):
    from app.services.backtest.option_split_basis import build_run_split_basis

    tmp, ohlcv = cache
    for sym in ("AAPL", "MSFT"):
        p = tmp / "FMPOHLCVProvider" / f"{sym}_1d.parquet"
        _write_fmp_daily(p)
        ohlcv.paths[sym] = str(p)
    with pytest.raises(SplitBasisRefused) as e:
        build_run_split_basis(["NFLX", "AAPL", "MSFT"], ohlcv)
    assert "no cached split calendar for 2 symbol(s): AAPL, MSFT" in str(e.value)
    assert "warm_market_conditions.py plan" in str(e.value)


def test_a_mixed_basis_cache_refuses_the_run(cache):
    from app.services.backtest.option_split_basis import build_run_split_basis

    tmp, ohlcv = cache
    p = Path(ohlcv.paths["NFLX"])
    df = pd.read_parquet(p)
    pre = pd.to_datetime(df["Date"]) < pd.Timestamp("2025-11-17")
    for c in ("Open", "High", "Low", "Close"):
        df.loc[pre, c] = df.loc[pre, c] * 10.0       # appended across the split, never refetched
    df.to_parquet(p)
    with pytest.raises(SplitBasisRefused, match="not verifiably on one split basis"):
        build_run_split_basis(["NFLX"], ohlcv)


def test_a_non_daily_options_run_refuses(cache):
    from app.services.backtest.option_split_basis import build_run_split_basis
    with pytest.raises(SplitBasisRefused, match="execution_interval"):
        build_run_split_basis(["NFLX"], cache[1], interval="1h")


def test_build_options_run_wires_a_basis_for_every_options_run_and_nothing_for_equity(cache):
    from app.services.backtest.options_store import build_options_run

    assert build_options_run({"enabled_instruments": ["NFLX"]}, price_source=object(),
                             ohlcv_provider=cache[1]) == (None, None)
    provider, basis = build_options_run(
        {"options_cache_db": str(cache[0] / "o.sqlite"), "enabled_instruments": ["NFLX"],
         "execution_interval": "1d", "options_risk_free_rate": 0.045},
        price_source=_price_source(), ohlcv_provider=cache[1])
    assert provider is not None and basis.factor("NFLX", date(2024, 5, 1)) == 10.0


def test_an_options_run_that_does_not_state_its_execution_interval_is_refused(cache):
    """No "1d" default: the split basis is only defined for the daily clock, so a config that
    dropped the key is refused rather than assumed daily."""
    from app.services.backtest.options_store import build_options_run

    with pytest.raises(ValueError, match="execution_interval"):
        build_options_run(
            {"options_cache_db": str(cache[0] / "o.sqlite"), "enabled_instruments": ["NFLX"]},
            price_source=_price_source(), ohlcv_provider=cache[1])


def test_the_handler_builds_its_options_run_through_build_options_run():
    """The one production construction site passes the basis to the ACCOUNT too."""
    src = Path(__file__).parents[2] / "app" / "services" / "backtest" / "daily_backtest_handler.py"
    text = src.read_text(encoding="utf-8")
    assert "build_options_run(" in text and "split_basis=split_basis" in text
    assert "build_options_provider(config, price_source=ps)" not in text


def test_the_greeks_overlay_key_carries_the_basis_and_is_unchanged_without_one(basis):
    from app.services.backtest.options_store import spot_scope

    cfg = {"enabled_instruments": ["NFLX"], "start_date": date(2024, 1, 2),
           "end_date": date(2024, 5, 1)}
    plain = spot_scope(cfg)
    assert plain == repr((("NFLX",), "1d", "2024-01-02", "2024-05-01", 0))
    assert spot_scope(cfg, basis) != plain and basis.digest() in spot_scope(cfg, basis)
    other = type(basis)({"NFLX": SymbolSplitBasis("NFLX", (), date(2025, 1, 2))})
    assert spot_scope(cfg, other) != spot_scope(cfg, basis)


# ---- E2: the option spot ------------------------------------------------------------------------
def test_option_spot_is_as_traded_while_the_equity_price_is_untouched(run):
    account, _, _, _ = run
    assert account.get_instrument_current_price("NFLX") == 55.17            # equity book
    assert account.get_option_underlying_price("NFLX", "mid") == pytest.approx(551.7)
    assert account.equity_shares_per_option_share("NFLX") == 10.0


def test_the_parquet_greeks_spot_is_as_traded(run):
    _, provider, _, _ = run
    assert provider.spot_source("NFLX", date(2024, 5, 1)) == pytest.approx(551.7)


def test_nflx_2024_05_01_five_pct_otm_call_is_near_580_not_58(run):
    from ba2_common.core.option_selector import select_single

    account, _, _, _ = run
    chain = account.get_option_chain("NFLX", date(2024, 6, 1), date(2024, 6, 30),
                                     OptionRight.CALL)
    assert chain, "fixture chain empty"
    spot = account.get_option_underlying_price("NFLX", "mid")
    c = select_single(chain, method="percent_otm", strike_param=5.0, spot=spot,
                      option_type=OptionRight.CALL, dte_min=20, dte_max=60,
                      today=date(2024, 5, 2))
    assert c is not None and 575.0 <= c.strike <= 585.0, c
    # and the greeks were inverted against that spot: a ~5% OTM 51-DTE call is not deep ITM
    assert 0.2 < c.delta < 0.5, c.delta


def test_intrinsic_and_no_arb_bounds_use_the_as_traded_spot(run):
    account, _, _, _ = run
    assert account._option_spot_asof("NFLX") == pytest.approx(551.7)
    lo, hi = account._no_arb_premium_bounds(500.0, True, account._option_spot_asof("NFLX"))
    assert lo == pytest.approx(51.7) and hi == pytest.approx(551.7)


def test_a_forward_filled_spot_takes_the_factor_of_its_own_date(basis):
    from app.services.backtest.backtest_account import BacktestAccount

    ps = _price_source(clock=datetime(2024, 5, 4))            # a Saturday: no exact bar
    acct = BacktestAccount(1, ps, CFG, options_provider=None, split_basis=basis)
    assert ps.close_asof_dated("NFLX") == (55.17, date(2024, 5, 1))
    assert acct._option_spot_asof("NFLX") == pytest.approx(551.7)


def test_without_a_basis_every_read_is_the_old_one():
    from app.services.backtest.backtest_account import BacktestAccount

    acct = BacktestAccount(1, _price_source(), CFG, options_provider=None)
    assert acct.get_option_underlying_price("NFLX") == 55.17
    assert acct._option_spot_asof("NFLX") == 55.17
    assert acct.equity_shares_per_option_share("NFLX") == 1.0
    assert acct.option_basis_price("NFLX", 55.17) == 55.17


# ---- E4: the guard ------------------------------------------------------------------------------
def test_the_guard_passes_the_as_traded_spot_on_real_data(run):
    account, provider, _, _ = run
    account.get_option_chain("NFLX", date(2024, 5, 2), date(2024, 6, 30), OptionRight.CALL)
    st = provider.basis_guard_stats()
    assert st["checks"] == 1 and st["unevaluable"] == 0 and st["outliers_passed"] == 0


def test_the_parity_spot_of_the_fixture_is_the_as_traded_price(run):
    from app.services.backtest.option_basis_guard import parity_spot

    _, provider, _, _ = run
    u = provider._u("NFLX")
    par = parity_spot(u, date(2024, 5, 1).toordinal())
    assert par == pytest.approx(551.7, rel=0.01)


def test_the_guard_refuses_an_unconverted_spot_loudly(run):
    """The pre-fix state: strikes as traded, spot adjusted. It must end the run."""
    from app.services.backtest.option_basis_guard import OptionSpotBasisMismatch
    from app.services.backtest.options_store import price_source_spot
    from app.services.backtest.parquet_options_provider import ParquetOptionsProvider

    _, _, ps, root = run
    bad = ParquetOptionsProvider(root, spot_source=price_source_spot(ps), risk_free_rate=0.04,
                                 spot_scope="unconverted", basis_guard=True)
    with pytest.raises(OptionSpotBasisMismatch) as e:
        bad.get_chain("NFLX", date(2024, 5, 1), expiry_min=date(2024, 6, 1),
                      expiry_max=date(2024, 6, 30), data_session=date(2024, 5, 1))
    msg = str(e.value)
    assert "NFLX 2024-05-01" in msg and "10.0" in msg


def test_an_isolated_bad_session_passes_a_persistent_one_refuses(monkeypatch):
    import app.services.backtest.option_basis_guard as g

    class _Raw:
        underlying, n_rows, c_occ = "X", 6, []
        date_of_ord = {}
        bar_ord = np.array([date(2024, 4, d).toordinal() for d in (24, 25, 26, 29, 30)]
                           + [date(2024, 5, 1).toordinal()])
        c_expiry_ord = np.array([], dtype=int)

    u = type("U", (), {"raw": _Raw()})()
    spots = {}
    monkeypatch.setattr(g, "parity_spot", lambda u_, o: spots.get(o))
    d1 = date(2024, 5, 1).toordinal()
    for o in _Raw.bar_ord:
        spots[int(o)] = 100.0
    spots[d1] = 120.0                                     # one bad print
    guard = g.BasisGuard(lambda s, d: 100.0)
    guard.check(u, "X", date(2024, 5, 1))
    assert guard.outliers_passed == 1
    for o in _Raw.bar_ord:
        spots[int(o)] = 1000.0                            # a x10 basis error
    guard2 = g.BasisGuard(lambda s, d: 100.0)
    with pytest.raises(g.OptionSpotBasisMismatch):
        guard2.check(u, "X", date(2024, 5, 1))


def test_the_engine_re_raises_a_basis_refusal_out_of_its_per_symbol_handlers():
    from app.services.backtest.daily_engine import _reraise_option_basis_refusal
    from app.services.backtest.option_basis_guard import OptionSpotBasisMismatch

    with pytest.raises(SplitBasisRefused):
        _reraise_option_basis_refusal(SplitBasisRefused("x"))
    with pytest.raises(OptionSpotBasisMismatch):
        _reraise_option_basis_refusal(OptionSpotBasisMismatch("x"))
    _reraise_option_basis_refusal(ValueError("ordinary"))    # returns: the handler logs it


# ---- E3: the option <-> stock boundary ------------------------------------------------------------
class _TenToOne:
    """A split basis where every AAPL share of the (synthetic) book is a tenth of a real one."""

    def factor(self, symbol, day):
        return 10.0

    def digest(self):
        return "ten"


_CALL_OCC = "AAPL240315C01800000"    # as-traded 1800 call
_AAPL_BARS = [   # ADJUSTED closes; x10 is the as-traded tape
    {"Date": datetime(2024, 3, 5), "Open": 150, "High": 152, "Low": 148, "Close": 151, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 151, "High": 154, "Low": 150, "Close": 153, "Volume": 1100},
    {"Date": datetime(2024, 3, 15), "Open": 199, "High": 201, "Low": 198, "Close": 200, "Volume": 1200},
    {"Date": datetime(2024, 3, 18), "Open": 205, "High": 207, "Low": 204, "Close": 206, "Volume": 1300},
]


@pytest.fixture
def covered_call(tmp_path):
    """1,000 ADJUSTED AAPL shares (= 100 real) @150 and ONE short as-traded 1800 call."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OrderStatus, OrderType

    db = str(tmp_path / "oc.sqlite")
    c = OptionsHistoryCache(db)
    c.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": _CALL_OCC, "option_type": "call", "strike": 1800.0,
         "expiry": "2024-03-15", "bid": 30.0, "ask": 32.0, "last": 31.0, "iv": 0.25}])
    c.write_bar_rows([{"occ_symbol": _CALL_OCC, "date": "2024-03-06", "open": 40.0,
                       "high": 48.0, "low": 39.0, "close": 45.0, "volume": 500,
                       "underlying": "AAPL", "option_type": "call", "strike": 1800.0,
                       "expiry": "2024-03-15"}])
    wire_backtest_seams()
    ctx = backtest_trading_db("split-basis-cc")
    ctx.__enter__()
    try:
        seed_account_definition(77, CFG)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", _AAPL_BARS)
        ps.set_clock(datetime(2024, 3, 5))
        acct = BacktestAccount(77, ps, CFG, options_provider=HistoricalOptionsProvider(db),
                               split_basis=_TenToOne())
        wire_backtest_seams().register_account(77, acct)
        o = TradingOrder(account_id=77, symbol="AAPL", quantity=1000, side=OrderDirection.BUY,
                         order_type=OrderType.MARKET, status=OrderStatus.NEW, comment="lot")
        acct.submit_order(o)
        acct._apply_fill(acct.get_order(o.broker_order_id), 150.0, datetime(2024, 3, 5))
        leg = OptionLeg(contract_symbol=_CALL_OCC, side=OrderDirection.SELL,
                        position_intent="sell_to_open", option_type=OptionRight.CALL,
                        strike=1800.0, expiry=date(2024, 3, 15), underlying="AAPL")
        verdict_two = acct.check_cover_for_covered_call([leg], 2, "covered_call")
        acct.submit_option_order(legs=[leg], quantity=1, order_type="market",
                                 option_strategy="covered_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
        engine.account, engine.price, engine.config = acct, ps, CFG
        yield engine, acct, ps, verdict_two
    finally:
        ctx.__exit__(None, None, None)


def test_1000_adjusted_shares_cover_one_contract_not_ten(covered_call):
    _, acct, _, verdict_two = covered_call
    assert not verdict_two.ok and verdict_two.required == 2000 and verdict_two.held == 1000
    assert len(acct.get_option_positions()) == 1
    assert acct._covered_short_call_contracts() == {_CALL_OCC}
    # the pledge locks all 1,000 adjusted shares (100 real): nothing is free to sell
    assert acct._pledged_share_lock("AAPL", 500.0, context="test") == 0.0


def test_assignment_round_trips_the_dollars_exactly(covered_call):
    engine, acct, ps, _ = covered_call
    cash_before = acct._cash
    ps.set_clock(datetime(2024, 3, 15))                      # adjusted 200 -> 2000 as traded
    engine._apply_option_expiry(datetime(2024, 3, 15))
    acct.refresh_transactions()
    # 100 real shares called away at 1800 == 1,000 adjusted shares at 180: $180,000 exactly
    assert acct._cash - cash_before == 180_000.0
    assert [p for p in acct.get_positions() if p["symbol"] == "AAPL"] == []
    eq = [t for t in acct.get_round_trip_trades()
          if t["symbol"] == "AAPL" and t["contract_symbol"] is None]
    assert len(eq) == 1 and eq[0]["exit_price"] == pytest.approx(180.0)
    assert eq[0]["pnl"] == pytest.approx(1000 * (180.0 - 150.0))


def test_an_itm_expiry_is_judged_on_the_as_traded_close(covered_call):
    """Adjusted 200 vs an 1800 strike would read OTM and expire the call worthless."""
    engine, acct, ps, _ = covered_call
    ps.set_clock(datetime(2024, 3, 15))
    engine._apply_option_expiry(datetime(2024, 3, 15))
    opt = [t for t in acct.get_round_trip_trades() if t["contract_symbol"] == _CALL_OCC]
    assert len(opt) == 1 and opt[0]["exit_price"] == pytest.approx(200.0)   # intrinsic 2000-1800


# ---- guard window (5 sessions) --------------------------------------------------------------------
def _guard_with(monkeypatch, par_by_day, spot=100.0):
    import app.services.backtest.option_basis_guard as g

    days = sorted(par_by_day)

    class _Raw:
        underlying, n_rows, c_occ = "X", len(days), []
        date_of_ord = {}
        bar_ord = np.array([d.toordinal() for d in days])
        c_expiry_ord = np.array([], dtype=int)

    u = type("U", (), {"raw": _Raw()})()
    table = {d.toordinal(): p for d, p in par_by_day.items()}
    monkeypatch.setattr(g, "parity_spot", lambda u_, o: table.get(o))
    g.clear_basis_guard_cache()
    return g, u, g.BasisGuard(lambda s, d: spot)


_SESSIONS = [date(2024, 4, 24), date(2024, 4, 25), date(2024, 4, 26), date(2024, 4, 29),
             date(2024, 4, 30), date(2024, 5, 1)]


def test_the_guard_median_spans_five_sessions():
    import app.services.backtest.option_basis_guard as g
    assert g.WINDOW == 5 and g.TOLERANCE == 0.05


def test_a_single_day_5_2pct_blip_passes(monkeypatch):
    par = {d: 100.0 for d in _SESSIONS}
    par[date(2024, 5, 1)] = 105.2
    g, u, guard = _guard_with(monkeypatch, par)
    guard.check(u, "X", date(2024, 5, 1))
    assert guard.outliers_passed == 1


def test_two_bad_prints_in_five_sessions_still_pass(monkeypatch):
    par = {d: 100.0 for d in _SESSIONS}
    par[date(2024, 5, 1)] = par[date(2024, 4, 30)] = 106.0
    g, u, guard = _guard_with(monkeypatch, par)
    guard.check(u, "X", date(2024, 5, 1))
    assert guard.outliers_passed == 1


def test_a_persistent_6pct_offset_over_five_sessions_refuses(monkeypatch):
    par = {d: 106.0 for d in _SESSIONS}
    g, u, guard = _guard_with(monkeypatch, par)
    with pytest.raises(g.OptionSpotBasisMismatch) as e:
        guard.check(u, "X", date(2024, 5, 1))
    assert "1.0600" in str(e.value) and str(e.value).count("2024-") >= 5


# ---- the option-overlay stock lot: 100 AS-TRADED shares per contract ------------------------------
def test_nflx_2024_covered_call_lot_is_1000_adjusted_shares_and_writes_one_call(run):
    """O_CC's equity entry (lot_size=100, the launcher's _with_round_lot_entry) must buy one
    CONTRACT's deliverable: 100 real NFLX shares = 1,000 adjusted shares in 2024 -- and 1,000
    adjusted shares then carry exactly one call."""
    from types import SimpleNamespace
    from ba2_common.core.TradeActions import BuyAction, SellCoveredCallAction
    from ba2_common.core.TradeRiskManagement import apply_lot_size
    from ba2_common.core.types import OrderRecommendation

    account, _, _, _ = run
    buy = BuyAction("NFLX", account, OrderRecommendation.BUY, lot_size=100)
    lot = buy._equity_lot_size()
    assert lot == 1000
    assert apply_lot_size({"lot_size": lot}, 1450) == 1000       # the RM floors to whole lots
    assert apply_lot_size({"lot_size": lot}, 999) == 0           # < one contract: unfunded

    cc = SellCoveredCallAction.__new__(SellCoveredCallAction)
    cc.account, cc.instrument_name = account, "NFLX"
    assert cc._contracts_coverable_by(1000.0) == 1


def test_the_overlay_lot_is_unchanged_without_a_basis_and_off_the_option_path():
    from ba2_common.core.TradeActions import BuyAction
    from ba2_common.core.types import OrderRecommendation
    from app.services.backtest.backtest_account import BacktestAccount

    acct = BacktestAccount(1, _price_source(), CFG, options_provider=None)
    assert BuyAction("NFLX", acct, OrderRecommendation.BUY, lot_size=100)._equity_lot_size() == 100
    assert BuyAction("NFLX", object(), OrderRecommendation.BUY, lot_size=100)._equity_lot_size() == 100


# ---- review follow-ups ----------------------------------------------------------------------------
def test_the_basis_identity_has_room_for_an_overrides_version():
    b = SymbolSplitBasis("NFLX", (), date(2026, 1, 2))
    assert b.identity()[-1] is None
    assert SymbolSplitBasis("NFLX", (), date(2026, 1, 2), overrides_version="g1-v1").identity()         != b.identity()


def test_basis_refusals_are_never_absorbed_in_any_error_mode(monkeypatch):
    from ba2_common.core.failure_modes import absorb_if_benign
    from app.services.backtest.option_basis_guard import OptionSpotBasisMismatch

    for mode in ("legacy", "observe", "enforce"):
        monkeypatch.setenv("BA2_ERROR_MODE", mode)
        for exc in (SplitBasisRefused("x"), OptionSpotBasisMismatch("y")):
            try:
                raise exc
            except Exception as e:
                with pytest.raises(type(exc)):
                    absorb_if_benign(e, RuntimeError)       # even when named benign


def test_the_guard_index_holds_no_reference_to_the_raw_store(run):
    import gc, weakref
    import app.services.backtest.option_basis_guard as g

    account, provider, _, _ = run
    account.get_option_chain("NFLX", date(2024, 5, 2), date(2024, 6, 30), OptionRight.CALL)
    st = g.basis_guard_cache_stats()
    assert st["indexes"] == 1 and st["parity_entries"] >= 1
    for idx in g._INDEX.values():
        assert all(isinstance(getattr(idx, a), (np.ndarray, dict)) for a in idx.__slots__)
    from app.services.backtest.parquet_options_provider import clear_worker_parquet_options_cache
    clear_worker_parquet_options_cache()
    assert g.basis_guard_cache_stats()["indexes"] == 0


def test_results_carry_the_guard_stats_next_to_the_staleness(run):
    from app.services.backtest.results import build_results

    account, _, _, _ = run
    account.get_option_chain("NFLX", date(2024, 5, 2), date(2024, 6, 30), OptionRight.CALL)
    account.snapshot_equity(datetime(2024, 5, 1))
    out = build_results(account, {"initial_capital": CFG["starting_cash"],
                                  "account_settings": dict(CFG), "option_trade_records": True})
    assert "option_chain_staleness" in out
    st = out["option_basis_guard"]
    assert st == account.option_basis_guard_stats()
    assert st["checks"] == 1 and st["unevaluable_symbols"] == {}


def test_a_symbol_the_guard_could_not_evaluate_is_warned(monkeypatch, caplog):
    import logging
    par = {d: None for d in _SESSIONS}
    g, u, guard = _guard_with(monkeypatch, par)
    for d in _SESSIONS:
        guard.check(u, "X", d)
    with caplog.at_level(logging.WARNING):
        st = guard.stats()
        guard.stats()
    assert st["unevaluable_symbols"] == {"X": 1.0}
    warns = [r for r in caplog.records if "UNEVALUABLE" in r.getMessage()]
    assert len(warns) == 1 and "X" in warns[0].getMessage() and "100.0%" in warns[0].getMessage()


def test_intraday_refinement_prices_the_move_in_the_as_traded_basis(run, monkeypatch):
    """entry_premium + delta * (px - entry_px): delta is per as-traded share, so on NFLX 2024
    a $0.50 adjusted dip is a $5.00 as-traded move."""
    import app.services.backtest.results as R
    import app.services.backtest.intraday_drawdown as I

    account, _, _, _ = run
    bars = pd.DataFrame({"Date": [pd.Timestamp("2024-05-01 10:00")],
                         "Low": [54.67], "High": [55.30]})
    monkeypatch.setattr(R, "_get_5m_bars_cached", lambda *a, **k: bars)
    seen = {}

    def spy(trades, max_dd, **kw):
        seen["entry"] = kw["underlying_price_at"]("NFLX", datetime(2024, 5, 1))
        seen["bars"] = kw["bars_5m_between"]("NFLX", pd.Timestamp("2024-05-01"),
                                              pd.Timestamp("2024-05-02"))
        return max_dd

    monkeypatch.setattr(I, "refine_max_drawdown", spy)
    fn = R._build_refine_drawdown_fn(
        account, {"account_settings": {"commission_per_trade": 0.0},
                  "start_date": date(2024, 4, 24), "end_date": date(2024, 5, 1)})
    fn([], 0.1)
    assert seen["entry"] == pytest.approx(551.7)
    assert seen["bars"][0]["Low"] == pytest.approx(546.7)
    assert seen["bars"][0]["High"] == pytest.approx(553.0)
