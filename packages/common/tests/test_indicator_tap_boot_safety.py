"""A capture tap on an indicator provider must never stop the process from booting.

``MarketIndicatorsInterface.__init_subclass__`` applies ``observe_provider`` to every
concrete ``get_indicator`` AT CLASS-CREATION TIME, which is import time for every
provider module in the platform. Anything that raises there -- a signature the
decorator cannot bind, a future change to ``observe_provider``, a ``get_indicator``
that is not an ordinary function -- propagates out of the ``class`` statement, out of
the module import, and out of ``main.initialize_system()``: the whole application
fails to start because an OBSERVABILITY wrapper could not be attached.

Capture is instrumentation. It may lose an observation; it may not take the platform
down. So the tap is attempted, and a failure leaves that one method untapped with an
ERROR naming it.
"""
import importlib
import logging

import pytest

from ba2_common.core.interfaces.MarketIndicatorsInterface import MarketIndicatorsInterface

# The MODULE, not the class the package re-exports under the same name.
module = importlib.import_module("ba2_common.core.interfaces.MarketIndicatorsInterface")


class _Records(logging.Handler):
    """Collect the package logger's ERROR records (it does not propagate to root)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def tap_errors():
    """The package logger's ERROR records, with any leaked ``logging.disable`` lifted."""
    handler = _Records()
    log = module.logger
    saved_disable = logging.root.manager.disable
    saved_disabled, saved_level = log.disabled, log.level
    logging.disable(logging.NOTSET)
    log.disabled = False
    if log.level > logging.ERROR:
        log.setLevel(logging.ERROR)
    log.addHandler(handler)
    try:
        yield handler.messages
    finally:
        log.removeHandler(handler)
        log.setLevel(saved_level)
        log.disabled = saved_disabled
        logging.disable(saved_disable)


def test_a_tap_that_raises_leaves_the_subclass_importable(monkeypatch, tap_errors):
    """The class still comes into existence, with its own unwrapped implementation."""
    errors = tap_errors

    def _explode(*args, **kwargs):
        raise RuntimeError("the tap could not be built")

    monkeypatch.setattr(module, "observe_provider", _explode)

    class _Provider(MarketIndicatorsInterface):
        def __init__(self, ohlcv_provider=None):
            self.ohlcv_provider = ohlcv_provider

        def get_indicator(self, symbol, indicator, interval="1d", start_date=None,
                          end_date=None, lookback_days=None, format_type="markdown"):
            return f"{symbol}:{indicator}"

    assert _Provider.get_indicator(object(), "AAPL", "atr") == "AAPL:atr", (
        "the provider must still work; an untapped method is a lost observation, "
        "not a broken provider")
    assert any("get_indicator" in m and "_Provider" in m for m in errors), (
        f"the failed tap must be reported by name so the gap is visible; got {errors}")


def test_a_working_tap_is_still_applied(monkeypatch):
    """The protection must not swallow the tap itself -- the wrapper is still attached."""
    from ba2_common.core.replay.observe import tapped_boundary

    class _Tapped(MarketIndicatorsInterface):
        def __init__(self, ohlcv_provider=None):
            self.ohlcv_provider = ohlcv_provider

        def get_indicator(self, symbol, indicator, interval="1d", start_date=None,
                          end_date=None, lookback_days=None, format_type="markdown"):
            return f"{symbol}:{indicator}"

    assert tapped_boundary(_Tapped.__dict__["get_indicator"]) is not None
