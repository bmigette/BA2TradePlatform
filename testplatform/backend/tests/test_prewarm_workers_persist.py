"""Prewarm writes its history files FROM ITS WORKER THREADS, from one shared fetcher table.

The 2026-09-10 live-replay readiness audit ("Two prewarm tooling gaps",
``reports/trading/live_backtest_replay_readiness_2026-09-10.md``) found the backend/API prewarm
handler entering ``frozen_ttl_cache()`` on the SUBMITTING thread only. That flag is thread-local
(``ba2_providers.fmp_common._tls``), so every ``ThreadPoolExecutor`` worker ran un-frozen and
``fmp_history_disk_cached`` took its live-passthrough branch: the fetches went out, the task
reported success, and NOT ONE cache file was written. The audit reproduced it with the real cache
helper, a fake payload and a temp directory (``prewarm_thread_probe.json``: api_pattern_wrote_file
false, cli_pattern_wrote_file true, network_calls 0). That probe is the first test here, both
halves of it, so the bug cannot come back silently.

Everything runs against the REAL ``fmp_history_disk_cached`` with fake ``fetch_fn``s and a temp
``CACHE_FOLDER``. ``socket.socket.connect`` is patched to raise for the whole module: any test
that reached the network would fail loudly instead of quietly warming from FMP.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from ba2_providers.fmp_common import (  # noqa: E402
    fmp_history_disk_cached, frozen_ttl_cache, persist_empty_sentinel, set_ttl_frozen,
)

END = datetime(2026, 9, 10, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Zero network, enforced at the transport. A prewarm test that fetches for real would
    otherwise pass while proving nothing about the cache."""
    def _refuse(self, address):  # noqa: ANN001
        raise AssertionError(f"prewarm test attempted a network connection to {address!r}")
    monkeypatch.setattr(socket.socket, "connect", _refuse)


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point the fmp_history cache at a temp dir (``_fmp_history_cache_dir`` re-reads
    ``ba2_common.config.CACHE_FOLDER`` on every call, so rebinding it is enough)."""
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    return tmp_path / "fmp_history"


def _history_file(cache_root, namespace: str, symbol: str):
    return cache_root / f"{namespace}__{symbol.upper()}.json"


# --------------------------------------------------------------------------- the audit's probe
def _run_pool_pattern(namespace: str, symbol: str, payload, *, with_initializer: bool,
                      with_sentinel: bool):
    """Run one fetch through a ThreadPoolExecutor exactly as a prewarm entry point does."""
    calls = []

    def _fetch():
        calls.append(symbol)
        return payload

    def _work(sym):
        return fmp_history_disk_cached(namespace, sym, _fetch)

    sentinel = persist_empty_sentinel() if with_sentinel else _nullcontext()
    kwargs = {"initializer": set_ttl_frozen, "initargs": (True,)} if with_initializer else {}
    with frozen_ttl_cache(), sentinel:
        with ThreadPoolExecutor(max_workers=1, **kwargs) as ex:
            result = ex.submit(_work, symbol).result()
    return result, calls


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def test_old_handler_pattern_returns_data_but_writes_nothing(cache_root):
    """The bug, reproduced: freeze on the submitting thread only. The fetch happens, the caller
    gets its data, and the worker thread — never frozen — writes no cache file at all."""
    payload = [{"date": "2026-09-01", "eps": 1.0}]
    result, calls = _run_pool_pattern("probe_old", "AAA", payload,
                                      with_initializer=False, with_sentinel=False)
    assert result == payload and calls == ["AAA"]
    assert not _history_file(cache_root, "probe_old", "AAA").exists()


def test_worker_initializer_pattern_writes_the_history_file(cache_root):
    """The fix: ``initializer=set_ttl_frozen`` sets the thread-local flag INSIDE each worker."""
    payload = [{"date": "2026-09-01", "eps": 1.0}]
    result, calls = _run_pool_pattern("probe_new", "AAA", payload,
                                      with_initializer=True, with_sentinel=True)
    assert result == payload and calls == ["AAA"]
    written = _history_file(cache_root, "probe_new", "AAA")
    assert written.exists()
    assert json.loads(written.read_text()) == payload


# --------------------------------------------------------------------------- the real handler
class _StubFetchers:
    """Stands in for ``PrewarmFetchers`` so the handler's EXECUTOR pattern is what is under
    test here (the real fetchers are exercised by the insider tests below)."""

    def __init__(self, fetch_result, namespace="stub_history"):
        self.fetch_result = fetch_result
        self.namespace = namespace
        self.symbols = []
        self.senate_latest_calls = 0

    def _do(self, sym):
        self.symbols.append(sym)
        fmp_history_disk_cached(self.namespace, sym, lambda: self.fetch_result)

    @property
    def table(self):
        return {"FMPRating": self._do, "FMPEarningsDrift": self._do,
                "FMPInsiderClusterBuy": self._do, "FactorRanker": self._do,
                "FMPSenateTraderWeight": self._do, "FinnHubRating": self._do,
                "DeterministicScorer": self._do}

    def validate(self, experts):
        return [e for e in experts if e not in self.table]

    def do_senate_latest(self):
        self.senate_latest_calls += 1

    def log(self, message):
        pass


@pytest.fixture
def handler_with_stub(monkeypatch):
    """The real ``handle_prewarm``, with the fetcher table stubbed at the shared seam."""
    monkeypatch.setenv("FMP_API_KEY", "TEST-FMP-KEY")
    import app.services.prewarm_fetchers as pf
    handler = importlib.import_module("app.services.data_build_handler")

    def _install(stub):
        monkeypatch.setattr(pf, "build_fetchers", lambda **kwargs: stub)
        return handler

    return _install


def test_handle_prewarm_writes_history_from_its_worker_threads(cache_root, handler_with_stub):
    payload_rows = [{"symbol": "AAA", "grade": "Buy"}]
    stub = _StubFetchers(payload_rows, namespace="handler_history")
    handler = handler_with_stub(stub)

    result = handler.handle_prewarm("t-1", {"symbols": ["AAA", "BBB"], "workers": 2,
                                            "experts": ["FMPRating"]})

    assert result["status"] == "completed", result
    assert result["summary"]["errors"] == 0
    assert sorted(stub.symbols) == ["AAA", "BBB"]
    for sym in ("AAA", "BBB"):
        written = _history_file(cache_root, "handler_history", sym)
        assert written.exists(), f"{sym}: handler prewarm wrote no cache file"
        assert json.loads(written.read_text()) == payload_rows


def test_handle_prewarm_persists_the_checked_empty_sentinel(cache_root, handler_with_stub):
    """A symbol FMP genuinely has no data for is cached as ``[]`` — "checked, no data" — so a
    hermetic backtest stops reading it as the fatal "never pre-warmed" of an absent file."""
    stub = _StubFetchers([], namespace="handler_empty")
    handler = handler_with_stub(stub)

    result = handler.handle_prewarm("t-2", {"symbols": ["ZZZ"], "workers": 1,
                                            "experts": ["FMPRating"]})

    assert result["status"] == "completed", result
    written = _history_file(cache_root, "handler_empty", "ZZZ")
    assert written.exists(), "no sentinel written for a checked-empty result"
    assert json.loads(written.read_text()) == []


def test_handle_prewarm_covers_all_seven_experts(handler_with_stub, monkeypatch):
    """Every expert in the shared table is warmable through the API handler — it used to know
    only three, and DeterministicScorer got FRED but no per-symbol history at all."""
    import app.services.prewarm_fetchers as pf
    stub = _StubFetchers([{"x": 1}], namespace="handler_all")
    handler = handler_with_stub(stub)
    # FRED is economy-wide and network-bound; the DS per-SYMBOL path is what is under test.
    monkeypatch.setattr(handler, "_prewarm_fred",
                        lambda *a, **k: {"refreshed": 0, "fresh": 0, "errors": 0})

    result = handler.handle_prewarm("t-3", {"symbols": ["AAA"], "workers": 1,
                                            "experts": list(pf.EXPERT_NAMES)})

    assert result["status"] == "completed", result
    assert result["summary"]["skipped"] == []
    assert result["summary"]["cached"] == {name: 1 for name in pf.EXPERT_NAMES}
    assert stub.senate_latest_calls == 1


def test_cli_prewarm_writes_history_from_its_worker_threads(cache_root, monkeypatch):
    """The other entry point, end to end through ``_cmd_prewarm``: same shared table, same
    worker-thread persistence. Guards the CLI half of the move."""
    import app.services.prewarm_fetchers as pf
    stub = _StubFetchers([{"symbol": "CLI1", "grade": "Buy"}], namespace="cli_history")
    monkeypatch.setattr(pf, "build_fetchers", lambda **kwargs: stub)
    monkeypatch.setenv("FMP_API_KEY", "TEST-FMP-KEY")
    monkeypatch.setenv("FINNHUB_API_KEY", "TEST-FINNHUB-KEY")

    launcher = _load_launcher()
    rc = launcher._cmd_prewarm(argparse.Namespace(
        symbols="CLI1", experts="FMPRating", workers=1, end="2026-09-10",
        start=None, expert_settings=None))

    assert rc == 0
    assert stub.symbols == ["CLI1"]
    written = _history_file(cache_root, "cli_history", "CLI1")
    assert written.exists(), "CLI prewarm wrote no cache file"


# --------------------------------------------------------------------------- one fetcher table
def _load_launcher():
    launcher = os.path.normpath(os.path.join(_BACKEND, "..", "ba2test_launcher.py"))
    spec = importlib.util.spec_from_file_location("ba2test_launcher_prewarm_test", launcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _BuildFetchersCalled(Exception):
    """Raised by the stubbed builder so each entry point stops right after resolving it."""


def test_launcher_and_handler_resolve_the_same_fetcher_table(monkeypatch):
    """Both prewarm entry points build their table from ``prewarm_fetchers.build_fetchers``.

    Patching that ONE attribute stops both, which is only possible because neither carries its
    own copy of the fetchers any more.
    """
    import app.services.prewarm_fetchers as pf
    calls = []

    def _record(**kwargs):
        calls.append(kwargs)
        raise _BuildFetchersCalled("stubbed shared builder")

    monkeypatch.setattr(pf, "build_fetchers", _record)
    monkeypatch.setenv("FMP_API_KEY", "TEST-FMP-KEY")
    monkeypatch.setenv("FINNHUB_API_KEY", "TEST-FINNHUB-KEY")

    handler = importlib.import_module("app.services.data_build_handler")
    result = handler.handle_prewarm("t-4", {"symbols": ["AAA"], "experts": ["FMPRating"]})
    assert result["status"] == "failed" and "stubbed shared builder" in result["error"]

    launcher = _load_launcher()
    args = argparse.Namespace(symbols="AAA", experts="FMPRating", workers=1, end=None,
                              start=None, expert_settings=None)
    with pytest.raises(_BuildFetchersCalled):
        launcher._cmd_prewarm(args)

    assert len(calls) == 2, "both entry points must go through the shared builder"
    assert all(set(c) >= {"fmp_key", "end_date", "finnhub_key", "expert_settings"} for c in calls)


def test_the_table_covers_seven_experts_and_is_built_once(monkeypatch):
    import app.services.prewarm_fetchers as pf
    assert set(pf.FETCHER_METHODS) == {
        "FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "FactorRanker",
        "FMPSenateTraderWeight", "FinnHubRating", "DeterministicScorer",
    }
    a = pf.build_fetchers(fmp_key="K", end_date=END, finnhub_key="F",
                          senate_hold_floor_days=1.0, senate_hold_min_roundtrips=3)
    b = pf.build_fetchers(fmp_key="K", end_date=END, finnhub_key="F",
                          senate_hold_floor_days=1.0, senate_hold_min_roundtrips=3)
    assert set(a.table) == set(pf.FETCHER_METHODS)
    # Two entry points get two instances but the SAME functions — one implementation.
    for name in pf.FETCHER_METHODS:
        assert a.table[name].__func__ is b.table[name].__func__


def test_unwarmable_expert_is_refused_up_front():
    """A configuration gap fails once, before any fetch, instead of N times mid-run."""
    import app.services.prewarm_fetchers as pf
    f = pf.build_fetchers(fmp_key="K", end_date=END, finnhub_key=None,
                          senate_hold_floor_days=None, senate_hold_min_roundtrips=None)
    assert f.validate(["FMPRating", "NotAnExpert"]) == ["NotAnExpert"]
    with pytest.raises(pf.PrewarmConfigError, match="finnhub"):
        f.validate(["FinnHubRating"])
    with pytest.raises(pf.PrewarmConfigError, match="senate_hold_floor_days"):
        f.validate(["FMPSenateTraderWeight"])


# --------------------------------------------------------------- insider model-mode inputs
@pytest.fixture
def fake_fmp(monkeypatch):
    """Real providers, faked transport: every FMP call returns an empty payload, so each read
    still travels the real namespace/caching path and lands a sentinel file on disk."""
    # import_module, not ``from ... import``: the packages re-export the CLASS under the
    # module's own name, so the plain import hands back the class, not the module.
    details_mod = importlib.import_module(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider")
    insider_mod = importlib.import_module("ba2_providers.insider.FMPInsiderProvider")

    monkeypatch.setattr(details_mod, "get_app_setting", lambda *a, **k: "TEST-FMP-KEY")
    monkeypatch.setattr(insider_mod, "get_app_setting", lambda *a, **k: "TEST-FMP-KEY")
    monkeypatch.setattr(insider_mod.FMPInsiderProvider, "_fetch_insider_history",
                        lambda self, symbol: [])
    monkeypatch.setattr(details_mod.fmpsdk, "historical_earning_calendar",
                        lambda **kwargs: [])

    class _EmptyResponse:
        def json(self):
            return []

    monkeypatch.setattr(details_mod, "fmp_http_get", lambda *a, **k: _EmptyResponse())


def _run_insider(expert_settings, symbol):
    import app.services.prewarm_fetchers as pf
    f = pf.build_fetchers(fmp_key="TEST-FMP-KEY", end_date=END, finnhub_key=None,
                          senate_hold_floor_days=None, senate_hold_min_roundtrips=None,
                          expert_settings=expert_settings)
    with frozen_ttl_cache(), persist_empty_sentinel():
        f.do_insider(symbol)


def test_insider_model_mode_warms_the_estimator_namespaces(cache_root, fake_fmp):
    """``expected_profit_mode='model'`` makes ``_gather`` call ``fetch_estimator_inputs``, which
    reads quarterly past earnings + earnings estimates. Warm those or the deployed model-mode
    instance replays/backtests against two namespaces prewarm never wrote."""
    _run_insider({"FMPInsiderClusterBuy": {"expected_profit_mode": "model"}}, "MODLA")

    assert _history_file(cache_root, "insider_v2", "MODLA").exists()
    assert _history_file(cache_root, "past_earnings_quarterly", "MODLA").exists()
    assert _history_file(cache_root, "earnings_estimates_quarterly", "MODLA").exists()


def test_insider_static_mode_warms_only_insider_transactions(cache_root, fake_fmp):
    """Default (static) mode never fetches the estimator inputs, so prewarm must not either —
    two extra FMP calls per symbol for data the expert will not read."""
    _run_insider({"FMPInsiderClusterBuy": {"expected_profit_mode": "static"}}, "STATB")

    assert _history_file(cache_root, "insider_v2", "STATB").exists()
    assert not _history_file(cache_root, "past_earnings_quarterly", "STATB").exists()
    assert not _history_file(cache_root, "earnings_estimates_quarterly", "STATB").exists()


def test_insider_without_supplied_settings_uses_the_declared_default(cache_root, fake_fmp):
    """No settings supplied -> the expert's own DECLARED default (static), which is what a
    default-configured instance actually reads. Not a guess made up by prewarm."""
    from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
    assert FMPInsiderClusterBuy.get_settings_definitions()["expected_profit_mode"]["default"] \
        == "static"

    _run_insider(None, "DEFLT")

    assert _history_file(cache_root, "insider_v2", "DEFLT").exists()
    assert not _history_file(cache_root, "past_earnings_quarterly", "DEFLT").exists()


def test_insider_null_setting_raises_instead_of_assuming_a_mode(cache_root, fake_fmp):
    import app.services.prewarm_fetchers as pf
    with pytest.raises(pf.PrewarmConfigError, match="expected_profit_mode"):
        _run_insider({"FMPInsiderClusterBuy": {"expected_profit_mode": None}}, "NULLX")


def test_insider_undeclared_setting_raises(cache_root, fake_fmp, monkeypatch):
    """If the knob is renamed away, prewarm fails loudly rather than falling back to a
    hardcoded 'static' and silently under-warming every model-mode instance."""
    from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
    real = FMPInsiderClusterBuy.get_settings_definitions

    def _without_the_key():
        return {k: v for k, v in real().items() if k != "expected_profit_mode"}

    monkeypatch.setattr(FMPInsiderClusterBuy, "get_settings_definitions",
                        staticmethod(_without_the_key))
    with pytest.raises(KeyError, match="expected_profit_mode"):
        _run_insider(None, "GONEX")
