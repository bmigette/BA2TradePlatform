"""IBKR Flex Web Service parsing and mapping (pure; no network). The sample XML follows IBKR's published
schema from memory and is UNVERIFIED against a live statement -- the operator's first run prints the parse."""
from datetime import date, datetime

import pytest

from ba2_common.core import ibkr_flex as F
from ba2_common.core.account_types import CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL

SEND_OK = """<FlexStatementResponse timestamp="03 October, 2026 10:00 AM EDT">
<Status>Success</Status><ReferenceCode>1234567890</ReferenceCode>
<Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
</FlexStatementResponse>"""

SEND_FAIL = """<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>
<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"""

IN_PROGRESS = """<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>
<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage></FlexStatementResponse>"""

STATEMENT = """<FlexQueryResponse queryName="ba2" type="AF"><FlexStatements count="2">
<FlexStatement accountId="U111" fromDate="20260101" toDate="20260930" whenGenerated="20261003;100000">
 <CashTransactions>
  <CashTransaction accountId="U111" currency="USD" symbol="SCHD" description="SCHD CASH DIVIDEND" dateTime="20260315;120000" amount="10.00" type="Dividends" transactionID="9001" fxRateToBase="1"/>
  <CashTransaction accountId="U111" currency="USD" symbol="SCHD" description="SCHD TAX" dateTime="20260315;120000" amount="-1.50" type="Withholding Tax" transactionID="9002" fxRateToBase="1"/>
  <CashTransaction accountId="U111" currency="USD" symbol="KO" description="KO DIV" dateTime="20260401" amount="4.00" type="Dividends" transactionID="9003" fxRateToBase="1"/>
  <CashTransaction accountId="U111" currency="EUR" symbol="SAP" description="SAP DIV" dateTime="2026-04-02" amount="10.00" type="Dividends" transactionID="9004" fxRateToBase="1.1"/>
  <CashTransaction accountId="U111" currency="USD" symbol="" description="Wire in" dateTime="20260105;090000" amount="5000.00" type="Deposits/Withdrawals" transactionID="9005"/>
  <CashTransaction accountId="U111" currency="USD" symbol="" description="Wire out" dateTime="20260601;090000" amount="-1000.00" type="Deposits/Withdrawals" transactionID="9006"/>
  <CashTransaction accountId="U111" currency="USD" symbol="" description="Interest" dateTime="20260601;090000" amount="-3.00" type="Broker Interest Paid" transactionID="9007"/>
 </CashTransactions>
 <EquitySummaryInBase>
  <EquitySummaryByReportDateInBase accountId="U111" reportDate="20260930" cash="40000" stock="60000" total="100000"/>
  <EquitySummaryByReportDateInBase accountId="U111" reportDate="20260929" cash="39000" stock="60500" total="99500"/>
 </EquitySummaryInBase>
</FlexStatement>
<FlexStatement accountId="U222"><CashTransactions>
  <CashTransaction accountId="U222" currency="USD" symbol="XOM" dateTime="20260315" amount="99" type="Dividends" transactionID="1"/>
</CashTransactions></FlexStatement>
</FlexStatements></FlexQueryResponse>"""


class TestParsing:
    def test_send_request(self):
        ref, url = F.parse_send_request(SEND_OK)
        assert ref == "1234567890" and url.endswith("/GetStatement")

    def test_a_refused_request_carries_the_brokers_code_and_text(self):
        with pytest.raises(F.FlexError, match="Token has expired") as info:
            F.parse_send_request(SEND_FAIL)
        assert info.value.code == "1012"

    def test_not_xml_is_a_flex_error(self):
        with pytest.raises(F.FlexError, match="not XML"):
            F.parse_send_request("<html>maintenance")

    def test_statement_filtered_to_one_account(self):
        only = F.parse_statement(STATEMENT, "U111")
        assert [s.account_id for s in only] == ["U111"]
        assert len(only[0].cash_transactions) == 7 and len(only[0].equity_summary) == 2
        assert len(F.parse_statement(STATEMENT)) == 2

    def test_in_progress_is_a_distinguishable_error(self):
        with pytest.raises(F.FlexError) as info:
            F.parse_statement(IN_PROGRESS)
        assert info.value.code == F.IN_PROGRESS_CODE

    @pytest.mark.parametrize("text,expected", [
        ("20260315", date(2026, 3, 15)), ("20260315;120000", date(2026, 3, 15)),
        ("2026-03-15", date(2026, 3, 15)), ("2026-03-15;12:00:00", date(2026, 3, 15)),
        ("2026-03-15 12:00:00", date(2026, 3, 15)), ("03/15/2026", date(2026, 3, 15)),
        ("", None), (None, None), ("garbage", None)])
    def test_flex_dates(self, text, expected):
        assert F.parse_flex_date(text) == expected


class TestMapping:
    @pytest.fixture
    def stmts(self):
        return F.parse_statement(STATEMENT, "U111")

    def test_dividends_are_net_of_withholding_per_symbol_and_date(self, stmts):
        by = {d["symbol"]: d for d in F.dividends_from(stmts)}
        assert by["SCHD"]["amount"] == 8.5 and by["SCHD"]["gross_amount"] == 10.0
        assert by["SCHD"]["tax_withheld"] == 1.5 and by["SCHD"]["date"] == date(2026, 3, 15)
        assert by["KO"]["amount"] == 4.0 and by["KO"]["tax_withheld"] == 0.0
        assert by["SAP"]["amount"] == pytest.approx(11.0)             # EUR converted to base
        assert by["SCHD"]["drip_quantity"] is None

    def test_symbol_and_window_filters(self, stmts):
        assert [d["symbol"] for d in F.dividends_from(stmts, symbol="ko")] == ["KO"]
        got = F.dividends_from(stmts, start=datetime(2026, 4, 1), end=date(2026, 4, 30))
        assert {d["symbol"] for d in got} == {"KO", "SAP"}

    def test_an_orphan_withholding_line_is_never_a_phantom_dividend(self):
        rows = F.parse_statement(
            '<FlexQueryResponse><FlexStatements><FlexStatement accountId="U1"><CashTransactions>'
            '<CashTransaction currency="USD" symbol="X" dateTime="20260101" amount="-2" type="Withholding Tax"/>'
            '</CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>')
        assert F.dividends_from(rows) == []

    def test_withholding_larger_than_the_gross_floors_at_zero(self):
        rows = F.parse_statement(
            '<FlexQueryResponse><FlexStatements><FlexStatement accountId="U1"><CashTransactions>'
            '<CashTransaction currency="USD" symbol="X" dateTime="20260101" amount="1" type="Dividends"/>'
            '<CashTransaction currency="USD" symbol="X" dateTime="20260101" amount="-5" type="Withholding Tax"/>'
            '</CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>')
        assert F.dividends_from(rows)[0]["amount"] == 0.0

    def test_cash_transfers_ledger(self, stmts):
        by = {t.external_id: t for t in F.cash_transfers_from(stmts)}
        assert by["IBKR:9005"].event_type == CASH_TRANSFER_DEPOSIT and by["IBKR:9005"].amount == 5000.0
        assert by["IBKR:9006"].event_type == CASH_TRANSFER_WITHDRAWAL and by["IBKR:9006"].amount == -1000.0
        div = by["IBKR:9001"]
        assert div.event_type == CASH_TRANSFER_DIVIDEND and div.amount == 8.5 and div.symbol == "SCHD"
        assert div.is_income and by["IBKR:9005"].is_income and not by["IBKR:9006"].is_income
        assert "IBKR:9002" not in by and "IBKR:9007" not in by       # tax leg / interest are not transfers

    def test_balance_history(self, stmts):
        hist = F.balance_history_from(stmts)
        assert [h["date"] for h in hist] == [date(2026, 9, 29), date(2026, 9, 30)]
        assert hist[1] == {"date": date(2026, 9, 30), "net_liquidating_value": 100000.0,
                           "cash_balance": 40000.0, "equity_value": 60000.0}
        assert len(F.balance_history_from(stmts, start=date(2026, 9, 30))) == 1


class TestClient:
    def make(self, responses, **kw):
        calls = []

        def get(url):
            calls.append(url)
            return responses.pop(0)

        return F.FlexClient("TOKEN", "42", http_get=get, sleep=lambda s: None, **kw), calls

    def test_two_step_fetch_with_the_token_and_reference(self):
        client, calls = self.make([SEND_OK, STATEMENT])
        out = client.fetch("U111")
        assert [s.account_id for s in out] == ["U111"]
        assert "SendRequest" in calls[0] and "t=TOKEN" in calls[0] and "q=42" in calls[0] and "v=3" in calls[0]
        assert "GetStatement" in calls[1] and "q=1234567890" in calls[1]

    def test_polls_while_the_statement_is_being_generated(self):
        client, calls = self.make([SEND_OK, IN_PROGRESS, IN_PROGRESS, STATEMENT])
        assert client.fetch("U111") and len(calls) == 4

    def test_gives_up_after_the_poll_budget(self):
        client, _ = self.make([SEND_OK] + [IN_PROGRESS] * 3, poll_attempts=3)
        with pytest.raises(F.FlexError, match="not ready"):
            client.fetch()

    def test_a_refusal_is_raised_not_polled(self):
        client, calls = self.make([SEND_FAIL])
        with pytest.raises(F.FlexError, match="expired"):
            client.fetch()
        assert len(calls) == 1

    def test_token_and_query_are_required(self):
        with pytest.raises(ValueError):
            F.FlexClient("", "1")
