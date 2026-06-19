"""Phase 3 / Task 1: the ScreenerProviderInterface.screen_stocks as_of seam.

Asserts (a) the interface abstract signature now carries an optional ``as_of``
defaulting to None (source-compatible for every existing caller), and (b) the
live FMPScreenerProvider rejects a non-None ``as_of`` loudly (live-only guard) so
``as_of=None`` stays byte-identical to the pre-Phase-3 live screener.
"""
import inspect

import pytest
from datetime import datetime, timezone


def test_interface_screen_stocks_accepts_as_of_kw():
    from ba2_common.core.interfaces.ScreenerProviderInterface import (
        ScreenerProviderInterface,
    )

    sig = inspect.signature(ScreenerProviderInterface.screen_stocks)
    assert "as_of" in sig.parameters
    assert sig.parameters["as_of"].default is None


def test_live_provider_rejects_as_of():
    from ba2_providers.screener.FMPScreenerProvider import FMPScreenerProvider

    p = FMPScreenerProvider()
    with pytest.raises(ValueError):
        p.screen_stocks({}, as_of=datetime(2022, 1, 3, tzinfo=timezone.utc))
