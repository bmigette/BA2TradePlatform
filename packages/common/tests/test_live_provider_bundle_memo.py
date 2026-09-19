"""``LiveProviderBundle`` resolves each registry provider once PER BUNDLE.

WHY THIS EXISTS. The accessors are called inside every expert's ``_gather``, i.e.
once per (symbol, decision), and ``get_provider`` builds a NEW object every time.
``FMPCompanyDetailsProvider.__init__`` reads ``FMP_API_KEY`` out of the AppSetting
table, so a real 10-symbol / 501-bar DeterministicScorer backtest constructed
10,020 of them and spent 4.6 s doing it.

STALENESS. The memo is on the LIVE path, so its scope is what keeps it honest: a
bundle is built fresh per ``run_analysis`` (``MarketExpertInterface._live_providers``)
and per backtest run, so a memoized provider can never outlive the scope it was
built for. ``ohlcv`` is deliberately excluded -- the backtest host's resolver
returns a per-run OHLCV OVERRIDE it may install or clear at any point
(``seam_wiring._current_ohlcv_override``), and caching that would pin one run's
price source across a change.
"""
from ba2_common.core.backtest_context import LiveProviderBundle


class _Recorder:
    """A get_provider stand-in that hands back a distinct object every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, category, name, **kwargs):
        self.calls.append((category, name, tuple(sorted(kwargs))))
        return object()

    def count(self, category):
        return sum(1 for c, _n, _k in self.calls if c == category)


def test_registry_providers_are_built_once_per_bundle():
    rec = _Recorder()
    bundle = LiveProviderBundle(rec)
    for _ in range(50):
        bundle.fundamentals_details()
        bundle.fundamentals_overview()
        bundle.insider()
        bundle.news()
    for category in ("fundamentals_details", "fundamentals_overview", "insider", "news"):
        assert rec.count(category) == 1, f"{category} was rebuilt per call"


def test_the_same_instance_comes_back_every_time():
    bundle = LiveProviderBundle(_Recorder())
    assert bundle.fundamentals_details() is bundle.fundamentals_details()
    assert bundle.news() is bundle.news()
    assert bundle.fundamentals_details() is not bundle.news()


def test_a_fresh_bundle_resolves_afresh():
    """The staleness rail: live builds one bundle per analysis, so nothing is
    carried from an earlier analysis into the next one."""
    rec = _Recorder()
    first = LiveProviderBundle(rec).fundamentals_details()
    second = LiveProviderBundle(rec).fundamentals_details()
    assert first is not second
    assert rec.count("fundamentals_details") == 2


def test_ohlcv_is_never_memoized():
    """A backtest run can swap the OHLCV override mid-run; the bundle must ask."""
    rec = _Recorder()
    bundle = LiveProviderBundle(rec)
    bundle.ohlcv()
    bundle.ohlcv()
    bundle.ohlcv()
    assert rec.count("ohlcv") == 3


def test_indicators_is_rebuilt_from_a_live_ohlcv_lookup():
    """``indicators`` is constructed FROM the ohlcv provider, so it inherits the
    override's lifetime and is not memoized either."""
    rec = _Recorder()
    bundle = LiveProviderBundle(rec)
    bundle.indicators()
    bundle.indicators()
    assert rec.count("indicators") == 2
    assert rec.count("ohlcv") == 2
    assert ("indicators", "pandas", ("ohlcv_provider",)) in rec.calls


def test_price_at_date_reads_through_the_ohlcv_provider():
    """The memo must not have changed what price_at_date resolves."""
    import pandas as pd

    class _Ohlcv:
        def get_ohlcv_data(self, symbol, end_date=None, lookback_days=None, interval=None):
            return pd.DataFrame({"Close": [10.0, 11.0, 12.5]})

    bundle = LiveProviderBundle(lambda c, n, **k: _Ohlcv())
    assert bundle.price_at_date("AAPL", None) == 12.5
