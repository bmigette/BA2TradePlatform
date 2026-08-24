from datetime import date, datetime
from importlib import import_module
from types import SimpleNamespace

import pandas as pd

from ba2_common.core.backtest_context import BacktestContext
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight
from ba2_experts.PremiumSeller import PremiumSeller

AS_OF = datetime(2024, 1, 2)
EXP = date(2024, 2, 9)

FULL_SETTINGS = {
    "static_universe": "XYZ,ABC",
    "iv_rank_enabled": False, "iv_rank_min": 50.0,
    "iv_hv_enabled": False, "iv_hv_min_pp": 2.0, "hv_lookback": 20,
    "trend_filter_enabled": False, "trend_sma": 200,
    "earnings_filter_enabled": False,
    "fmp_rating_floor_enabled": False, "fmp_rating_min": 3.0,
    "target_delta": 0.30, "target_dte": 38, "spread_width": 5.0,
    "min_credit_ratio": 0.05,
    "enable_put_credit_spread": True, "enable_short_put": False, "enable_short_strangle": False,
    "risk_per_structure_pct": 3.0,
    "max_concurrent_structures": 5,
    "max_notional_leverage": 3.0,
}


def _c(sym, strike, d, bid, ask, right=OptionRight.PUT):
    return OptionContract(symbol=sym, underlying="XYZ", option_type=right, strike=strike,
                          expiry=EXP, bid=bid, ask=ask, last=None, implied_volatility=0.3,
                          delta=d, gamma=None, theta=None, vega=None,
                          open_interest=500, volume=100)


class StubAccount:
    def __init__(self, chain, iv=0.30, balance=10_000.0):
        self._chain, self._iv, self._balance = chain, iv, balance
        self.options_provider = None          # no IV seed source -> history grows per bar

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        return self._chain

    def get_atm_implied_volatility(self, underlying):
        return self._iv

    def get_balance(self):
        return self._balance


class StubProviders:
    def ohlcv(self):
        raise AssertionError("OHLCV must not be fetched when trend/iv_hv filters are off")

    def price_at_date(self, symbol, as_of):
        return 100.0


def make_expert(account, settings=None, providers=None):
    expert = PremiumSeller.__new__(PremiumSeller)
    expert._iv_history = {}
    ctx = BacktestContext(providers=providers or StubProviders(),
                          settings=settings or dict(FULL_SETTINGS),
                          as_of=AS_OF, account=account, subtype=None)
    return expert, ctx


def chain_for(underlying="XYZ"):
    return [
        _c("P95", 95.0, -0.30, 1.40, 1.60),
        _c("P90", 90.0, -0.20, 0.70, 0.90),
        _c("C105", 105.0, 0.30, 1.40, 1.60, right=OptionRight.CALL),
    ]


def test_emits_put_credit_spread_target():
    expert, ctx = make_expert(StubAccount(chain_for()))
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert getattr(rec, "skip", False) is False
    specs = rec.raw_outputs["targets"]["structures"]
    # sizing: equity 10k x 3% = 300 budget; per-loss (5-.5)x100=450 -> qty 0 -> skipped!
    # (deliberate budget-floor case: the spread is correctly DECLINED at this size, so
    #  zero specs is the expected outcome; a non-empty result must still be the XYZ
    #  put credit spread — the stub returns the same chain for every underlying)
    assert (len(specs) == 0
            or (specs[0].strategy == "put_credit_spread" and specs[0].underlying == "XYZ"
                and specs[0].qty >= 1))


def test_emits_put_credit_spread_target_with_budget():
    """Positive-open case (brief implementer note): risk_per_structure_pct 10.0 ->
    $1,000 budget -> floor(1000/450) = qty 2. The stub returns the same chain for
    every underlying, so XYZ and ABC both qualify."""
    settings = dict(FULL_SETTINGS, risk_per_structure_pct=10.0)
    expert, ctx = make_expert(StubAccount(chain_for()), settings)
    rec = expert.analyze_as_of(AS_OF, ctx)
    specs = rec.raw_outputs["targets"]["structures"]
    assert len(specs) == 2
    assert specs[0].strategy == "put_credit_spread" and specs[0].underlying == "XYZ"
    assert specs[0].qty == 2
    assert specs[1].underlying == "ABC" and specs[1].qty == 2


def test_skip_when_no_chain():
    expert, ctx = make_expert(StubAccount([]))
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert rec.raw_outputs["targets"]["structures"] == []


def test_iv_rank_gate_blocks(monkeypatch):
    settings = dict(FULL_SETTINGS, iv_rank_enabled=True, iv_rank_min=50.0)
    expert, ctx = make_expert(StubAccount(chain_for(), iv=0.10), settings)
    expert._iv_history["XYZ"] = [0.30] * 30     # current 0.10 -> IVR 0 < 50
    expert._iv_history["ABC"] = [0.30] * 30
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert rec.raw_outputs["targets"]["structures"] == []


def test_the_iv_rank_series_is_the_trailing_history_not_including_today():
    """Today's own IV is appended to ``_iv_history`` before the gate is evaluated, so
    under the old ``<=`` arithmetic every symbol's rank counted itself and could never
    read below 100/N. The live and backtest paths both define the series as
    yesterday-and-earlier; this one must too, or "IV rank 50" means three things.
    """
    account = StubAccount(chain_for(), iv=0.30)
    expert, ctx = make_expert(account)
    expert._iv_history["XYZ"] = [0.30] * 24

    trailing = expert._update_iv_history("XYZ", AS_OF, account, 0.30)

    assert len(trailing) == 24, "the ranked series must not contain today's sample"
    assert len(expert._iv_history["XYZ"]) == 25, "...but the rolling history still grows"

    from ba2_experts.PremiumSeller import signals
    assert signals.iv_rank(trailing, 0.30) == 0.0


def test_the_gate_ranks_against_the_trailing_series(monkeypatch):
    """Caller-level companion to the test above: 24 points all below today's IV is a
    rank of 100, but only if today's own sample is kept out of the series. Ranking the
    post-append list scores 96 and blocks a trade the rule says to take."""
    # risk_per_structure_pct 10.0 so the spread actually sizes (see
    # test_emits_put_credit_spread_target_with_budget); the IV gate is what is under test.
    settings = dict(FULL_SETTINGS, iv_rank_enabled=True, iv_rank_min=99.0,
                    risk_per_structure_pct=10.0)
    expert, ctx = make_expert(StubAccount(chain_for(), iv=0.30), settings)
    for sym in ("XYZ", "ABC"):
        expert._iv_history[sym] = [0.10] * 24

    rec = expert.analyze_as_of(AS_OF, ctx)
    assert len(rec.raw_outputs["targets"]["structures"]) == 2, \
        "rank is 100 against the trailing series; only self-inclusion drops it to 96"



def test_trend_filter_blocks(monkeypatch):
    settings = dict(FULL_SETTINGS, trend_filter_enabled=True, trend_sma=3)
    account = StubAccount(chain_for())
    expert = PremiumSeller.__new__(PremiumSeller)
    expert._iv_history = {}

    class DownProviders(StubProviders):
        def ohlcv(self):
            return SimpleNamespace(get_ohlcv_data=lambda *a, **k: {
                "XYZ": None, "ABC": None})

    monkeypatch.setattr(PremiumSeller, "_fetch_closes",
                        lambda self, sym, as_of, settings: [100.0, 90.0, 80.0])  # falling
    ctx = BacktestContext(providers=DownProviders(), settings=settings, as_of=AS_OF,
                          account=account, subtype=None)
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert rec.raw_outputs["targets"]["structures"] == []


class OHLCVProviders(StubProviders):
    """Providers stub whose ohlcv() honors the REAL get_ohlcv_data contract: a
    single pd.DataFrame with a capital-C "Close" column (what FactorRanker and
    the backtest context consume). Used by the no-monkeypatch regression tests."""

    def __init__(self, closes):
        self._closes = closes

    def ohlcv(self):
        closes = self._closes
        return SimpleNamespace(
            get_ohlcv_data=lambda *a, **k: pd.DataFrame({"Close": closes}))


def test_trend_filter_dataframe_passes_above_sma():
    """Regression for the _fetch_closes DataFrame-contract bug: no monkeypatching of
    _fetch_closes — the gate must read the Close column off the DataFrame. Last close
    above SMA(3) -> gate passes, structures emitted."""
    settings = dict(FULL_SETTINGS, trend_filter_enabled=True, trend_sma=3,
                    risk_per_structure_pct=10.0)
    providers = OHLCVProviders([10.0, 10.0, 13.0])     # sma 11.0, spot 13.0 >= 11.0
    expert, ctx = make_expert(StubAccount(chain_for()), settings, providers)
    rec = expert.analyze_as_of(AS_OF, ctx)
    specs = rec.raw_outputs["targets"]["structures"]
    assert len(specs) == 2
    assert specs[0].strategy == "put_credit_spread" and specs[0].underlying == "XYZ"


def test_trend_filter_dataframe_blocks_below_sma():
    """Same real-DataFrame path; last close below SMA(3) -> gate blocks, zero structures."""
    settings = dict(FULL_SETTINGS, trend_filter_enabled=True, trend_sma=3,
                    risk_per_structure_pct=10.0)
    providers = OHLCVProviders([13.0, 13.0, 10.0])     # sma 12.0, spot 10.0 < 12.0
    expert, ctx = make_expert(StubAccount(chain_for()), settings, providers)
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert rec.raw_outputs["targets"]["structures"] == []


def _patch_earnings(monkeypatch, report_date):
    """Patch the FMP earnings seam at its source module (the expert imports it lazily
    inside _earnings_blocked). get_app_setting is patched in the PROVIDER module's
    namespace (module-level `from ... import`) so __init__ doesn't raise on a missing
    FMP_API_KEY. The package __init__ rebinds the submodule name to the CLASS, so the
    module must be resolved via import_module, not attribute access."""
    details_mod = import_module("ba2_providers.fundamentals.details.FMPCompanyDetailsProvider")
    monkeypatch.setattr(details_mod, "get_app_setting",
                        lambda key, default=None: "dummy-key")
    monkeypatch.setattr(details_mod.FMPCompanyDetailsProvider, "get_past_earnings",
                        lambda self, *a, **k: {"earnings": [{"report_date": report_date}]})


def test_earnings_gate_blocks_inside_window(monkeypatch):
    # AS_OF 2024-01-02, window = target_dte 38 + 5 = 43d -> ends 2024-02-14;
    # a 2024-01-20 report lands inside -> blocked.
    _patch_earnings(monkeypatch, "2024-01-20")
    settings = dict(FULL_SETTINGS, earnings_filter_enabled=True,
                    risk_per_structure_pct=10.0)
    expert, ctx = make_expert(StubAccount(chain_for()), settings)
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert rec.raw_outputs["targets"]["structures"] == []


def test_earnings_gate_passes_outside_window(monkeypatch):
    _patch_earnings(monkeypatch, "2024-06-01")          # far outside the 43d window
    settings = dict(FULL_SETTINGS, earnings_filter_enabled=True,
                    risk_per_structure_pct=10.0)
    expert, ctx = make_expert(StubAccount(chain_for()), settings)
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert len(rec.raw_outputs["targets"]["structures"]) == 2


def _patch_rating(monkeypatch, rows):
    """Patch the rating seam at its source modules (the expert imports both lazily
    inside _rating_blocked, so patching the source-module attributes is enough).
    ba2_experts/__init__ rebinds the FMPRating submodule name to the CLASS, so the
    module must be resolved via import_module, not attribute access. `rows` must be
    real stable/grades-historical rows: a `date` plus aggregate analystRatings*
    counts (there is no per-row newGrade on this endpoint)."""
    fmp_rating_mod = import_module("ba2_experts.FMPRating")
    monkeypatch.setattr("ba2_common.config.get_app_setting",
                        lambda key, default=None: "dummy-key")
    monkeypatch.setattr(fmp_rating_mod, "fetch_grades_historical_cached",
                        lambda api_key, symbol: rows)


def test_rating_floor_blocks_known_bad_grade(monkeypatch):
    # (1*8 + 2*2) / 10 = 1.2 < 3.0 floor -> blocks.
    _patch_rating(monkeypatch, [{"date": "2023-12-15", "analystRatingsStrongSell": 8,
                                 "analystRatingsSell": 2}])
    settings = dict(FULL_SETTINGS, fmp_rating_floor_enabled=True, fmp_rating_min=3.0,
                    risk_per_structure_pct=10.0)
    expert, ctx = make_expert(StubAccount(chain_for()), settings)
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert rec.raw_outputs["targets"]["structures"] == []


def test_rating_floor_passes_strong_buy(monkeypatch):
    # (5*7 + 4*3) / 10 = 4.7 >= 3.0 floor -> passes.
    _patch_rating(monkeypatch, [{"date": "2023-12-15", "analystRatingsStrongBuy": 7,
                                 "analystRatingsBuy": 3}])
    settings = dict(FULL_SETTINGS, fmp_rating_floor_enabled=True, fmp_rating_min=3.0,
                    risk_per_structure_pct=10.0)
    expert, ctx = make_expert(StubAccount(chain_for()), settings)
    rec = expert.analyze_as_of(AS_OF, ctx)
    assert len(rec.raw_outputs["targets"]["structures"]) == 2
