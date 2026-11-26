"""TastyTradeAccount.read_share_movements: the account's SHARE movements read from the transactions endpoint through
the session's own authenticated client, page by page, parsed from the RAW JSON (values are strings, ids numbers, as
the API returns them: https://developer.tastytrade.com/reference/transactions/), with COMPLETENESS PROVEN from the
server's ``pagination`` block. Classification is by INSTRUMENT: only an ``Equity`` row can change a tile's quantity.
No network."""
import asyncio
import threading
from datetime import date, datetime, timezone

import pytest

from ba2_trade_platform.modules.accounts.TastyTradeAccount import (
    SHARE_HISTORY_PAGE_TIMEOUT_S, ShareHistoryIncomplete, ShareHistoryStopped, classify_transaction,
)
from tests.test_tastytrade_account import _bare_account

ACCOUNT_NUMBER = "5WX00000"


def item(rid=1, ttype="Trade", sub="Buy to Open", instrument="Equity", symbol="AAOI", underlying="AAOI",
         action="Buy to Open", quantity="2.0", price="30.5", executed="2026-11-23T15:00:00.000+00:00", reverses=None,
         description="Bought 2 AAOI @ 30.50"):
    out = {"id": rid, "account-number": ACCOUNT_NUMBER, "transaction-type": ttype, "transaction-sub-type": sub,
           "description": description, "executed-at": executed, "transaction-date": executed[:10],
           "value": "61.0", "value-effect": "Debit", "net-value": "61.0", "net-value-effect": "Debit",
           "is-estimated-fee": True}
    for key, value in (("instrument-type", instrument), ("symbol", symbol), ("underlying-symbol", underlying),
                       ("action", action), ("quantity", quantity), ("price", price), ("reverses-id", reverses)):
        if value is not None:
            out[key] = value
    return out


# ---- classification by instrument -----------------------------------------------------------------------------

def test_an_equity_trade_is_a_signed_movement_from_string_values():
    buy = classify_transaction(item())
    sell = classify_transaction(item(rid=2, action="Sell to Close", sub="Sell to Close", quantity="0.5", price="35"))
    assert (buy["id"], buy["symbol"], buy["quantity"], buy["price"], buy["kind"]) == (1, "AAOI", 2.0, 30.5, "trade")
    assert buy["executed_at"] == datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
    assert (sell["quantity"], sell["price"]) == (-0.5, 35.0)


@pytest.mark.parametrize("action,sign", [("Buy", 1), ("Sell", -1), ("Buy to Close", 1), ("Sell to Open", -1)])
def test_plain_and_open_close_actions_have_their_sign(action, sign):
    assert classify_transaction(item(action=action, sub=action))["quantity"] == sign * 2.0


@pytest.mark.parametrize("ttype", ["Trade", "Receive Deliver"])
def test_both_reinvested_dividend_shapes_are_buys(ttype):
    m = classify_transaction(item(ttype=ttype, sub="Dividend", action=None, quantity="1.68074", price="20.15",
                                  description="Received 1.68074 Long KBWD via Dividend"))
    assert (m["quantity"], m["price"], m["kind"]) == (1.68074, 20.15, "trade")


@pytest.mark.parametrize("instrument", ["Bond", "Cryptocurrency", "Currency Pair", "Event Contract",
                                        "Fixed Income Security", "Future", "Future Option", "Index", "Liquidity Pool"])
def test_a_row_of_a_known_non_equity_instrument_is_ignored_whatever_its_type(instrument):
    for ttype, sub in (("Trade", "Sell to Open"), ("Receive Deliver", "Expiration"),
                       ("Receive Deliver", "Assignment"), ("Receive Deliver", "Exercise")):
        assert classify_transaction(item(ttype=ttype, sub=sub, instrument=instrument,
                                         symbol="AAOI  261218C00040000", action="Sell to Close")) is None


OPTION = "AAOI  261218C00040000"
ASSIGN_WHY = "an option assignment or exercise in the period: the share delivery cannot be verified"


@pytest.mark.parametrize("ttype,sub,description", [
    ("Receive Deliver", "Assignment", "Removal of option due to assignment"),
    ("Receive Deliver", "Exercise", "Removal of option due to exercise"),
    ("Receive Deliver", "Journal", "Removal of 1.0 AAOI 12/18/26 Call 40.00"),     # not recognisably an expiration
    ("Trade", "Cash Settled Assignment", ""),
])
def test_an_option_assignment_or_exercise_refuses_its_underlyings_tile_only(ttype, sub, description):
    m = classify_transaction(item(ttype=ttype, sub=sub, instrument="Equity Option", symbol=OPTION, action=None,
                                  quantity="1", price=None, description=description))
    assert (m["kind"], m["underlying"], m["detail"]) == ("tile_only", "AAOI", ASSIGN_WHY)


@pytest.mark.parametrize("ttype,sub,action,description", [
    ("Receive Deliver", "Expiration", None, "Removal of 1.0 AAOI 12/18/26 Call 40.00 due to expiration."),
    ("Trade", "Sell to Open", "Sell to Open", "Sold 1 AAOI 12/18/26 Call 40.00 @ 1.20"),
    ("Trade", "Buy to Close", "Buy to Close", "Bought 1 AAOI 12/18/26 Call 40.00 @ 0.10"),
])
def test_option_trades_and_expirations_stay_ignored(ttype, sub, action, description):
    assert classify_transaction(item(ttype=ttype, sub=sub, instrument="Equity Option", symbol=OPTION,
                                     action=action, description=description)) is None


def test_an_assignment_without_an_underlying_refuses_the_account():
    m = classify_transaction(item(ttype="Receive Deliver", sub="Assignment", instrument="Equity Option",
                                  symbol=OPTION, underlying=None, action=None, price=None))
    assert m["kind"] == "other"


@pytest.mark.parametrize("instrument", ["Equity Offering", "Warrant"])
def test_an_equity_offering_or_warrant_row_refuses_its_tile_naming_the_instrument(instrument):
    m = classify_transaction(item(ttype="Receive Deliver", sub="Allocate", instrument=instrument, symbol="NEWCO",
                                  underlying=None, action="Allocate"))
    assert m["kind"] == "tile_only" and m["symbol"] == "NEWCO" and instrument in m["detail"]


def test_the_equity_leg_of_an_assignment_is_refused_on_its_symbol():
    m = classify_transaction(item(ttype="Receive Deliver", sub="Assignment", action="Buy to Open", quantity="100",
                                  price="40"))
    assert m["kind"] == "other" and m["symbol"] == "AAOI"


def test_an_allocate_row_is_refused_on_its_symbol_only():
    m = classify_transaction(item(action="Allocate", sub="Allocate"))
    assert m["kind"] == "other" and m["symbol"] == "AAOI"


@pytest.mark.parametrize("change", [dict(price=None), dict(quantity="0"), dict(reverses=4),
                                    dict(executed="2026-11-23T15:00:00"), dict(action="Exercise")])
def test_an_equity_row_that_is_not_a_clean_trade_is_refused(change):
    m = classify_transaction(item(**change))
    assert m["kind"] == "other"


def test_a_reversal_keeps_what_it_reverses():
    assert classify_transaction(item(rid=9, reverses=4))["reverses_id"] == 4


@pytest.mark.parametrize("sub,description", [("Forward Split", ""), ("Symbol Change", ""),
                                             ("Receive Deliver", "Spin-off of NEWCO"), ("Merger", "")])
def test_a_corporate_action_refuses_the_whole_account(sub, description):
    m = classify_transaction(item(ttype="Receive Deliver", sub=sub, action=None, price=None, description=description))
    assert m["kind"] == "split"


def test_plain_cash_without_symbol_or_quantity_is_ignored():
    for instrument in (None, "Unknown"):
        assert classify_transaction(item(ttype="Money Movement", sub="Credit Interest", instrument=instrument,
                                         symbol=None, underlying=None, action=None, quantity=None,
                                         price=None)) is None


def test_an_unknown_instrument_row_with_a_symbol_or_quantity_is_refused():
    m = classify_transaction(item(ttype="Receive Deliver", sub="Journal", instrument="Unknown", action=None))
    assert m["kind"] == "other"
    m = classify_transaction(item(ttype="Receive Deliver", sub="Journal", instrument=None, symbol="AAOI",
                                  action=None, quantity=None, price=None))
    assert m["kind"] == "other"


def test_an_instrument_type_not_in_the_reference_is_refused():
    assert classify_transaction(item(instrument="Something New"))["kind"] == "other"


# ---- the read: completeness from the server's pagination block ----------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, ""

    def json(self):
        return self.body


class FakeClient:
    """The session's httpx client: answers GET /accounts/<n>/transactions from a list of raw rows."""

    def __init__(self, rows, cap=None, ignore_offset=False, pagination=True, total_items=None):
        self.rows, self.cap, self.ignore_offset, self.pagination = rows, cap, ignore_offset, pagination
        self.total_items = total_items
        self.calls = []
        self.tracker = {"now": 0, "max": 0}

    async def get(self, url, params=None):
        self.tracker["now"] += 1
        self.tracker["max"] = max(self.tracker["max"], self.tracker["now"])
        await asyncio.sleep(0)
        self.calls.append((url, dict(params)))
        per = min(params["per-page"], self.cap or params["per-page"])
        offset = 0 if self.ignore_offset else params["page-offset"]
        page = self.rows[offset * per:(offset + 1) * per]
        total = len(self.rows) if self.total_items is None else self.total_items
        body = {"data": {"items": page}}
        if self.pagination:
            body["pagination"] = {"per-page": per, "page-offset": offset, "item-offset": offset * per,
                                  "total-items": total, "total-pages": -(-total // per) if total else 0,
                                  "current-item-count": len(page), "previous-link": None, "next-link": None}
        self.tracker["now"] -= 1
        return FakeResponse(body)


def _account(client):
    acct = _bare_account()
    acct._account.account_number = ACCOUNT_NUMBER

    async def refresh(force=False):
        return None

    acct._session = type("S", (), {})()
    acct._session.refresh = refresh
    acct._session._client = client
    timeouts = []

    def run(coro, timeout=None):
        timeouts.append(timeout)
        return asyncio.run(coro)

    acct._run_async = run
    return acct, timeouts


def _rows(n, start=1):
    return [item(rid=i, executed=f"2026-11-{1 + i // 20:02d}T15:{i % 60:02d}:00.000+00:00")
            for i in range(start, start + n)]


def test_a_complete_read_follows_the_servers_pages_through_the_sessions_client():
    client = FakeClient(_rows(250), cap=100)
    acct, timeouts = _account(client)
    got = acct.read_share_movements(date(2025, 12, 28))
    assert got["pages"] == 3 and got["total_items"] == 250 and len(got["rows"]) == 250
    assert [c[1]["page-offset"] for c in client.calls] == [0, 1, 2]
    url, params = client.calls[0]
    assert url == f"/accounts/{ACCOUNT_NUMBER}/transactions"
    assert params["sort"] == "Asc" and params["start-date"] == "2025-12-28"
    assert timeouts == [SHARE_HISTORY_PAGE_TIMEOUT_S] * 3


def test_a_row_the_sdk_model_would_reject_does_not_fail_the_read():
    rows = [item(rid=1), item(rid=2, action="Allocate", sub="Allocate", executed="2026-11-23T16:00:00.000+00:00"),
            item(rid=3, instrument="Event Contract", executed="2026-11-23T17:00:00.000+00:00")]
    acct, _t = _account(FakeClient(rows))
    got = acct.read_share_movements(date(2025, 12, 28))
    assert [(r["id"], r["kind"]) for r in got["rows"]] == [(1, "trade"), (2, "other")]


def test_a_server_that_ignores_page_offset_is_an_incomplete_read():
    acct, _t = _account(FakeClient(_rows(250), cap=100, ignore_offset=True))
    with pytest.raises(ShareHistoryIncomplete):
        acct.read_share_movements(date(2025, 12, 28))


def test_a_response_without_a_pagination_block_is_an_incomplete_read():
    acct, _t = _account(FakeClient(_rows(3), pagination=False))
    with pytest.raises(ShareHistoryIncomplete):
        acct.read_share_movements(date(2025, 12, 28))


def test_fewer_rows_than_the_servers_total_is_an_incomplete_read():
    acct, _t = _account(FakeClient(_rows(3), total_items=4))
    with pytest.raises(ShareHistoryIncomplete):
        acct.read_share_movements(date(2025, 12, 28))


def test_an_empty_history_is_complete_only_when_the_server_says_zero():
    acct, _t = _account(FakeClient([]))
    assert acct.read_share_movements(date(2025, 12, 28))["rows"] == []
    acct, _t = _account(FakeClient([], pagination=False))
    with pytest.raises(ShareHistoryIncomplete):
        acct.read_share_movements(date(2025, 12, 28))


def test_a_repeated_id_or_rows_out_of_order_is_an_incomplete_read():
    rows = _rows(3)
    acct, _t = _account(FakeClient(rows + [dict(rows[1])]))
    with pytest.raises(ShareHistoryIncomplete):
        acct.read_share_movements(date(2025, 12, 28))
    acct, _t = _account(FakeClient([item(rid=1, executed="2026-11-23T15:00:00.000+00:00"),
                                    item(rid=2, executed="2026-11-22T15:00:00.000+00:00")]))
    with pytest.raises(ShareHistoryIncomplete):
        acct.read_share_movements(date(2025, 12, 28))


def test_the_page_cap_is_its_own_kind_of_incomplete():
    acct, _t = _account(FakeClient(_rows(30), cap=10))
    with pytest.raises(ShareHistoryIncomplete) as caught:
        acct.read_share_movements(date(2025, 12, 28), max_pages=2)
    assert caught.value.heatmap_kind == "page_cap"


def test_the_read_stops_before_the_next_page_when_the_stop_flag_is_set():
    stop = threading.Event()
    client = FakeClient(_rows(250), cap=100)
    original = client.get

    async def get_then_stop(url, params=None):
        stop.set()
        return await original(url, params)

    client.get = get_then_stop
    acct, _t = _account(client)
    with pytest.raises(ShareHistoryStopped):
        acct.read_share_movements(date(2025, 12, 28), stop=stop)
    assert len(client.calls) == 1


def test_a_failure_carries_no_account_number():
    class Broken(FakeClient):
        async def get(self, url, params=None):
            raise ConnectionError(f"GET https://api.tastyworks.com{url} failed for {ACCOUNT_NUMBER}")

    acct, _t = _account(Broken([]))
    with pytest.raises(Exception) as caught:
        acct.read_share_movements(date(2025, 12, 28))
    assert ACCOUNT_NUMBER not in str(caught.value)


def test_pages_are_requested_one_after_the_other():
    client = FakeClient(_rows(250), cap=50)
    acct, _t = _account(client)
    acct.read_share_movements(date(2025, 12, 28))
    assert client.tracker["max"] == 1


# ---- cash Money Movement rows (dividends, interest, fees, withholding) -------------------------------------------

@pytest.mark.parametrize("instrument", ["Equity", None, "Unknown"])
@pytest.mark.parametrize("quantity", [None, "", "0", "0.0"])
def test_a_cash_money_movement_is_ignored_whatever_its_instrument_or_symbol(instrument, quantity):
    for sub in ("Dividend", "Credit Interest", "Fee", "Withholding", "Deposit"):
        assert classify_transaction(item(ttype="Money Movement", sub=sub, instrument=instrument, symbol="KBWD",
                                         action=None, quantity=quantity, price=None,
                                         description="KBWD dividend")) is None


def test_a_money_movement_with_a_quantity_still_refuses_its_symbol():
    m = classify_transaction(item(ttype="Money Movement", sub="Dividend", instrument="Equity", symbol="KBWD",
                                  action=None, quantity="1.5", price=None))
    assert m["kind"] == "other" and m["symbol"] == "KBWD"


def test_cash_dividends_on_held_and_sold_symbols_never_reach_the_heatmap():
    dividend = dict(ttype="Money Movement", sub="Dividend", action=None, quantity=None, price=None)
    rows = [item(rid=1),
            item(rid=2, symbol="AAOI", underlying=None, executed="2026-11-23T16:00:00.000+00:00", **dividend),
            item(rid=3, symbol="SOLDCO", underlying=None, executed="2026-11-23T17:00:00.000+00:00", **dividend)]
    acct, _t = _account(FakeClient(rows))
    got = acct.read_share_movements(date(2025, 12, 28))
    assert [(r["id"], r["kind"]) for r in got["rows"]] == [(1, "trade")]
