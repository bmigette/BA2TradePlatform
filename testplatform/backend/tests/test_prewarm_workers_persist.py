"""Prewarm writes its history files FROM ITS WORKER THREADS, through one shared run.

The 2026-09-10 live-replay readiness audit ("Two prewarm tooling gaps",
``reports/trading/live_backtest_replay_readiness_2026-09-10.md``) found the backend/API prewarm
handler entering ``frozen_ttl_cache()`` on the SUBMITTING thread only. That flag is thread-local
(``ba2_providers.fmp_common._tls``), so every ``ThreadPoolExecutor`` worker ran un-frozen and
``fmp_history_disk_cached`` took its live-passthrough branch: the fetches went out, the task
reported success, and NOT ONE cache file was written. The audit reproduced it with the real cache
helper, a fake payload and a temp directory (``prewarm_thread_probe.json``: api_pattern_wrote_file
false, cli_pattern_wrote_file true, network_calls 0). That probe is the first test here, and the
fixed pattern is now tested where it lives -- ``prewarm_fetchers.run_prewarm``, the one call both
entry points make.

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
    """Zero network, enforced at the transport. A prewarm test that fetched for real would
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
def test_submitting_thread_freeze_alone_writes_nothing(cache_root):
    """The bug, reproduced: freeze on the submitting thread only. The fetch happens, the caller
    gets its data, and the worker thread — never frozen — writes no cache file at all. Kept as a
    negative control for the shared run below, which does it right."""
    payload = [{"date": "2026-09-01", "eps": 1.0}]
    calls = []

    def _work(sym):
        return fmp_history_disk_cached("probe_old", sym, lambda: (calls.append(sym) or payload))

    with frozen_ttl_cache():                      # no initializer, no sentinel: the old handler
        with ThreadPoolExecutor(max_workers=1) as ex:
            result = ex.submit(_work, "AAA").result()

    assert result == payload and calls == ["AAA"]
    assert not _history_file(cache_root, "probe_old", "AAA").exists()


# --------------------------------------------------------------------------- the shared run
class _StubFetchers:
    """Stands in for ``PrewarmFetchers`` so ``run_prewarm``'s freeze/pool/counting block is what
    is under test (the real fetchers are exercised by the estimator-input tests below)."""

    def __init__(self, fetch_result, namespace="stub_history", raiser=None):
        import app.services.prewarm_fetchers as pf
        self.fetch_result = fetch_result
        self.namespace = namespace
        self.raiser = raiser            # (symbol) -> raise, to test failure handling
        self.symbols = []
        self.senate_latest_calls = 0
        self.logs = []
        # Derived from the shared table, so an expert added there is covered here too.
        self.table = {name: self._do for name in pf.FETCHER_METHODS}

    def _do(self, sym):
        self.symbols.append(sym)
        if self.raiser is not None:
            self.raiser(sym)
        fmp_history_disk_cached(self.namespace, sym, lambda: self.fetch_result)

    def validate(self, experts):
        return [e for e in experts if e not in self.table]

    def do_senate_latest(self):
        self.senate_latest_calls += 1

    def log(self, message):
        self.logs.append(message)


def _run(stub, experts, symbols, workers=2):
    import app.services.prewarm_fetchers as pf
    return pf.run_prewarm(stub, experts, symbols, workers, end=END)


def test_run_prewarm_writes_history_from_its_worker_threads(cache_root):
    payload_rows = [{"symbol": "AAA", "grade": "Buy"}]
    stub = _StubFetchers(payload_rows, namespace="run_history")

    summary = _run(stub, ["FMPRating"], ["AAA", "BBB"])

    assert summary["errors"] == 0 and summary["cached"] == {"FMPRating": 2}
    assert sorted(stub.symbols) == ["AAA", "BBB"]
    for sym in ("AAA", "BBB"):
        written = _history_file(cache_root, "run_history", sym)
        assert written.exists(), f"{sym}: the shared run wrote no cache file"
        assert json.loads(written.read_text()) == payload_rows


def test_run_prewarm_persists_the_checked_empty_sentinel(cache_root):
    """A symbol FMP genuinely has no data for is cached as ``[]`` — "checked, no data" — so a
    hermetic backtest stops reading it as the fatal "never pre-warmed" of an absent file."""
    stub = _StubFetchers([], namespace="run_empty")

    _run(stub, ["FMPRating"], ["ZZZ"], workers=1)

    written = _history_file(cache_root, "run_empty", "ZZZ")
    assert written.exists(), "no sentinel written for a checked-empty result"
    assert json.loads(written.read_text()) == []


def test_run_prewarm_covers_all_seven_experts(cache_root):
    """Every expert in the shared table is warmable from either entry point — the handler used
    to know only three, and DeterministicScorer got FRED but no per-symbol history at all."""
    import app.services.prewarm_fetchers as pf
    stub = _StubFetchers([{"x": 1}], namespace="run_all")

    summary = _run(stub, list(pf.EXPERT_NAMES), ["AAA"], workers=1)

    assert summary["skipped"] == []
    assert summary["cached"] == {name: 1 for name in pf.EXPERT_NAMES}
    assert summary["senate_latest"] is True
    assert summary["senate_skill_scores"] is False
    assert any("trader-skill" in n for n in summary["notes"])


def test_run_prewarm_warms_the_unscoped_senate_feed_with_no_per_symbol_work(cache_root):
    """``--experts FMPSenateTraderCopy`` has no per-symbol fetcher, but basket-mode Copy still
    reads the unscoped ALL_FULL_HISTORY feed — so it must be warmed before the empty-work exit."""
    stub = _StubFetchers([{"x": 1}], namespace="run_copy")

    summary = _run(stub, ["FMPSenateTraderCopy"], ["AAA"], workers=1)

    assert stub.senate_latest_calls == 1
    assert stub.symbols == []
    assert summary["skipped"] == ["FMPSenateTraderCopy"]
    assert any("no per-symbol" in n for n in summary["notes"])


def test_run_prewarm_counts_a_symbol_failure_and_redacts_the_key(cache_root):
    """One instrument's data gap must not abort a 500-symbol warm — and the log carries the
    exception TYPE plus a redacted message, never the api key FMP quotes back in its errors."""
    def _boom(sym):
        raise ValueError("500 Server Error for url: https://fmp/api/v3/x?apikey=SECRET123&y=1")

    stub = _StubFetchers([{"x": 1}], namespace="run_fail", raiser=_boom)

    summary = _run(stub, ["FMPRating"], ["AAA"], workers=1)

    assert summary["errors"] == 1 and summary["cached"] == {}
    assert summary["failures"] == [
        "FMPRating/AAA: ValueError: 500 Server Error for url: "
        "https://fmp/api/v3/x?apikey=<redacted>&y=1"]
    assert "SECRET123" not in " ".join(stub.logs)


def test_config_error_from_a_worker_aborts_the_whole_run(cache_root):
    """A configuration gap is NOT a per-symbol error: it would repeat for every remaining
    symbol, so it escapes the pool by name instead of being counted 500 times."""
    import app.services.prewarm_fetchers as pf

    def _boom(sym):
        raise pf.PrewarmConfigError("finnhub_api_key not configured")

    stub = _StubFetchers([{"x": 1}], namespace="run_cfg", raiser=_boom)

    with pytest.raises(pf.PrewarmConfigError, match="finnhub_api_key"):
        _run(stub, ["FinnHubRating"], ["AAA", "BBB"], workers=1)


# --------------------------------------------------------------- both entry points, one run
def _load_launcher():
    launcher = os.path.normpath(os.path.join(_BACKEND, "..", "ba2test_launcher.py"))
    spec = importlib.util.spec_from_file_location("ba2test_launcher_prewarm_test", launcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _prewarm_args(**over):
    args = {"symbols": "AAA", "experts": "FMPRating", "workers": 1, "end": "2026-09-10",
            "start": None}
    args.update(over)
    return argparse.Namespace(**args)


class _FetchersBuilt(Exception):
    """Raised by the stubbed class so each entry point stops right after resolving it."""


def test_both_entry_points_build_the_same_fetchers(monkeypatch):
    """Patching the ONE shared class stops both entry points, which is only possible because
    neither carries its own copy of the fetchers any more."""
    import app.services.prewarm_fetchers as pf
    calls = []

    def _record(**kwargs):
        calls.append(kwargs)
        raise _FetchersBuilt("stubbed shared fetchers")

    monkeypatch.setattr(pf, "PrewarmFetchers", _record)
    monkeypatch.setattr(pf, "resolve_keys", lambda: {"fmp": "K", "finnhub": "F"})

    handler = importlib.import_module("app.services.data_build_handler")
    result = handler.handle_prewarm("t-1", {"symbols": ["AAA"], "experts": ["FMPRating"]})
    assert result["status"] == "failed" and "stubbed shared fetchers" in result["error"]

    launcher = _load_launcher()
    with pytest.raises(_FetchersBuilt):
        launcher._cmd_prewarm(_prewarm_args())

    assert len(calls) == 2, "both entry points must build the shared fetchers"
    assert all(set(c) == {"fmp_key", "end_date", "finnhub_key", "log"} for c in calls)


def test_cli_prewarm_writes_history_from_its_worker_threads(cache_root, monkeypatch):
    """The CLI end to end through ``_cmd_prewarm``: same shared run, same worker-thread
    persistence, and it still returns 0 and prints its summary."""
    import app.services.prewarm_fetchers as pf
    stub = _StubFetchers([{"symbol": "CLI1", "grade": "Buy"}], namespace="cli_history")
    monkeypatch.setattr(pf, "PrewarmFetchers", lambda **kwargs: stub)
    monkeypatch.setattr(pf, "resolve_keys", lambda: {"fmp": "K", "finnhub": "F"})

    launcher = _load_launcher()
    rc = launcher._cmd_prewarm(_prewarm_args(symbols="CLI1"))

    assert rc == 0
    assert stub.symbols == ["CLI1"]
    assert _history_file(cache_root, "cli_history", "CLI1").exists()


def test_missing_fmp_key_fails_both_entry_points(monkeypatch):
    import app.services.prewarm_fetchers as pf
    monkeypatch.setattr(pf, "resolve_keys", lambda: {"fmp": None, "finnhub": None})

    handler = importlib.import_module("app.services.data_build_handler")
    result = handler.handle_prewarm("t-2", {"symbols": ["AAA"], "experts": ["FMPRating"]})
    assert result["status"] == "failed" and "FMP_API_KEY" in result["error"]

    launcher = _load_launcher()
    assert launcher._cmd_prewarm(_prewarm_args()) == 1


def test_missing_finnhub_key_fails_both_entry_points(monkeypatch):
    """Resolved up front by ``validate``, before a single fetch — not once per symbol."""
    import app.services.prewarm_fetchers as pf
    monkeypatch.setattr(pf, "resolve_keys", lambda: {"fmp": "K", "finnhub": None})

    handler = importlib.import_module("app.services.data_build_handler")
    result = handler.handle_prewarm("t-3", {"symbols": ["AAA"], "experts": ["FinnHubRating"]})
    assert result["status"] == "failed" and "finnhub" in result["error"]

    launcher = _load_launcher()
    assert launcher._cmd_prewarm(_prewarm_args(experts="FinnHubRating")) == 1


def test_senate_scalper_bounds_have_one_source():
    """The GA grid's scalper gene floor and the prewarm's skip read the SAME two numbers, so a
    grid edit cannot silently make the prewarm skip traders a trial still needs."""
    import app.services.prewarm_fetchers as pf
    launcher = _load_launcher()
    params = launcher._EXPERT_OPT["FMPSenateTraderWeight"]["expert_params"]
    gene = params["min_trader_avg_hold_days"]
    fixed = launcher._EXPERT_OPT["FMPSenateTraderWeight"]["fixed_settings"]

    assert gene["min"] == pf.SENATE_SCALPER_BOUNDS["hold_floor_days"]
    assert fixed["min_trader_hold_roundtrips"] == pf.SENATE_SCALPER_BOUNDS["hold_min_roundtrips"]
    # 0 would mean "filter disabled", which makes the prewarm's scalper skip unsound.
    assert pf.SENATE_SCALPER_BOUNDS["hold_floor_days"] > 0


def test_the_table_covers_seven_experts_and_is_built_once():
    import app.services.prewarm_fetchers as pf
    assert set(pf.FETCHER_METHODS) == {
        "FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "FactorRanker",
        "FMPSenateTraderWeight", "FinnHubRating", "DeterministicScorer",
    }
    a = pf.PrewarmFetchers(fmp_key="K", end_date=END, finnhub_key="F")
    b = pf.PrewarmFetchers(fmp_key="K", end_date=END, finnhub_key="F")
    assert set(a.table) == set(pf.FETCHER_METHODS)
    # Two entry points get two instances but the SAME functions — one implementation.
    for name in pf.FETCHER_METHODS:
        assert a.table[name].__func__ is b.table[name].__func__


def test_unknown_expert_is_reported_not_raised():
    import app.services.prewarm_fetchers as pf
    f = pf.PrewarmFetchers(fmp_key="K", end_date=END, finnhub_key=None)
    assert f.validate(["FMPRating", "NotAnExpert"]) == ["NotAnExpert"]
    with pytest.raises(pf.PrewarmConfigError, match="finnhub"):
        f.validate(["FinnHubRating"])


# ------------------------------------------------------- estimator inputs (union semantics)
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


def _warm(method_name, symbol):
    import app.services.prewarm_fetchers as pf
    f = pf.PrewarmFetchers(fmp_key="TEST-FMP-KEY", end_date=END, finnhub_key=None)
    with frozen_ttl_cache(), persist_empty_sentinel():
        getattr(f, method_name)(symbol)


@pytest.mark.parametrize("method,own_namespace,symbol", [
    ("do_insider", "insider_v2", "INSDA"),
    ("do_earnings_drift", "past_earnings_quarterly", "DRFTA"),
])
def test_estimator_inputs_are_warmed_unconditionally(cache_root, fake_fmp, method,
                                                     own_namespace, symbol):
    """``expected_profit_mode`` is a GA GENE for both of these experts, so prewarm cannot know
    which trial will call ``analyst_target_model.fetch_estimator_inputs``. Warm its two
    namespaces for every symbol — the union, exactly like FactorRanker warms every factor."""
    _warm(method, symbol)

    assert _history_file(cache_root, own_namespace, symbol).exists()
    assert _history_file(cache_root, "past_earnings_quarterly", symbol).exists()
    assert _history_file(cache_root, "earnings_estimates_quarterly", symbol).exists()
