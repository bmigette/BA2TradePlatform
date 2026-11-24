"""Where a page load spends its time: ONE ``[PERF:page:load]`` line per load, every step named.

The Account Growth tab and the Portfolio Allocation page each fetch several broker datasets, prices and then draw;
before this nothing recorded which of those a slow load was waiting on."""
import asyncio

import pytest

from ba2_trade_platform.core.portfolio_allocation import VALUATION_MODE_MARKET
from ba2_trade_platform.services import load_timing
from ba2_trade_platform.services.load_timing import LoadBreakdown
from tests.portfolio_golden import build_scenario
from tests.test_overview_cache_ttl import env  # noqa: F401  (fixture)
from tests.test_overview_review_fixes2 import nicegui_client  # noqa: F401  (fixture)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def perf_lines(monkeypatch):
    lines = []
    monkeypatch.setattr(load_timing.logger, "info", lambda msg, *a, **k: lines.append(msg))
    return lines


def test_steps_are_reported_in_order_with_their_time_and_note():
    clock = Clock()
    b = LoadBreakdown("AccountGrowth", "accounts=[2]", clock=clock)
    with b.step("dividends") as s:
        clock.t += 1.25
        s.note = "233 rows"
    with b.step("trades"):
        clock.t += 0.5
    clock.t += 0.25
    assert b.summary() == ("[PERF:page:load] AccountGrowth(accounts=[2]) - 2000ms "
                           "(dividends 1250ms [233 rows] | trades 500ms | untimed 250ms)")


def test_a_step_that_raises_is_still_timed_and_says_so():
    clock = Clock()
    b = LoadBreakdown("AccountGrowth", clock=clock)
    with pytest.raises(RuntimeError):
        with b.step("positions"):
            clock.t += 2
            raise RuntimeError("broker down")
    assert b.summary() == "[PERF:page:load] AccountGrowth - 2000ms (positions 2000ms [FAILED: RuntimeError] | untimed 0ms)"


def test_log_writes_the_summary_once_as_one_info_line(perf_lines):
    clock = Clock()
    b = LoadBreakdown("AccountGrowth", clock=clock)
    with b.step("prices"):
        clock.t += 1
    b.log()
    b.log()                    # a load that reaches two exits still reports once
    assert perf_lines == ["[PERF:page:load] AccountGrowth - 1000ms (prices 1000ms | untimed 0ms)"]


def test_a_load_nothing_was_timed_in_logs_nothing(perf_lines):
    LoadBreakdown("AccountGrowth", clock=Clock()).log()      # overtaken before its first step
    assert perf_lines == []


def test_a_caller_without_a_breakdown_times_nothing():
    with load_timing.step(None, "prices") as s:
        s.note = "ignored"


def test_the_row_count_note_never_raises():
    assert load_timing.rows_note([1, 2, 3]) == "3 rows"
    assert load_timing.rows_note(None) == "no answer"          # the brokers' "fetch failed" for positions
    assert load_timing.rows_note(iter(())) == "tuple_iterator"   # unsized: named, not a TypeError in the read


def test_the_allocator_page_refresh_is_timed_and_always_reported():
    """No test renders the whole page, so this reads its source: ``_refresh`` reports in a ``finally`` and the four
    waits of a refresh are steps."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "ba2_trade_platform" / "ui" / "pages"
           / "portfolio_allocation.py").read_text(encoding="utf-8")
    start = src.index("        async def _refresh() -> None:")
    wrapper = src[start:src.index("        async def _refresh_timed(timing) -> None:")]
    assert "LoadBreakdown('AllocatorPage'" in wrapper
    assert "await _refresh_timed(timing)" in wrapper and "finally:" in wrapper and "timing.log()" in wrapper
    body = src[src.index("        async def _refresh_timed(timing) -> None:"):]
    for name in ("payload", "render labels", "reminder", "income panel"):
        assert f"timing.step('{name}')" in body, name


def test_a_growth_load_logs_every_dataset_the_prices_the_maths_and_the_draw(env, perf_lines):  # noqa: F811
    async def go():
        await env.load(env.new_tab())
    asyncio.run(go())
    lines = [line for line in perf_lines if line.startswith("[PERF:page:load] AccountGrowth(")]
    assert len(lines) == 1, perf_lines
    line = lines[0]
    assert "accounts=[1]" in line and "range=YTD" in line
    for part in ("snapshot", "acct1 dividends", "acct1 trades", "acct1 positions", "acct1 balance", "prices",
                 "monthly realized", "value denominators", "scope inputs", "dividend forecast", "draw"):
        assert f"{part} " in line, (part, line)
    assert "compute " not in line              # the maths is four named steps now
    assert "snapshot " in line and "[none]" in line   # no stored snapshot: the load waited for the broker
    assert "[2 rows]" in line                 # the two filled trades the fake broker returned
    assert "[2 symbols, downloaded]" in line


def test_a_redraw_from_the_cache_logs_no_broker_step(env, perf_lines):  # noqa: F811
    async def go():
        tab = env.new_tab()
        await env.load(tab)
        tab._set_range('6m')
        await env.load(tab)
    asyncio.run(go())
    lines = [line for line in perf_lines if line.startswith("[PERF:page:load] AccountGrowth(")]
    assert len(lines) == 2, perf_lines
    assert "acct1 dividends" not in lines[1] and "acct1 trades" not in lines[1]
    assert "range=6m" in lines[1] and "draw " in lines[1]


def _use(monkeypatch):
    import ba2_trade_platform.core.utils as core_utils

    def install(account):
        monkeypatch.setattr(core_utils, "get_account_instance_from_id", lambda account_id: account)
    return install


def test_the_allocator_payload_logs_each_broker_read(monkeypatch, perf_lines):
    from ba2_trade_platform.services import portfolio as svc
    acct_id, _ = build_scenario(_use(monkeypatch))
    svc.load_view_payload(acct_id, VALUATION_MODE_MARKET)
    lines = [line for line in perf_lines if line.startswith("[PERF:page:load] AllocatorPayload(")]
    assert len(lines) == 1, perf_lines
    for part in ("positions", "protection", "prices", "symbol facts", "snapshot", "dividends", "label views"):
        assert f"{part} " in lines[0], (part, lines[0])
    assert f"account={acct_id}" in lines[0]


def test_the_allocator_income_panel_logs_its_broker_sync(monkeypatch, perf_lines):
    from ba2_trade_platform.services import portfolio as svc
    acct_id, _ = build_scenario(_use(monkeypatch))
    svc.load_income_panel(acct_id)
    lines = [line for line in perf_lines if line.startswith("[PERF:page:load] AllocatorIncome(")]
    assert len(lines) == 1, perf_lines
    assert "income sync " in lines[0] and "ledger read " in lines[0]
