"""Connection lifecycle and threading of IBKRAccount (design doc s3), against FakeIB.

Pins: IB I/O lives on the account's private loop thread; a facade call cannot deadlock (guard) or
wait unboundedly (timeouts); the UI loop keeps ticking while IBKR is slow; lazy connect; reconnect
after a drop; cooldown instead of a connect storm; client-id collisions are reported and never
worked around; paper/live and account-id guards; one shared connection per account definition."""
import asyncio
import threading
import time

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
from ba2_trade_platform.modules.accounts.ibkr_runtime import (
    IBKRConnectionError, IBKRRuntime, shutdown_all_runtimes)
from tests.factories import create_account_definition
from tests.ibkr_fakes import FakeIB
from tests.ibkr_helpers import ACCOUNT_ID, DEFAULT_SETTINGS, ibkr_logs, make_account, write_settings  # noqa: F401


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    fake.add_stock("AAPL", 265598)
    yield account, fake
    account.close()
    shutdown_all_runtimes()


class TestThreading:
    def test_ib_calls_run_on_the_private_loop_thread_not_the_callers(self, world):
        account, fake = world
        account.get_account_snapshot()
        rt = account._runtime()
        assert fake.call_threads["accountSummaryAsync"] == rt.loop_thread.ident
        assert fake.call_threads["accountSummaryAsync"] != threading.get_ident()
        assert fake.call_threads["connectAsync"] == rt.loop_thread.ident
        assert rt.loop_thread.daemon is True

    def test_a_facade_call_from_the_loop_thread_raises_instead_of_deadlocking(self, world):
        account, fake = world
        rt = account._runtime()

        def inner(ib):
            try:
                rt.call(lambda ib2: 1, timeout=1.0, op="nested")
            except RuntimeError as e:
                return str(e)
            return "no error"

        assert "deadlock" in account._call(inner, op="outer")

    def test_a_slow_ibkr_call_times_out_boundedly_and_the_loop_survives(self, world, monkeypatch):
        account, fake = world
        monkeypatch.setattr(IBKRAccount, "_READ_TIMEOUT", 0.3)
        fake.block_calls["accountSummaryAsync"] = 5.0
        started = time.monotonic()
        assert account.get_balance() is None                       # logged, not raised
        assert time.monotonic() - started < 2.0
        fake.block_calls.clear()
        assert account.get_balance() == 100000.0                    # the loop is still healthy

    def test_timeout_names_the_account_and_the_operation(self, world, monkeypatch):
        account, fake = world
        account.get_positions()                                      # connected: no connect budget
        fake.block_calls["accountSummaryAsync"] = 5.0
        with pytest.raises(TimeoutError, match=r"IBKR account \d+.*account info"):
            account._runtime().call(lambda ib: account._account_numbers(ib), timeout=0.2,
                                    op="account info")

    def test_a_slow_call_does_not_block_another_threads_call(self, world, monkeypatch):
        account, fake = world
        account.get_positions()                                      # connect first
        fake.block_calls["accountSummaryAsync"] = 0.6
        done = {}

        def slow():
            account.get_account_snapshot()
            done["slow"] = time.monotonic()

        t = threading.Thread(target=slow)
        start = time.monotonic()
        t.start()
        time.sleep(0.05)
        assert account.get_positions() == []
        done["fast"] = time.monotonic()
        t.join()
        assert done["fast"] - start < done["slow"] - start

    def test_the_ui_loop_keeps_ticking_while_ibkr_is_slow(self, world):
        """The NiceGUI pattern: await run.io_bound(account.method). IB I/O is on the account's own
        thread, so the UI loop is never the one waiting."""
        account, fake = world
        account.get_positions()
        fake.block_calls["accountSummaryAsync"] = 0.5
        ticks = []

        async def main():
            loop = asyncio.get_running_loop()

            async def heartbeat():
                while True:
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.02)

            hb = asyncio.create_task(heartbeat())
            await loop.run_in_executor(None, account.get_balance)
            hb.cancel()

        asyncio.run(main())
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert len(ticks) > 15 and max(gaps) < 0.25

    def test_a_call_made_on_a_running_loop_thread_is_logged_with_its_call_site(self, world, ibkr_logs):
        account, fake = world

        async def on_the_ui_thread():
            return account.get_balance()          # blocking facade call on a loop thread

        assert asyncio.run(on_the_ui_thread()) == 100000.0
        assert "blocking a thread that is running an asyncio loop" in ibkr_logs.text()

    def test_info_codes_are_not_warnings_but_lost_connectivity_is(self, world, ibkr_logs):
        account, fake = world
        account.get_positions()
        fake.simulate_error(-1, 2104, "Market data farm connection is OK:usfarm")
        fake.simulate_error(-1, 2158, "Sec-def data farm connection is OK")
        assert "2104" not in ibkr_logs.text() and "2158" not in ibkr_logs.text()
        fake.simulate_error(-1, 1100, "Connectivity between IB and Trader Workstation has been lost")
        assert "1100" in ibkr_logs.text()


class TestConnection:
    def test_construction_does_not_connect(self, monkeypatch):
        account, fake = make_account(monkeypatch)
        try:
            assert fake.connect_calls == []
            account.get_positions()
            assert len(fake.connect_calls) == 1
        finally:
            account.close()

    def test_gateway_down_at_construction_is_not_fatal(self, monkeypatch):
        fake = FakeIB()
        fake.connect_failure = ConnectionRefusedError("refused")
        account, _ = make_account(monkeypatch, fake)
        try:
            assert account.get_positions() is None
            fake.connect_failure = None
            time.sleep(0.3)                                       # past the cooldown
            assert account.get_positions() == []
        finally:
            account.close()

    def test_connect_arguments_and_market_data_type(self, world):
        account, fake = world
        account.get_positions()
        call = fake.connect_calls[0]
        assert call == {"host": "127.0.0.1", "port": 4002, "clientId": 7, "readonly": False,
                        "account": ACCOUNT_ID}
        assert fake.market_data_type == 2                          # frozen/live, never delayed

    def test_cooldown_stops_a_connect_storm_then_it_retries(self, world):
        account, fake = world
        fake.connect_failure = ConnectionRefusedError("refused")
        for _ in range(5):
            assert account.get_positions() is None
        assert len(fake.connect_calls) == 1                        # four calls inside the cooldown
        time.sleep(0.3)
        assert account.get_positions() is None
        assert len(fake.connect_calls) == 2
        fake.connect_failure = None
        time.sleep(0.3)
        assert account.get_positions() == []

    def test_reconnects_after_the_gateway_drops_the_session(self, world):
        account, fake = world
        assert account.get_positions() == []
        fake.simulate_disconnect()
        assert account.get_positions() == []                       # transparently reconnected
        assert len(fake.connect_calls) == 2

    def test_client_id_collision_is_reported_and_never_worked_around(self, world, ibkr_logs):
        account, fake = world
        fake.client_id_in_use = True
        assert account.get_positions() is None
        text = ibkr_logs.text()
        assert "326" in text and "choose a different client_id" in text
        time.sleep(0.3)
        account.get_positions()
        assert {c["clientId"] for c in fake.connect_calls} == {7}   # never auto-incremented

    def test_account_not_managed_by_the_login_is_refused(self, monkeypatch, ibkr_logs):
        fake = FakeIB(account=ACCOUNT_ID, managed=["DU7777777"])
        account, _ = make_account(monkeypatch, fake)
        try:
            assert account.get_positions() is None
            assert "not among the accounts" in ibkr_logs.text()
        finally:
            account.close()

    def test_paper_flag_on_a_live_account_is_refused(self, monkeypatch, ibkr_logs):
        fake = FakeIB(account="U7654321")
        account, _ = make_account(monkeypatch, fake, account_id="U7654321", paper_account=True)
        try:
            assert account.get_positions() is None
            assert "paper_account=True but account 'U7654321' is a LIVE account" in ibkr_logs.text()
            assert fake.isConnected() is False                     # it hung up
        finally:
            account.close()

    def test_live_flag_on_a_paper_account_is_refused(self, monkeypatch, ibkr_logs):
        account, fake = make_account(monkeypatch, paper_account=False)
        try:
            assert account.get_positions() is None
            assert "PAPER" in ibkr_logs.text()
        finally:
            account.close()

    def test_a_live_account_with_the_flag_unset_is_allowed_when_it_matches(self, monkeypatch):
        fake = FakeIB(account="U7654321")
        account, _ = make_account(monkeypatch, fake, account_id="U7654321", paper_account=False)
        try:
            assert account.get_positions() == []
        finally:
            account.close()

    def test_lost_ib_connectivity_probes_before_trusting_the_socket(self, world):
        account, fake = world
        account.get_positions()
        fake.simulate_error(-1, 1100, "Connectivity between IB and TWS has been lost")
        fake.fail_calls["reqCurrentTimeAsync"] = ConnectionError("no route")
        assert account.get_positions() is None
        fake.fail_calls.clear()
        assert account.get_positions() == []                      # probe succeeded -> trusted again
        fake.simulate_error(-1, 1100, "lost again")
        fake.simulate_error(-1, 1102, "Connectivity restored - data maintained")
        assert account.get_positions() == []

    def test_order_errors_are_recorded_per_request_and_chatter_is_excluded(self, world):
        account, fake = world
        account.get_positions()
        fake.simulate_error(321, 399, "Order Message: warning text")
        fake.simulate_error(321, 201, "Order rejected")
        fake.simulate_error(321, 354, "No market data subscription")
        assert account._runtime().order_errors(321) == [(201, "Order rejected")]


class TestSharedRuntime:
    def test_every_account_object_of_one_definition_shares_one_connection(self, world):
        account, fake = world
        other = IBKRAccount(account.id)
        assert other._runtime() is account._runtime()
        account.get_positions()
        other.get_positions()
        assert len(fake.connect_calls) == 1                        # not two sessions on one client id

    def test_dropping_an_account_object_does_not_disconnect_the_shared_session(self, world):
        account, fake = world
        other = IBKRAccount(account.id)
        other.get_positions()
        del other
        import gc
        gc.collect()
        assert fake.isConnected() is True

    def test_a_settings_change_replaces_the_runtime_and_closes_the_old_session(self, monkeypatch):
        account, fake = make_account(monkeypatch)
        try:
            account.get_positions()
            first = account._runtime()
            from ba2_trade_platform.core.models import AccountSetting
            from sqlmodel import select
            from ba2_trade_platform.core.db import get_db, update_instance
            with get_db() as session:
                row = session.exec(select(AccountSetting).where(
                    AccountSetting.account_id == account.id, AccountSetting.key == "client_id")).first()
                row.value_float = 9.0
                session.add(row)
                session.commit()
            changed = IBKRAccount(account.id)
            assert changed._runtime() is not first
            changed.get_positions()
            assert fake.connect_calls[-1]["clientId"] == 9
        finally:
            shutdown_all_runtimes()


class TestSettings:
    def test_required_settings_are_enforced(self, monkeypatch):
        # host/port/client_id/paper_account have declared defaults; the account id must be chosen
        # explicitly (no silent guess at WHICH account a multi-account login should trade).
        with pytest.raises(ValueError, match="account_id"):
            make_account(monkeypatch, account_id=None)

    def test_definitions_declare_the_operator_facing_settings(self):
        defs = IBKRAccount.get_settings_definitions()
        assert {"host", "port", "client_id", "account_id", "paper_account", "read_only"} <= set(defs)
        assert defs["port"]["default"] == 4002 and defs["client_id"]["default"] == 1
        assert defs["read_only"]["default"] is True                # conservative default
        assert defs["account_id"]["required"] and "default" not in defs["account_id"]
        # paper by default; the connect-time paper/live guard makes that default fail-safe
        assert defs["paper_account"]["required"] and defs["paper_account"]["default"] is True
        assert defs["paper_account"]["type"] == "bool" and defs["read_only"]["type"] == "bool"
        assert defs["flex_token"]["required"] is False

    def test_read_only_defaults_to_true_when_never_saved(self, monkeypatch):
        settings = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "read_only"}
        fake = FakeIB()
        monkeypatch.setattr(IBKRAccount, "_ib_factory", staticmethod(lambda: fake))
        definition = create_account_definition(name="ro", provider="IBKR")
        write_settings(definition.id, settings)
        account = IBKRAccount(definition.id)
        try:
            account.get_positions()
            assert fake.connect_calls[0]["readonly"] is True
        finally:
            account.close()
