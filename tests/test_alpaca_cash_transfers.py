"""AlpacaAccount.get_cash_transfers against a mocked activities endpoint.

Deposits (CSD) and withdrawals (CSW) come from /account/activities/<TYPE>;
dividends come through the existing get_dividends(), which itself reads
/account/activities/DIV and nets out DIVNRA tax withholding.

No live API call: client.get is a MagicMock with a routing side_effect.
"""
from datetime import date
from unittest.mock import MagicMock

from ba2_trade_platform.core.account_types import (
    CASH_TRANSFER_DEPOSIT, CASH_TRANSFER_DIVIDEND, CASH_TRANSFER_WITHDRAWAL,
)
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


def _bare_account(activities):
    """activities: {"CSD": [...], "CSW": [...], "DIV": [...], "DIVNRA": [...]}"""
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._margin_info_cache = {}

    def _get(path, params=None):
        for key in ("DIVNRA", "DIV", "CSD", "CSW"):
            if path.endswith("/" + key):
                return activities.get(key, [])
        return []

    acct.client.get.side_effect = _get
    return acct


def _by_type(transfers):
    return {t.event_type: t for t in transfers}


def test_a_deposit_becomes_a_positive_income_event_keyed_by_the_broker_activity_id():
    acct = _bare_account({"CSD": [{"id": "act-1", "date": "2026-08-01",
                                   "net_amount": "1000", "description": "ACH IN"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DEPOSIT]

    assert ev.external_id == "act-1"
    assert ev.event_date == date(2026, 8, 1)
    assert ev.amount == 1000.0
    assert ev.symbol is None
    assert ev.is_income is True


def test_the_external_id_is_the_real_alpaca_activity_id_not_a_synthesised_one():
    """Alpaca non-trade activity ids look like <17 digits>::<uuid>; the ledger
    upserts on (account_id, external_id), so it must be carried through verbatim."""
    broker_id = "20260801000000000::9b8e1b4e-1a2f-4c3d-9e5a-6f7a8b9c0d1e"
    acct = _bare_account({"CSD": [{"id": broker_id, "date": "2026-08-01",
                                   "net_amount": "1000"}]})

    assert acct.get_cash_transfers()[0].external_id == broker_id


def test_a_withdrawal_is_negative_and_is_not_income():
    acct = _bare_account({"CSW": [{"id": "act-2", "date": "2026-08-05",
                                   "net_amount": "-250"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_WITHDRAWAL]

    assert ev.amount == -250.0
    assert ev.is_income is False


def test_a_withdrawal_reported_with_a_positive_amount_is_still_negated():
    """Do not depend on the broker's sign convention for CSW."""
    acct = _bare_account({"CSW": [{"id": "act-2", "date": "2026-08-05",
                                   "net_amount": "250"}]})

    assert _by_type(acct.get_cash_transfers())[CASH_TRANSFER_WITHDRAWAL].amount == -250.0


def test_a_reversed_deposit_keeps_its_negative_sign_and_is_not_income():
    """A clawed-back ACH arrives as a CSD with a NEGATIVE net_amount.

    CashTransfer.is_income guards this with ``amount > 0`` (pinned by
    packages/common/tests/test_account_types.py), so the adapter must NOT
    abs() a deposit -- that would resurrect the clawback as new money.
    """
    acct = _bare_account({"CSD": [{"id": "act-6", "date": "2026-08-07",
                                   "net_amount": "-1000"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DEPOSIT]

    assert ev.amount == -1000.0
    assert ev.is_income is False


def test_a_dividend_is_keyed_by_the_broker_activity_id_when_there_is_one():
    """CONTRACT: the key is DIV:<broker activity id>, not DIV:<symbol>:<date>.

    This test previously asserted "DIV:AAPL:2026-08-10". Alpaca supplies an ``id``
    on every non-trade activity; get_dividends() simply never copied it into
    div_record, and netting DIVNRA withholding into the AMOUNT says nothing about
    the DIV activity's own id. Keying on symbol+date collapsed two DIV activities
    for one payer on one date -- a special dividend paid alongside the regular
    one, or a correction -- into a single ledger row, because external_id is the
    (account_id, external_id) UPSERT key.
    """
    acct = _bare_account({"DIV": [{"id": "act-3", "symbol": "AAPL",
                                   "date": "2026-08-10", "net_amount": "12.34"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DIVIDEND]

    assert ev.symbol == "AAPL"
    assert ev.amount == 12.34
    assert ev.event_date == date(2026, 8, 10)
    assert ev.external_id == "DIV:act-3"
    assert ev.is_income is True


def test_two_dividends_from_one_payer_on_one_date_stay_two_ledger_rows():
    """The whole point of I1. A special dividend alongside the regular one, or a
    correction, shares symbol AND pay date; under the old symbol/date key the two
    upserted onto each other and one payment vanished from the ledger."""
    acct = _bare_account({"DIV": [
        {"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "12.34"},
        {"id": "act-4", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "500.00"},
    ]})

    transfers = acct.get_cash_transfers()

    assert len(transfers) == 2
    assert {t.external_id for t in transfers} == {"DIV:act-3", "DIV:act-4"}
    assert sorted(t.amount for t in transfers) == [12.34, 500.0]


def test_a_dividend_with_no_broker_id_falls_back_to_the_symbol_and_date_key():
    """The synthetic key is the FALLBACK now, not the only option -- still stable,
    still namespaced so it cannot be mistaken for a broker id."""
    acct = _bare_account({"DIV": [{"symbol": "AAPL", "date": "2026-08-10",
                                   "net_amount": "12.34"}]})

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DIVIDEND]

    assert ev.external_id == "DIV:AAPL:2026-08-10"


def test_the_nra_netting_does_not_cost_the_dividend_its_broker_id():
    """The old rationale for dropping the id was that get_dividends() nets DIVNRA
    withholding into the amount. Netting the amount and carrying the id are
    independent -- pin that they now coexist."""
    acct = _bare_account({
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "100.00"}],
        "DIVNRA": [{"symbol": "AAPL", "date": "2026-08-10", "net_amount": "-15.00"}],
    })

    ev = _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DIVIDEND]

    assert ev.amount == 85.0
    assert ev.external_id == "DIV:act-3"


def test_get_dividends_exposes_the_broker_activity_id():
    """The seam I1 actually widened: div_record now carries 'id'."""
    acct = _bare_account({"DIV": [{"id": "act-3", "symbol": "AAPL",
                                   "date": "2026-08-10", "net_amount": "12.34"}]})

    assert acct.get_dividends()[0]["id"] == "act-3"


def test_a_dividend_amount_is_net_of_the_nra_tax_withholding():
    acct = _bare_account({
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "100.00"}],
        "DIVNRA": [{"symbol": "AAPL", "date": "2026-08-10", "net_amount": "-15.00"}],
    })

    assert _by_type(acct.get_cash_transfers())[CASH_TRANSFER_DIVIDEND].amount == 85.0


def test_a_dividend_without_a_payer_symbol_is_still_skipped():
    """Unchanged behaviour, but the reason narrowed.

    It used to be "DIV:None:<date> is a fabricated identity". Now that the broker
    id keys the row, the identity would be real -- and the row is STILL dropped,
    because a dividend that names no payer cannot be attributed to an instrument
    and the income ledger is per-symbol. Loud (a warning), never guessed.
    """
    acct = _bare_account({"DIV": [{"id": "act-3", "symbol": None,
                                   "date": "2026-08-10", "net_amount": "12.34"}]})

    assert acct.get_cash_transfers() == []


def test_all_three_activity_kinds_come_back_from_one_call():
    acct = _bare_account({
        "CSD": [{"id": "act-1", "date": "2026-08-01", "net_amount": "1000"}],
        "CSW": [{"id": "act-2", "date": "2026-08-05", "net_amount": "-250"}],
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "12.34"}],
    })

    assert len(acct.get_cash_transfers()) == 3


def test_a_dividend_id_can_never_collide_with_a_cash_transfer_id():
    """Two id spaces: CSD/CSW carry the broker's own id, dividends a DIV: key.

    Worst case -- the broker hands a CSD the very id a dividend would produce --
    the two events must still be distinct ledger rows.
    """
    acct = _bare_account({
        "CSD": [{"id": "DIV:act-3", "date": "2026-08-10", "net_amount": "1000"}],
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "12.34"}],
    })

    ids = [t.external_id for t in acct.get_cash_transfers()]

    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_resyncing_the_same_window_yields_the_same_external_ids():
    """The (account_id, external_id) upsert key must be stable across calls."""
    payload = {
        "CSD": [{"id": "act-1", "date": "2026-08-01", "net_amount": "1000"}],
        "CSW": [{"id": "act-2", "date": "2026-08-05", "net_amount": "-250"}],
        "DIV": [{"id": "act-3", "symbol": "AAPL", "date": "2026-08-10", "net_amount": "12.34"}],
    }
    acct = _bare_account(payload)

    first = [t.external_id for t in acct.get_cash_transfers()]
    second = [t.external_id for t in acct.get_cash_transfers()]

    assert first == second == ["act-1", "act-2", "DIV:act-3"]


def test_the_date_window_is_passed_to_the_broker_as_after_and_until():
    acct = _bare_account({"CSD": []})

    acct.get_cash_transfers(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))

    csd_call = next(c for c in acct.client.get.call_args_list if c[0][0].endswith("/CSD"))
    assert csd_call[0][1] == {"after": "2026-08-01", "until": "2026-08-31"}


def test_an_activity_with_no_usable_date_is_skipped_rather_than_guessed():
    acct = _bare_account({"CSD": [{"id": "act-1", "date": None, "net_amount": "1000"},
                                  {"id": "act-9", "date": "2026-08-02", "net_amount": "5"}]})

    transfers = acct.get_cash_transfers()

    assert [t.external_id for t in transfers] == ["act-9"]


def test_an_activity_with_no_usable_amount_is_skipped_rather_than_zeroed():
    acct = _bare_account({"CSD": [{"id": "act-1", "date": "2026-08-01", "net_amount": None},
                                  {"id": "act-9", "date": "2026-08-02", "net_amount": "5"}]})

    assert [t.external_id for t in acct.get_cash_transfers()] == ["act-9"]


def test_a_failing_activities_endpoint_yields_an_empty_list_not_an_exception():
    """This seam does NOT distinguish failure from emptiness -- it logs and returns []."""
    acct = _bare_account({})
    acct.client.get.side_effect = Exception("503 service unavailable")

    assert acct.get_cash_transfers() == []
