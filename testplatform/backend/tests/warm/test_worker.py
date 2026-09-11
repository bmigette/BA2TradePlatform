"""The low-priority warm queue (spec section 6, lifecycle steps 3 and 4).

The queue runs INSIDE the live trading process, which is what most of these tests
are about:

* it must never hold a trading lock ("Background warmup must not hold an account
  submission lock or delay scheduled trading to finish");
* its empty-result sentinel must not leak into concurrent work ("process-global
  empty-sentinel flags must not leak into concurrent live work", spec section 9) --
  the warm threads use the THREAD-LOCAL override for exactly this;
* repeating an unchanged warm must download nothing at all, and two workers asking
  for the same payload must fetch it once.
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay import dependencies as dep
from ba2_providers import fmp_common
from app.services.warm import budget as warm_budget
from app.services.warm import planner, worker

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
WINDOW = dep.Window(start=NOW - timedelta(days=365), end=NOW)


def _req(namespace="price_target", symbol="AAPL"):
    return dep.Requirement(provider="fmp", namespace=namespace, symbol=symbol, window=WINDOW,
                           interval=None, kind=dep.KIND_HISTORY, optional=False, reason="t")


def _budget(allowance=10_000_000):
    return warm_budget.WarmBudget(allowance_bytes=allowance, unknown_reserve_bytes=1000)


class _Recorder:
    """A fetcher that records what it was asked for and how it was called."""

    def __init__(self, delay=0.0):
        self.calls = []
        self.frozen = []
        self.sentinel = []
        self.purpose = []
        self.lock = threading.Lock()
        self._delay = delay

    def __call__(self, requirement):
        if self._delay:
            import time
            time.sleep(self._delay)
        with self.lock:
            self.calls.append(requirement.key)
            self.frozen.append(fmp_common._is_ttl_frozen())
            self.sentinel.append(fmp_common._persist_empty_sentinel_enabled())
            self.purpose.append(fmp_common.current_fmp_purpose())


@pytest.fixture
def queue_factory():
    started = []

    def make(fetcher, workers=2, budget=None):
        q = worker.WarmQueue(workers=workers, budget=budget or _budget(), fetcher=fetcher)
        q.start()
        started.append(q)
        return q

    try:
        yield make
    finally:
        for q in started:
            q.stop(timeout=5.0)


# --------------------------------------------------------------------------- #
# Fetch context
# --------------------------------------------------------------------------- #
def test_each_worker_fetches_frozen_sentinel_scoped_and_tagged_as_warm(queue_factory):
    rec = _Recorder()
    q = queue_factory(rec)

    q.submit(_req())
    assert q.join(timeout=5.0)

    assert rec.calls == [_req().key]
    assert rec.frozen == [True], (
        "the freeze flag is what makes fmp_history_disk_cached WRITE; unfrozen, a warm "
        "fetches over the network and persists nothing")
    assert rec.sentinel == [True]
    assert rec.purpose == ["warm"], "bytes must be charged to the warm allowance, not to live"


def test_the_warm_sentinel_is_scoped_to_its_own_thread(queue_factory):
    """A concurrent frozen reader in the same process must keep live sentinel semantics."""
    started = threading.Event()
    release = threading.Event()

    def _slow(requirement):
        started.set()
        release.wait(5.0)

    q = queue_factory(_slow, workers=1)
    q.submit(_req())
    assert started.wait(5.0)
    try:
        assert fmp_common._persist_empty_sentinel_enabled() is False, (
            "the warm worker's sentinel must not reach this thread")
    finally:
        release.set()
    assert q.join(timeout=5.0)


def test_the_worker_never_touches_an_account_submission_lock(queue_factory):
    from ba2_common.core.interfaces.AccountInterface import AccountInterface

    before = dict(AccountInterface._submit_locks)
    q = queue_factory(_Recorder())

    q.submit(_req())
    assert q.join(timeout=5.0)

    assert dict(AccountInterface._submit_locks) == before


# --------------------------------------------------------------------------- #
# Dedupe and repeat runs
# --------------------------------------------------------------------------- #
def test_a_duplicated_requirement_is_fetched_once(queue_factory):
    rec = _Recorder(delay=0.05)
    q = queue_factory(rec, workers=2)

    first = q.submit(_req())
    second = q.submit(_req())
    assert q.join(timeout=5.0)

    assert first is True and second is False
    assert rec.calls == [_req().key], "two workers must share one fetch, not race for it"


def test_two_different_requirements_both_run(queue_factory):
    rec = _Recorder()
    q = queue_factory(rec, workers=2)

    q.submit(_req("price_target", "AAPL"))
    q.submit(_req("grades_historical", "AAPL"))
    assert q.join(timeout=5.0)

    assert sorted(rec.calls) == sorted([_req("price_target", "AAPL").key,
                                        _req("grades_historical", "AAPL").key])


def test_a_second_run_of_an_unchanged_plan_downloads_nothing(tmp_path, queue_factory):
    history = tmp_path / "fmp_history"
    history.mkdir(parents=True)
    (history / "price_target__AAPL.json").write_text('[{"t": 1}]', encoding="utf-8")
    rec = _Recorder()
    q = queue_factory(rec)

    p = planner.plan([_req()], [str(tmp_path)], as_of_now=NOW)
    enqueued = q.submit_plan(p)
    assert q.join(timeout=5.0)

    assert enqueued == [] and rec.calls == [], (
        "a present artifact is not work; repeating the warm must cost zero requests")


def test_resubmitting_a_plan_the_queue_already_completed_enqueues_nothing(tmp_path,
                                                                         queue_factory):
    rec = _Recorder()
    q = queue_factory(rec)
    p = planner.plan([_req()], [str(tmp_path)], as_of_now=NOW)

    assert q.submit_plan(p) == [_req().key]
    assert q.join(timeout=5.0)
    assert q.submit_plan(p) == []

    assert rec.calls == [_req().key]


def test_a_plan_entry_that_is_not_warmable_is_never_enqueued(tmp_path, queue_factory):
    state = dep.Requirement(provider="platform", namespace="closed_transactions", symbol=None,
                            window=WINDOW, interval=None, kind=dep.KIND_STATE, optional=False,
                            reason="cooldown")
    rec = _Recorder()
    q = queue_factory(rec)

    assert q.submit_plan(planner.plan([state], [str(tmp_path)], as_of_now=NOW)) == []


# --------------------------------------------------------------------------- #
# Budget interaction
# --------------------------------------------------------------------------- #
def test_running_out_of_allowance_pauses_with_a_gap_report_and_fetches_no_more(tmp_path,
                                                                              queue_factory):
    history = tmp_path / "fmp_history"
    history.mkdir(parents=True)
    (history / "price_target__SEED.json").write_text("x" * 400, encoding="utf-8")
    rec = _Recorder()

    def _spends_400(requirement):
        rec(requirement)
        # What the wire actually cost, counted the way fmp_http_get counts it. The
        # RESERVATION is released on settle; it is the MEASURED spend that consumes
        # the allowance, so this is the only honest way to exhaust it.
        fmp_common._record_fmp_request("price-target", 400)

    fmp_common.reset_purpose_stats()
    # One 400-byte item fits; the second cannot.
    q = queue_factory(_spends_400, workers=1,
                      budget=warm_budget.WarmBudget(allowance_bytes=500,
                                                    unknown_reserve_bytes=400))

    p = planner.plan([_req("price_target", "AAPL"), _req("price_target", "MSFT")],
                     [str(tmp_path)], as_of_now=NOW)
    q.submit_plan(p)
    assert q.join(timeout=5.0)

    assert len(rec.calls) == 1
    stats = q.stats()
    assert stats["paused"] is True
    assert stats["gap"]["shortfall_bytes"] > 0
    assert stats["gap"]["pending"] or stats["gap"]["shortfall_bytes"], (
        "the report must quantify what was NOT downloaded")
    fmp_common.reset_purpose_stats()


def test_a_failing_fetch_is_counted_and_does_not_stop_the_queue(queue_factory):
    calls = []

    def _boom(requirement):
        calls.append(requirement.key)
        if requirement.symbol == "AAPL":
            raise RuntimeError("one bad symbol")

    q = queue_factory(_boom, workers=1)
    q.submit(_req("price_target", "AAPL"))
    q.submit(_req("price_target", "MSFT"))
    assert q.join(timeout=5.0)

    assert len(calls) == 2
    assert q.stats()["failed"] == 1 and q.stats()["fetched"] == 1


def test_a_failed_item_releases_its_reservation(queue_factory):
    b = _budget(allowance=1000)

    def _boom(requirement):
        raise RuntimeError("nope")

    q = queue_factory(_boom, workers=1, budget=b)
    q.submit(_req(), estimated_bytes=900)
    assert q.join(timeout=5.0)

    assert b.reserved_bytes() == 0, "a failure must not strand the allowance"


# --------------------------------------------------------------------------- #
# The default fetcher writes through the real data layer
# --------------------------------------------------------------------------- #
def test_the_default_fetcher_writes_an_empty_sentinel_for_a_checked_empty_history(
        tmp_path, monkeypatch):
    """A symbol FMP genuinely has no data for must stop looking like a prewarm gap."""
    import ba2_common.config as common_config

    monkeypatch.setattr(common_config, "CACHE_FOLDER", str(tmp_path))
    fetched = []

    def _fake_history(symbol, requirement):
        """Shaped exactly like FMPRating.fetch_price_target_history_cached."""
        fetched.append(symbol)
        return fmp_common.fmp_history_disk_cached("price_target", symbol, lambda: [])

    fetcher = worker.DefaultWarmFetcher(
        ohlcv_provider="fmp", end_date=NOW, fmp_key=None, fred_key=None,
        history_fetchers={"price_target": _fake_history})
    with fmp_common.frozen_ttl_cache(), fmp_common.thread_persist_empty_sentinel(True):
        fetcher(_req())

    path = tmp_path / "fmp_history" / "price_target__AAPL.json"
    assert fetched == ["AAPL"]
    assert path.read_text(encoding="utf-8") == "[]"


def test_the_default_fetcher_refuses_a_namespace_it_has_no_fetcher_for():
    fetcher = worker.DefaultWarmFetcher(ohlcv_provider="fmp", end_date=NOW, fmp_key=None,
                                        fred_key=None, history_fetchers={})

    with pytest.raises(worker.WarmFetchError):
        fetcher(_req("not_a_namespace"))


def test_the_default_fetcher_refuses_platform_state():
    fetcher = worker.DefaultWarmFetcher(ohlcv_provider="fmp", end_date=NOW, fmp_key=None,
                                        fred_key=None, history_fetchers={})
    state = dep.Requirement(provider="platform", namespace="closed_transactions", symbol=None,
                            window=WINDOW, interval=None, kind=dep.KIND_STATE, optional=False,
                            reason="cooldown")

    with pytest.raises(worker.WarmFetchError):
        fetcher(state)
