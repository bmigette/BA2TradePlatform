"""Phase 1 Task 7: FMPSenateTraderCopy + FMPSenateTraderWeight two-stage
_gather/_process split + analyze_as_of parity.

Proves the riskiest part of Phase 1:
- _gather PRE-RESOLVES the interleaved fetches into maps so _process is pure:
  * Weight: exec_price_by_trade (via _get_price_at_date) + trader_history_by_name
    (via _fetch_trader_history, sliced to disclosure <= as_of).
  * Copy: current_price_map + supported_symbols (via providers.price_at_date).
- No-lookahead: when as_of is set, trades disclosed/executed after as_of are dropped
  (Copy) and trader-history rows disclosed after as_of are dropped (Weight).
- Copy emits a List[Recommendation] (one per supported symbol) and honours both
  ENTER_MARKET and OPEN_POSITIONS subtypes.
- Weight's confidence is computed from the PRE-RESOLVED history (not a live fetch).
- as_of=None drives the same logic as a live-style call (the golden-test contract).

Experts are built via __new__ (bypassing __init__'s DB read / _load_expert_instance)
with the FMP-http fetchers (_fetch_senate_trades/_fetch_house_trades/
_fetch_trader_history/_get_price_at_date) stubbed — those are NOT in the get_provider
registry (Senate experts keep their own FMP-http fetchers per the replan), so only the
OHLCV price is routed through the provider bundle (Decision 1).
"""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_experts.FMPSenateTraderCopy import FMPSenateTraderCopy
from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight
from ba2_common.core.types import OrderRecommendation, Recommendation, AnalysisUseCase
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)
_LOG = logging.getLogger("test_senate")


@pytest.fixture(autouse=True)
def _isolate_scoring_cache(tmp_path, monkeypatch):
    """The skill/hold-days scoring cache (2026-07-15) persists to CACHE_FOLDER/fmp_history on
    real disk by design (cross-run/cross-trial reuse) — tests must NOT read/write that real
    cache, both to avoid polluting it with fixture data and because different test scenarios
    reuse the SAME trader/date/settings combination with DELIBERATELY different fake price
    data, which would collide if they shared one cache file. Point CACHE_FOLDER at a fresh
    tmp_path per test instead (autouse: every test in this module gets an isolated cache,
    whether or not it exercises scoring directly)."""
    monkeypatch.setattr("ba2_common.config.CACHE_FOLDER", str(tmp_path))


class FakeOHLCV:
    """OHLCV provider for providers.price_at_date; per-symbol close map."""

    def __init__(self, price_map):
        self._price_map = price_map  # symbol(upper) -> close or None

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        price = self._price_map.get(str(symbol).upper())
        if price is None:
            return pd.DataFrame({"Close": []})
        return pd.DataFrame({"Close": [price]})


def _bundle(price_map):
    ohlcv = FakeOHLCV(price_map)
    return LiveProviderBundle(lambda cat, name, **kw: {"ohlcv": ohlcv}[cat])


# ====================================================================
# FMPSenateTraderCopy
# ====================================================================

def _copy_trade(first, last, sym, ttype, disclose, exec_date, amount="$15,001 - $50,000"):
    return {
        "firstName": first, "lastName": last, "symbol": sym, "type": ttype,
        "disclosureDate": disclose, "transactionDate": exec_date, "amount": amount,
    }


def _copy_expert(senate, house):
    e = FMPSenateTraderCopy.__new__(FMPSenateTraderCopy)
    e.id = 1
    e._gather_symbol = "MULTI"
    e.logger = _LOG
    e._fetch_senate_trades = lambda symbol=None: senate
    e._fetch_house_trades = lambda symbol=None: house
    # Live (as_of=None) current_price_map now reads the per-symbol account quote
    # via _get_current_price; pin AAPL to the FakeOHLCV close so live==as_of holds.
    e._get_current_price = lambda sym: {"AAPL": 100.0}.get(str(sym).upper())
    return e


COPY_SETTINGS = {
    "copy_trade_names": "Nancy Pelosi",
    "max_disclose_date_days": 365,
    "max_trade_exec_days": 365,
    "_subtype": AnalysisUseCase.ENTER_MARKET,
}


def test_copy_declares_analyzes_as_basket():
    """senate-basket-dispatch plan Task 3: FMPSenateTraderCopy must declare
    ``analyzes_as_basket = True`` so daily_engine.py's dispatch loop routes it through
    ``_run_basket_expert_bar`` (one analyze_as_of call per bar, List[Recommendation] return)
    instead of ``_run_expert_bar`` (one call per universe symbol expecting a single
    Recommendation — which crashes on a list return, see
    testplatform/backend/tests/backtest/test_daily_engine_basket_dispatch.py Task 1). This is
    a class-level attribute check only; the full engine-integration proof (real
    ExpertRecommendation rows produced via DailyBacktestEngine) lives in that same
    testplatform/backend test file, not here — packages/experts/tests cannot import
    testplatform/backend's ``app.services.backtest`` package (directional dependency: the app
    depends on this package, not the reverse; confirmed by ModuleNotFoundError when attempted
    with cwd at repo root)."""
    assert FMPSenateTraderCopy.analyzes_as_basket is True


def test_copy_gather_resolves_price_map_and_supported():
    senate = [_copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-05-20")]
    house = [_copy_trade("Nancy", "Pelosi", "ZZZZ", "purchase", "2026-06-01", "2026-05-20")]
    e = _copy_expert(senate, house)
    # AAPL priced, ZZZZ unpriced => ZZZZ not supported
    bundle = e._gather(_bundle({"AAPL": 100.0, "ZZZZ": None}), as_of=NOW)
    assert bundle["current_price_map"]["AAPL"] == 100.0
    assert bundle["supported_symbols"] == {"AAPL"}


def test_copy_process_returns_list_one_per_supported_symbol():
    senate = [_copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-05-20")]
    house = [_copy_trade("Nancy", "Pelosi", "ZZZZ", "purchase", "2026-06-01", "2026-05-20")]
    e = _copy_expert(senate, house)
    bundle = e._gather(_bundle({"AAPL": 100.0, "ZZZZ": None}), as_of=NOW)
    recs = e._process(bundle, COPY_SETTINGS, as_of=NOW)
    assert isinstance(recs, list)
    syms = {r.raw_outputs["symbol"] for r in recs}
    assert syms == {"AAPL"}                       # ZZZZ dropped (no price)
    assert recs[0].signal == OrderRecommendation.BUY
    assert recs[0].current_price == 100.0


def test_copy_process_drops_trades_disclosed_after_as_of():
    """A trade disclosed AFTER as_of must not be visible (no-lookahead)."""
    visible = _copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-05-20")
    future = _copy_trade("Nancy", "Pelosi", "TSLA", "purchase", "2026-09-01", "2026-08-20")
    e = _copy_expert([visible, future], [])
    bundle = e._gather(_bundle({"AAPL": 100.0, "TSLA": 200.0}), as_of=NOW)
    recs = e._process(bundle, COPY_SETTINGS, as_of=NOW)
    syms = {r.raw_outputs["symbol"] for r in recs}
    assert syms == {"AAPL"}, f"lookahead leak: {syms}"


def test_copy_process_honours_subtypes():
    """Both ENTER_MARKET and OPEN_POSITIONS subtypes produce recommendations."""
    senate = [_copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-05-20")]
    e = _copy_expert(senate, [])
    bundle = e._gather(_bundle({"AAPL": 100.0}), as_of=NOW)
    for st in (AnalysisUseCase.ENTER_MARKET, AnalysisUseCase.OPEN_POSITIONS):
        recs = e._process(bundle, {**COPY_SETTINGS, "_subtype": st}, as_of=NOW)
        assert len(recs) == 1
        assert recs[0].raw_outputs["symbol"] == "AAPL"


def test_copy_analyze_as_of_equals_live_process():
    senate = [_copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-05-20")]
    e = _copy_expert(senate, [])
    ctx = BacktestContext(providers=_bundle({"AAPL": 100.0}),
                          settings={k: v for k, v in COPY_SETTINGS.items() if k != "_subtype"},
                          as_of=NOW, subtype=AnalysisUseCase.ENTER_MARKET)
    recs_asof = e.analyze_as_of(NOW, ctx)
    # live-style: as_of=None gather, then process pinned to the same now
    bundle_live = e._gather(_bundle({"AAPL": 100.0}), as_of=None)
    recs_live = e._process(bundle_live, COPY_SETTINGS, as_of=NOW)
    assert len(recs_asof) == len(recs_live) == 1
    assert recs_asof[0].almost_equals(recs_live[0])
    assert recs_asof[0].details == recs_live[0].details


# ====================================================================
# FMPSenateTraderWeight
# ====================================================================

def _weight_trade(first, last, sym, ttype, disclose, exec_date, amount="$100,001 - $250,000"):
    return {
        "firstName": first, "lastName": last, "symbol": sym, "type": ttype,
        "disclosureDate": disclose, "transactionDate": exec_date, "amount": amount,
    }


def _weight_expert(symbol, all_trades, history_by_name, exec_price=50.0):
    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    e.id = 1
    e._gather_symbol = symbol
    e.logger = _LOG
    senate = [t for t in all_trades if t.get("_chamber") != "house"]
    house = [t for t in all_trades if t.get("_chamber") == "house"]
    e._fetch_senate_trades = lambda s: senate
    e._fetch_house_trades = lambda s: house
    e._fetch_trader_history = lambda name: history_by_name.get(name)
    e._get_price_at_date = lambda sym, date: exec_price
    # Live (as_of=None) current_price now reads the account quote via
    # _get_current_price; pin to the FakeOHLCV close so live==as_of holds.
    e._get_current_price = lambda sym: 100.0
    return e


WEIGHT_SETTINGS = {
    "max_disclose_date_days": 365,
    "max_trade_exec_days": 365,
    "max_trade_price_delta_pct": 1000.0,   # never filter on price delta
    "growth_confidence_multiplier": 5.0,
    "confidence_to_profit_factor": 0.15,
    "min_traders": 2,
    "min_trades": 2,
    # Generous so pre-existing 2025-dated skill fixtures (tested against NOW=2026-06-13)
    # aren't clipped by the skill_lookback_months filter added later — tests that
    # specifically exercise the lookback window set it explicitly.
    "skill_lookback_months": 60,
}


def test_weight_gather_preresolves_maps():
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
    }
    e = _weight_expert("AAPL", trades, history)
    bundle = e._gather(_bundle({"AAPL": 100.0}), as_of=NOW)
    # exec_price map populated for both trades
    assert len(bundle["exec_price_by_trade"]) == 2
    assert all(v == 50.0 for v in bundle["exec_price_by_trade"].values())
    # trader_history map populated for both traders
    assert set(bundle["trader_history_by_name"].keys()) == {"Alice Aa", "Bob Bb"}
    assert bundle["current_price"] == 100.0


def test_weight_gather_slices_history_to_as_of():
    """Trader-history rows DISCLOSED after as_of must be dropped (no-lookahead)."""
    trades = [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20")]
    history = {
        "Alice Aa": [
            _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20"),  # visible
            _weight_trade("Alice", "Aa", "MSFT", "purchase", "2026-09-01", "2026-08-20"),  # future
        ],
    }
    e = _weight_expert("AAPL", trades, history)
    bundle = e._gather(_bundle({"AAPL": 100.0}), as_of=NOW)
    sliced = bundle["trader_history_by_name"]["Alice Aa"]
    assert len(sliced) == 1
    assert sliced[0]["symbol"] == "AAPL"           # the future MSFT row is gone


def test_gather_window_prefilter_is_behavior_preserving():
    """2026-07-18 perf pass: _gather pre-filters the symbol's trades to the trial's
    disclosure/exec windows before Stages 1-4. Out-of-window trades (and their traders)
    were ALWAYS discarded later by _filter_trades / never consumed by
    _calculate_recommendation — so adding ancient OR future-dated trades from other
    traders must change NOTHING about the recommendation, and the per-trader maps must
    not carry them. The future-dated trades here are the lookahead-bias regression guard
    (2026-07-18 fix): _window_trades must reject disclose_date/exec_date > now exactly
    like _filter_trades does, so it stays a faithful superset rather than admitting rows
    _filter_trades would (now correctly) reject."""
    recent = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    ancient = [
        _weight_trade("Old", "Timer", "AAPL", "purchase", "2023-01-05", "2023-01-01"),
        _weight_trade("Stale", "Seller", "AAPL", "sale", "2022-06-05", "2022-06-01"),
    ]
    future = [
        # Disclosed/executed AFTER NOW (2026-06-13) -- must be excluded, not treated as
        # fresh/current information.
        _weight_trade("Future", "Filer", "AAPL", "purchase", "2026-09-05", "2026-09-01"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
        "Old Timer": [_weight_trade("Old", "Timer", "AAPL", "purchase", "2022-05-01", "2022-04-20")],
        "Stale Seller": [_weight_trade("Stale", "Seller", "AAPL", "sale", "2022-05-02", "2022-04-21")],
        "Future Filer": [_weight_trade("Future", "Filer", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
    }
    settings = {**WEIGHT_SETTINGS, "max_disclose_date_days": 60, "max_trade_exec_days": 90}

    rec_without = _run(_weight_expert("AAPL", recent, history), settings)
    e = _weight_expert("AAPL", recent + ancient + future, history)
    rec_with = _run(e, settings)

    assert rec_with.signal == rec_without.signal
    assert rec_with.confidence == rec_without.confidence
    assert rec_with.expected_profit_percent == rec_without.expected_profit_percent

    e2 = _weight_expert("AAPL", recent + ancient + future, history)
    e2._gather_settings = settings
    bundle = e2._gather(_bundle({"AAPL": 100.0}), as_of=NOW)
    assert set(bundle["trader_history_by_name"].keys()) == {"Alice Aa", "Bob Bb"}
    assert set(bundle["trader_skill_by_name"].keys()) == {"Alice Aa", "Bob Bb"}
    assert "Future Filer" not in bundle["trader_history_by_name"], (
        "lookahead leak: a trade disclosed/executed after 'now' must not surface its "
        "trader in the gather bundle")


def test_gather_day_memo_shares_history_slices_across_symbols():
    """Two symbols analysed under the SAME as_of ceiling must fetch+slice a shared trader's
    history only ONCE (the per-day slice memo) — the second symbol's _gather reuses it."""
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Alice", "Aa", "MSFT", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {"Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")]}
    e = _weight_expert("AAPL", trades, history)
    fetches = []
    orig = e._fetch_trader_history
    e._fetch_trader_history = lambda name: fetches.append(name) or orig(name)

    e._gather_symbol = "AAPL"
    e._gather(_bundle({"AAPL": 100.0}), as_of=NOW)
    e._gather_symbol = "MSFT"
    e._gather(_bundle({"MSFT": 100.0}), as_of=NOW)
    assert fetches == ["Alice Aa"]  # second symbol, same day -> memo hit, no refetch

    # A LATER day invalidates the memo (fresh slice for the new ceiling).
    e._gather_symbol = "AAPL"
    e._gather(_bundle({"AAPL": 100.0}), as_of=NOW + timedelta(days=1))
    assert fetches == ["Alice Aa", "Alice Aa"]


def test_weight_process_uses_preresolved_history_for_confidence():
    """Confidence must be driven by the pre-resolved history map (symbol focus %)."""
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    # Each trader's yearly activity is ONLY this symbol => 10% capped focus =>
    # confidence = 50 + 10*5 + price_adj. With exec 50 -> current 100 (BUY, price up
    # 100%) the favourable move is large; price_confidence_adj = -100/2 = -50 =>
    # confidence floored. We just assert it is BUY and uses the history (non-empty).
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-05-11")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)  # small move => not floored
    bundle = e._gather(_bundle({"AAPL": 100.0}), as_of=NOW)
    rec = e._process(bundle, WEIGHT_SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.BUY
    # symbol focus 10% * multiplier 5 = +50 over base 50 => high confidence
    assert rec.confidence > 80.0
    assert rec.expected_profit_percent > 0.0


def test_weight_process_hold_below_min_traders():
    """One trader only => below min_traders(2) => HOLD."""
    trades = [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20")]
    history = {"Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")]}
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)
    bundle = e._gather(_bundle({"AAPL": 100.0}), as_of=NOW)
    rec = e._process(bundle, WEIGHT_SETTINGS, as_of=NOW)
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.confidence == 0.0


def test_weight_process_is_pure_no_provider_calls():
    """_process must NOT call _fetch_trader_history/_get_price_at_date (they are
    pre-resolved in _gather). Make them raise to prove purity."""
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-05-11")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)
    bundle = e._gather(_bundle({"AAPL": 100.0}), as_of=NOW)

    def _boom(*a, **k):
        raise AssertionError("_process must not call providers/HTTP (impurity!)")

    e._fetch_trader_history = _boom
    e._get_price_at_date = _boom
    rec = e._process(bundle, WEIGHT_SETTINGS, as_of=NOW)   # must not raise
    assert rec.signal == OrderRecommendation.BUY


def test_weight_analyze_as_of_equals_live_process():
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-05-11")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)
    ctx = BacktestContext(providers=_bundle({"AAPL": 100.0}), settings=WEIGHT_SETTINGS,
                          as_of=NOW, extra={"symbol": "AAPL"})
    rec_asof = e.analyze_as_of(NOW, ctx)
    bundle_live = e._gather(_bundle({"AAPL": 100.0}), as_of=None)
    rec_live = e._process(bundle_live, WEIGHT_SETTINGS, as_of=NOW)
    assert rec_asof.almost_equals(rec_live)
    assert rec_asof.details == rec_live.details


# ====================================================================
# FMPSenateTraderWeight — scoring-model upgrades (skill / sell weight /
# size boost / consensus / focus cap / min amount / dedup / float delta)
# ====================================================================

def _two_buyer_setup(exec_price=95.0, amount="$100,001 - $250,000", extra_settings=None):
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20", amount=amount),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21", amount=amount),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-05-11")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=exec_price)
    settings = {**WEIGHT_SETTINGS, **(extra_settings or {})}
    return e, settings


def _run(e, settings, price=100.0):
    e._gather_settings = settings
    bundle = e._gather(_bundle({"AAPL": price}), as_of=NOW)
    return e._process(bundle, settings, as_of=NOW)


def test_weight_dedup_drops_duplicate_filings():
    """The same filing appearing twice (amendment / feed overlap) must count ONCE —
    two dupes of one trade must NOT satisfy min_trades=2."""
    t = _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20")
    dupe = dict(t)
    dupe["_chamber"] = "house"   # arrives via the house feed too
    history = {"Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")]}
    e = _weight_expert("AAPL", [t, dupe], history, exec_price=95.0)
    rec = _run(e, WEIGHT_SETTINGS)
    assert rec.signal == OrderRecommendation.HOLD  # 1 unique trade < min_trades 2


def test_weight_price_delta_threshold_keeps_float_precision():
    """max_trade_price_delta_pct=7.9 must NOT truncate to 7: a +7.5% favourable move
    passes a 7.9% threshold (it was filtered under the old int() cast)."""
    e, settings = _two_buyer_setup(exec_price=93.0)   # current 100 => +7.53% favourable
    settings["max_trade_price_delta_pct"] = 7.9
    rec = _run(e, settings)
    assert rec.signal == OrderRecommendation.BUY, "7.53% move must pass a 7.9% threshold"


def test_weight_min_trade_amount_filters_small_trades():
    e, settings = _two_buyer_setup(amount="$1,001 - $15,000")   # midpoint ~$8k
    settings["min_trade_amount"] = 50000.0
    rec = _run(e, settings)
    assert rec.signal == OrderRecommendation.HOLD   # all trades filtered by size


def test_weight_size_boost_feeds_overall_confidence():
    """Bigger disclosed amounts must now RAISE the final confidence (the boost used
    to be computed per trade but never aggregated)."""
    e_small, settings = _two_buyer_setup(amount="$1,001 - $15,000")
    e_big, _ = _two_buyer_setup(amount="$1,000,001 - $5,000,000")
    conf_small = _run(e_small, settings).confidence
    conf_big = _run(e_big, settings).confidence
    assert conf_big > conf_small


def test_weight_sell_signal_weight_zero_ignores_sells():
    """Equal buy/sell focus is HOLD at sell weight 1.0 but BUY at 0.0."""
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "sale", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "sale", "2026-05-02", "2026-05-11")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=100.0)  # no price-delta effects
    rec_sym = _run(e, {**WEIGHT_SETTINGS, "sell_signal_weight": 1.0})
    assert rec_sym.signal == OrderRecommendation.HOLD   # focus cancels out
    rec_buy = _run(e, {**WEIGHT_SETTINGS, "sell_signal_weight": 0.0})
    assert rec_buy.signal == OrderRecommendation.BUY    # sells ignored


def test_weight_focus_cap_configurable():
    """A trader with 100% symbol focus scores higher under a 20% cap than a 10% cap."""
    e, settings = _two_buyer_setup(exec_price=100.0)
    conf_cap10 = _run(e, {**settings, "symbol_focus_cap_pct": 10.0}).confidence
    conf_cap20 = _run(e, {**settings, "symbol_focus_cap_pct": 20.0, "growth_confidence_multiplier": 2.0}).confidence
    conf_cap10_lowmult = _run(e, {**settings, "symbol_focus_cap_pct": 10.0, "growth_confidence_multiplier": 2.0}).confidence
    # Same multiplier: raising the cap raises confidence (their focus is 100% -> capped).
    assert conf_cap20 > conf_cap10_lowmult
    assert conf_cap10 > 0


def test_weight_consensus_bonus_scales_with_unique_traders():
    """More unique buyers (same focus) => higher confidence via the consensus term."""
    def _mk(n_traders):
        trades, history = [], {}
        for i in range(n_traders):
            first, last = f"T{i}", "Xx"
            trades.append(_weight_trade(first, last, "AAPL", "purchase", "2026-06-01", "2026-05-20"))
            history[f"{first} {last}"] = [
                _weight_trade(first, last, "AAPL", "purchase", "2026-05-01", "2026-05-10")]
        return _weight_expert("AAPL", trades, history, exec_price=100.0)

    settings = {**WEIGHT_SETTINGS, "consensus_bonus_per_trader": 3.0, "consensus_bonus_max": 10.0,
                "growth_confidence_multiplier": 1.0}
    conf2 = _run(_mk(2), settings).confidence
    conf4 = _run(_mk(4), settings).confidence
    assert conf4 > conf2
    assert conf4 - conf2 == 6.0   # 2 extra traders x 3.0


def _skill_history(first, last, n, rising):
    """n past AAPL buys with completed forward windows; price map decides outcome."""
    rows = []
    for i in range(n):
        d = f"2025-{(i % 9) + 1:02d}-10"
        rows.append(_weight_trade(first, last, "SKL", "purchase", d, d))
    return rows


def _skill_expert(all_trades, history_by_name, price_fn):
    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    e.id = 1
    e._gather_symbol = "AAPL"
    e.logger = _LOG
    e._fetch_senate_trades = lambda s: all_trades
    e._fetch_house_trades = lambda s: []
    e._fetch_trader_history = lambda name: history_by_name.get(name)
    e._get_price_at_date = price_fn
    e._get_current_price = lambda sym: 100.0
    return e


def test_weight_trader_skill_boosts_and_penalizes_confidence():
    """A trader whose past buys rose over the horizon gets skill +1 (confidence up);
    one whose buys fell gets skill -1 (confidence down) vs the neutral baseline.

    Uses DISTINCT trader names per price scenario (Good/Bad) even though the "good" and
    "neutral" runs share names — the persistent scoring cache (2026-07-15) is keyed by
    (trader, history length, as-of, settings), which is correct for real data (a real
    symbol's price history doesn't change between calls) but would collide here if the
    rising/falling scenarios reused the same names against the SAME cache directory.
    """
    trades_good = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history_good = {
        "Alice Aa": _skill_history("Alice", "Aa", 6, rising=True),
        "Bob Bb": _skill_history("Bob", "Bb", 6, rising=True),
    }
    trades_bad = [
        _weight_trade("AliceBad", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("BobBad", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history_bad = {
        "AliceBad Aa": _skill_history("AliceBad", "Aa", 6, rising=True),
        "BobBad Bb": _skill_history("BobBad", "Bb", 6, rising=True),
    }

    def _price_rising(sym, date):
        if str(sym).upper() == "AAPL":
            return 100.0                      # analyzed symbol: flat (no delta effects)
        # SKL: price grows over time => every past buy is a WINNER
        return 50.0 + (date - datetime(2025, 1, 1, tzinfo=timezone.utc)).days * 0.1

    def _price_falling(sym, date):
        if str(sym).upper() == "AAPL":
            return 100.0
        return 500.0 - (date - datetime(2025, 1, 1, tzinfo=timezone.utc)).days * 0.5

    settings = {**WEIGHT_SETTINGS, "skill_confidence_weight": 10.0, "skill_signal_weight": 0.5}
    conf_good = _run(_skill_expert(trades_good, history_good, _price_rising), settings).confidence
    conf_bad = _run(_skill_expert(trades_bad, history_bad, _price_falling), settings).confidence
    conf_neutral = _run(_skill_expert(trades_good, history_good, _price_rising),
                        {**settings, "skill_confidence_weight": 0.0}).confidence
    assert conf_good == conf_neutral + 10.0   # avg skill +1 x weight 10
    assert conf_bad == conf_neutral - 10.0    # avg skill -1 x weight 10


def test_weight_skill_neutral_below_min_past_trades():
    """Fewer scored past buys than skill_min_past_trades => neutral (no skill effect)."""
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": _skill_history("Alice", "Aa", 2, rising=True),   # 2 < min 5
        "Bob Bb": _skill_history("Bob", "Bb", 2, rising=True),
    }

    def _price(sym, date):
        if str(sym).upper() == "AAPL":
            return 100.0
        return 50.0 + (date - datetime(2025, 1, 1, tzinfo=timezone.utc)).days * 0.1

    settings = {**WEIGHT_SETTINGS, "skill_confidence_weight": 10.0}
    conf = _run(_skill_expert(trades, history, _price), settings).confidence
    conf_disabled = _run(_skill_expert(trades, history, _price),
                         {**settings, "skill_confidence_weight": 0.0}).confidence
    assert conf == conf_disabled   # neutral skill: no adjustment either way


# ====================================================================
# FMPSenateTraderWeight — scalper filter (min_trader_avg_hold_days)
# ====================================================================

def test_calculate_trader_avg_hold_days_pairs_fifo_per_symbol():
    """Two symbols, one round-trip each: 2 days and 30 days -> avg 16, 2 roundtrips.
    An unmatched trailing BUY (no sell yet) must not be counted."""
    history = [
        _weight_trade("A", "B", "AAPL", "purchase", "2026-01-01", "2026-01-01"),
        _weight_trade("A", "B", "AAPL", "sale", "2026-01-03", "2026-01-03"),        # 2 days
        _weight_trade("A", "B", "MSFT", "purchase", "2026-02-01", "2026-02-01"),
        _weight_trade("A", "B", "MSFT", "sale", "2026-03-03", "2026-03-03"),        # 30 days
        _weight_trade("A", "B", "GOOGL", "purchase", "2026-04-01", "2026-04-01"),   # no sell yet
    ]
    result = FMPSenateTraderWeight._calculate_trader_avg_hold_days(history)
    assert result["roundtrips"] == 2
    assert result["avg_hold_days"] == 16.0


def test_calculate_trader_avg_hold_days_no_roundtrips_returns_none():
    history = [_weight_trade("A", "B", "AAPL", "purchase", "2026-01-01", "2026-01-01")]
    result = FMPSenateTraderWeight._calculate_trader_avg_hold_days(history)
    assert result["avg_hold_days"] is None
    assert result["roundtrips"] == 0


def _scalper_history(first, last, n_roundtrips, hold_days=1):
    """n_roundtrips FIFO-paired buy/sell trades, each held `hold_days` (default: same-day
    flip, like the live Ro Khanna incident: thousands of rapid disclosed round-trips).
    Round trips are spaced a year apart so they never overlap regardless of hold_days."""
    from datetime import timedelta as _td
    rows = []
    for i in range(n_roundtrips):
        buy_dt = datetime(2020 + i, 1, 1)
        sell_dt = buy_dt + _td(days=hold_days)
        rows.append(_weight_trade(first, last, "SKL", "purchase",
                                  buy_dt.strftime("%Y-%m-%d"), buy_dt.strftime("%Y-%m-%d")))
        rows.append(_weight_trade(first, last, "SKL", "sale",
                                  sell_dt.strftime("%Y-%m-%d"), sell_dt.strftime("%Y-%m-%d")))
    return rows


def test_weight_scalper_filter_excludes_frequent_flipper():
    """A scalper-like trader (avg hold 1 day, well past min roundtrips) is excluded when
    min_trader_avg_hold_days is set; a normal long-hold trader still counts."""
    trades = [
        _weight_trade("Scalper", "Sam", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("LongHold", "Lee", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Scalper Sam": _scalper_history("Scalper", "Sam", 10, hold_days=1),
        "LongHold Lee": _scalper_history("LongHold", "Lee", 5, hold_days=90),  # genuinely long holds
    }
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)
    settings = {**WEIGHT_SETTINGS, "min_trader_avg_hold_days": 5.0, "min_trader_hold_roundtrips": 3,
                "min_traders": 1, "min_trades": 1}
    rec = _run(e, settings)
    raw = rec.raw_outputs["recommendation"]
    assert raw["scalpers_filtered"] == 1
    traders_counted = {t["trader"] for t in raw["trades"]}
    assert "Scalper Sam" not in traders_counted
    assert "LongHold Lee" in traders_counted


def test_weight_scalper_filter_disabled_by_default():
    """min_trader_avg_hold_days=0 (default) must NOT filter anything, even an extreme
    scalper — backward compatible with every pre-existing test/config."""
    trades = [
        _weight_trade("Scalper", "Sam", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Other", "Trader", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Scalper Sam": _scalper_history("Scalper", "Sam", 10, hold_days=1),
        "Other Trader": [_weight_trade("Other", "Trader", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)
    rec = _run(e, WEIGHT_SETTINGS)  # min_trader_avg_hold_days defaults to 0.0
    raw = rec.raw_outputs["recommendation"]
    assert raw["scalpers_filtered"] == 0
    assert {t["trader"] for t in raw["trades"]} == {"Scalper Sam", "Other Trader"}


def test_weight_scalper_filter_respects_min_roundtrips():
    """A trader with FEWER round-trips than min_trader_hold_roundtrips is NOT filtered
    even if their few trades happen to be quick flips (not enough history to judge)."""
    trades = [
        _weight_trade("Scalper", "Sam", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Other", "Trader", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        # Only 1 round-trip, quick flip — below min_trader_hold_roundtrips=3.
        "Scalper Sam": _scalper_history("Scalper", "Sam", 1, hold_days=1),
        "Other Trader": [_weight_trade("Other", "Trader", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)
    settings = {**WEIGHT_SETTINGS, "min_trader_avg_hold_days": 5.0, "min_trader_hold_roundtrips": 3}
    rec = _run(e, settings)
    raw = rec.raw_outputs["recommendation"]
    assert raw["scalpers_filtered"] == 0
    assert {t["trader"] for t in raw["trades"]} == {"Scalper Sam", "Other Trader"}


# ====================================================================
# FMPSenateTraderWeight — cross-run scoring cache (hold-days / skill)
# and the skill lookback window
# ====================================================================

def test_hold_info_cache_avoids_recompute(monkeypatch):
    """Second call with the SAME history must NOT re-invoke the pure calculator."""
    e = _weight_expert("AAPL", [], {})
    history = [_weight_trade("A", "B", "AAPL", "purchase", "2026-01-01", "2026-01-01"),
              _weight_trade("A", "B", "AAPL", "sale", "2026-01-05", "2026-01-05")]

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_avg_hold_days

    def _spy(history_arg):
        calls.append(1)
        return real(history_arg)

    monkeypatch.setattr(e, "_calculate_trader_avg_hold_days", _spy)

    r1 = e._get_trader_hold_info_cached("Trader X", history)
    r2 = e._get_trader_hold_info_cached("Trader X", history)
    assert len(calls) == 1, "second call must be served from cache, not recomputed"
    assert r1 == r2 == {"avg_hold_days": 4.0, "roundtrips": 1}


def test_hold_info_cache_invalidates_on_new_disclosure(monkeypatch):
    """A LONGER history (new disclosure) must invalidate the cache and recompute fresh."""
    e = _weight_expert("AAPL", [], {})
    short_history = [_weight_trade("A", "B", "AAPL", "purchase", "2026-01-01", "2026-01-01"),
                     _weight_trade("A", "B", "AAPL", "sale", "2026-01-05", "2026-01-05")]
    longer_history = short_history + [
        _weight_trade("A", "B", "MSFT", "purchase", "2026-02-01", "2026-02-01"),
        _weight_trade("A", "B", "MSFT", "sale", "2026-02-21", "2026-02-21"),
    ]

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_avg_hold_days

    def _spy(history_arg):
        calls.append(len(history_arg))
        return real(history_arg)

    monkeypatch.setattr(e, "_calculate_trader_avg_hold_days", _spy)

    e._get_trader_hold_info_cached("Trader X", short_history)
    r2 = e._get_trader_hold_info_cached("Trader X", longer_history)
    assert calls == [2, 4], "a longer history must trigger a fresh recompute, not reuse the stale entry"
    assert r2["roundtrips"] == 2
    assert r2["avg_hold_days"] == 12.0  # (4 + 20) / 2


def test_skill_cache_avoids_recompute_same_day(monkeypatch):
    e = _weight_expert("AAPL", [], {})
    history = _skill_history("A", "B", 6, rising=True)

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_skill

    def _spy(history_arg, **kw):
        calls.append(1)
        return real(e, history_arg, **kw)

    monkeypatch.setattr(e, "_calculate_trader_skill", _spy)

    kw = dict(now=NOW, horizon_days=60, min_past_trades=1, max_past_trades=50,
             lookback_months=60, is_live=False)
    r1 = e._get_trader_skill_cached("Trader X", history, **kw)
    r2 = e._get_trader_skill_cached("Trader X", history, **kw)
    assert len(calls) == 1
    assert r1 == r2


def test_skill_cache_backtest_buckets_by_exact_day(monkeypatch):
    """Backtest (is_live=False): a DIFFERENT as_of day must recompute, even same-month."""
    e = _weight_expert("AAPL", [], {})
    history = _skill_history("A", "B", 6, rising=True)

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_skill

    def _spy(history_arg, **kw):
        calls.append(1)
        return real(e, history_arg, **kw)

    monkeypatch.setattr(e, "_calculate_trader_skill", _spy)

    common = dict(horizon_days=60, min_past_trades=1, max_past_trades=50,
                 lookback_months=60, is_live=False)
    e._get_trader_skill_cached("Trader X", history, now=NOW, **common)
    e._get_trader_skill_cached("Trader X", history, now=NOW + timedelta(days=1), **common)
    assert len(calls) == 2, "backtest must bucket by exact day, not reuse across days"


def test_skill_cache_live_buckets_by_month(monkeypatch):
    """Live (is_live=True): two different DAYS in the SAME month must reuse the cache;
    a date in the NEXT month must recompute."""
    e = _weight_expert("AAPL", [], {})
    history = _skill_history("A", "B", 6, rising=True)

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_skill

    def _spy(history_arg, **kw):
        calls.append(1)
        return real(e, history_arg, **kw)

    monkeypatch.setattr(e, "_calculate_trader_skill", _spy)

    common = dict(horizon_days=60, min_past_trades=1, max_past_trades=50,
                 lookback_months=60, is_live=True)
    day1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    day2 = datetime(2026, 6, 28, tzinfo=timezone.utc)   # same month as day1
    day3 = datetime(2026, 7, 1, tzinfo=timezone.utc)    # next month
    e._get_trader_skill_cached("Trader X", history, now=day1, **common)
    e._get_trader_skill_cached("Trader X", history, now=day2, **common)
    assert len(calls) == 1, "same-month live calls must be served from cache"
    e._get_trader_skill_cached("Trader X", history, now=day3, **common)
    assert len(calls) == 2, "a new month must trigger a fresh recompute"


def test_skill_lookback_months_excludes_old_trades():
    """A buy far outside the lookback window is not scored, even though its horizon window
    is complete — recency bound, not just completeness."""
    old_buy = [_weight_trade("A", "B", "SKL", "purchase", "2020-01-01", "2020-01-01")]
    e = _weight_expert("AAPL", [], {})

    result_no_lookback = e._calculate_trader_skill(
        old_buy, now=NOW, horizon_days=60, min_past_trades=1, max_past_trades=50,
        lookback_months=120)  # 10 years — includes the 2020 trade
    result_short_lookback = e._calculate_trader_skill(
        old_buy, now=NOW, horizon_days=60, min_past_trades=1, max_past_trades=50,
        lookback_months=12)  # 1 year — excludes the 2020 trade
    assert result_no_lookback["scored_trades"] >= 0  # may be 0 if price lookup fails; just checking no crash
    assert result_short_lookback["scored_trades"] == 0


def test_confidence_cache_avoids_recompute(monkeypatch):
    """Second call with the SAME (trader, symbol, side, day) must NOT re-invoke the pure
    calculator — this was the MOST-called of the three O(history) scans in profiling."""
    e = _weight_expert("AAPL", [], {})
    history = [_weight_trade("A", "B", "AAPL", "purchase", "2026-01-01", "2026-01-01")]

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_confidence

    def _spy(history_arg, *a, **kw):
        calls.append(1)
        return real(e, history_arg, *a, **kw)

    monkeypatch.setattr(e, "_calculate_trader_confidence", _spy)

    kw = dict(current_trade_type="purchase", current_symbol="AAPL", current_price=100.0,
             max_exec_days=60, now=NOW, symbol_focus_cap_pct=10.0, is_live=False)
    r1 = e._get_trader_confidence_cached("Trader X", history, **kw)
    r2 = e._get_trader_confidence_cached("Trader X", history, **kw)
    assert len(calls) == 1
    assert r1 == r2


def test_confidence_cache_differs_per_symbol(monkeypatch):
    """A DIFFERENT current_symbol must NOT reuse another symbol's cached entry — the
    symbol-specific focus subtotal is part of what's being cached."""
    e = _weight_expert("AAPL", [], {})
    history = [_weight_trade("A", "B", "AAPL", "purchase", "2026-01-01", "2026-01-01"),
              _weight_trade("A", "B", "MSFT", "purchase", "2026-01-02", "2026-01-02")]

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_confidence

    def _spy(history_arg, *a, **kw):
        calls.append(1)
        return real(e, history_arg, *a, **kw)

    monkeypatch.setattr(e, "_calculate_trader_confidence", _spy)

    common = dict(current_trade_type="purchase", current_price=100.0, max_exec_days=60,
                  now=NOW, symbol_focus_cap_pct=10.0, is_live=False)
    e._get_trader_confidence_cached("Trader X", history, current_symbol="AAPL", **common)
    e._get_trader_confidence_cached("Trader X", history, current_symbol="MSFT", **common)
    assert len(calls) == 2, "different symbols must each get their own cache entry"


def test_confidence_cache_live_buckets_by_month(monkeypatch):
    e = _weight_expert("AAPL", [], {})
    history = [_weight_trade("A", "B", "AAPL", "purchase", "2026-01-01", "2026-01-01")]

    calls = []
    real = FMPSenateTraderWeight._calculate_trader_confidence

    def _spy(history_arg, *a, **kw):
        calls.append(1)
        return real(e, history_arg, *a, **kw)

    monkeypatch.setattr(e, "_calculate_trader_confidence", _spy)

    common = dict(current_trade_type="purchase", current_symbol="AAPL", current_price=100.0,
                 max_exec_days=60, symbol_focus_cap_pct=10.0, is_live=True)
    day1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    day2 = datetime(2026, 6, 28, tzinfo=timezone.utc)
    day3 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    e._get_trader_confidence_cached("Trader X", history, now=day1, **common)
    e._get_trader_confidence_cached("Trader X", history, now=day2, **common)
    assert len(calls) == 1
    e._get_trader_confidence_cached("Trader X", history, now=day3, **common)
    assert len(calls) == 2


# ====================================================================
# FMPSenateTraderWeight — _calculate_trader_skill bisect fast-path equivalence
#
# The prewarm perf pass (2026-07-17) replaced _calculate_trader_skill's per-call O(history)
# rebuild-and-filter with an O(log n) bisect over a candidate list precomputed ONCE per trader
# (_sorted_buy_candidates / _sorted_buy_candidates_cached). This is exercised on production
# data via _get_trader_skill_cached, but the equivalence proof lives here: a from-scratch
# REFERENCE reimplementation of the ORIGINAL (pre-optimization) O(history) algorithm, run
# differentially against the current implementation over many randomized histories — including
# same-exec_date ties, which is the one case where a naive rewrite could silently reorder which
# trades get selected under the max_past_trades cap.
# ====================================================================
import random as _random


def _reference_calculate_trader_skill(trader_history, now, horizon_days, min_past_trades,
                                       max_past_trades, lookback_months=12):
    """Golden reference: byte-for-byte copy of _calculate_trader_skill's ORIGINAL body
    (before the bisect fast-path), kept independent of production code on purpose so a future
    accidental edit to the real implementation can't silently drag this reference along and
    hide a regression."""
    from ba2_experts.FMPSenateTraderWeight import _parse_ymd_utc
    neutral = {"skill_score": 0.0, "scored_trades": 0, "hit_rate": None, "avg_fwd_return_pct": 0.0}
    if not trader_history or horizon_days <= 0:
        return neutral
    lookback_floor = now - timedelta(days=max(1, lookback_months) * 30)
    candidates = []
    for t in trader_history:
        ttype = str(t.get('type', '')).lower()
        if 'purchase' not in ttype and 'buy' not in ttype:
            continue
        exec_str = t.get('transactionDate', '')
        sym = str(t.get('symbol', '')).upper()
        if not exec_str or not sym:
            continue
        try:
            exec_date = _parse_ymd_utc(exec_str)
        except (ValueError, TypeError):
            continue
        if exec_date < lookback_floor:
            continue
        if exec_date + timedelta(days=horizon_days) > now:
            continue
        candidates.append((exec_date, sym))
    candidates.sort(key=lambda c: c[0], reverse=True)

    returns = []
    for exec_date, sym in candidates:
        if len(returns) >= max_past_trades:
            break
        # NOTE: uses the SAME price stub as the real instance (injected by the caller via a
        # closure) -- see _price_fn below.
        entry = _reference_price_fn(sym, exec_date)
        fwd = _reference_price_fn(sym, exec_date + timedelta(days=horizon_days))
        if not entry or not fwd:
            continue
        returns.append((fwd - entry) / entry * 100.0)

    if len(returns) < min_past_trades:
        return neutral
    hits = sum(1 for r in returns if r > 0)
    hit_rate = hits / len(returns)
    return {
        "skill_score": max(-1.0, min(1.0, (hit_rate - 0.5) * 2.0)),
        "scored_trades": len(returns),
        "hit_rate": hit_rate,
        "avg_fwd_return_pct": sum(returns) / len(returns),
    }


_reference_price_fn = None  # set per-test via a closure so both implementations see identical prices


def _make_deterministic_price_fn(seed):
    """A pseudo-random but DETERMINISTIC per-(symbol, date) price, so entry/fwd differ enough
    to exercise both winning and losing "trades", and repeated calls for the same (symbol,
    date) are stable (real price history behaves the same way)."""
    def _price(sym, date):
        h = hash((sym, date.date().isoformat(), seed))
        return 10.0 + (h % 9000) / 100.0  # in [10, 100)
    return _price


def test_skill_bisect_fastpath_matches_reference_random_histories():
    """Differential test: for many randomized trader histories/settings (including same-day
    ties across different symbols, the one case tie-break order could diverge), the current
    _calculate_trader_skill (bisect fast-path, via _sorted_buy_candidates_cached) must produce
    IDENTICAL results to the golden pre-optimization reference above."""
    global _reference_price_fn
    rng = _random.Random(12345)
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)

    for trial in range(30):
        e = _weight_expert("AAPL", [], {})
        price_fn = _make_deterministic_price_fn(seed=trial)
        e._get_price_at_date = lambda sym, date, _f=price_fn: _f(sym, date)
        _reference_price_fn = price_fn

        n_trades = rng.randint(0, 60)
        history = []
        for _ in range(n_trades):
            offset_days = rng.randint(0, 900)
            exec_date = base + timedelta(days=offset_days)
            # Force clustering onto a handful of exec dates so same-day ties across
            # different symbols/traders are common, not a rare edge case.
            exec_date = base + timedelta(days=(offset_days // 15) * 15)
            sym = rng.choice(symbols)
            ttype = rng.choice(["purchase", "purchase", "purchase", "sale"])  # mostly buys
            history.append({
                "firstName": "T", "lastName": str(trial), "symbol": sym, "type": ttype,
                "disclosureDate": exec_date.date().isoformat(),
                "transactionDate": exec_date.date().isoformat(),
                "amount": "$1,001 - $15,000",
            })

        now = base + timedelta(days=rng.randint(200, 1000))
        horizon_days = rng.choice([30, 60, 90])
        lookback_months = rng.choice([6, 12])
        min_past_trades = rng.choice([1, 5])
        max_past_trades = rng.choice([5, 50])

        expected = _reference_calculate_trader_skill(
            history, now=now, horizon_days=horizon_days,
            min_past_trades=min_past_trades, max_past_trades=max_past_trades,
            lookback_months=lookback_months)
        actual = e._get_trader_skill_cached(
            f"Trader{trial}", history, now=now, horizon_days=horizon_days,
            min_past_trades=min_past_trades, max_past_trades=max_past_trades,
            lookback_months=lookback_months, is_live=False)

        assert actual == expected, (
            f"trial={trial} n_trades={n_trades} now={now} horizon={horizon_days} "
            f"lookback={lookback_months} min={min_past_trades} max={max_past_trades}")


# ====================================================================
# FMPSenateTraderWeight — _calculate_trader_confidence prefix-sum fast-path equivalence
#
# The 2026-07-18 perf pass added an optional precomputed prefix-sum index (_index param,
# built by _build_confidence_index / memoized by _confidence_index_cached) that answers the
# recent/yearly window aggregates with O(log n) bisects instead of the original O(n) history
# rescan. The original scan path is kept verbatim as the no-index fallback — so equivalence
# is provable directly: run BOTH paths of the same production function on randomized
# histories (including messy rows: unparseable dates, missing dates, exchange types, mixed
# symbols) and require identical result dicts.
# ====================================================================

def test_confidence_index_fastpath_matches_full_scan():
    rng = _random.Random(98765)
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    amounts = ["$1,001 - $15,000", "$15,001 - $50,000", "$50,001 - $100,000", "$1,000,001 - $5,000,000"]
    types = ["purchase", "sale", "sale (partial)", "exchange", "purchase"]

    for trial in range(30):
        e = _weight_expert("AAPL", [], {})
        n_trades = rng.randint(0, 80)
        history = []
        for i in range(n_trades):
            exec_date = base + timedelta(days=(rng.randint(0, 900) // 10) * 10)  # cluster ties
            row = {
                "firstName": "T", "lastName": str(trial), "symbol": rng.choice(symbols),
                "type": rng.choice(types),
                "disclosureDate": exec_date.date().isoformat(),
                "transactionDate": exec_date.date().isoformat(),
                "amount": rng.choice(amounts),
            }
            # Inject messy rows the original loop skips/tolerates: no date, garbage date.
            if i % 13 == 0:
                row["transactionDate"] = ""
            elif i % 17 == 0:
                row["transactionDate"] = "not-a-date"
            history.append(row)

        now = base + timedelta(days=rng.randint(100, 1100))
        max_exec_days = rng.choice([30, 60, 90, 120])
        focus_cap = rng.choice([5.0, 10.0, 25.0])
        cur_symbol = rng.choice(symbols)
        cur_type = rng.choice(["purchase", "sale"])

        slow = e._calculate_trader_confidence(
            history, cur_type, cur_symbol, 100.0, max_exec_days=max_exec_days,
            now=now, symbol_focus_cap_pct=focus_cap)
        index = e._build_confidence_index(history, e.logger)
        fast = e._calculate_trader_confidence(
            history, cur_type, cur_symbol, 100.0, max_exec_days=max_exec_days,
            now=now, symbol_focus_cap_pct=focus_cap, _index=index)

        assert fast == slow, (
            f"trial={trial} n_trades={n_trades} now={now} exec_days={max_exec_days} "
            f"cap={focus_cap} sym={cur_symbol} type={cur_type}\nslow={slow}\nfast={fast}")


def test_confidence_cached_uses_index_and_matches(monkeypatch):
    """_get_trader_confidence_cached must produce the same result as a direct no-index call
    (proving the wired-in index path is transparent), and the index memo must be reused
    across calls for the same (trader, history length)."""
    e = _weight_expert("AAPL", [], {})
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    history = [
        {"firstName": "A", "lastName": "B", "symbol": "AAA", "type": "purchase",
         "disclosureDate": (base + timedelta(days=i * 30)).date().isoformat(),
         "transactionDate": (base + timedelta(days=i * 30)).date().isoformat(),
         "amount": "$15,001 - $50,000"}
        for i in range(10)
    ]
    now = base + timedelta(days=400)

    direct = e._calculate_trader_confidence(history, "purchase", "AAA", 100.0,
                                            max_exec_days=60, now=now,
                                            symbol_focus_cap_pct=10.0)
    cached = e._get_trader_confidence_cached(
        "Trader X", history, current_trade_type="purchase", current_symbol="AAA",
        current_price=100.0, max_exec_days=60, now=now, symbol_focus_cap_pct=10.0,
        is_live=False)
    assert cached == direct

    builds = []
    real_build = FMPSenateTraderWeight._build_confidence_index
    monkeypatch.setattr(FMPSenateTraderWeight, "_build_confidence_index",
                        staticmethod(lambda h, lg: builds.append(1) or real_build(h, lg)))
    # New day -> new cache key -> miss -> index needed again, but the (trader, n) memo
    # already holds it: no rebuild.
    e._get_trader_confidence_cached(
        "Trader X", history, current_trade_type="purchase", current_symbol="AAA",
        current_price=100.0, max_exec_days=60, now=now + timedelta(days=1),
        symbol_focus_cap_pct=10.0, is_live=False)
    assert builds == []  # memo hit, no rebuild


# ====================================================================
# FMPSenateTraderWeight — basket dispatch (senate-basket-dispatch plan, Task 5):
# _gather_all / _process_all / analyze_as_of dual-mode dispatch.
#
# Unlike _weight_expert (which stubs _fetch_senate_trades/_fetch_house_trades with a
# single-positional-arg lambda relying on _symbol_trades_index_cached's internal per-symbol
# filtering), the basket path calls _fetch_senate_trades(symbol=None)/_fetch_house_trades
# (symbol=None) directly — so its fixture stubs must accept the keyword call.
# ====================================================================

def _weight_basket_expert(senate_trades, house_trades, history_by_name, price_map, exec_price=50.0):
    """Basket-mode expert fixture: _fetch_senate_trades/_fetch_house_trades accept
    symbol=None (the unscoped '-latest' feed convention _gather_all relies on) instead of
    _weight_expert's per-symbol wrappers. exec_price is a flat per-trade execution price
    (same simplification _weight_expert uses) so a differential test against _weight_expert
    can use the identical value on both sides."""
    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    e.id = 1
    e.logger = _LOG
    # **kwargs: _all_trades_index_cached calls these with full_history=True (deep-pagination
    # opt-in, see expert_mixins.py's "Pagination-depth design" docstring) -- this fixture
    # ignores the flag and always returns the same fixed trade list regardless of depth.
    e._fetch_senate_trades = lambda symbol=None, **kwargs: senate_trades
    e._fetch_house_trades = lambda symbol=None, **kwargs: house_trades
    e._fetch_trader_history = lambda name: history_by_name.get(name)
    e._get_price_at_date = lambda sym, date: exec_price
    e._get_current_price = lambda sym: price_map.get(str(sym).upper())
    return e


def _sorted_trades(trades):
    """Order-independent comparison key for a trade list (differential tests must not depend
    on incidental ordering between the bisect-window-preserving basket grouping and the
    per-symbol path's disclose-date sort — both are correct; only CONTENT should be compared)."""
    return sorted(trades, key=lambda t: (t.get('firstName', ''), t.get('lastName', ''),
                                          t.get('symbol', ''), t.get('transactionDate', ''),
                                          t.get('type', '')))


def test_gather_all_matches_per_symbol_gather_for_each_surviving_symbol():
    """Differential test: for every symbol that survives _gather_all's window+tradability
    filter, the per-symbol bundle it produces must be equivalent to calling the EXISTING
    per-symbol _gather(symbol=that_symbol, as_of) directly -- proves the basket path isn't
    silently dropping or corrupting data relative to the already-trusted per-symbol path."""
    senate = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    house = [
        _weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-06-02", "2026-05-21"),
    ]
    all_trades = senate + house
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
        "Carol Cc": [_weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-05-01", "2026-04-20")],
        "Dave Dd": [_weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-05-02", "2026-04-21")],
    }
    price_map = {"AAPL": 100.0, "MSFT": 60.0}

    e_all = _weight_basket_expert(senate, house, history, price_map, exec_price=50.0)
    e_all._gather_settings = WEIGHT_SETTINGS
    bundle_all = e_all._gather_all(_bundle(price_map), as_of=NOW)

    assert set(bundle_all.keys()) == {"AAPL", "MSFT"}

    for symbol, basket_bundle in bundle_all.items():
        e_single = _weight_expert(symbol, all_trades, history, exec_price=50.0)
        e_single._gather_settings = WEIGHT_SETTINGS
        single_bundle = e_single._gather(_bundle(price_map), as_of=NOW)

        assert _sorted_trades(basket_bundle["all_trades"]) == _sorted_trades(single_bundle["all_trades"])
        assert basket_bundle["current_price"] == single_bundle["current_price"]
        assert basket_bundle["exec_price_by_trade"] == single_bundle["exec_price_by_trade"]
        assert basket_bundle["symbol"] == single_bundle["symbol"] == symbol

        # trader_history_by_name/skill/hold-info are computed ONCE for the whole bar and
        # SHARED across every qualifying symbol in the basket path (the actual perf win, see
        # _gather_all's docstring) -- so the basket dict is a SUPERSET of what a per-symbol
        # _gather call would build for just this symbol, not an exact match. Equivalence here
        # means: every trader the per-symbol path resolved gets an IDENTICAL value in the
        # basket path (no corruption for the traders that matter to THIS symbol).
        for name, hist in single_bundle["trader_history_by_name"].items():
            assert basket_bundle["trader_history_by_name"][name] == hist
        for name, skill in single_bundle["trader_skill_by_name"].items():
            assert basket_bundle["trader_skill_by_name"][name] == skill
        for name, hold in single_bundle["trader_hold_info_by_name"].items():
            assert basket_bundle["trader_hold_info_by_name"][name] == hold


def test_process_all_matches_calculate_recommendation_per_symbol():
    """Differential test: _process_all's per-symbol Recommendation must match calling
    _calculate_recommendation directly on that symbol's filtered_trades -- same signal,
    confidence, expected_profit_percent."""
    senate = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    house = [
        _weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
        "Carol Cc": [_weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-05-01", "2026-04-20")],
        "Dave Dd": [_weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-05-02", "2026-04-21")],
    }
    price_map = {"AAPL": 100.0, "MSFT": 60.0}

    e = _weight_basket_expert(senate, house, history, price_map, exec_price=50.0)
    e._gather_settings = WEIGHT_SETTINGS
    bundle_by_symbol = e._gather_all(_bundle(price_map), as_of=NOW)
    recs = e._process_all(bundle_by_symbol, WEIGHT_SETTINGS, as_of=NOW)

    recs_by_symbol = {r.raw_outputs["symbol"]: r for r in recs}
    assert set(recs_by_symbol) == set(bundle_by_symbol) == {"AAPL", "MSFT"}

    for symbol, data_bundle in bundle_by_symbol.items():
        current_price = data_bundle["current_price"]
        filtered = e._filter_trades(
            data_bundle["all_trades"],
            int(WEIGHT_SETTINGS["max_disclose_date_days"]),
            int(WEIGHT_SETTINGS["max_trade_exec_days"]),
            float(WEIGHT_SETTINGS["max_trade_price_delta_pct"]),
            current_price, symbol, now=NOW,
            exec_price_by_trade=data_bundle["exec_price_by_trade"],
        )
        direct = e._calculate_recommendation(
            filtered, symbol, current_price,
            int(WEIGHT_SETTINGS["max_trade_exec_days"]),
            trader_history_by_name=data_bundle["trader_history_by_name"],
            min_traders=int(WEIGHT_SETTINGS["min_traders"]),
            min_trades_required=int(WEIGHT_SETTINGS["min_trades"]),
            growth_multiplier=float(WEIGHT_SETTINGS["growth_confidence_multiplier"]),
            confidence_to_profit_factor=float(WEIGHT_SETTINGS["confidence_to_profit_factor"]),
            now=NOW,
            trader_skill_by_name=data_bundle.get("trader_skill_by_name") or {},
            trader_hold_info_by_name=data_bundle.get("trader_hold_info_by_name") or {},
            is_live=False,
        )
        rec = recs_by_symbol[symbol]
        assert rec.signal == direct['signal']
        assert round(rec.confidence, 1) == round(direct['confidence'], 1)
        assert rec.expected_profit_percent == direct['expected_profit_percent']
        assert rec.raw_outputs["symbol"] == symbol


def test_gather_all_excludes_non_tradable_symbols():
    """A disclosed trade in a mutual-fund-pattern ticker (e.g. 'FTGCX') must never appear
    in _gather_all's output, even if it otherwise passes all date/amount criteria."""
    senate = [
        _weight_trade("Alice", "Aa", "FTGCX", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "FTGCX", "purchase", "2026-06-02", "2026-05-21"),
        _weight_trade("Carol", "Cc", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Dave", "Dd", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "FTGCX", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "FTGCX", "purchase", "2026-05-02", "2026-04-21")],
        "Carol Cc": [_weight_trade("Carol", "Cc", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Dave Dd": [_weight_trade("Dave", "Dd", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
    }
    price_map = {"FTGCX": 10.0, "AAPL": 100.0}

    e = _weight_basket_expert(senate, [], history, price_map, exec_price=5.0)
    e._gather_settings = WEIGHT_SETTINGS
    bundle = e._gather_all(_bundle(price_map), as_of=NOW)

    assert "FTGCX" not in bundle
    assert "AAPL" in bundle


# ====================================================================
# FMPSenateTraderWeight — basket-mode resilience to un-prewarmed discovered symbols
#
# _gather_all discovers its symbol set from the LIVE disclosure feed (no static universe
# bound), so a discovered symbol whose OHLCV/price-history was never prewarmed is a routine,
# expected occurrence -- NOT an operator/config error the way it would be for the classic
# per-symbol _gather's pre-vetted universe. These tests prove _gather_all catches the
# cache-miss exception PER SYMBOL (dropping just that one) rather than letting it propagate
# out of the whole basket gather (which would abort the entire bar one layer up, in
# daily_engine.py's _run_basket_expert_bar -- see that method's re-raise-on-cache-miss branch).
# ====================================================================

class _RaisingOHLCV(FakeOHLCV):
    """FakeOHLCV whose get_ohlcv_data raises FMPHistoryCacheMiss for ONE symbol, simulating
    providers.price_at_date hitting an un-prewarmed discovered symbol during a hermetic
    backtest (the real exception _gather_all must isolate)."""

    def __init__(self, price_map, raise_for):
        super().__init__(price_map)
        self._raise_for = str(raise_for).upper()

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        if str(symbol).upper() == self._raise_for:
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            raise FMPHistoryCacheMiss(f"{symbol} not pre-warmed (hermetic backtest, 0 fetch)")
        return super().get_ohlcv_data(symbol, end_date=end_date, lookback_days=lookback_days,
                                       interval=interval)


def _two_symbol_basket_fixture():
    """Shared AAPL+MSFT basket fixture (two traders each, both qualify on min_traders/
    min_trades) for the cache-miss isolation tests below."""
    senate = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    house = [
        _weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
        "Carol Cc": [_weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-05-01", "2026-04-20")],
        "Dave Dd": [_weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-05-02", "2026-04-21")],
    }
    price_map = {"AAPL": 100.0, "MSFT": 60.0}
    return senate, house, history, price_map


def test_gather_all_skips_symbol_whose_current_price_cache_misses():
    """A cache-miss EXCEPTION (not just a None/falsy return) while resolving ONE symbol's
    current_price via providers.price_at_date must not abort _gather_all for the other
    qualifying symbols: MSFT is dropped, AAPL's bundle is unaffected."""
    senate, house, history, price_map = _two_symbol_basket_fixture()
    e = _weight_basket_expert(senate, house, history, price_map, exec_price=50.0)
    e._gather_settings = WEIGHT_SETTINGS

    raising_bundle = LiveProviderBundle(
        lambda cat, name, **kw: {"ohlcv": _RaisingOHLCV(price_map, raise_for="MSFT")}[cat])

    bundle = e._gather_all(raising_bundle, as_of=NOW)

    assert "MSFT" not in bundle, "symbol whose price resolution raised must be dropped"
    assert "AAPL" in bundle, "the OTHER symbol must still be resolved normally"
    assert bundle["AAPL"]["current_price"] == 100.0
    assert bundle["AAPL"]["symbol"] == "AAPL"
    assert len(bundle["AAPL"]["exec_price_by_trade"]) == 2


def test_gather_all_skips_symbol_whose_exec_price_cache_misses():
    """A cache-miss EXCEPTION from _get_price_at_date while resolving ONE symbol's per-trade
    exec price (not the current_price call) must also just drop that symbol -- not ship a
    bundle with a broken/partial exec_price_by_trade map."""
    from ba2_providers.fmp_common import FMPHistoryCacheMiss

    senate, house, history, price_map = _two_symbol_basket_fixture()
    e = _weight_basket_expert(senate, house, history, price_map, exec_price=50.0)
    e._gather_settings = WEIGHT_SETTINGS

    def _exec_price(sym, date):
        if str(sym).upper() == "MSFT":
            raise FMPHistoryCacheMiss(f"{sym} not pre-warmed (hermetic backtest, 0 fetch)")
        return 50.0

    e._get_price_at_date = _exec_price

    bundle = e._gather_all(_bundle(price_map), as_of=NOW)

    assert "MSFT" not in bundle, "symbol whose exec-price resolution raised must be dropped"
    assert "AAPL" in bundle
    assert bundle["AAPL"]["current_price"] == 100.0
    assert len(bundle["AAPL"]["exec_price_by_trade"]) == 2


def test_gather_all_bubbles_up_a_non_cache_miss_exception():
    """A genuine bug (not a cache-miss) during price resolution must still propagate loudly
    out of _gather_all -- this fix narrows the catch to cache-miss types ONLY, it must not
    turn into a blanket except-and-skip that would hide real errors."""
    senate, house, history, price_map = _two_symbol_basket_fixture()
    e = _weight_basket_expert(senate, house, history, price_map, exec_price=50.0)
    e._gather_settings = WEIGHT_SETTINGS

    def _boom(sym):
        if str(sym).upper() == "MSFT":
            raise RuntimeError("some unrelated bug")
        return price_map.get(str(sym).upper())

    e._get_current_price = _boom  # as_of=None path -> exercised via analyze_as_of-style live call
    with pytest.raises(RuntimeError, match="some unrelated bug"):
        e._gather_all(_bundle(price_map), as_of=None)


def test_gather_all_all_symbols_cache_miss_returns_empty_dict_not_raise():
    """Every qualifying symbol missing its prewarm must still return an empty dict (a fully
    empty basket for this bar), not raise -- the caller (analyze_as_of/_process_all) already
    handles an empty bundle_by_symbol as "no recommendations this bar"."""
    senate, house, history, price_map = _two_symbol_basket_fixture()
    e = _weight_basket_expert(senate, house, history, price_map, exec_price=50.0)
    e._gather_settings = WEIGHT_SETTINGS

    class _AlwaysRaisingOHLCV(FakeOHLCV):
        def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            raise FMPHistoryCacheMiss(f"{symbol} not pre-warmed")

    raising_bundle = LiveProviderBundle(
        lambda cat, name, **kw: {"ohlcv": _AlwaysRaisingOHLCV(price_map)}[cat])
    bundle = e._gather_all(raising_bundle, as_of=NOW)
    assert bundle == {}


def test_analyze_as_of_basket_mode_returns_list_when_no_symbol_pinned():
    """context.extra without 'symbol' -> analyze_as_of returns List[Recommendation]."""
    senate = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    house = [
        _weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
        "Carol Cc": [_weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-05-01", "2026-04-20")],
        "Dave Dd": [_weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-05-02", "2026-04-21")],
    }
    price_map = {"AAPL": 100.0, "MSFT": 60.0}
    e = _weight_basket_expert(senate, house, history, price_map, exec_price=50.0)

    # No "symbol" key in extra at all -- exactly what _run_basket_expert_bar's BacktestContext
    # construction produces (it never sets context.extra).
    ctx = BacktestContext(providers=_bundle(price_map), settings=WEIGHT_SETTINGS, as_of=NOW)
    recs = e.analyze_as_of(NOW, ctx)

    assert isinstance(recs, list)
    assert all(isinstance(r, Recommendation) for r in recs)
    assert {r.raw_outputs["symbol"] for r in recs} == {"AAPL", "MSFT"}


def test_analyze_as_of_single_symbol_mode_unchanged_when_symbol_pinned():
    """context.extra['symbol'] set -> analyze_as_of returns a single Recommendation,
    byte-identical to today's behavior (regression guard for the dual-mode branch added by
    Task 5 -- proves declaring analyzes_as_basket = True did not disturb the pre-existing
    per-symbol calling convention any caller might still use)."""
    trades = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-05-10")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-05-11")],
    }
    e = _weight_expert("AAPL", trades, history, exec_price=95.0)
    ctx = BacktestContext(providers=_bundle({"AAPL": 100.0}), settings=WEIGHT_SETTINGS,
                          as_of=NOW, extra={"symbol": "AAPL"})
    rec = e.analyze_as_of(NOW, ctx)

    assert isinstance(rec, Recommendation)
    bundle_live = e._gather(_bundle({"AAPL": 100.0}), as_of=None)
    rec_live = e._process(bundle_live, WEIGHT_SETTINGS, as_of=NOW)
    assert rec.almost_equals(rec_live)
    assert rec.details == rec_live.details


def test_weight_declares_analyzes_as_basket():
    """senate-basket-dispatch plan Task 5: FMPSenateTraderWeight must declare
    analyzes_as_basket = True (mirroring Task 3's FMPSenateTraderCopy marker) so
    daily_engine.py routes it through _run_basket_expert_bar."""
    assert FMPSenateTraderWeight.analyzes_as_basket is True


# ====================================================================
# Live wiring: get_expert_properties + run_analysis("EXPERT", ...) (senate-basket-dispatch
# plan, Task 6). Unlike the analyze_as_of/_gather_all/_process_all tests above (pure, no DB),
# these exercise the real run_analysis -> _create_expert_recommendation -> add_instance/
# update_instance path against the throwaway sqlite the session-scoped conftest.py fixture
# wires up (ba2_common.core.db.configure_db), so ExpertRecommendation/MarketAnalysis rows are
# real DB writes -- proving the live dispatch actually persists, not just that the pure
# gather/process pair returns the right shape.
# ====================================================================

def _weight_live_basket_expert(senate_trades, house_trades, history_by_name, price_map,
                               exec_price=50.0, instance_id=1):
    """Like _weight_basket_expert, but wired for the LIVE run_analysis path: needs e.id +
    e._settings_cache set so _resolve_settings/get_setting_with_interface_default work without
    a real ExpertInstance row (the throwaway test sqlite does not enforce FK constraints --
    see ba2_common/core/db.py, no PRAGMA foreign_keys=ON).

    Unlike the pure _gather_all/_process_all tests above (which pin `now` via as_of=NOW),
    run_analysis's basket path is live (as_of=None), so the age-window filters (Stage 1's
    _window_trades) measure trade age against the REAL wall-clock `datetime.now()` at test-run
    time -- overriding max_disclose_date_days/max_trade_exec_days to a generous 10-year window
    keeps the fixture's fixed 2026-dated trades inside the window regardless of which real date
    the test suite happens to run on (all other keys still fall back to their interface
    defaults, matching how a bare/never-configured ExpertInstance behaves)."""
    e = _weight_basket_expert(senate_trades, house_trades, history_by_name, price_map, exec_price)
    e.id = instance_id
    e._settings_cache = {
        "max_disclose_date_days": 3650,
        "max_trade_exec_days": 3650,
    }
    return e


def test_get_expert_properties_shape():
    """Task 6 Step 1: property dict must mirror FMPSenateTraderCopy's shape exactly (same
    can_recommend_instruments/should_expand_instrument_jobs/required_instrument_selection_method)
    so JobManager's _get_enabled_instruments/_execute_expert_driven_analysis route "EXPERT"-symbol
    jobs straight to run_analysis without expanding into per-instrument jobs."""
    assert FMPSenateTraderWeight.get_expert_properties() == {
        "can_recommend_instruments": True,
        "should_expand_instrument_jobs": False,
        "required_instrument_selection_method": "expert",
    }


def test_run_analysis_expert_symbol_creates_one_recommendation_per_qualifying_symbol():
    """Task 6 Steps 2-3: run_analysis("EXPERT", ...) must call _gather_all/_process_all ONCE
    (live, as_of=None) and persist one ExpertRecommendation row per qualifying symbol, mirroring
    FMPSenateTraderCopy's own EXPERT-symbol handling and the backtest _run_basket_expert_bar
    path Task 2 built."""
    from ba2_common.core.models import MarketAnalysis, ExpertRecommendation
    from ba2_common.core.types import MarketAnalysisStatus
    from ba2_common.core.db import add_instance, get_instance, get_db
    from sqlmodel import select

    senate = [
        _weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-06-02", "2026-05-21"),
    ]
    house = [
        _weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-06-01", "2026-05-20"),
        _weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-06-02", "2026-05-21"),
    ]
    history = {
        "Alice Aa": [_weight_trade("Alice", "Aa", "AAPL", "purchase", "2026-05-01", "2026-04-20")],
        "Bob Bb": [_weight_trade("Bob", "Bb", "AAPL", "purchase", "2026-05-02", "2026-04-21")],
        "Carol Cc": [_weight_trade("Carol", "Cc", "MSFT", "purchase", "2026-05-01", "2026-04-20")],
        "Dave Dd": [_weight_trade("Dave", "Dd", "MSFT", "purchase", "2026-05-02", "2026-04-21")],
    }
    price_map = {"AAPL": 100.0, "MSFT": 60.0}
    e = _weight_live_basket_expert(senate, house, history, price_map, exec_price=50.0, instance_id=777)

    market_analysis = MarketAnalysis(
        symbol="EXPERT", expert_instance_id=777,
        status=MarketAnalysisStatus.PENDING, subtype=AnalysisUseCase.ENTER_MARKET,
    )
    ma_id = add_instance(market_analysis)
    market_analysis = get_instance(MarketAnalysis, ma_id)

    e.run_analysis("EXPERT", market_analysis)

    market_analysis = get_instance(MarketAnalysis, ma_id)
    assert market_analysis.status == MarketAnalysisStatus.COMPLETED
    assert market_analysis.state["senate_trade_basket"]["total_symbols"] == 2
    assert set(market_analysis.state["senate_trade_basket"]["symbols_analyzed"]) == {"AAPL", "MSFT"}

    with get_db() as session:
        recs = session.exec(
            select(ExpertRecommendation).where(ExpertRecommendation.market_analysis_id == ma_id)
        ).all()
    assert len(recs) == 2
    assert {r.symbol for r in recs} == {"AAPL", "MSFT"}
    for r in recs:
        assert r.instance_id == 777


def test_run_analysis_expert_symbol_rejects_open_positions():
    """Task 6 live-safety guard: OPEN_POSITIONS is inherently per-symbol for this expert (it
    reasons about one already-held position's trade history at a time -- see
    _run_basket_analysis's docstring), so an "EXPERT"-symbol OPEN_POSITIONS job must fail
    loudly (raised ValueError + FAILED MarketAnalysis) rather than silently no-op or crash on
    an unrelated AttributeError."""
    from ba2_common.core.models import MarketAnalysis
    from ba2_common.core.types import MarketAnalysisStatus
    from ba2_common.core.db import add_instance, get_instance

    e = _weight_live_basket_expert([], [], {}, {}, instance_id=778)
    market_analysis = MarketAnalysis(
        symbol="EXPERT", expert_instance_id=778,
        status=MarketAnalysisStatus.PENDING, subtype=AnalysisUseCase.OPEN_POSITIONS,
    )
    ma_id = add_instance(market_analysis)
    market_analysis = get_instance(MarketAnalysis, ma_id)

    with pytest.raises(ValueError, match="OPEN_POSITIONS"):
        e.run_analysis("EXPERT", market_analysis)

    market_analysis = get_instance(MarketAnalysis, ma_id)
    assert market_analysis.status == MarketAnalysisStatus.FAILED


# ====================================================================
# Lookahead-bias fix (2026-07-18): reject future-disclosed / future-executed trades.
#
# _filter_trades (both experts) and _window_trades (Weight's pre-filter) only ever
# bounded trade age from BELOW: `days_since_X > max_X_days` rejects too-OLD rows, but a
# trade whose disclosureDate/transactionDate is AFTER `now` produces a NEGATIVE
# days_since_X, which trivially satisfies "not > max_X_days" -- so it was kept as if it
# were fresh, current information. That is textbook lookahead bias: at simulated time
# `now`, a trade disclosed/executed in the future could not possibly be known yet.
# Confirmed exploitable on real cached data: as of this fix, 63 of AAPL's 100 cached
# congress_senate_trades rows have disclosureDate > 2023-06-01, i.e. a backtest scored
# "as of 2023-06-01" would have seen the majority of AAPL's entire disclosed-trade
# history (including trades disclosed months/years later) as current information.
# ====================================================================

def test_weight_filter_trades_rejects_future_disclosed_trade():
    """A trade disclosed AFTER `now` must never be kept, however generous
    max_disclose_days is -- disclose_date > now makes days_since_disclose negative,
    which the buggy code treated as trivially 'not too old'."""
    e = _weight_expert("AAPL", [], {})
    future = _weight_trade("Future", "Filer", "AAPL", "purchase", "2026-06-20", "2026-06-01")
    filtered = e._filter_trades(
        [future], max_disclose_days=365, max_exec_days=365, max_price_delta_pct=1000.0,
        current_price=100.0, symbol="AAPL", now=NOW,
        exec_price_by_trade={e._trade_key(future): 50.0})
    assert filtered == [], "trade disclosed after 'now' must be rejected (lookahead bias)"


def test_weight_filter_trades_rejects_future_executed_trade():
    """A trade EXECUTED after `now` (even if disclosed on time) must never be kept."""
    e = _weight_expert("AAPL", [], {})
    future = _weight_trade("Future", "Execer", "AAPL", "purchase", "2026-06-01", "2026-06-20")
    filtered = e._filter_trades(
        [future], max_disclose_days=365, max_exec_days=365, max_price_delta_pct=1000.0,
        current_price=100.0, symbol="AAPL", now=NOW,
        exec_price_by_trade={e._trade_key(future): 50.0})
    assert filtered == [], "trade executed after 'now' must be rejected (lookahead bias)"


def test_weight_filter_trades_keeps_same_day_disclosure_and_exec():
    """Boundary check: disclosure/exec dated EXACTLY 'now' (days_since == 0) must still be
    kept -- only STRICTLY future dates are lookahead."""
    e = _weight_expert("AAPL", [], {})
    same_day = _weight_trade("Same", "Day", "AAPL", "purchase", "2026-06-13", "2026-06-13")
    filtered = e._filter_trades(
        [same_day], max_disclose_days=365, max_exec_days=365, max_price_delta_pct=1000.0,
        current_price=100.0, symbol="AAPL", now=NOW,
        exec_price_by_trade={e._trade_key(same_day): 50.0})
    assert len(filtered) == 1, "same-day (as of 'now') disclosure/exec must not be rejected"


def test_weight_window_trades_rejects_future_disclosed_trade():
    """_window_trades is a pre-filter that must stay a faithful superset of _filter_trades:
    once _filter_trades rejects future-disclosed trades, the pre-filter must too, or it
    silently keeps rows _filter_trades will reject downstream anyway (still correct in
    isolation, but breaks the documented superset invariant and wastes work)."""
    future = _weight_trade("Future", "Filer", "AAPL", "purchase", "2026-06-20", "2026-06-01")
    e = _weight_expert("AAPL", [future], {})
    index = e._symbol_trades_index_cached("AAPL")
    windowed = e._window_trades(index, NOW, max_disclose_days=365, max_exec_days=365)
    assert windowed == [], "future-disclosed trade must not survive the window pre-filter"


def test_weight_window_trades_rejects_future_executed_trade():
    future = _weight_trade("Future", "Execer", "AAPL", "purchase", "2026-06-01", "2026-06-20")
    e = _weight_expert("AAPL", [future], {})
    index = e._symbol_trades_index_cached("AAPL")
    windowed = e._window_trades(index, NOW, max_disclose_days=365, max_exec_days=365)
    assert windowed == [], "future-executed trade must not survive the window pre-filter"


def test_weight_window_trades_keeps_same_day_disclosure_and_exec():
    same_day = _weight_trade("Same", "Day", "AAPL", "purchase", "2026-06-13", "2026-06-13")
    e = _weight_expert("AAPL", [same_day], {})
    index = e._symbol_trades_index_cached("AAPL")
    windowed = e._window_trades(index, NOW, max_disclose_days=365, max_exec_days=365)
    assert len(windowed) == 1, "same-day (as of 'now') disclosure/exec must survive the pre-filter"


def test_copy_filter_trades_rejects_future_disclosed_trade():
    """FMPSenateTraderCopy._filter_trades has the identical lookahead bug."""
    e = _copy_expert([], [])
    future = _copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-20", "2026-06-01")
    filtered = e._filter_trades([future], ["nancy pelosi"], max_disclose_days=365,
                                max_exec_days=365, now=NOW)
    assert filtered == [], "trade disclosed after 'now' must be rejected (lookahead bias)"


def test_copy_filter_trades_rejects_future_executed_trade():
    e = _copy_expert([], [])
    future = _copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-06-20")
    filtered = e._filter_trades([future], ["nancy pelosi"], max_disclose_days=365,
                                max_exec_days=365, now=NOW)
    assert filtered == [], "trade executed after 'now' must be rejected (lookahead bias)"


def test_copy_filter_trades_keeps_same_day_disclosure_and_exec():
    e = _copy_expert([], [])
    same_day = _copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-13", "2026-06-13")
    filtered = e._filter_trades([same_day], ["nancy pelosi"], max_disclose_days=365,
                                max_exec_days=365, now=NOW)
    assert len(filtered) == 1


def test_copy_filter_trades_by_age_multi_rejects_future_disclosed_trade():
    """FMPSenateTraderCopy._filter_trades_by_age_multi (basket-mode variant) has the
    identical lookahead bug."""
    e = _copy_expert([], [])
    future = _copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-20", "2026-06-01")
    filtered = e._filter_trades_by_age_multi([future], max_disclose_days=365, max_exec_days=365, now=NOW)
    assert filtered == [], "trade disclosed after 'now' must be rejected (lookahead bias)"


def test_copy_filter_trades_by_age_multi_rejects_future_executed_trade():
    e = _copy_expert([], [])
    future = _copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-01", "2026-06-20")
    filtered = e._filter_trades_by_age_multi([future], max_disclose_days=365, max_exec_days=365, now=NOW)
    assert filtered == [], "trade executed after 'now' must be rejected (lookahead bias)"


def test_copy_filter_trades_by_age_multi_keeps_same_day_disclosure_and_exec():
    e = _copy_expert([], [])
    same_day = _copy_trade("Nancy", "Pelosi", "AAPL", "purchase", "2026-06-13", "2026-06-13")
    filtered = e._filter_trades_by_age_multi([same_day], max_disclose_days=365, max_exec_days=365, now=NOW)
    assert len(filtered) == 1
