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
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FMPSenateTraderCopy import FMPSenateTraderCopy
from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight
from ba2_common.core.types import OrderRecommendation, Recommendation, AnalysisUseCase
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)
_LOG = logging.getLogger("test_senate")


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
