"""Re-review fixes: outage breaker, socket timeouts, no_spot retry, 429 bucket drain, FRED key
hygiene, hook ordering / skips / placeholders, light gate scan, multi-day incremental replay."""
import inspect
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core import market_calendar
from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from ba2_trade_platform.modules.dataproviders.options import atm_iv_task_hook as K
from tests.atm_iv_fakes import (NOW, FakeBars, FakeLister, FakeRate, FakeSpots, FakeWorld, MultiBars,
                                MultiSpots, RateLimitError, make_provider, reset_module_state)

END = date(2026, 3, 6)


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    K.invalidate_gate_cache()
    yield
    reset_module_state()
    K.invalidate_gate_cache()


def _multi(tmp_path, syms, **kw):
    worlds = {s: FakeWorld(root=s) for s in syms}
    bars = {s: FakeBars(worlds[s]) for s in syms}
    lists = {s: FakeLister(worlds[s]) for s in syms}
    client = MultiBars(bars, lists)
    prov = H.AtmIvHistoryProvider(
        cache_dir=str(tmp_path / "AtmIvHistory"), bars_client=client, contract_lister=client,
        spot_source=MultiSpots({s: FakeSpots(worlds[s]) for s in syms}), rate_source=lambda a, b: FakeRate(),
        now=lambda: NOW, sleep=lambda x: None, bucket=H.TokenBucket(per_minute=1e9, burst=1e9),
        jitter=lambda: 0.0, **kw)
    return prov, worlds, bars, lists


# ---- F1: outage / breaker -------------------------------------------------------------------------
def test_blackholed_api_is_bounded_by_the_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REQUEST_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(H, "MAX_RETRIES", 1)
    monkeypatch.setattr(H, "BREAKER_SECONDS", 0.8)
    monkeypatch.setattr(H, "MAX_ABANDONED_CALLS", 99)
    msgs = {"w": [], "i": []}
    monkeypatch.setattr(H.logger, "warning", lambda m, *a, **k: msgs["w"].append(m))
    monkeypatch.setattr(H.logger, "info", lambda m, *a, **k: msgs["i"].append(m))
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
    prov, worlds, bars, lists = _multi(tmp_path, syms)
    gates = []
    for b in bars.values():
        b.gate = threading.Event()
        gates.append(b.gate)                                    # every bars call hangs
    t0 = time.monotonic()
    out = [prov.ensure_filled(s, END, 30, deadline_seconds=60) for s in syms]
    elapsed = time.monotonic() - t0
    assert all(r.status == H.STATUS_FILLING for r in out)
    assert elapsed < 6                                          # NOT 8 symbols x 7 attempts x 60 s
    calls_open = prov.api_calls
    again = prov.ensure_filled("AAA", END, 30)
    assert again.reason.startswith("API circuit open")
    assert prov.api_calls == calls_open                         # open: zero calls, instantly
    assert len([m for m in msgs["w"] if "circuit OPEN" in m]) == 1
    assert sum(1 for b in bars.values() if b.calls) == H.BREAKER_SYMBOLS
    time.sleep(0.9)                                             # breaker window passes
    for g in gates:
        g.set()
    res = prov.ensure_filled("HHH", END, 30, deadline_seconds=60)
    assert res.status == H.STATUS_COMPLETE
    assert len([m for m in msgs["i"] if "circuit CLOSED" in m]) == 1


def test_429_storm_also_opens_the_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "MAX_RETRIES", 1)
    prov, worlds, bars, lists = _multi(tmp_path, ["AAA", "BBB", "CCC", "DDD"])
    for b in bars.values():
        b.fail_always = True
    r = [prov.ensure_filled(s, END, 30) for s in ["AAA", "BBB", "CCC", "DDD"]]
    assert all(x.status == H.STATUS_FILLING for x in r)
    assert r[3].reason.startswith("API circuit open") and not bars["DDD"].calls


def test_a_successful_symbol_resets_the_failure_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "MAX_RETRIES", 0)
    prov, worlds, bars, lists = _multi(tmp_path, ["AAA", "BBB", "CCC", "DDD"])
    bars["AAA"].fail_always = bars["BBB"].fail_always = True
    prov.ensure_filled("AAA", END, 30)
    prov.ensure_filled("BBB", END, 30)
    assert prov.ensure_filled("CCC", END, 30).status == H.STATUS_COMPLETE       # resets the streak
    assert H._BREAKER.fails == 0 and not H._BREAKER.blocked()


# ---- F3: socket timeouts / abandoned calls ---------------------------------------------------------
def test_socket_timeout_is_injected_once_into_the_clients_session():
    seen = []

    class Sess:
        def request(self, *a, **k):
            seen.append(k.get("timeout"))
            return "ok"
    client = type("C", (), {})()
    client._session = Sess()
    H._apply_socket_timeout(client)
    H._apply_socket_timeout(client)                              # idempotent
    client._session.request("GET", "u")
    client._session.request("GET", "u", timeout=5)
    assert seen == [H.SOCKET_TIMEOUT, 5]


def test_new_calls_are_refused_while_too_many_abandoned_calls_are_outstanding(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "REQUEST_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(H, "MAX_RETRIES", 0)
    monkeypatch.setattr(H, "MAX_ABANDONED_CALLS", 1)
    monkeypatch.setattr(H, "BREAKER_SYMBOLS", 99)
    prov, world, bars, lister = make_provider(tmp_path)
    bars.gate = threading.Event()
    for _ in range(4):
        prov.ensure_filled("TEST", END, 30, deadline_seconds=30)
    assert len(bars.calls) <= 3                                  # refused instead of leaking more threads
    bars.gate.set()


# ---- F4: no_spot always retried -------------------------------------------------------------------
def test_no_spot_sessions_are_retried_on_every_later_pass_not_just_for_three_sessions(tmp_path):
    world = FakeWorld()
    hole = date(2026, 2, 26)
    world.no_spot_days = {hole}
    prov, world, bars, lister = make_provider(tmp_path, world)
    r = prov.ensure_filled("TEST", END, 60)
    assert hole not in r.values and prov.peek("TEST", END, 60).status == H.STATUS_COMPLETE
    world.no_spot_days = set()                                   # the spot source catches up
    prov._now = lambda: datetime(2026, 3, 20, 14, 0, tzinfo=timezone.utc)       # much later
    world.sessions = market_calendar.regular_session_dates(date(2025, 1, 2), date(2026, 3, 19))
    lister.today = date(2026, 3, 20)
    r2 = prov.ensure_filled("TEST", date(2026, 3, 19), 60)
    assert hole in r2.values


# ---- F6: a 429 drains the shared bucket ------------------------------------------------------------
def test_429_drains_the_bucket_for_every_thread(tmp_path):
    clock = [0.0]
    bucket = H.TokenBucket(per_minute=60, burst=5, clock=lambda: clock[0], sleep=lambda s: None)
    def sleep(sec):
        clock[0] += sec
    prov, world, bars, lister = make_provider(tmp_path, bucket=bucket, sleep=sleep)
    bars.fail_first = 1
    bars.fail_exc = lambda: RateLimitError(retry_after=30)
    seen = []
    orig = bucket.penalize
    bucket.penalize = lambda sec: (seen.append(sec), orig(sec))
    prov.ensure_filled("TEST", END, 30)
    assert seen and seen[0] == pytest.approx(30.0)                # Retry-After honoured globally
    bucket.penalize(30)
    assert bucket.try_acquire() > 20                              # another thread must wait ~30 s


# ---- F2: FRED key never reaches the log -------------------------------------------------------------
def test_fred_key_is_not_in_the_log_text_with_the_redaction_filter_off(tmp_path):
    import requests
    secret = "SYNTHETICFREDKEY0123456789abcdef"
    msgs, kws = [], []
    orig = K.logger.error
    K.logger.error = lambda m, *a, **k: (msgs.append(str(m)), kws.append(k))
    try:
        def refresher(sid, key):
            raise requests.exceptions.HTTPError(
                f"500 Server Error: for url: https://api.stlouisfed.org/fred/series/observations?"
                f"series_id=DGS3MO&api_key={key}&file_type=json")
        out = K.ensure_dgs3mo(only_if_stale=False, path=str(tmp_path / "x.json"),
                              key_resolver=lambda: secret, refresher=refresher)
    finally:
        K.logger.error = orig
    assert out == "failed" and msgs
    assert all(secret not in m for m in msgs) and "api_key=***" in msgs[0]
    assert not any(k.get("exc_info") for k in kws)               # no traceback with the URL either


# ---- F7/F9/F8 -----------------------------------------------------------------------------------------
def test_placeholder_symbols_return_before_any_db_or_api_work():
    def boom(*a, **k):
        raise AssertionError("must not be reached")
    assert K.ensure_for_analysis_task(1, "EXPERT", "enter_market", account_resolver=boom) is None
    assert K.ensure_for_analysis_task(1, "OPEN_POSITIONS", "enter_market", account_resolver=boom) is None


def test_the_gate_scan_is_light_and_never_resolves_expert_universes(monkeypatch, mock_account_def):
    from ba2_common.core import iv_rank_audit as audit
    from tests.factories import create_event_action, create_expert_instance, create_ruleset, link_rule_to_ruleset

    def heavy():
        raise AssertionError("heavy")
    monkeypatch.setattr(audit, "find_iv_rank_gates", heavy)
    assert K.has_iv_rank_gates() is False
    rs = create_ruleset(name="rs")
    ea = create_event_action(name="r", triggers={"trigger_0": {"event_type": "iv_rank", "operator": "<=", "value": 40.0}})
    link_rule_to_ruleset(rs.id, ea.id, order_index=0)
    create_expert_instance(account_id=mock_account_def.id, expert="MockExpert", enabled=True,
                           enter_market_ruleset_id=rs.id)
    assert K.has_iv_rank_gates() is True


def test_api_reload_invalidates_the_gate_cache():
    from ba2_trade_platform.ui import api_routes
    assert "invalidate_gate_cache" in inspect.getsource(api_routes)


# ---- _execute_task ordering and skips -------------------------------------------------------------
def _task_env(monkeypatch, mock_account_def, sufficient_balance):
    from ba2_trade_platform.core.WorkerQueue import AnalysisTask, WorkerQueue
    from tests.factories import create_expert_instance
    inst = create_expert_instance(account_id=mock_account_def.id, expert="MockExpert")
    order = []

    class FakeExpert:
        def has_sufficient_balance_for_entry(self):
            return sufficient_balance

        def should_skip_analysis_for_symbol(self, symbol):
            return False, None

        def run_analysis(self, symbol, market_analysis):
            order.append("run_analysis")
    monkeypatch.setattr("ba2_trade_platform.core.utils.get_expert_instance_from_id",
                        lambda i, use_cache=True: FakeExpert())
    monkeypatch.setattr(K, "ensure_for_analysis_task", lambda *a, **k: order.append("hook"))
    q = WorkerQueue.__new__(WorkerQueue)
    q._task_lock = threading.RLock()
    q._task_keys = {}
    q._tasks = {}
    q._update_persisted_task_status = lambda *a, **k: None
    q._check_and_process_expert_recommendations = lambda *a, **k: None
    task = AnalysisTask(id="t1", expert_instance_id=inst.id, symbol="AAPL")
    return q, task, order


def test_execute_task_fires_the_hook_before_run_analysis(monkeypatch, mock_account_def):
    q, task, order = _task_env(monkeypatch, mock_account_def, True)
    try:
        q._execute_task(task, "w")
    except AttributeError:
        pass          # post-run bookkeeping on the bare queue stub; the order is already recorded
    assert order[:2] == ["hook", "run_analysis"]


def test_execute_task_does_not_fire_the_hook_for_a_skipped_task(monkeypatch, mock_account_def):
    q, task, order = _task_env(monkeypatch, mock_account_def, False)
    try:
        q._execute_task(task, "w")
    except Exception:
        pass
    assert "hook" not in order and "run_analysis" not in order


# ---- multi-day incremental replay ---------------------------------------------------------------------
def test_a_week_of_sessions_fed_one_at_a_time_matches_one_cold_fill(tmp_path):
    days = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6)]
    inc, w1, b1, l1 = make_provider(tmp_path / "inc", FakeWorld(last_session=days[0]),
                                    now=datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc))
    inc.ensure_filled("TEST", days[0], 60)
    for d in days[1:]:
        w1.sessions = market_calendar.regular_session_dates(date(2025, 1, 2), d)
        nxt = market_calendar.regular_session_dates(d + timedelta(days=1), d + timedelta(days=5))[0]
        inc._now = lambda nxt=nxt: datetime(nxt.year, nxt.month, nxt.day, 14, 0, tzinfo=timezone.utc)
        l1.today = nxt
        calls = inc.api_calls
        r = inc.ensure_filled("TEST", d, 60)
        assert r.status == H.STATUS_COMPLETE and inc.api_calls - calls <= 4       # one session each day
    cold, w2, b2, l2 = make_provider(tmp_path / "cold", FakeWorld(last_session=days[-1]),
                                     now=datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc))
    cold.ensure_filled("TEST", days[-1], 60)
    a = inc.read_store("TEST").set_index("session_date")
    c = cold.read_store("TEST").set_index("session_date")
    common = a.index.intersection(c.index)
    assert len(common) >= 40
    assert (a.loc[common, "occ"] == c.loc[common, "occ"]).all()
    assert (abs(a.loc[common, "iv"] - c.loc[common, "iv"]) < 1e-12).all()
