"""The Portfolio Allocation page reads its dividend rows from the stored copy instead of waiting for the broker.

``get_dividends()`` (1.2 s on an 89-position TastyTrade account) only feeds the display-only "w/ div" P&L column. The
PAGE path (``read_only=False``, no ``dividends_source``) uses the stored ``"dividends"`` row when it is at most 24 h old
and says how old; the page's ``_refresh`` tops it up in a background thread when it is older than 15 minutes. The API
path is unchanged."""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.portfolio_allocation import VALUATION_MODE_MARKET
from ba2_trade_platform.services import growth_snapshot as gs
from ba2_trade_platform.services import load_timing
from ba2_trade_platform.services import portfolio as svc
from tests.portfolio_golden import build_scenario

UTC = timezone.utc
STORED = [{"symbol": "O", "amount": 99.0, "drip_quantity": None, "date": datetime(2026, 8, 1)}]


def _use(monkeypatch):
    import ba2_trade_platform.core.utils as core_utils

    def install(account):
        monkeypatch.setattr(core_utils, "get_account_instance_from_id", lambda account_id: account)
    return install


@pytest.fixture
def scenario(monkeypatch):
    return build_scenario(_use(monkeypatch))


@pytest.fixture
def perf_lines(monkeypatch):
    lines = []
    monkeypatch.setattr(load_timing.logger, "info", lambda msg, *a, **k: lines.append(msg))
    return lines


def _o_dividend_cash(payload):
    rows = [r for v in payload["views"] for r in v.rows if r.symbol == "O"]
    return rows[0].pnl.dividends


def test_a_stored_copy_younger_than_24h_is_used_without_asking_the_broker(scenario, perf_lines):
    acct_id, account = scenario
    at = datetime.now(UTC) - timedelta(minutes=5)
    gs.save_dividends(acct_id, STORED, at)
    payload = svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    assert account.dividend_calls == 0
    assert payload["dividends_as_of"] == at and payload["dividends_stale"] is False
    assert payload["dividend_payments"] == 1 and _o_dividend_cash(payload) == pytest.approx(99.0)
    line = next(x for x in perf_lines if x.startswith("[PERF:page:load] AllocatorPayload("))
    assert "dividends " in line and "[1 rows, stored 5 min old]" in line


def test_a_stored_copy_older_than_15_minutes_is_used_and_flagged_stale(scenario):
    acct_id, account = scenario
    gs.save_dividends(acct_id, STORED, datetime.now(UTC) - timedelta(hours=3))
    payload = svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    assert account.dividend_calls == 0 and payload["dividends_stale"] is True
    assert _o_dividend_cash(payload) == pytest.approx(99.0)


def test_with_no_stored_copy_the_broker_is_asked_and_the_answer_stored(scenario, perf_lines):
    acct_id, account = scenario
    payload = svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    assert account.dividend_calls == 1
    assert payload["dividends_as_of"] is None and payload["dividends_stale"] is True
    stored = gs.load_dividends(acct_id)
    assert stored.rows == account.dividends
    assert (datetime.now(UTC) - stored.fetched_at).total_seconds() < 60
    line = next(x for x in perf_lines if x.startswith("[PERF:page:load] AllocatorPayload("))
    assert "[2 rows]" in line                      # a live read: no "stored" in the note
    again = svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    assert account.dividend_calls == 1 and again["dividends_stale"] is False


def test_a_stored_copy_older_than_24h_is_not_used(scenario):
    acct_id, account = scenario
    gs.save_dividends(acct_id, STORED, datetime.now(UTC) - timedelta(hours=25))
    payload = svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    assert account.dividend_calls == 1 and payload["dividends_as_of"] is None and payload["dividends_stale"] is True
    assert _o_dividend_cash(payload) == pytest.approx(40.0)          # the broker's rows
    assert gs.load_dividends(acct_id).rows == account.dividends     # and stored


def test_a_failed_live_fetch_never_overwrites_a_stored_non_empty_copy(scenario):
    acct_id, account = scenario
    old = datetime.now(UTC) - timedelta(hours=25)
    gs.save_dividends(acct_id, STORED, old)
    account.dividends = []                                           # the lenient get_dividends' failure shape
    svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    stored = gs.load_dividends(acct_id)
    assert stored.rows == STORED and stored.fetched_at == old


def test_the_api_path_neither_reads_nor_writes_the_stored_copy(scenario):
    acct_id, account = scenario
    gs.save_dividends(acct_id, STORED, datetime.now(UTC) - timedelta(minutes=5))
    payload = svc.load_view_payload(acct_id, VALUATION_MODE_MARKET, read_only=True,
                                    dividends_source=lambda: list(account.dividends))
    assert _o_dividend_cash(payload) == pytest.approx(40.0)
    assert gs.load_dividends(acct_id).rows == STORED
    assert payload["dividends_as_of"] is None and payload["dividends_stale"] is False


def test_the_loader_starts_no_thread(scenario):
    import threading
    acct_id, _ = scenario
    before = set(threading.enumerate())
    svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    assert set(threading.enumerate()) - before == set()


# ---- the page's top-up ------------------------------------------------------------------------------------------------

def test_refresh_stored_dividends_fetches_and_stores(scenario):
    acct_id, account = scenario
    gs.save_dividends(acct_id, STORED, datetime.now(UTC) - timedelta(hours=3))
    svc.refresh_stored_dividends(acct_id)
    assert account.dividend_calls == 1
    stored = gs.load_dividends(acct_id)
    assert stored.rows == account.dividends and (datetime.now(UTC) - stored.fetched_at).total_seconds() < 60


def test_refresh_stored_dividends_never_overwrites_non_empty_rows_with_an_empty_answer(scenario):
    acct_id, account = scenario
    old = datetime.now(UTC) - timedelta(hours=3)
    gs.save_dividends(acct_id, STORED, old)
    account.dividends = []
    svc.refresh_stored_dividends(acct_id)
    stored = gs.load_dividends(acct_id)
    assert stored.rows == STORED and stored.fetched_at == old


def test_refresh_stored_dividends_stores_an_empty_answer_when_nothing_was_stored(scenario):
    acct_id, account = scenario
    account.dividends = []
    svc.refresh_stored_dividends(acct_id)
    assert gs.load_dividends(acct_id).rows == []


def test_refresh_stored_dividends_logs_and_returns_when_the_broker_raises(scenario, monkeypatch):
    acct_id, account = scenario
    warned = []
    monkeypatch.setattr(svc.logger, "warning", lambda msg, *a, **k: warned.append(str(msg)))

    def boom(*a, **k):
        raise RuntimeError("broker down")
    account.get_dividends = boom
    svc.refresh_stored_dividends(acct_id)                         # never raises: it runs in a daemon thread
    assert gs.load_dividends(acct_id) is None
    assert any("broker down" in w for w in warned), warned


def test_the_page_starts_the_top_up_thread_only_when_the_stored_copy_is_stale():
    """Reads the page's source, like ``test_load_timing`` does for the refresh's timing steps."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "ba2_trade_platform" / "ui" / "pages"
           / "portfolio_allocation.py").read_text(encoding="utf-8")
    body = src[src.index("        async def _refresh_timed(timing) -> None:"):]
    assert ("if payload.get('dividends_stale'):\n"
            "                portfolio_svc.start_dividends_refresh(account_id)") in body


def test_the_top_up_thread_is_a_named_daemon_running_refresh_stored_dividends_and_can_be_joined(scenario):
    acct_id, account = scenario
    thread = svc.start_dividends_refresh(acct_id)
    assert thread.daemon and thread.name == "dividends-refresh"
    svc.join_dividends_refresh(timeout=10)
    assert not thread.is_alive() and account.dividend_calls == 1
