"""Live market-regime publishing: a failed classification is retried and is an ERROR.

Until 2026-10-07 one failed SPY fetch was a single WARNING and was cached for the whole calendar
day, so every overlay-enabled expert traded at neutral scales until tomorrow.
"""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ba2_common.core import regime_overlay
from ba2_trade_platform.core import TradeManager as tm_mod


class _Provider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def get_ohlcv_data(self, *a, **k):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _closes(n=700):
    # A calm, classifiable series: enough closes for the vol window + the rank lookback.
    return pd.DataFrame({"Close": [100.0 + (i % 7) * 0.1 for i in range(n)]})


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(tm_mod, "_LIVE_REGIME_CACHE",
                        {"day": None, "stressed": None, "retry_after": None, "failures": 0})
    seen = []
    monkeypatch.setattr(regime_overlay, "set_stressed", lambda v: seen.append(v))
    m = tm_mod.TradeManager()
    m._seen = seen
    # The platform logger does not propagate, so caplog never sees it: capture on the logger.
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture(level=logging.DEBUG)
    m.logger.addHandler(handler)
    m._records = records
    yield m
    m.logger.removeHandler(handler)


def _use(monkeypatch, provider):
    import ba2_providers
    monkeypatch.setattr(ba2_providers, "get_provider", lambda *a, **k: provider)


def test_a_failed_fetch_is_an_error_and_is_not_cached_for_the_day(manager, monkeypatch):
    _use(monkeypatch, _Provider([RuntimeError("FMP 503")]))
    manager._publish_market_regime()
    assert manager._seen[-1] is None                      # neutral, never a guess
    assert tm_mod._LIVE_REGIME_CACHE["day"] is None        # NOT cached as done
    errors = [r for r in manager._records if r.levelno >= logging.ERROR]
    assert any("UNCLASSIFIED" in r.getMessage() for r in errors)
    assert not any(r.levelno == logging.WARNING and "regime" in r.getMessage().lower()
                   for r in manager._records)


def test_no_retry_before_the_wait_then_a_retry_that_succeeds(manager, monkeypatch):
    prov = _Provider([RuntimeError("FMP 503"), _closes()])
    _use(monkeypatch, prov)
    manager._publish_market_regime()
    assert prov.calls == 1
    manager._publish_market_regime()                       # inside the wait: no new fetch
    assert prov.calls == 1 and manager._seen[-1] is None
    tm_mod._LIVE_REGIME_CACHE["retry_after"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    manager._publish_market_regime()                       # wait elapsed: retried
    assert prov.calls == 2
    assert tm_mod._LIVE_REGIME_CACHE["day"] == datetime.now(timezone.utc).date()
    assert manager._seen[-1] is False
    assert tm_mod._LIVE_REGIME_CACHE["failures"] == 0
    manager._publish_market_regime()                       # a success IS cached for the day
    assert prov.calls == 2


@pytest.mark.parametrize("bad", [None, pd.DataFrame({"Close": []})])
def test_no_bars_is_a_failure_too(manager, monkeypatch, bad):
    _use(monkeypatch, _Provider([bad]))
    manager._publish_market_regime()
    assert tm_mod._LIVE_REGIME_CACHE["day"] is None
    assert any("no SPY daily bars" in r.getMessage() for r in manager._records if r.levelno >= logging.ERROR)


def test_too_few_closes_is_unclassified_not_silently_calm(manager, monkeypatch):
    _use(monkeypatch, _Provider([_closes(30)]))
    manager._publish_market_regime()
    assert manager._seen[-1] is None
    assert tm_mod._LIVE_REGIME_CACHE["day"] is None
    assert any("could not answer" in r.getMessage() for r in manager._records if r.levelno >= logging.ERROR)


def test_a_regime_failure_never_raises_into_the_refresh(manager, monkeypatch):
    _use(monkeypatch, _Provider([ValueError("boom")]))
    manager._publish_market_regime()                       # must not raise
    assert tm_mod._LIVE_REGIME_CACHE["failures"] == 1
