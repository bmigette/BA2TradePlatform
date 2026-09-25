"""A hermetic ATR source for real-engine tests that size equity entries through the classic RM.

``DailyBacktestEngine.run()`` REPLACES an ``indicator_provider=None`` with the real, FMP-backed
indicator provider (``make_atr_cache_indicator_provider(store) or make_indicator_provider()``),
so a test that passes None does NOT get ``position_sizing.get_latest_atr``'s no-ATR path: every
equity sizing pass / safeguard-stop lookup calls FMP with the suite's fake key and only reads as
"no ATR" because that request fails. A bare ``object()`` is no answer either -- it raises inside
the sizing pass. This stub answers every ATR request with NO value, offline, which is the input
those tests always meant (``test_equity_golden_run._NoAtrIndicatorProvider`` pins the same one).
"""


class NoAtrIndicatorProvider:
    """Reports no ATR for every symbol (the documented no-ATR-available sizing path)."""

    def get_indicator(self, symbol, indicator, **kwargs):
        return {"values": []}
