"""The stored copy of an account's broker share movements (``services.broker_share_history``): stored in the growth
snapshot store (dataset ``share_movements``), synced incrementally (one small page) with a daily full read, merged
by transaction id with the overlap checked, one sync at a time per account, a history used only when its sync started
AFTER the positions it is used with, and a failure backoff.

DB through the in-memory test database (conftest); the broker is a fake returning classified rows. No network."""
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_trade_platform.services import broker_share_history as bsh
from ba2_trade_platform.services import growth_snapshot
from tests.factories import create_account_definition

pytestmark = pytest.mark.api_db          # the syncs run in threads: one shared DB connection

FIRST_DAY = date(2025, 12, 31)                         # the earliest reference session the heatmap needs
NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def row(rid, qty=1.0, when=datetime(2026, 11, 2, 15, tzinfo=timezone.utc), symbol="AAOI", kind="trade", price=30.0):
    return {"id": rid, "symbol": symbol, "underlying": symbol, "quantity": qty, "price": price, "executed_at": when,
            "kind": kind, "detail": "Trade / Buy to Open", "transaction_type": "Trade", "sub_type": "Buy to Open",
            "instrument_type": "Equity", "action": "Buy to Open", "reverses_id": None}


class Broker:
    """Answers ``read_share_movements`` from a list of rows: every row executed on/after the requested day."""

    def __init__(self, rows, fail=None, delay=0.0):
        self.rows, self.fail, self.delay = list(rows), fail, delay
        self.reads = []
        self.in_flight = 0
        self.max_in_flight = 0

    def read_share_movements(self, start_date, *, stop=None):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.reads.append(start_date)
            if self.delay:
                time.sleep(self.delay)
            if self.fail is not None:
                raise self.fail
            day0 = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
            rows = [dict(r) for r in self.rows if r["executed_at"] >= day0 - timedelta(hours=12)]
            return {"rows": rows, "pages": 1, "total_items": len(rows), "requested_from": start_date}
        finally:
            self.in_flight -= 1


@pytest.fixture
def account():
    bsh.clear()
    yield create_account_definition(name="Tasty")
    bsh.clear()


# ---- the sync rule ----------------------------------------------------------------------------------------------

def test_the_first_sync_is_a_full_read_and_the_store_survives_a_restart(account):
    broker = Broker([row(1), row(2, qty=-0.5)])
    store = bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    assert broker.reads == [FIRST_DAY - timedelta(days=3)]
    assert store.covered_from == datetime(2025, 12, 30, tzinfo=timezone.utc)    # requested day + 2, 00:00 UTC
    assert store.full_read_at == NOW and store.synced_at == NOW
    bsh.clear()                                                              # "a new process"
    again = bsh.load_store(account.id)
    assert [r["id"] for r in again.rows] == [1, 2] and again.rows[1]["quantity"] == -0.5
    assert again.rows[0]["executed_at"] == datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    assert again.covered_from == store.covered_from


def test_a_later_sync_reads_one_small_window_and_merges_new_rows(account):
    broker = Broker([row(1), row(2, when=datetime(2026, 11, 20, 15, tzinfo=timezone.utc))])
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    broker.rows.append(row(3, when=datetime(2026, 11, 24, 15, 5, tzinfo=timezone.utc)))
    later = NOW + timedelta(hours=1)
    store = bsh.sync(account.id, broker, FIRST_DAY, now=later)
    assert broker.reads[1] == date(2026, 11, 17)                       # last synced New York day - 7
    assert sorted(r["id"] for r in store.rows) == [1, 2, 3]
    assert store.full_read_at == NOW and store.synced_at == later


def test_a_stored_row_missing_from_the_overlap_forces_a_full_re_read(account):
    broker = Broker([row(1), row(2, when=datetime(2026, 11, 20, 15, tzinfo=timezone.utc))])
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    broker.rows = [row(1)]                                             # row 2 vanished inside the overlap window
    store = bsh.sync(account.id, broker, FIRST_DAY, now=NOW + timedelta(hours=1))
    assert broker.reads[1:] == [date(2026, 11, 17), FIRST_DAY - timedelta(days=3)]
    assert [r["id"] for r in store.rows] == [1]


def test_a_changed_row_in_the_overlap_forces_a_full_re_read(account):
    broker = Broker([row(1), row(2, when=datetime(2026, 11, 20, 15, tzinfo=timezone.utc))])
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    broker.rows[1] = row(2, qty=5.0, when=datetime(2026, 11, 20, 15, tzinfo=timezone.utc))
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW + timedelta(hours=1))
    assert len(broker.reads) == 3


def test_a_full_re_read_happens_once_a_day(account):
    broker = Broker([row(1)])
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW + timedelta(hours=25))
    assert broker.reads[1] == FIRST_DAY - timedelta(days=3)


def test_a_failed_read_stores_nothing(account):
    broker = Broker([row(1)])
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    with pytest.raises(ValueError):
        bsh.sync(account.id, Broker([row(1), row(2)], fail=ValueError("incomplete")), FIRST_DAY,
                 now=NOW + timedelta(hours=1))
    assert [r["id"] for r in bsh.load_store(account.id).rows] == [1]
    assert bsh.load_store(account.id).synced_at == NOW


def test_the_store_lives_in_the_growth_snapshot_table_as_its_own_dataset(account):
    bsh.sync(account.id, Broker([row(1)]), FIRST_DAY, now=NOW)
    assert growth_snapshot.load_share_movements(account.id) is not None
    assert growth_snapshot.load_growth_snapshot(account.id) is None


# ---- a history newer than the positions; one sync at a time ------------------------------------------------------

def test_a_load_whose_positions_came_after_a_running_sync_gets_the_next_sync(account):
    broker = Broker([row(1)], delay=0.3)
    early = bsh.next_seq()                                   # a load's positions read, BEFORE the sync started
    first = bsh.request_sync(account.id, broker, FIRST_DAY, early)
    assert bsh.request_sync(account.id, broker, FIRST_DAY, early) is first
    late = bsh.next_seq()                                    # another load's positions read, AFTER it started
    queued = bsh.request_sync(account.id, broker, FIRST_DAY, late)
    assert queued is not first
    assert bsh.request_sync(account.id, broker, FIRST_DAY, late) is queued
    assert bsh.wait_for_store(first, 5.0)[1] is None
    store, why = bsh.wait_for_store(queued, 5.0)
    assert why is None and store is not None
    assert len(broker.reads) == 2 and broker.max_in_flight == 1


def test_a_load_does_not_wait_past_its_deadline_and_the_sync_still_lands(account):
    broker = Broker([row(1)], delay=0.5)
    future = bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq())
    store, why = bsh.wait_for_store(future, 0.05)
    assert store is None and why == "broker history still loading, reload"
    store, why = bsh.wait_for_store(future, 5.0)
    assert why is None and bsh.load_store(account.id) is not None


def test_clear_does_not_let_a_second_sync_start_while_one_runs(account):
    broker = Broker([row(1)], delay=0.3)
    seq = bsh.next_seq()
    first = bsh.request_sync(account.id, broker, FIRST_DAY, seq)
    bsh.clear()
    assert bsh.request_sync(account.id, broker, FIRST_DAY, seq) is first
    bsh.wait_for_store(first, 5.0)
    assert len(broker.reads) == 1


# ---- failures: logged, remembered, backed off ---------------------------------------------------------------------

def _logged(monkeypatch):
    lines = []
    monkeypatch.setattr(bsh.logger, "warning", lambda msg, *a, **k: lines.append(msg))
    return lines


def test_a_failed_sync_is_logged_remembered_and_not_retried_during_the_backoff(account, monkeypatch):
    lines = _logged(monkeypatch)
    broker = Broker([row(1)], fail=ConnectionError("GET /accounts/5WX00000/transactions failed"))
    future = bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq())
    for _ in range(50):                                      # nobody waits: the sync thread itself must log it
        if future.done():
            break
        time.sleep(0.02)
    assert any("ConnectionError" in line and "5WX00000" not in line for line in lines)
    again = bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq())
    store, why = bsh.wait_for_store(again, 1.0)
    assert store is None and "ConnectionError" in why and "retrying after" in why
    assert len(broker.reads) == 1
    until, _reason = bsh.failure_of(account.id)
    assert 290 <= (until - datetime.now(timezone.utc)).total_seconds() <= bsh.BACKOFF_S


def test_the_page_cap_backs_off_longer(account):
    class PageCap(ValueError):
        heatmap_kind = "page_cap"

    future = bsh.request_sync(account.id, Broker([], fail=PageCap("more than 40 pages")), FIRST_DAY, bsh.next_seq())
    store, why = bsh.wait_for_store(future, 2.0)
    assert store is None and "40 pages" in why
    until, _reason = bsh.failure_of(account.id)
    assert (until - datetime.now(timezone.utc)).total_seconds() > bsh.BACKOFF_S


def test_a_page_timeout_is_named_as_such(account):
    class PageTimeout(RuntimeError):
        heatmap_kind = "page_timeout"

    future = bsh.request_sync(account.id, Broker([], fail=PageTimeout("page 3: TimeoutError")), FIRST_DAY,
                              bsh.next_seq())
    store, why = bsh.wait_for_store(future, 2.0)
    assert "did not answer within" in why and "page" in why


def test_the_backoff_ends(account, monkeypatch):
    monkeypatch.setattr(bsh, "BACKOFF_S", 0.2)
    broker = Broker([row(1)], fail=ConnectionError("down"))
    bsh.wait_for_store(bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq()), 2.0)
    time.sleep(0.3)
    broker.fail = None
    store, why = bsh.wait_for_store(bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq()), 2.0)
    assert why is None and store is not None and len(broker.reads) == 2


def test_a_save_failure_is_a_sync_failure(account, monkeypatch):
    def broken(*a, **k):
        raise TypeError("cannot store 'x'")

    monkeypatch.setattr(growth_snapshot, "save_share_movements", broken)
    broker = Broker([row(1)])
    store, why = bsh.wait_for_store(bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq()), 2.0)
    assert store is None and "TypeError" in why
    bsh.wait_for_store(bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq()), 1.0)
    assert len(broker.reads) == 1                               # not retried on every load


def test_a_sync_is_bounded_by_its_own_stop(account, monkeypatch):
    monkeypatch.setattr(bsh, "SYNC_MAX_S", 0.2)

    class Stopped(TimeoutError):
        heatmap_kind = "stopped"

    class Slow(Broker):
        def read_share_movements(self, start_date, *, stop=None):
            stop.wait(2)
            raise Stopped("stopped")

    store, why = bsh.wait_for_store(bsh.request_sync(account.id, Slow([]), FIRST_DAY, bsh.next_seq()), 3.0)
    assert store is None and "did not finish within" in why
    assert bsh.load_store(account.id) is None


# ---- the daily full re-read failing does not block the incremental path ------------------------------------------

class FullFails(Broker):
    """A broker whose FULL read (from the first day - 3) fails once ``failing`` is set; incremental reads work."""

    failing = False
    error = ConnectionError("the full read broke")

    def read_share_movements(self, start_date, *, stop=None):
        if self.failing and start_date == FIRST_DAY - timedelta(days=3):
            self.reads.append(start_date)
            raise self.error
        return super().read_share_movements(start_date, stop=stop)


def test_a_failed_daily_full_read_falls_back_to_the_incremental_sync(account, monkeypatch):
    lines = _logged(monkeypatch)
    broker = FullFails([row(1)])
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    broker.failing = True
    broker.rows.append(row(2, when=datetime(2026, 11, 25, 15, tzinfo=timezone.utc)))
    later = NOW + timedelta(hours=25)
    store = bsh.sync(account.id, broker, FIRST_DAY, now=later)
    full = FIRST_DAY - timedelta(days=3)
    assert broker.reads == [full, full, date(2026, 11, 17)]          # full (failed), then the incremental window
    assert sorted(r["id"] for r in store.rows) == [1, 2] and store.synced_at == later
    assert store.full_read_at == NOW                                  # still due
    assert any("full re-read" in line and "ConnectionError" in line for line in lines)
    bsh.sync(account.id, broker, FIRST_DAY, now=later + timedelta(minutes=4))
    assert broker.reads[3:] == [date(2026, 11, 18)]                   # inside the backoff: no second full attempt
    bsh.sync(account.id, broker, FIRST_DAY, now=later + timedelta(minutes=6))
    assert broker.reads[4] == full                                    # after it: tried again


def test_a_full_read_that_hits_the_page_cap_backs_off_longer(account):
    class PageCap(ValueError):
        heatmap_kind = "page_cap"

    broker = FullFails([row(1)])
    bsh.sync(account.id, broker, FIRST_DAY, now=NOW)
    broker.failing, broker.error = True, PageCap("more than 40 pages")
    later = NOW + timedelta(hours=25)
    bsh.sync(account.id, broker, FIRST_DAY, now=later)
    bsh.sync(account.id, broker, FIRST_DAY, now=later + timedelta(minutes=20))
    assert broker.reads.count(FIRST_DAY - timedelta(days=3)) == 2     # the first full + the one failed attempt


def test_without_a_store_a_failed_full_read_is_n_a_with_the_reason(account):
    broker = FullFails([row(1)])
    broker.failing = True
    store, why = bsh.wait_for_store(bsh.request_sync(account.id, broker, FIRST_DAY, bsh.next_seq()), 2.0)
    assert store is None and "ConnectionError" in why and "full read broke" in why
