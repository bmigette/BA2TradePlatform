"""TTLCache freeze behaviour (backtest perf): a long backtest must NOT let the 15-min
FMP TTL expire mid-run and re-fetch. ``frozen_ttl_cache()`` makes entries non-expiring
for its duration; the LIVE path (no context) keeps normal TTL expiry.
"""
from ba2_providers.fmp_common import TTLCache, frozen_ttl_cache, set_ttl_frozen


def test_ttl_expires_normally_when_not_frozen():
    t = [1000.0]
    c = TTLCache(900, clock=lambda: t[0])
    calls = []
    c.get_or_call("AAPL", lambda: (calls.append(1), "v1")[1])
    t[0] += 1000  # past the 900s TTL
    c.get_or_call("AAPL", lambda: (calls.append(1), "v2")[1])
    assert len(calls) == 2  # expired -> re-fetched


def test_frozen_ignores_expiry():
    t = [1000.0]
    c = TTLCache(900, clock=lambda: t[0])
    calls = []
    with frozen_ttl_cache():
        first = c.get_or_call("AAPL", lambda: (calls.append(1), "v1")[1])
        t[0] += 100_000  # way past TTL
        again = c.get_or_call("AAPL", lambda: (calls.append(1), "v2")[1])
    assert len(calls) == 1   # frozen -> single fetch reused
    assert first == again == "v1"


def test_freeze_restored_after_context():
    t = [1000.0]
    c = TTLCache(900, clock=lambda: t[0])
    calls = []
    with frozen_ttl_cache():
        c.get_or_call("AAPL", lambda: (calls.append(1), "v1")[1])
    t[0] += 1000  # past TTL, now OUTSIDE the frozen context
    c.get_or_call("AAPL", lambda: (calls.append(1), "v2")[1])
    assert len(calls) == 2   # TTL expiry enforced again after the context


def test_frozen_context_is_reentrant_safe():
    # nested/!restore: the flag is saved/restored, not hard-reset to False
    set_ttl_frozen(True)
    try:
        with frozen_ttl_cache():
            pass
        from ba2_providers import fmp_common
        assert fmp_common._TTL_FROZEN is True  # restored to prior (True), not forced False
    finally:
        set_ttl_frozen(False)
