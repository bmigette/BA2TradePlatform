"""Hermetic cache-miss policy: one missing symbol disables itself, many abort the run.

Written after the goal2020 grid. A single never-prewarmed symbol (OP) raised on every touch, and
because a failed trial is scored at the always-worst sentinel, 77 genomes across three jobs were
eliminated for admitting that symbol through their screener genes — a selection bias produced by a
missing file. The counter is DISTINCT SYMBOLS, not miss events, precisely so that case (77 events,
1 symbol) stays far below the abort threshold.
"""
import pytest

from ba2_providers import fmp_common as fc


def _boom():
    raise AssertionError("hermetic mode must never network-fetch")


@pytest.fixture(autouse=True)
def _clean():
    fc.reset_hermetic_misses()
    yield
    fc.reset_hermetic_misses()


@pytest.fixture
def hermetic():
    """Both contexts: the disk-cache layer only engages under frozen_ttl_cache()."""
    with fc.frozen_ttl_cache(), fc.hermetic_fmp_history():
        yield


# --------------------------------------------------------------------------------------------
# One symbol -> disabled, not fatal
# --------------------------------------------------------------------------------------------

def test_single_missing_symbol_returns_empty_instead_of_raising(hermetic):
    assert fc.fmp_history_disk_cached("grades_historical", "ZZZ_NOPE", _boom, 3650) == []


def test_missing_symbol_is_registered(hermetic):
    fc.fmp_history_disk_cached("grades_historical", "ZZZ_REG", _boom, 3650)
    assert fc.hermetic_miss_symbols() == {"grades_historical/ZZZ_REG"}


def test_reset_also_unmemoizes_so_a_later_run_recounts(hermetic):
    """The recorder returns [], which the history memo caches. Without dropping that entry the
    NEXT run would be served the memoized empty, never re-register the symbol, and under-count a
    cache that is still broken."""
    fc.fmp_history_disk_cached("grades_historical", "ZZZ_MEMO", _boom, 3650)
    fc.reset_hermetic_misses()
    fc.fmp_history_disk_cached("grades_historical", "ZZZ_MEMO", _boom, 3650)
    assert fc.hermetic_miss_symbols() == {"grades_historical/ZZZ_MEMO"},         "second run must re-register the still-missing symbol"


def test_repeated_misses_on_one_symbol_never_abort(hermetic):
    """THE regression: OP produced 77 miss events. With an event-counted limit of 10 that would
    abort a healthy run; counting distinct symbols keeps it at 1."""
    for _ in range(200):
        assert fc.fmp_history_disk_cached("grades_historical", "OP", _boom, 3650) == []
    assert len(fc.hermetic_miss_symbols()) == 1


def test_misses_are_counted_per_symbol_across_namespaces(hermetic):
    fc.fmp_history_disk_cached("grades_historical", "AAA_NOPE", _boom, 3650)
    fc.fmp_history_disk_cached("price_target_history", "AAA_NOPE", _boom, 3650)
    assert len(fc.hermetic_miss_symbols()) == 2, "same symbol, different dataset = two gaps"


# --------------------------------------------------------------------------------------------
# Many symbols -> abort
# --------------------------------------------------------------------------------------------

def test_aborts_at_the_limit(hermetic, monkeypatch):
    monkeypatch.setattr(fc, "_HERMETIC_MISS_LIMIT", 5)
    for i in range(4):
        fc.fmp_history_disk_cached("grades_historical", f"MISS{i}", _boom, 3650)
    with pytest.raises(fc.FMPHistoryCacheMiss):
        fc.fmp_history_disk_cached("grades_historical", "MISS4", _boom, 3650)


def test_abort_message_is_actionable(hermetic, monkeypatch):
    monkeypatch.setattr(fc, "_HERMETIC_MISS_LIMIT", 3)
    for i in range(2):
        fc.fmp_history_disk_cached("grades_historical", f"MSG{i}", _boom, 3650)
    with pytest.raises(fc.FMPHistoryCacheMiss) as e:
        fc.fmp_history_disk_cached("grades_historical", "MSG2", _boom, 3650)
    msg = str(e.value)
    assert "not pre-warmed" in msg
    assert "prewarm" in msg, "must say how to fix it"
    assert "MSG0" in msg, "must name what is missing"


def test_limit_is_env_overridable():
    import importlib
    import os
    os.environ["BA2_PREWARM_MISS_LIMIT"] = "3"
    try:
        importlib.reload(fc)
        assert fc._HERMETIC_MISS_LIMIT == 3
    finally:
        del os.environ["BA2_PREWARM_MISS_LIMIT"]
        importlib.reload(fc)


# --------------------------------------------------------------------------------------------
# Everything else must be untouched
# --------------------------------------------------------------------------------------------

def test_reset_clears_the_registry(hermetic):
    fc.fmp_history_disk_cached("grades_historical", "ZZZ_NOPE", _boom, 3650)
    fc.reset_hermetic_misses()
    assert fc.hermetic_miss_symbols() == set()


def test_non_hermetic_mode_still_fetches():
    """Outside a hermetic run a miss must go to the network exactly as before."""
    calls = []

    def fetch():
        calls.append(1)
        return [{"x": 1}]

    with fc.frozen_ttl_cache():                      # frozen but NOT hermetic
        fc.fmp_history_disk_cached("grades_historical", "ZZZ_NOPE_LIVE", fetch, 3650)
    assert calls, "non-hermetic miss must fetch"
    assert fc.hermetic_miss_symbols() == set(), "and must not pollute the miss registry"


# --- hermetic contract enforced at the CHOKE POINT (2026-08-06) --------------------------------
# The contract says a backtest makes ZERO network fetches, but the guard was only ever consulted
# inside fmp_history_disk_cached -- so the 9 modules calling fmp_http_get directly were exempt.
# FactorRanker's live-StockScreener fallback blocked every worker of opt 255 in the FMP rate gate
# for 8 hours with 0 trials, and exhausted the daily quota on 2026-08-05.

def test_fmp_http_get_refuses_to_leave_the_process_in_a_backtest(monkeypatch):
    import ba2_providers.fmp_common as fc

    def _boom(*a, **k):
        raise AssertionError("a hermetic backtest must never reach the network")
    monkeypatch.setattr(fc.requests, "get", _boom)

    with fc.hermetic_fmp_history():
        with pytest.raises(fc.FMPHermeticViolation, match="HERMETIC CONTRACT BREACH"):
            fc.fmp_http_get("https://financialmodelingprep.com/api/v3/quote/AAPL",
                            endpoint="quote", symbol="AAPL")


def test_fmp_list_call_is_guarded_too(monkeypatch):
    """Both entry points, or a caller just switches to the unguarded one."""
    import ba2_providers.fmp_common as fc
    with fc.hermetic_fmp_history():
        with pytest.raises(fc.FMPHermeticViolation):
            fc.fmp_list_call(lambda: [], endpoint="income_statement", symbol="AAPL")


def test_violation_is_not_an_FMPError(monkeypatch):
    """Several providers wrap FMP calls in `except FMPError` and degrade to an empty result.
    If the breach were an FMPError it would be swallowed -- back to the silent behaviour."""
    import ba2_providers.fmp_common as fc
    assert not issubclass(fc.FMPHermeticViolation, fc.FMPError)


def test_live_path_is_untouched(monkeypatch):
    """Outside hermetic mode nothing changes -- live analysis still fetches."""
    import ba2_providers.fmp_common as fc

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"symbol": "AAPL"}]
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: _Resp())
    out = fc.fmp_http_get("https://financialmodelingprep.com/api/v3/quote/AAPL", endpoint="quote")
    assert out.json() == [{"symbol": "AAPL"}]


def test_escape_hatch_reopens_the_network_for_a_deliberate_diagnostic(monkeypatch):
    import ba2_providers.fmp_common as fc

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return []
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setenv("BA2_HERMETIC_ALLOW_NETWORK", "1")
    with fc.hermetic_fmp_history():
        assert fc.fmp_http_get("https://financialmodelingprep.com/api/v3/quote/AAPL",
                               endpoint="quote").json() == []
