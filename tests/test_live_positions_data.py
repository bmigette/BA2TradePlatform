"""Live Trades "Live View" tab: the loader (one ``get_positions`` per account in scope, in parallel, each under a
deadline; instrument labels by symbol; per-account status). Accounts are fakes, the DB is the in-memory test DB."""
import logging
import threading

import pytest

from ba2_trade_platform.core.utils import add_label_to_instruments
from ba2_trade_platform.services import account_fanout
from ba2_trade_platform.services import live_positions as svc
from ba2_trade_platform.ui.utils import live_positions as lp
from tests.factories import create_account_definition


class Broker:
    def __init__(self, positions, raises=None, hang=None):
        self.positions = positions
        self.raises = raises
        self.hang = hang
        self.reads = 0

    def get_positions(self):
        self.reads += 1
        if self.hang is not None:
            self.hang.wait(10)
        if self.raises is not None:
            raise self.raises
        return self.positions


def _pos(symbol, pl_day=1.0, **kw):
    row = {"symbol": symbol, "qty": 10.0, "current_price": 110.0, "avg_entry_price": 100.0, "cost_basis": 1000.0,
           "market_value": 1100.0, "unrealized_pl": 100.0, "unrealized_plpc": 0.1, "change_today": 0.01,
           "unrealized_intraday_pl": pl_day, "unrealized_intraday_plpc": 0.001, "asset_class": "us_equity"}
    row.update(kw)
    return row


@pytest.fixture
def world(monkeypatch):
    svc.clear_state()
    a1 = create_account_definition(name="One")
    a2 = create_account_definition(name="Two")
    brokers = {a1.id: Broker([_pos("AAPL"), _pos("MSFT", pl_day=None)]), a2.id: Broker([_pos("XOM")])}
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: brokers.get(i))
    yield a1, a2, brokers
    for b in brokers.values():
        if b.hang is not None:
            b.hang.set()
    svc.clear_state()


def _by_account(load):
    return {r.account_id: r for r in load.reads}


def test_all_accounts_are_read_once_each(world):
    a1, a2, brokers = world
    load = svc.load_live_positions(None)
    reads = _by_account(load)
    assert set(reads) == {a1.id, a2.id}
    assert reads[a1.id].status == lp.STATUS_OK and reads[a1.id].name == "One"
    assert [r.symbol for r in reads[a1.id].rows] == ["AAPL", "MSFT"]
    assert [r.symbol for r in reads[a2.id].rows] == ["XOM"]
    assert brokers[a1.id].reads == 1 and brokers[a2.id].reads == 1
    assert load.calls["positions"] == 2


def test_one_account_reads_only_that_account(world):
    a1, a2, brokers = world
    load = svc.load_live_positions(a2.id)
    assert [r.account_id for r in load.reads] == [a2.id]
    assert brokers[a1.id].reads == 0 and brokers[a2.id].reads == 1


def test_a_missing_number_stays_none(world):
    a1, *_ = world
    rows = {r.symbol: r for r in _by_account(svc.load_live_positions(a1.id))[a1.id].rows}
    assert rows["MSFT"].pl_day is None and rows["AAPL"].pl_day == 1.0


def test_a_read_that_returns_none_is_failed_with_no_rows(world):
    a1, a2, brokers = world
    brokers[a2.id].positions = None
    reads = _by_account(svc.load_live_positions(None))
    assert reads[a2.id].status == lp.STATUS_FAILED and reads[a2.id].rows == ()
    assert "failed" in reads[a2.id].reason
    assert reads[a1.id].status == lp.STATUS_OK


def test_a_read_that_raises_is_failed_and_names_only_the_error_class(world):
    a1, a2, brokers = world
    brokers[a2.id].raises = ConnectionError("secret-token-in-message")
    reads = _by_account(svc.load_live_positions(None))
    assert reads[a2.id].status == lp.STATUS_FAILED and reads[a2.id].rows == ()
    assert "ConnectionError" in reads[a2.id].reason and "secret" not in reads[a2.id].reason


def test_an_account_that_cannot_be_instantiated_is_failed(world, monkeypatch):
    a1, a2, brokers = world
    del brokers[a2.id]
    reads = _by_account(svc.load_live_positions(None))
    assert reads[a2.id].status == lp.STATUS_FAILED and reads[a2.id].rows == ()


def test_a_flat_account_is_ok_and_empty(world):
    a1, a2, brokers = world
    brokers[a2.id].positions = []
    reads = _by_account(svc.load_live_positions(None))
    assert reads[a2.id].status == lp.STATUS_OK and reads[a2.id].rows == ()


def test_a_hung_account_times_out_and_the_others_still_load(world, monkeypatch):
    a1, a2, brokers = world
    monkeypatch.setattr(svc, "BROKER_DEADLINE_S", 0.3)
    brokers[a2.id].hang = threading.Event()
    reads = _by_account(svc.load_live_positions(None))
    assert reads[a2.id].status == lp.STATUS_TIMED_OUT and "0.3s" in reads[a2.id].reason
    assert reads[a1.id].status == lp.STATUS_OK and len(reads[a1.id].rows) == 2


def test_a_hung_read_is_not_started_again_while_it_is_still_running(world, monkeypatch):
    a1, a2, brokers = world
    monkeypatch.setattr(svc, "BROKER_DEADLINE_S", 0.3)
    brokers[a2.id].hang = threading.Event()
    svc.load_live_positions(None)
    reads = _by_account(svc.load_live_positions(None))
    assert brokers[a2.id].reads == 1                 # the second load did not pile a second thread on it
    assert reads[a2.id].status == lp.STATUS_TIMED_OUT and "previous read" in reads[a2.id].reason
    brokers[a2.id].hang.set()
    brokers[a2.id].hang = None
    for _ in range(50):
        if not svc._still_running(a2.id):
            break
        threading.Event().wait(0.02)
    reads = _by_account(svc.load_live_positions(None))
    assert reads[a2.id].status == lp.STATUS_OK and brokers[a2.id].reads == 2


def test_labels_are_attached_by_normalised_symbol(world):
    a1, a2, brokers = world
    add_label_to_instruments(["AAPL"], "CORE")
    brokers[a2.id].positions = [_pos("xom ")]
    add_label_to_instruments(["XOM"], "ENERGY")
    reads = _by_account(svc.load_live_positions(None))
    rows = {r.symbol: r for r in reads[a1.id].rows}
    assert rows["AAPL"].labels == ("CORE",) and rows["MSFT"].labels == ()
    assert reads[a2.id].rows[0].labels == ("ENERGY",)


def test_a_position_without_a_symbol_is_skipped(world):
    a1, a2, brokers = world
    brokers[a1.id].positions = [_pos("AAPL"), _pos(None)]
    reads = _by_account(svc.load_live_positions(a1.id))
    assert [r.symbol for r in reads[a1.id].rows] == ["AAPL"]


def test_the_timing_line_is_info_on_the_first_load_and_debug_after(world, monkeypatch):
    seen = []

    class Recorder:
        def info(self, msg, *a, **k):
            seen.append(("info", msg))

        def debug(self, msg, *a, **k):
            seen.append(("debug", msg))

        def warning(self, msg, *a, **k):
            seen.append(("warning", msg))

    monkeypatch.setattr(svc, "logger", Recorder())
    svc.load_live_positions(None, first_load=True)
    svc.load_live_positions(None)
    perf = [(level, msg) for level, msg in seen if "[PERF:page:load]" in msg]
    assert [level for level, _ in perf] == ["info", "debug"]
    assert "LiveView" in perf[0][1] and "brokers" in perf[0][1]


def test_the_heatmap_and_the_live_view_share_the_scope_and_deadline():
    from ba2_trade_platform.services import position_heatmap as hm_svc
    assert hm_svc._accounts_in_scope is account_fanout.accounts_in_scope
    assert svc.BROKER_DEADLINE_S == hm_svc.BROKER_DEADLINE_S == account_fanout.BROKER_DEADLINE_S


def test_fan_out_reports_ok_failed_and_timed_out():
    gate = threading.Event()

    def work(aid):
        if aid == 2:
            raise ValueError("no")
        if aid == 3:
            gate.wait(5)
        return aid * 10

    try:
        outcomes, alive = account_fanout.fan_out([1, 2, 3], work, 0.3, "test-acct")
    finally:
        gate.set()
    assert outcomes[1].status == account_fanout.OK and outcomes[1].value == 10
    assert outcomes[2].status == account_fanout.FAILED and isinstance(outcomes[2].error, ValueError)
    assert outcomes[3].status == account_fanout.TIMED_OUT and set(alive) == {3}
