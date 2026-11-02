"""tools/ibkr_paper_smoke.py against FakeIB: read-only by default, refuses live accounts, places and
cancels exactly one far-from-market paper order when asked. No network."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.ibkr_fakes import NAN, FakeIB

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "ibkr_paper_smoke.py"


def load_script():
    spec = importlib.util.spec_from_file_location("ibkr_paper_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load_script()
TODAY = datetime.now(timezone.utc).date()
EXP = TODAY + timedelta(days=35)


def make_fake(account="DU1234567", **kw):
    fake = FakeIB(account=account, **kw)
    aapl = fake.add_stock("AAPL", 265598)
    fake.set_quote(aapl, bid=199.9, ask=200.1, last=200.0, close=199.0)
    fake.shortable_shares[265598] = 5.0
    return fake, aapl


def run(argv, fake, http_get=None):
    lines = []
    code = smoke.main(argv, ib_factory=lambda: fake, out=lines.append, http_get=http_get)
    return code, "\n".join(lines)


class TestReadOnlyByDefault:
    def test_a_default_run_reads_everything_and_places_nothing(self):
        fake, _ = make_fake()
        code, out = run(["--timeout", "2"], fake)
        assert code == 0, out
        assert fake.placed == [] and fake.cancel_requests == []
        assert [c["readonly"] for c in fake.connect_calls] == [True, True]   # + the reconnect check, still read-only
        assert "connected to 127.0.0.1:4002 (IB Gateway paper)" in out
        assert "[CHECK] paper accounts start with 'DU': DU1234567 -> paper" in out
        assert "BuyingPower / AvailableFunds" in out and "4.00" in out
        assert "adapter's Reg-T multiplier" in out and ": 2.0" in out
        assert "conId=265598" in out and "fractionable=False" in out
        assert "shortableShares" in out and "easy_to_borrow=True" in out
        assert "whatIfOrder" in out and "initMarginChange=5000.0" in out
        assert "Summary: facts to confirm" in out and "(nothing was placed)" in out
        assert "Reg-T room: AvailableFunds x 2 vs SMA x 2" in out
        assert "a COMPLETED order's orderStatus.filled vs order.filledQuantity" in out
        assert "an orderStatus is delivered PER ORDER" in out
        assert "executions carry the orderRef" in out
        assert a_adapter_style_connect(fake)
        assert "positions cache immediately after the adapter-style connect" in out
        assert "how long completed orders survive" in out
        assert "request/order ids after (re)connect" in out
        assert "order WARNING codes seen" in out

    def test_delayed_data_is_flagged(self):
        fake, aapl = make_fake()
        fake.set_quote(aapl, bid=199.9, ask=200.1, last=200.0, market_data_type=3)
        code, out = run(["--timeout", "2"], fake)
        assert code == 0 and "delayed=True" in out

    def test_option_section(self):
        fake, _ = make_fake()
        fake.add_option_chain("AAPL", [EXP.strftime("%Y%m%d")], [190.0, 200.0, 210.0], under_con_id=265598)
        for strike in (190.0, 200.0, 210.0):
            fake.add_option("AAPL", EXP.strftime("%Y%m%d"), strike, "C", bid=5.0, ask=5.2, iv=0.3,
                            delta=0.5, theta=-0.05, oi=900)
        code, out = run(["--option", "--timeout", "2"], fake)
        assert code == 0, out
        assert "localSymbol with spaces removed IS the OCC symbol" in out and "equal=True" in out
        assert "3 contracts for expiry" in out and "callOI=900.0" in out
        assert "BAG order with a NEGATIVE limit" in out and "per-leg combo fill shape" in out
        assert fake.placed == []

    def test_dump_executions_flags_orders_without_an_order_id(self):
        fake, aapl = make_fake()
        fake.make_fill_record(aapl, "BOT", 10, 150.0, order_ref="ba2:1:1", perm=111, order_id=5)
        fake.make_fill_record(aapl, "SLD", 100, 150.0, perm=222, order_id=0)
        code, out = run(["--dump-executions", "--timeout", "2"], fake)
        assert code == 0
        assert out.count("candidate assignment/exercise") == 1
        assert "BOT 10.0@150.0 orderId=5" in out
        assert "COMBO executions" in out

    def test_flex(self):
        fake, _ = make_fake()
        send = ("<FlexStatementResponse><Status>Success</Status><ReferenceCode>1</ReferenceCode>"
                "<Url>https://x.invalid/G</Url></FlexStatementResponse>")
        stmt = ('<FlexQueryResponse><FlexStatements><FlexStatement accountId="DU1234567"><CashTransactions>'
                '<CashTransaction currency="USD" symbol="KO" dateTime="20260401" amount="4" type="Dividends" '
                'transactionID="1"/></CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>')
        code, out = run(["--flex-token", "T", "--flex-query", "9", "--timeout", "2"], fake,
                        http_get=lambda url: send if "SendRequest" in url else stmt)
        assert code == 0, out
        assert "CashTransaction types present: {'Dividends': 1}" in out and "dividends parsed: 1" in out


class TestRefusals:
    def test_test_order_on_a_live_account_is_refused_and_never_writable(self):
        fake, _ = make_fake(account="U7654321")
        code, out = run(["--place-test-order", "--timeout", "2"], fake)
        assert code == 2
        assert "REFUSED" in out and "not a paper account" in out
        assert fake.placed == []
        assert [c["readonly"] for c in fake.connect_calls] == [True]     # a live account never saw a writable session

    def test_several_accounts_need_an_explicit_choice(self):
        fake, _ = make_fake(managed=["DU1234567", "DU7654321"])
        code, out = run(["--timeout", "2"], fake)
        assert code == 2 and "pass --account" in out

    def test_an_account_the_login_does_not_manage_is_refused(self):
        fake, _ = make_fake()
        code, out = run(["--account", "DU9999999", "--timeout", "2"], fake)
        assert code == 2 and "not managed by this login" in out

    def test_a_dead_gateway_is_a_clean_failure(self):
        fake, _ = make_fake()
        fake.connect_failure = ConnectionRefusedError("refused")
        code, out = run(["--timeout", "2"], fake)
        assert code == 1 and "FAILED: ConnectionRefusedError" in out


def a_adapter_style_connect(fake):
    """Every connect the script makes uses the adapter's startup arguments (not defaults, and not
    raiseSyncErrors=True)."""
    from ib_async import StartupFetch
    return (fake.connect_kwargs["raiseSyncErrors"] is False
            and fake.connect_kwargs["fetchFields"] == StartupFetch.ACCOUNT_UPDATES)


class TestTestOrder:
    def test_places_far_orders_only_cancels_everything_and_checks_modify_and_oca(self):
        fake, _ = make_fake()
        code, out = run(["--place-test-order", "--timeout", "3"], fake)
        assert code == 0, out
        assert [c["readonly"] for c in fake.connect_calls][:2] == [True, False]   # writable only after DU confirmed
        placed = [p for p in fake.placed if not p["modification"]]
        first = placed[0]
        assert (first["orderType"], first["action"], first["qty"], first["tif"]) == ("LMT", "BUY", 1.0, "DAY")
        assert first["lmt"] == pytest.approx(100.0)               # half of the 200 last: cannot fill
        assert first["ref"] == "ba2-smoke-test" and first["account"] == "DU1234567"
        assert all(p["qty"] == 1.0 for p in placed)
        # the OCA pair: stop first, both transmitted, ocaType 2
        oca = [p for p in placed if p["oca"]]
        assert [p["orderType"] for p in oca] == ["STP LMT", "LMT"]
        assert all(p["transmit"] is True and p["oca_type"] == 2 for p in oca)
        # every order the script placed ended Cancelled
        assert {t.orderStatus.status for t in fake.trades()} == {"Cancelled"}
        assert "final status=Cancelled" in out
        assert "a modification is acknowledged by a 'Modified' log entry" in out
        assert "a REFUSED modification" in out
        assert "cancelling ONE OCA leg leaves the other working" in out
        assert "does TWS list a NOT-YET-ACKNOWLEDGED order" in out
        assert "modify + IMMEDIATE re-read" in out
        assert "LOCAL status after it" in out
        assert "EXACT 321 text" in out
        assert a_adapter_style_connect(fake)
        assert "auxPrice as echoed for a LIMIT order" in out
        assert "error codes when CANCELLING AN ALREADY-CANCELLED order" in out
        assert "are HELD orders" in out
        assert "OTHER API clients' orders" in out
        assert "a RESTING STOP's modification" in out
        assert "resting status=PreSubmitted" in out and "'Modified' logged=False" in out
        assert "shows the new stop=True" in out

    def test_no_price_means_no_order(self):
        fake, aapl = make_fake()
        fake.quotes.clear()
        code, out = run(["--place-test-order", "--timeout", "2"], fake)
        assert code == 2 and "no price" in out and fake.placed == []
