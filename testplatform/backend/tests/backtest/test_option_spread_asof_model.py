"""Plan Part F: an option fill is charged the AS-OF real spread, else the calibrated model.

F1  When the DECISION (as-of) bar carries a valid NBBO the fill pays ``(ask - bid) / 2`` of
    THAT bar -- never the fill bar's quote, and never the fill bar's volume.
F2  Otherwise ``option_spread_model.half_spread`` of the as-of close + volume.
    ``option_spread_pct`` / ``option_spread_min_tick`` survive only as the EXPLICIT legacy
    model, which must reproduce the old formula (fill-day thin doubling included) exactly.

The fill-day blindness is pinned the strong way: the fill bar is given a wildly different
quote and volume, and the fill price must not move.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.services.backtest.backtest_account import BacktestAccount
from ba2_common.core import option_spread_model as M
from ba2_common.core.types import OptionRight, OrderDirection, OrderType


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

_OCC = "AAPL240315C00180000"
_AS_OF_DAY = date(2024, 3, 5)
_FILL_DAY = date(2024, 3, 6)
_AS_OF = datetime(2024, 3, 5)
_SPOT = 170.0                 # OTM vs strike 180: every premium here is arb-consistent


def test_the_worktree_module_is_the_one_under_test():
    import ba2_common.core.option_spread_model as mod
    assert _in_this_checkout(mod.__file__), mod.__file__


class _StubOptions:
    def __init__(self, bars):
        self.bars = bars        # day -> bar dict
        self.reads = []

    def get_bar(self, occ_symbol, day):
        self.reads.append(day)
        if occ_symbol != _OCC or day not in self.bars:
            return None
        return dict(self.bars[day])


class _StubPrice:
    def now(self):
        return _AS_OF

    def next_bar_date(self, symbol, as_of):
        return _FILL_DAY

    def bar_at(self, symbol, day):
        return {"open": _SPOT, "high": _SPOT, "low": _SPOT, "close": _SPOT}


def _bar(px, volume=500.0, bid=None, ask=None):
    return {"open": px, "high": px, "low": px, "close": px, "volume": volume,
            "bid": bid, "ask": ask, "strike": 180.0, "option_type": "call"}


def _acct(as_of_bar, fill_bar, **cfg):
    base = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
            "fill_model": "next_bar_open", "option_spread_model": M.SPREAD_MODEL_VERSION}
    base.update(cfg)
    if base["option_spread_model"] is None:      # None = "no key at all" (a pre-Part-F config)
        del base["option_spread_model"]
    a = BacktestAccount(id=1, price_source=_StubPrice(), settings=base)
    bars = {}
    if as_of_bar is not None:
        bars[_AS_OF_DAY] = as_of_bar
    if fill_bar is not None:
        bars[_FILL_DAY] = fill_bar
    a._options = _StubOptions(bars)
    return a


def _order(side, order_type=None, limit=None, child=False):
    ot = order_type or (OrderType.BUY_LIMIT if side == OrderDirection.BUY else OrderType.SELL_LIMIT)
    return SimpleNamespace(
        id=1, symbol="AAPL", underlying_symbol="AAPL", contract_symbol=_OCC,
        order_type=ot, limit_price=limit, side=side, quantity=1.0,
        multiplier=100, strike=180.0, option_type=OptionRight.CALL,
        position_intent="buy_to_open" if side == OrderDirection.BUY else "sell_to_open",
        parent_order_id=None,
    )


# --------------------------------------------------------------------------- #
# F1: the as-of quote
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("side,limit,sign", [
    (OrderDirection.BUY, None, +1), (OrderDirection.SELL, None, -1),        # market-style
    (OrderDirection.BUY, 10.0, +1), (OrderDirection.SELL, 0.01, -1),        # limit branch
])
def test_the_fill_pays_the_AS_OF_quote_not_the_fill_bars(side, limit, sign):
    as_of = _bar(4.00, bid=3.90, ask=4.30)                   # half = 0.20
    fill = _bar(4.00, volume=20.0, bid=1.00, ask=9.00)       # half would be 4.00
    a = _acct(as_of, fill)
    got = a._option_fill_price(_order(side, limit=limit), _AS_OF)
    assert got == pytest.approx(4.00 + sign * 0.20)


@pytest.mark.parametrize("fill_quote,fill_volume", [
    ((None, None), 500.0), ((0.5, 50.0), 1.0), ((4.0, 4.0), 10_000_000.0),
    ((3.99, 4.01), None), ((0.0, 0.0), 0.0),
])
@pytest.mark.parametrize("as_of_quote", [(3.90, 4.30), (None, None)])
def test_the_fill_days_quote_and_volume_are_never_read(fill_quote, fill_volume, as_of_quote):
    """Only the fill bar's OPEN (the price being traded) may matter; its quote and its
    volume move nothing. The volume must stay >= 10 so the participation cap is not the
    thing under test -- except where it is None/0, where the cap rejects (None) either way
    and the spread is simply unobservable, so those rows assert the half spread directly."""
    as_of = _bar(4.00, bid=as_of_quote[0], ask=as_of_quote[1])
    ref = _acct(as_of, _bar(4.00))
    probe = _acct(as_of, _bar(4.00, volume=fill_volume, bid=fill_quote[0], ask=fill_quote[1]))
    fill_bar = probe._options.get_bar(_OCC, _FILL_DAY)
    as_of_bar = probe._options.get_bar(_OCC, _AS_OF_DAY)
    assert probe._option_half_spread(4.00, fill_bar, as_of_bar=as_of_bar) == \
        ref._option_half_spread(4.00, _bar(4.00), as_of_bar=as_of_bar)
    if fill_volume and fill_volume >= 10:
        for side in (OrderDirection.BUY, OrderDirection.SELL):
            assert probe._option_fill_price(_order(side), _AS_OF) == \
                ref._option_fill_price(_order(side), _AS_OF)


def test_a_zero_bid_as_of_quote_is_not_a_valid_quote():
    as_of = _bar(0.05, volume=500.0, bid=0.0, ask=0.10)
    a = _acct(as_of, _bar(0.05))
    got = a._option_fill_price(_order(OrderDirection.BUY), _AS_OF)
    assert got == pytest.approx(0.05 + M.half_spread(0.05, 500.0))


def test_same_bar_close_reads_the_one_bar_it_fills_on():
    """Under same_bar_close the decision bar IS the fill bar, and its closing quote is causal."""
    as_of = _bar(4.00, bid=3.80, ask=4.20)
    a = _acct(as_of, _bar(4.00, bid=1.0, ask=9.0), fill_model="same_bar_close")
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) == pytest.approx(4.20)


# --------------------------------------------------------------------------- #
# F2: the calibrated fallback
# --------------------------------------------------------------------------- #
def test_no_as_of_quote_charges_the_model_on_the_AS_OF_close_and_volume():
    as_of = _bar(4.00, volume=500.0)                        # no bid/ask
    fill = _bar(5.00, volume=20.0)                          # different premium AND volume
    a = _acct(as_of, fill)
    half = M.half_spread(4.00, 500.0)
    assert half == pytest.approx(0.22856492942925036 / 2, rel=1e-12)
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) == pytest.approx(5.00 + half)
    assert a._option_fill_price(_order(OrderDirection.SELL), _AS_OF) == pytest.approx(5.00 - half)


def test_no_as_of_bar_at_all_models_the_fill_premium_as_thin():
    a = _acct(None, _bar(5.00, volume=20.0))
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) == \
        pytest.approx(5.00 + M.half_spread(5.00, 1))


def test_the_thin_doubling_is_retired_in_the_model():
    """The old model doubled on a thin FILL bar. The new one has no such cliff."""
    as_of = _bar(4.00, volume=500.0)
    a = _acct(as_of, _bar(4.00, volume=20.0))
    b = _acct(as_of, _bar(4.00, volume=5000.0))
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) == \
        b._option_fill_price(_order(OrderDirection.BUY), _AS_OF)


# --------------------------------------------------------------------------- #
# the entry-quote seam agrees with the fill
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("as_of", [_bar(4.00, bid=3.90, ask=4.30), _bar(4.00, volume=500.0)])
def test_the_seam_returns_exactly_what_the_fill_charges(as_of):
    a = _acct(as_of, _bar(4.00, volume=20.0, bid=1.0, ask=9.0))
    seam = a.option_modelled_half_spread(_OCC)
    buy = a._option_fill_price(_order(OrderDirection.BUY), _AS_OF)
    assert seam == pytest.approx(buy - 4.00)


def test_the_seam_answers_from_a_quote_only_as_of_row():
    """A quote-only row (no trade that day: close None) still has a real spread."""
    as_of = {"open": None, "high": None, "low": None, "close": None, "volume": None,
             "bid": 3.90, "ask": 4.30}
    a = _acct(as_of, _bar(4.00))
    assert a.option_modelled_half_spread(_OCC) == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# the legacy model: explicit, and exact
# --------------------------------------------------------------------------- #
def _legacy_half(px, fill_volume, pct=5.0, tick=0.02):
    full = max(tick, abs(px) * pct / 100.0)
    if fill_volume is None or fill_volume < 100:
        full *= 2.0
    return full / 2.0


@pytest.mark.parametrize("cfg", [
    {"option_spread_model": M.LEGACY_PCT_MODEL, "option_spread_pct": 5.0,
     "option_spread_min_tick": 0.02},
    {"option_spread_model": None, "option_spread_pct": 5.0, "option_spread_min_tick": 0.02},
])
@pytest.mark.parametrize("px,fill_volume", [(4.00, 500.0), (4.00, 20.0), (0.12, 500.0),
                                            (0.12, 50.0)])
def test_explicit_legacy_reproduces_the_old_formula_exactly(cfg, px, fill_volume):
    """Including its fill-day thin doubling and ignoring the as-of quote entirely. A config
    with no model key at all (every stored run before Part F) is the legacy model too."""
    a = _acct(_bar(px, bid=px - 0.5, ask=px + 0.5), _bar(px, volume=fill_volume), **cfg)
    half = _legacy_half(px, fill_volume)
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) == pytest.approx(px + half)
    assert a.option_modelled_half_spread(_OCC) == pytest.approx(_legacy_half(px, 500.0))


def test_legacy_reads_no_as_of_bar_on_the_fill_path():
    """Byte-identity with pre-Part-F runs also means no extra provider reads."""
    a = _acct(_bar(4.0), _bar(4.0), option_spread_model=M.LEGACY_PCT_MODEL,
              option_spread_pct=5.0, option_spread_min_tick=0.02)
    a._option_fill_price(_order(OrderDirection.BUY), _AS_OF)
    assert a._options.reads == [_FILL_DAY]


# --------------------------------------------------------------------------- #
# no silent zero spread: every resolution branch
# --------------------------------------------------------------------------- #
_BASE = {"starting_cash": 100_000.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
         "fill_model": "next_bar_open"}


def _options_account(**cfg):
    """Constructed WITH a provider, as run_daily_backtest does: resolution is eager."""
    return BacktestAccount(id=1, price_source=_StubPrice(), settings={**_BASE, **cfg},
                           options_provider=_StubOptions({}))


@pytest.mark.parametrize("cfg,expected", [
    ({"option_spread_model": M.SPREAD_MODEL_VERSION}, M.SPREAD_MODEL_VERSION),
    ({"option_spread_model": M.LEGACY_PCT_MODEL, "option_spread_pct": 5.0,
      "option_spread_min_tick": 0.02}, M.LEGACY_PCT_MODEL),
    # the explicit zero-spread run
    ({"option_spread_model": M.LEGACY_PCT_MODEL, "option_spread_pct": 0.0,
      "option_spread_min_tick": 0.0}, M.LEGACY_PCT_MODEL),
    # a stored pre-Part-F launcher run: no key, both knobs non-zero
    ({"option_spread_pct": 5.0, "option_spread_min_tick": 0.02}, M.LEGACY_PCT_MODEL),
])
def test_resolution_accepts(cfg, expected):
    assert _options_account(**cfg).option_spread_record()["option_spread_model"] == expected


@pytest.mark.parametrize("cfg", [
    {},                                                               # nothing stated
    {"option_spread_pct": None, "option_spread_min_tick": None},
    {"option_spread_pct": 5.0},                                       # one knob only
    {"option_spread_pct": 0.0, "option_spread_min_tick": 0.0},        # implicit zero spread
    {"option_spread_pct": 5.0, "option_spread_min_tick": 0.0},
    {"option_spread_pct": 0.0, "option_spread_min_tick": 0.02},
    {"option_spread_model": M.LEGACY_PCT_MODEL},                      # legacy without knobs
    {"option_spread_model": M.LEGACY_PCT_MODEL, "option_spread_pct": 5.0},
    {"option_spread_model": M.SPREAD_MODEL_VERSION, "option_spread_pct": 5.0},  # ignored knob
    {"option_spread_model": M.SPREAD_MODEL_VERSION, "option_spread_min_tick": 0.0},
])
def test_resolution_refuses_an_unstated_or_implicit_zero_spread(cfg):
    with pytest.raises(ValueError):
        _options_account(**cfg)


def test_an_unknown_model_is_refused_at_construction():
    with pytest.raises(ValueError, match="option_spread_model"):
        _options_account(option_spread_model="pow-1999-01-01")


def test_the_explicit_zero_spread_run_charges_nothing():
    a = _acct(_bar(4.0, bid=3.9, ask=4.3), _bar(4.0), option_spread_model=M.LEGACY_PCT_MODEL,
              option_spread_pct=0.0, option_spread_min_tick=0.0)
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) == 4.0


def test_an_equity_account_needs_no_spread_keys():
    """Zero impact on equity: an account with no options provider never resolves the model."""
    a = BacktestAccount(id=3, price_source=_StubPrice(), settings=dict(_BASE))
    assert a._spread_model_cache is None


def test_a_lazily_attached_reader_still_refuses_at_the_first_option_fill():
    a = BacktestAccount(id=4, price_source=_StubPrice(), settings=dict(_BASE))
    a._options = _StubOptions({_AS_OF_DAY: _bar(4.0), _FILL_DAY: _bar(4.0)})
    with pytest.raises(ValueError, match="spread model"):
        a._option_fill_price(_order(OrderDirection.BUY), _AS_OF)


# --------------------------------------------------------------------------- #
# locked / crossed / sub-tick as-of quotes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bid,ask", [(4.0, 4.0), (4.1, 4.0)])
def test_a_locked_or_crossed_as_of_quote_goes_to_the_model(bid, ask):
    a = _acct(_bar(4.0, volume=500.0, bid=bid, ask=ask), _bar(4.0))
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) ==         pytest.approx(4.0 + M.half_spread(4.0, 500.0))


def test_a_sub_tick_as_of_quote_is_floored_at_half_a_tick():
    a = _acct(_bar(4.0, bid=4.000, ask=4.004), _bar(4.0))
    assert a._option_fill_price(_order(OrderDirection.BUY), _AS_OF) == pytest.approx(4.005)
    assert a.option_modelled_half_spread(_OCC) == pytest.approx(0.005)


# --------------------------------------------------------------------------- #
# the record: config -> trial config -> results
# --------------------------------------------------------------------------- #
def test_the_account_reports_its_model_and_where_each_fill_was_priced_from():
    a = _acct(_bar(4.00, bid=3.90, ask=4.30), _bar(4.00))
    a._option_fill_price(_order(OrderDirection.BUY), _AS_OF)
    a._options.bars[_AS_OF_DAY] = _bar(4.00)
    a._option_fill_price(_order(OrderDirection.BUY), _AS_OF)
    rec = a.option_spread_record()
    assert rec == {"option_spread_model": M.SPREAD_MODEL_VERSION,
                   "option_spread_fill_sources": {"quote": 1, "model": 1}}


def test_the_results_block_is_added_for_an_options_run_only():
    from app.services.backtest.daily_backtest_handler import apply_option_spread_record

    a = _acct(_bar(4.00), _bar(4.00))
    results = {"x": 1}
    apply_option_spread_record(results, a)
    assert results["option_spread_model"] == M.SPREAD_MODEL_VERSION
    eq = BacktestAccount(id=2, price_source=_StubPrice(), settings={
        "starting_cash": 1.0, "commission_per_trade": 0.0, "slippage_bps": 0.0,
        "fill_model": "next_bar_open"})
    untouched = {"x": 1}
    apply_option_spread_record(untouched, eq)
    assert untouched == {"x": 1}


def test_the_trial_config_carries_the_model_key():
    """A knob dropped by _build_daily_trial_config is silently inert (the whitelist trap)."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    backtest_cfg = {
        "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0,
        "seed": 42,
        "account_settings": {"option_spread_model": M.SPREAD_MODEL_VERSION},
        "experts": [{"class": "FMPRating", "settings": {}}],
    }
    decoded = {"expert_overrides": {}, "screener_overrides": {}, "schedule_days": None,
               "entry_rules": None, "exit_rules": None}
    cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
    assert cfg["account_settings"]["option_spread_model"] == M.SPREAD_MODEL_VERSION


def test_the_api_payload_passes_the_model_through():
    from app.services.backtest.daily_backtest_handler import _option_spread_settings

    assert _option_spread_settings({}) == {}          # never an invented 0.0 spread
    assert _option_spread_settings({"option_spread_model": M.SPREAD_MODEL_VERSION}) == {
        "option_spread_model": M.SPREAD_MODEL_VERSION}
    assert _option_spread_settings({"option_spread_model": M.LEGACY_PCT_MODEL,
                                    "option_spread_pct": 0, "option_spread_min_tick": 0}) == {
        "option_spread_model": M.LEGACY_PCT_MODEL,
        "option_spread_pct": 0.0, "option_spread_min_tick": 0.0}


# --------------------------------------------------------------------------- #
# launcher + matrix identity
# --------------------------------------------------------------------------- #
import ba2test_launcher as L  # noqa: E402  (testplatform/ on sys.path via conftest)


def _ns(**kw):
    base = {"option_spread_model": M.SPREAD_MODEL_VERSION, "option_spread_pct": None,
            "option_spread_min_tick": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_launcher_default_is_the_model():
    # the legacy knobs are OMITTED, never 0.0 (a stored 0.0 would read as a zero-spread run)
    assert L._option_spread_account_settings(_ns()) == {
        "option_spread_model": M.SPREAD_MODEL_VERSION}


@pytest.mark.parametrize("kw,expected", [
    ({"option_spread_pct": 5.0}, (5.0, 0.02)),
    ({"option_spread_min_tick": 0.05}, (5.0, 0.05)),
    ({"option_spread_pct": 0.0, "option_spread_min_tick": 0.0}, (0.0, 0.0)),
    ({"option_spread_model": M.LEGACY_PCT_MODEL}, (5.0, 0.02)),
])
def test_launcher_explicit_pct_selects_the_legacy_formula(kw, expected):
    got = L._option_spread_account_settings(_ns(**kw))
    assert got == {"option_spread_model": M.LEGACY_PCT_MODEL,
                   "option_spread_pct": expected[0], "option_spread_min_tick": expected[1]}


def test_launcher_parser_defaults():
    """Both optimize parsers share ``_add_option_spread_args``: the model is the default and the
    legacy knobs are unset (None), so only an EXPLICIT value can select the old formula."""
    import argparse
    p = argparse.ArgumentParser()
    L._add_option_spread_args(p)
    args = p.parse_args([])
    assert args.option_spread_model == M.SPREAD_MODEL_VERSION
    assert args.option_spread_pct is None and args.option_spread_min_tick is None
    assert L._option_spread_account_settings(args)["option_spread_model"] == M.SPREAD_MODEL_VERSION
    with pytest.raises(SystemExit):
        p.parse_args(["--option-spread-model", "pow-1999-01-01"])


def _matrix():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location("rom_spread", root / "tools/run_options_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_matrix_passes_the_model_and_it_moves_the_discovery_identity():
    R = _matrix()
    base = ["--profile", "discovery", "--screener-gate-store", "s", "--max-stock-price", "0"]
    a = R.resolve_args(R.build_parser(), base)
    assert a.option_spread_model == M.SPREAD_MODEL_VERSION
    cmd = R.build_cmd(a, "launcher.py", "n", "FMPRating", "O_LC", "AAPL")
    i = cmd.index("--option-spread-model")
    assert cmd[i + 1] == M.SPREAD_MODEL_VERSION
    b = R.resolve_args(R.build_parser(), base + ["--option-spread-model", M.LEGACY_PCT_MODEL])
    name = lambda x: R.discovery_name(x, "launcher.py", "n", "FMPRating", "O_LC", "AAPL")
    assert name(a) != name(b)


# --------------------------------------------------------------------------- #
# the API/UI create path and the standalone re-run: the current model, stated and recorded
# --------------------------------------------------------------------------- #
_OPTION_EXIT = [{"action": "buy_call", "conditions": {}}]


def _api_payload(**over):
    p = {"backtest_id": 77, "name": "ui-option", "enabled_instruments": ["AAPL"],
         "experts": ["FMPEarningsDrift"], "start_date": "2024-03-01", "end_date": "2024-03-08",
         "initial_capital": 100_000.0, "commission": 1.0, "slippage": 0.0,
         "fill_model": "next_bar_open", "seed": 42, "exit_rules": _OPTION_EXIT}
    p.update(over)
    return p


def test_a_ui_options_run_with_no_spread_gets_the_current_model_explicitly(caplog):
    from app.services.backtest.daily_backtest_handler import _build_config

    with caplog.at_level("INFO"):
        cfg = _build_config(_api_payload())
    assert cfg["options_cache_db"]
    assert cfg["account_settings"]["option_spread_model"] == M.SPREAD_MODEL_VERSION
    assert "option_spread_pct" not in cfg["account_settings"]
    assert any("applying the current model" in r.getMessage() for r in caplog.records)
    # ...and the account accepts it
    BacktestAccount(id=5, price_source=_StubPrice(), settings=cfg["account_settings"],
                    options_provider=_StubOptions({}))


def test_a_stated_spread_is_never_overridden():
    from app.services.backtest.daily_backtest_handler import _build_config

    cfg = _build_config(_api_payload(option_spread_model=M.LEGACY_PCT_MODEL,
                                     option_spread_pct=5.0, option_spread_min_tick=0.02))
    assert cfg["account_settings"]["option_spread_model"] == M.LEGACY_PCT_MODEL
    assert cfg["account_settings"]["option_spread_pct"] == 5.0


def test_an_equity_run_gets_no_spread_keys():
    from app.services.backtest.daily_backtest_handler import _build_config

    cfg = _build_config(_api_payload(exit_rules=None, start_date="2024-01-02",
                                     end_date="2024-01-08"))
    assert not cfg["options_cache_db"]
    assert not any(k.startswith("option_spread") for k in cfg["account_settings"])


class _Db:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_the_create_path_records_the_model_on_the_row():
    from app.services.backtest.daily_backtest_handler import (
        STORED_SPREAD_MODEL_KEY, _build_config, record_option_spread_model)

    bt = SimpleNamespace(id=77, optimization_id=None, strategy_params={"seed": 42})
    db = _Db()
    record_option_spread_model(db, bt, _build_config(_api_payload()))
    assert bt.strategy_params == {"seed": 42, STORED_SPREAD_MODEL_KEY: M.SPREAD_MODEL_VERSION}
    assert db.commits == 1
    # equity run: nothing written
    eq = SimpleNamespace(id=78, optimization_id=None, strategy_params={"seed": 42})
    record_option_spread_model(db, eq, _build_config(_api_payload(
        exit_rules=None, start_date="2024-01-02", end_date="2024-01-08")))
    assert eq.strategy_params == {"seed": 42}
    # optimization-derived row: its config lives on the optimization
    opt = SimpleNamespace(id=79, optimization_id=5, strategy_params={})
    record_option_spread_model(db, opt, {"account_settings": {
        "option_spread_model": M.SPREAD_MODEL_VERSION}})
    assert opt.strategy_params == {}


def _standalone_row(sp_extra=None):
    sp = {"universe": {"mode": "static", "symbols": ["AAPL"]}, "seed": 42,
          "fillModel": "next_bar_open", "exitConditions": _OPTION_EXIT}
    sp.update(sp_extra or {})
    return SimpleNamespace(
        id=80, name="old-option-row", expert_name="FMPEarningsDrift", optimization_id=None,
        start_date=datetime(2024, 3, 1), end_date=datetime(2024, 3, 8),
        initial_capital=100_000.0, commission=1.0, slippage=0.0, strategy_params=sp)


def test_a_standalone_rerun_of_a_row_with_no_model_applies_and_records_the_current_one():
    from app.services.backtest.daily_backtest_handler import (
        STORED_SPREAD_MODEL_KEY, record_option_spread_model)
    from app.services.backtest.rerun_handler import _build_standalone_rerun_config

    bt = _standalone_row()
    cfg = _build_standalone_rerun_config(bt)
    assert cfg["account_settings"]["option_spread_model"] == M.SPREAD_MODEL_VERSION
    record_option_spread_model(_Db(), bt, cfg)
    assert bt.strategy_params[STORED_SPREAD_MODEL_KEY] == M.SPREAD_MODEL_VERSION


def test_a_standalone_rerun_uses_the_model_the_row_recorded():
    from app.services.backtest.rerun_handler import _build_standalone_rerun_config

    cfg = _build_standalone_rerun_config(_standalone_row({"optionSpreadModel": "legacy-pct"}))
    # recorded legacy without knobs is refused by the account, not silently re-priced
    assert cfg["account_settings"]["option_spread_model"] == M.LEGACY_PCT_MODEL
    with pytest.raises(ValueError):
        BacktestAccount(id=6, price_source=_StubPrice(), settings=cfg["account_settings"],
                        options_provider=_StubOptions({}))


@pytest.mark.parametrize("as_of,source,premium", [
    (_bar(4.00, bid=3.90, ask=4.30), "quote", 4.20),
    (_bar(4.00, volume=500.0), "model", 4.00 + M.half_spread(4.00, 500.0)),
])
def test_a_forced_liquidation_is_counted_as_an_option_fill(monkeypatch, as_of, source, premium):
    a = _acct(as_of, None)
    monkeypatch.setattr(a, "_lot_no_arb_bounds", lambda sym: None)
    monkeypatch.setattr(a, "_option_transaction_for_contract", lambda sym: None)
    monkeypatch.setattr(a, "_zero_option_lot", lambda lot: None)
    cash0 = a._cash
    lot = SimpleNamespace(contract_symbol=_OCC, qty=-1.0, multiplier=100, avg_price=4.0)
    assert a._liquidate_option_lot(lot) is True
    assert a._cash == pytest.approx(cash0 - premium * 100)
    assert a.option_spread_record()["option_spread_fill_sources"][source] == 1


def test_matrix_mode_job_names_carry_the_spread_model():
    """Matrix-mode names ARE the completion key (no digest): a pow relaunch must not match a
    legacy-priced completion, and legacy-pct must keep today's names exactly."""
    R = _matrix()
    assert R._spread_name_tag(M.LEGACY_PCT_MODEL) == ""
    assert R._spread_name_tag("pow-2026-09-22") == "-spow0922"
    names = lambda extra: [n for n, *_ in R.planned_jobs(
        R.resolve_args(R.build_parser(), ["--strategies", "OS1", "--name-suffix=-x"] + extra),
        "launcher.py", ["FMPRating"], ["OS1"], "AAPL")]
    assert names([]) == ["optm-FMPRating-OS1-spow0922-x"]
    assert names(["--option-spread-model", M.LEGACY_PCT_MODEL]) == ["optm-FMPRating-OS1-x"]
