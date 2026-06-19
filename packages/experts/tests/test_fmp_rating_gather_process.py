"""FMPRating _gather/_process split (SKIP first-class) + no-lookahead backtest
reconstruction.

Proves: _process is pure (no provider/API reads), SKIP is first-class (no consensus
=> skip_reason="no consensus data"; below min_analysts => skip_reason="insufficient
analysts"), target_price_type flows from settings into _calculate_recommendation
(not from self), the as_of=None LIVE path equals analyze_as_of(now) on the golden
tuple, and the AS_OF design docstring is present (regression guard so the live
snapshot/lookahead rationale is never silently dropped).

FMPRating is now a REAL backtestable expert: the two FMP consensus endpoints carry
no per-row date, so the live (as_of=None) path keeps using those CURRENT snapshots,
while the backtest (as_of) path reconstructs BOTH inputs as-of the date with no
lookahead from FMP's dated grades-historical + v4/price-target history. The golden
case exercises the as_of reconstruction path (the dated history reconstructs the
live snapshot exactly, so live == analyze_as_of(NOW)). See
test_reconstruction_no_lookahead.py for the dedicated no-lookahead proof.
"""
import inspect
from datetime import datetime, timezone

import pandas as pd

from ba2_experts.FMPRating import FMPRating
from ba2_common.core.types import OrderRecommendation
from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)

# Resolved settings dict exactly as run_analysis builds it via _resolve_settings.
SETTINGS = {"profit_ratio": 1.0, "min_analysts": 10, "target_price_type": "consensus",
            "price_target_window_days": 90}

# Consensus with upside above current (FakeOHLCV close=100) -> BUY territory.
CONSENSUS = {"targetConsensus": 130.0, "targetHigh": 160.0,
             "targetLow": 110.0, "targetMedian": 128.0}

# Upgrade/downgrade consensus row: buy-dominated, 20 analysts total (>= min 10).
#   buy_score = 10*2 + 5 = 25 ; sell_score = 1*2 + 1 = 3 ; hold_score = 3
#   -> BUY signal.
UPGRADE = [{"strongBuy": 10, "buy": 5, "hold": 3, "sell": 1, "strongSell": 1}]

# Dated FMP history (backtest as_of path) that reconstructs EXACTLY the live
# snapshot above when filtered no-lookahead (date/publishedDate <= NOW) within the
# 90-day window: targets sorted [110,124,128,128,160] -> mean 130, max 160, min 110,
# median 128 == CONSENSUS; latest grades row <= NOW -> UPGRADE counts.
PRICE_TARGET_HISTORY = [
    {"publishedDate": "2026-06-10", "priceTarget": 110.0},
    {"publishedDate": "2026-06-09", "priceTarget": 124.0},
    {"publishedDate": "2026-06-08", "priceTarget": 128.0},
    {"publishedDate": "2026-06-07", "priceTarget": 128.0},
    {"publishedDate": "2026-06-06", "priceTarget": 160.0},
]
GRADES_HISTORY = [
    {"date": "2026-06-10", "analystRatingsStrongBuy": 10, "analystRatingsbuy": 5,
     "analystRatingsHold": 3, "analystRatingsSell": 1, "analystRatingsStrongSell": 1},
]


class FakeOHLCV:
    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        return pd.DataFrame({"Close": [100.0]})


def _provider_resolver():
    ohlcv = FakeOHLCV()
    return lambda cat, name, **kw: ohlcv


def _expert(consensus=CONSENSUS, upgrade=UPGRADE,
            price_target_history=PRICE_TARGET_HISTORY, grades_history=GRADES_HISTORY):
    """Build an FMPRating via __new__ (bypass __init__ DB read / API-key fetch) and
    stub BOTH the live snapshot fetchers (as_of=None path) and the dated history
    fetchers (as_of reconstruction path) so _gather has no network."""
    e = FMPRating.__new__(FMPRating)
    e.id = 1
    e._gather_symbol = "AAPL"
    e._gather_window_days = 90
    # Live (as_of=None) current_price now reads the account quote via
    # _get_current_price; pin it to the FakeOHLCV close so live==as_of holds.
    e._get_current_price = lambda sym: 100.0
    e._fetch_price_target_consensus = lambda symbol: consensus
    e._fetch_upgrade_downgrade = lambda symbol: upgrade
    e._fetch_price_target_history = lambda symbol: price_target_history
    e._fetch_grades_historical = lambda symbol: grades_history
    return e


def test_process_buy_from_consensus():
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec = e._process(bundle, SETTINGS, as_of=None)
    assert rec.signal == OrderRecommendation.BUY
    assert rec.skip is False
    assert rec.current_price == 100.0
    assert rec.expected_profit_percent > 0.0       # upside to 130 consensus
    assert rec.raw_outputs["type"] == "analyst_rating_analysis"
    assert "FMP Analyst Price Target Consensus Analysis" in rec.details


def test_process_surfaces_target_price_on_recommendation():
    """The Recommendation value object carries the computed analyst target price
    (Prereq 2 / S1 fidelity) so the backtest can reference it for the initial TP
    bracket. With target_price_type='consensus' (130) the BUY rec.target_price==130.

    Changing target_price_type to 'high' (160) must change rec.target_price too,
    proving it tracks the SAME computed target the math uses (not a constant)."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec = e._process(bundle, SETTINGS, as_of=None)
    assert rec.target_price == 130.0
    assert rec.raw_outputs["calc"]["target_price"] == rec.target_price

    rec_high = e._process(bundle, dict(SETTINGS, target_price_type="high"), as_of=None)
    assert rec_high.target_price == 160.0


def test_skip_recommendation_has_no_target_price():
    """SKIP recs (no coverage / insufficient analysts) leave target_price None so the
    backtest falls back to expected_profit_percent for non-target outcomes."""
    e = _expert(consensus=None)
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec = e._process(bundle, SETTINGS, as_of=None)
    assert rec.skip is True
    assert rec.target_price is None


def test_process_skip_no_consensus():
    """No analyst coverage => SKIP first-class (skip_reason='no consensus data')."""
    e = _expert(consensus=None)
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    # _gather must NOT fetch upgrades when consensus is None (mirrors live ordering).
    assert bundle["consensus_data"] is None
    assert bundle["upgrade_data"] is None
    rec = e._process(bundle, SETTINGS, as_of=None)
    assert rec.skip is True
    assert rec.skip_reason == "no consensus data"
    assert rec.signal == OrderRecommendation.HOLD
    assert rec.details == "No analyst coverage"


def test_process_skip_insufficient_analysts():
    """Below min_analysts => SKIP first-class (skip_reason='insufficient analysts')."""
    thin = [{"strongBuy": 2, "buy": 1, "hold": 1, "sell": 0, "strongSell": 0}]  # 4 < 10
    e = _expert(upgrade=thin)
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec = e._process(bundle, SETTINGS, as_of=None)
    assert rec.skip is True
    assert rec.skip_reason == "insufficient analysts"
    assert rec.signal == OrderRecommendation.HOLD
    assert "Insufficient analysts (4 < 10)" in rec.details


def test_target_price_type_flows_from_settings():
    """target_price_type must come from settings, NOT self. Spy on
    _calculate_recommendation to confirm the value is threaded through."""
    e = _expert()
    captured = {}
    orig = e._calculate_recommendation

    def spy(consensus_data, upgrade_data, current_price, profit_ratio, min_analysts,
            target_price_type="consensus"):
        captured["target_price_type"] = target_price_type
        return orig(consensus_data, upgrade_data, current_price, profit_ratio,
                    min_analysts, target_price_type)

    e._calculate_recommendation = spy
    settings = dict(SETTINGS, target_price_type="high")
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    e._process(bundle, settings, as_of=None)
    assert captured["target_price_type"] == "high"


def test_calculate_recommendation_does_not_read_self_for_target():
    """The 'high' target must change the math relative to 'consensus' WITHOUT any
    self.settings read (proves the param is honored). With current=100, high=160
    yields a larger expected profit than consensus=130."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec_consensus = e._process(bundle, dict(SETTINGS, target_price_type="consensus"), as_of=None)
    rec_high = e._process(bundle, dict(SETTINGS, target_price_type="high"), as_of=None)
    assert rec_high.expected_profit_percent > rec_consensus.expected_profit_percent


def test_process_byte_equal_details_vs_direct_calculate():
    """_process details/signal/profit must match a direct _calculate_recommendation
    call on the same inputs (no detail-string drift in the refactor)."""
    e = _expert()
    bundle = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec = e._process(bundle, SETTINGS, as_of=None)
    direct = e._calculate_recommendation(
        CONSENSUS, UPGRADE, 100.0, 1.0, 10, "consensus")
    assert rec.details == direct["details"]
    assert rec.signal == direct["signal"]
    assert round(direct["confidence"], 1) == rec.confidence
    assert rec.expected_profit_percent == direct["expected_profit_percent"]


def test_analyze_as_of_equals_live():
    """Golden contract: analyze_as_of(NOW) == _process(_gather(live, None)) on
    (signal, confidence, expected_profit, details, skip, skip_reason), with
    current_price pinned identically.

    The as_of path RECONSTRUCTS the consensus inputs from the dated grades-historical
    + price-target history (no-lookahead); the fixtures are built so that
    reconstruction reproduces the live snapshot EXACTLY, so live == as_of proves the
    reconstruction reproduces the live consensus math (not merely as_of plumbing)."""
    e = _expert()
    ctx = BacktestContext(
        providers=LiveProviderBundle(_provider_resolver()),
        settings=SETTINGS, as_of=NOW, extra={"symbol": "AAPL"})
    rec_asof = e.analyze_as_of(NOW, ctx)

    bundle_live = e._gather(LiveProviderBundle(_provider_resolver()), as_of=None)
    rec_live = e._process(bundle_live, SETTINGS, as_of=None)

    rec_asof.current_price = rec_live.current_price  # pin price source per golden harness
    assert rec_live.almost_equals(rec_asof)
    assert rec_asof.signal == OrderRecommendation.BUY


def test_as_of_design_docstring_present():
    """Regression guard: the AS_OF design rationale must NOT be silently dropped.
    The FMPRating source must keep describing the live snapshot/lookahead
    distinction (live uses current snapshots; backtest reconstructs no-lookahead)."""
    src = inspect.getsource(FMPRating)
    assert "AS_OF" in src
    # The live snapshot/lookahead distinction must remain documented.
    assert "snapshot" in src.lower()
    assert "lookahead" in src.lower()
    # And the reconstruction sources must be named so the design is discoverable.
    assert "grades-historical" in src
    assert "price-target" in src
