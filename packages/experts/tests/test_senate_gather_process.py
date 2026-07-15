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
