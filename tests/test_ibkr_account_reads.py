"""IBKRAccount read side against FakeIB: account state, positions, prices, symbols, margin metadata,
fills. No network. See docs/plans/2026-10-03-ibkr-support-design.md."""
from datetime import datetime, timedelta, timezone

import pytest
from ib_async import Option, Stock

from ba2_trade_platform.core.account_types import AccountSnapshot
from ba2_trade_platform.core.types import OrderDirection
from tests.ibkr_fakes import NAN, FakeIB
from tests.ibkr_helpers import ACCOUNT_ID, ibkr_logs, make_account  # noqa: F401


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    msft = fake.add_stock("MSFT", 272093)
    yield account, fake, aapl, msft
    account.close()


# ----------------------------------------------------------------- account info / snapshot
class TestAccountState:
    def test_info_buying_power_is_available_funds_times_regt_not_ibkr_buying_power(self, world):
        account, fake, *_ = world
        info = account.get_account_info()
        # AvailableFunds 80000, IB BuyingPower 320000 (4x => margin account) => Reg-T 2x => 160000
        assert info["buying_power"] == pytest.approx(160000.0)
        assert info["ib_buying_power"] == 320000.0
        assert info["equity"] == 100000.0 and info["net_liquidation"] == 100000.0
        assert info["cash"] == 50000.0
        assert info["margin_multiplier"] == 2.0
        assert info["account_number"] == ACCOUNT_ID
        assert list(info)[0:3] == ["account_number", "account_type", "currency"]

    def test_cash_account_gets_no_leverage(self, world):
        account, fake, *_ = world
        fake.set_account_value("BuyingPower", "80000")     # == AvailableFunds => 1x
        snap = account.get_account_snapshot()
        assert snap.margin_multiplier == 1.0 and snap.is_margin_account is False
        assert snap.buying_power == pytest.approx(80000.0)

    def test_unknown_leverage_is_not_assumed(self, world):
        account, fake, *_ = world
        fake.set_account_value("AvailableFunds", "0")
        assert account.get_account_snapshot().margin_multiplier == 1.0

    def test_snapshot_fields_and_exposure(self, world):
        account, fake, aapl, msft = world
        fake.add_position(aapl, 10, 150.0, mark=160.0)
        fake.add_position(msft, -5, 300.0, mark=290.0)
        snap = account.get_account_snapshot()
        assert isinstance(snap, AccountSnapshot)
        assert snap.equity == snap.net_liquidation == 100000.0
        assert snap.cash == 50000.0 and snap.non_marginable_buying_power == 48000.0
        assert snap.option_buying_power == 80000.0
        assert snap.long_market_value == pytest.approx(1600.0)
        assert snap.short_market_value == pytest.approx(-1450.0)   # negative by contract
        assert snap.supports_fractional is False

    def test_a_position_with_no_mark_makes_exposure_unknown_not_zero(self, world):
        account, fake, aapl, _ = world
        fake.add_position(aapl, 10, 150.0, mark=None)
        snap = account.get_account_snapshot()
        assert snap.long_market_value is None and snap.short_market_value is None

    def test_failed_fetch_is_an_all_none_snapshot_never_zeros(self, world):
        account, fake, *_ = world
        fake.fail_account_reads(ConnectionError("socket closed"))
        snap = account.get_account_snapshot()
        assert snap == AccountSnapshot()
        assert account.get_balance() is None
        assert account.get_account_info() == {}

    def test_the_live_account_stream_beats_the_three_minute_summary(self, world):
        """AvailableFunds moves at once on a fill in the account-updates stream; reqAccountSummary
        refreshes only every ~3 minutes, so a stale summary must never win."""
        from ib_async import AccountValue
        account, fake, *_ = world
        fake.summary_rows = [AccountValue(ACCOUNT_ID, "NetLiquidation", "1", "USD", ""),
                             AccountValue(ACCOUNT_ID, "AvailableFunds", "999999", "USD", ""),
                             AccountValue(ACCOUNT_ID, "BuyingPower", "999999", "USD", "")]
        fake.set_account_value("AvailableFunds", "10000")
        fake.set_account_value("BuyingPower", "40000")
        snap = account.get_account_snapshot()
        assert snap.buying_power == pytest.approx(20000.0) and snap.equity == 100000.0

    def test_the_summary_is_only_the_fallback_before_the_stream_has_delivered(self, world):
        account, fake, *_ = world
        fake.updates_available = False
        assert account.get_account_snapshot().buying_power == pytest.approx(160000.0)

    def test_balance_is_net_liquidation(self, world):
        account, *_ = world
        assert account.get_balance() == 100000.0

    def test_other_accounts_rows_are_ignored(self, world):
        account, fake, *_ = world
        from ib_async import AccountValue
        fake.account_rows.append(AccountValue("DU9999999", "NetLiquidation", "5", "USD", ""))
        assert account.get_balance() == 100000.0

    def test_non_usd_base_currency_is_refused_loudly(self, world):
        account, fake, *_ = world
        fake.account_rows = [r for r in fake.account_rows if r.tag != "NetLiquidation"]
        fake.set_account_value("NetLiquidation", "90000", "EUR")
        assert account.get_balance() is None        # raised IBKRUnsupportedCurrency, logged, None


# ----------------------------------------------------------------- positions
class TestPositions:
    def test_flat_is_empty_list(self, world):
        account, *_ = world
        assert account.get_positions() == []

    def test_fetch_failure_is_none_not_flat(self, world):
        account, fake, *_ = world
        fake.fail_calls["positions"] = ConnectionError("down")
        assert account.get_positions() is None

    def test_gateway_down_is_none(self, monkeypatch):
        account, fake = make_account(monkeypatch)
        fake.connect_failure = ConnectionRefusedError("refused")
        try:
            assert account.get_positions() is None
            assert account.refresh_positions() is False
        finally:
            account.close()

    def test_long_and_short_equity_rows(self, world):
        account, fake, aapl, msft = world
        fake.add_position(aapl, 10, 150.0, mark=165.0, unrealized=150.0)
        fake.add_position(msft, -4, 300.0, mark=290.0, unrealized=40.0)
        fake.set_quote(aapl, close=160.0)
        fake.set_quote(msft, close=295.0)
        by = {p.symbol: p for p in account.get_positions()}
        a, m = by["AAPL"], by["MSFT"]
        assert (a.qty, a.side, a.avg_entry_price, a.current_price) == (10, OrderDirection.BUY, 150.0, 165.0)
        assert a.cost_basis == 1500.0 and a.market_value == 1650.0 and a.unrealized_pl == 150.0
        assert a.lastday_price == 160.0 and a.change_today == pytest.approx(5.0 / 160.0)
        assert m.qty == -4 and m.side == OrderDirection.SELL          # SIGNED: a short is negative
        assert m.cost_basis == -1200.0 and m.market_value == -1160.0
        assert account.get_signed_position_quantity("MSFT") == -4.0   # the exposure gate reads a short as one
        assert account.get_signed_position_quantity("AAPL") == 10.0
        assert a.qty_available == 10                                  # IB publishes no per-order hold

    def test_option_rows_are_excluded_from_equity_positions(self, world):
        account, fake, aapl, _ = world
        opt = Option("AAPL", "20261218", 150.0, "C", "SMART", multiplier="100", currency="USD",
                     conId=999)
        fake.add_position(opt, 1, 500.0, mark=5.0, multiplier=100)
        fake.add_position(aapl, 10, 150.0, mark=160.0)
        assert [p.symbol for p in account.get_positions()] == ["AAPL"]

    def test_other_account_rows_are_filtered_and_that_is_flat_not_failed(self, world):
        account, fake, aapl, _ = world
        fake.add_position(aapl, 10, 150.0, mark=160.0, account="DU9999999")
        assert account.get_positions() == []

    def test_no_mark_anywhere_refuses_to_fabricate_one(self, world):
        account, fake, aapl, _ = world
        fake.add_position(aapl, 10, 150.0, mark=None)       # portfolio row has nan, no quote either
        assert account.get_positions() is None

    def test_mark_falls_back_to_a_snapshot_when_the_portfolio_has_none(self, world):
        account, fake, aapl, _ = world
        fake.add_position(aapl, 10, 150.0, mark=None)
        fake.set_quote(aapl, bid=170.0, ask=170.2, last=170.1, close=168.0)
        row = account.get_positions()[0]
        assert row.current_price == pytest.approx(170.1)

    def test_available_quantity_is_derived_and_never_none(self, world):
        account, fake, aapl, _ = world
        fake.add_position(aapl, 10, 150.0, mark=160.0)
        assert account.get_available_position_quantity("AAPL") == 10.0
        assert account.get_available_position_quantity("TSLA") == 0.0
        fake.fail_calls["positions"] = ConnectionError("x")
        assert account.get_available_position_quantity("AAPL") == 0.0

    def test_floating_pl_is_the_brokers_own_sum_or_none(self, world):
        account, fake, aapl, msft = world
        fake.add_position(aapl, 10, 150.0, mark=160.0, unrealized=100.0)
        fake.add_position(msft, 2, 300.0, mark=310.0, unrealized=20.0)
        assert account.get_broker_floating_pl() == pytest.approx(120.0)
        fake._portfolio[0] = fake._portfolio[0]._replace(unrealizedPNL=NAN)
        assert account.get_broker_floating_pl() is None

    def test_refresh_positions_true_when_readable(self, world):
        account, *_ = world
        assert account.refresh_positions() is True


# ----------------------------------------------------------------- prices
class TestPrices:
    def test_ladder_requested_type_then_mid_last_close(self, world):
        account, fake, aapl, _ = world
        fake.set_quote(aapl, bid=100.0, ask=100.2, last=100.1, close=99.0)
        assert account._get_instrument_current_price_impl("AAPL", "bid") == 100.0
        assert account._get_instrument_current_price_impl("AAPL", "ask") == 100.2
        assert account._get_instrument_current_price_impl("AAPL", "mid") == pytest.approx(100.1)

    def test_missing_bid_falls_back_down_the_ladder(self, world):
        account, fake, aapl, _ = world
        fake.set_quote(aapl, last=101.0, close=99.0)
        assert account._get_instrument_current_price_impl("AAPL", "bid") == 101.0
        fake.set_quote(aapl, close=99.0)
        # yesterday's close is NOT a quote: nothing live means no price (review follow-up)
        assert account._get_instrument_current_price_impl("AAPL", "bid") is None

    def test_nothing_published_is_none_never_a_default(self, world):
        account, fake, aapl, _ = world
        assert account._get_instrument_current_price_impl("AAPL", "bid") is None

    def test_delayed_data_is_never_a_price(self, world):
        account, fake, aapl, _ = world
        fake.set_quote(aapl, bid=100.0, ask=100.2, last=100.1, market_data_type=3)
        assert account._get_instrument_current_price_impl("AAPL", "bid") is None
        fake.set_quote(aapl, bid=100.0, ask=100.2, last=100.1, market_data_type=4)
        assert account._get_instrument_current_price_impl("AAPL", "bid") is None
        fake.set_quote(aapl, bid=100.0, ask=100.2, last=100.1, market_data_type=2)   # frozen is fine
        assert account._get_instrument_current_price_impl("AAPL", "bid") == 100.0

    def test_bulk_returns_every_requested_symbol(self, world):
        account, fake, aapl, msft = world
        fake.set_quote(aapl, bid=10.0, ask=10.2)
        out = account._get_instrument_current_price_impl(["AAPL", "MSFT", "NOPE"], "bid")
        assert out == {"AAPL": 10.0, "MSFT": None, "NOPE": None}

    def test_public_method_goes_through_the_shared_cache(self, world):
        account, fake, aapl, _ = world
        fake.set_quote(aapl, bid=50.0, ask=50.2)
        assert account.get_instrument_current_price("AAPL") == 50.0
        fake.set_quote(aapl, bid=51.0, ask=51.2)
        assert account.get_instrument_current_price("AAPL") == 50.0     # cached, not re-fetched

    def test_unknown_price_type_is_a_typo_not_a_default(self, world):
        account, *_ = world
        with pytest.raises(ValueError):
            account._get_instrument_current_price_impl("AAPL", "bidd")

    def test_a_failed_fetch_is_none(self, world):
        account, fake, aapl, _ = world
        fake.fail_calls["reqTickersAsync"] = TimeoutError("slow")
        assert account._get_instrument_current_price_impl("AAPL", "bid") is None

    def test_share_class_symbols_use_the_ib_spelling(self, world):
        account, fake, *_ = world
        brk = fake.add_stock("BRK B", 72063691, primary="NYSE")
        fake.set_quote(brk, bid=400.0, ask=400.4)
        assert account._get_instrument_current_price_impl("BRK.B", "bid") == 400.0


# ----------------------------------------------------------------- symbols / margin info
class TestSymbolsAndMargin:
    def test_symbols_exist(self, world):
        account, *_ = world
        assert account.symbols_exist(["AAPL", "NOPE", "MSFT"]) == {"AAPL": True, "NOPE": False,
                                                                    "MSFT": True}

    def test_a_lookup_failure_reports_false_not_a_crash(self, world):
        account, fake, *_ = world
        fake.fail_calls["reqContractDetailsAsync"] = TimeoutError("slow")
        assert account.symbols_exist(["AAPL"]) == {"AAPL": False}

    def test_ambiguous_listing_is_refused_not_guessed(self, world):
        account, fake, *_ = world
        fake.add_stock("DUAL", 1, primary="PINK")
        fake.add_stock("DUAL", 2, primary="VALUE")
        assert account.symbols_exist(["DUAL"]) == {"DUAL": False}

    def test_us_primary_exchange_disambiguates(self, world):
        account, fake, *_ = world
        fake.add_stock("TWIN", 1, primary="PINK")
        fake.add_stock("TWIN", 2, primary="NYSE")
        assert account.symbols_exist(["TWIN"]) == {"TWIN": True}

    def test_fractionable_is_tri_state_from_min_size(self, world):
        account, fake, *_ = world
        fake.add_stock("SCHD", 5, fractional=True)
        fake.add_stock("NOINFO", 6, min_size=NAN, size_increment=NAN)
        info = account.get_symbol_margin_info(["AAPL", "SCHD", "NOINFO", "UNKNOWN"])
        assert info["AAPL"].fractionable is False and info["AAPL"].min_trade_increment == 1.0
        assert info["SCHD"].fractionable is True and info["SCHD"].min_trade_increment == 0.0001
        assert info["NOINFO"].fractionable is None and info["NOINFO"].min_trade_increment is None
        assert "UNKNOWN" not in info          # omitted, never defaulted
        assert info["AAPL"].bp_factor == 1.0 and info["AAPL"].tradable is True
        assert account.get_fractionable(["SCHD", "AAPL", "NOINFO"]) == {
            "SCHD": True, "AAPL": False, "NOINFO": None}

    def test_marginable_follows_the_account(self, world):
        account, fake, *_ = world
        assert account.get_symbol_margin_info(["AAPL"])["AAPL"].marginable is True
        fake.set_account_value("BuyingPower", "80000")
        assert account.get_symbol_margin_info(["AAPL"])["AAPL"].marginable is False


# ----------------------------------------------------------------- fills
class TestFilledTrades:
    def test_stock_fills_with_side_qty_price(self, world):
        account, fake, aapl, msft = world
        now = datetime.now(timezone.utc)
        fake.make_fill_record(aapl, "BOT", 10, 150.0, when=now)
        fake.make_fill_record(msft, "SLD", 3, 300.0, when=now)
        opt = Option("AAPL", "20261218", 150.0, "C", "SMART", conId=9)
        fake.make_fill_record(opt, "BOT", 1, 5.0, when=now)
        trades = account.get_filled_trades()
        assert {(t["symbol"], t["side"], t["qty"], t["price"]) for t in trades} == {
            ("AAPL", "BUY", 10.0, 150.0), ("MSFT", "SELL", 3.0, 300.0)}
        assert [t["symbol"] for t in account.get_filled_trades(symbol="MSFT")] == ["MSFT"]

    def test_date_window_filters(self, world):
        account, fake, aapl, _ = world
        now = datetime.now(timezone.utc)
        fake.make_fill_record(aapl, "BOT", 1, 1.0, when=now - timedelta(days=3))
        fake.make_fill_record(aapl, "BOT", 2, 1.0, when=now)
        got = account.get_filled_trades(start_date=now - timedelta(days=1))
        assert [t["qty"] for t in got] == [2.0]

    def test_a_window_older_than_the_api_keeps_warns_once(self, world, ibkr_logs):
        account, fake, aapl, _ = world
        account.get_filled_trades(start_date=datetime.now(timezone.utc) - timedelta(days=60))
        account.get_filled_trades(start_date=datetime.now(timezone.utc) - timedelta(days=60))
        assert sum("PARTIAL" in r.getMessage() for r in ibkr_logs.records) == 1

    def test_failure_is_empty_and_logged(self, world):
        account, fake, *_ = world
        fake.fail_calls["reqExecutionsAsync"] = ConnectionError("x")
        assert account.get_filled_trades() == []


# ----------------------------------------------------------------- unsupported history seams
class TestHistorySeamsWithoutFlex:
    def test_return_empty_and_warn_once_naming_the_setting(self, world, ibkr_logs):
        account, *_ = world
        assert account.get_dividends() == []
        assert account.get_dividends() == []
        assert account.get_balance_history() == []
        assert account.get_cash_transfers() == []
        text = ibkr_logs.text()
        assert text.count("dividend history") == 1 and "flex_token" in text


# ----------------------------------------------------------------- Flex-backed history seams
FLEX_SEND = ("<FlexStatementResponse><Status>Success</Status><ReferenceCode>77</ReferenceCode>"
             "<Url>https://example.invalid/GetStatement</Url></FlexStatementResponse>")
FLEX_STATEMENT = (
    '<FlexQueryResponse><FlexStatements><FlexStatement accountId="DU1234567"><CashTransactions>'
    '<CashTransaction currency="USD" symbol="SCHD" dateTime="20260315" amount="10" type="Dividends" transactionID="1"/>'
    '<CashTransaction currency="USD" symbol="SCHD" dateTime="20260315" amount="-1" type="Withholding Tax" transactionID="2"/>'
    '<CashTransaction currency="USD" symbol="" dateTime="20260105" amount="500" type="Deposits/Withdrawals" transactionID="3"/>'
    '</CashTransactions><EquitySummaryInBase>'
    '<EquitySummaryByReportDateInBase reportDate="20260930" cash="40" stock="60" total="100"/>'
    '</EquitySummaryInBase></FlexStatement></FlexStatements></FlexQueryResponse>')


class TestFlexSeams:
    @pytest.fixture
    def flex_world(self, monkeypatch):
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        calls = []

        def http(url):
            calls.append(url)
            return FLEX_SEND if "SendRequest" in url else FLEX_STATEMENT

        monkeypatch.setattr(IBKRAccount, "_flex_http_get", staticmethod(http))
        account, fake = make_account(monkeypatch, extra={"flex_token": "TOK", "flex_query_id": "42"})
        yield account, calls
        account.close()

    def test_dividends_cash_transfers_and_nav_come_from_the_statement(self, flex_world):
        account, calls = flex_world
        div = account.get_dividends()
        assert [(d["symbol"], d["amount"], d["tax_withheld"]) for d in div] == [("SCHD", 9.0, 1.0)]
        assert {t.external_id for t in account.get_cash_transfers()} == {"IBKR:1", "IBKR:3"}
        assert account.get_balance_history()[0]["net_liquidating_value"] == 100.0
        assert len(calls) == 2                      # one fetch, then cached for the other two seams

    def test_a_failing_flex_service_is_empty_and_logged_not_raised(self, monkeypatch, ibkr_logs):
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount

        def boom(url):
            raise OSError("network unreachable")

        monkeypatch.setattr(IBKRAccount, "_flex_http_get", staticmethod(boom))
        account, _ = make_account(monkeypatch, extra={"flex_token": "TOK", "flex_query_id": "42"})
        try:
            assert account.get_dividends() == []
            assert "Flex fetch" in ibkr_logs.text() and "network unreachable" in ibkr_logs.text()
        finally:
            account.close()
