"""GET /accounts/{id}/portfolio."""
import pytest

from ba2_trade_platform.core.models import AccountSymbolFacts
from ba2_trade_platform.services import accounts as accounts_svc
from tests.api_helpers import v1_client
from tests.factories import create_account_definition
from tests.portfolio_golden import build_scenario

pytestmark = pytest.mark.api_db


@pytest.fixture
def scenario(monkeypatch):
    def install(account):
        monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda account_id: account)
    return build_scenario(install)


def _label(body, name):
    return next(lb for lb in body["labels"] if lb["label"] == name)


def test_portfolio_payload_per_label_and_symbol(scenario):
    acct_id, account = scenario
    r = v1_client().get(f"/accounts/{acct_id}/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["valuation_mode"] == "market" and "as_of" in body
    assert body["base_notional"] == 10650.0 and body["unallocated_pct"] == 10.0
    core = _label(body, "CORE")
    assert core["target_pct"] == 60.0 and core["complete"] is True
    assert core["current_value"] == 4000.0 and core["invested"] == 3300.0
    assert round(core["current_pct_of_base"], 3) == 37.559
    assert core["pnl"]["amount"] == 700.0 and core["pnl"]["with_dividends_amount"] is None
    assert core["symbol_counts"] == {"total": 3, "held": 3, "excluded": 1, "unmeasured": 0}
    aapl = next(s for s in core["symbols"] if s["symbol"] == "AAPL")
    assert aapl["target_share_pct"] == 50.0 and aapl["current_value"] == 2000.0
    assert next(s for s in core["symbols"] if s["symbol"] == "KO")["excluded"] is True
    income = _label(body, "INCOME")
    assert income["pnl"]["dividend_cash"] == 40.0 and income["pnl"]["with_dividends_amount"] == -10.0


def test_unpriced_holding_is_unmeasured_never_zero(scenario):
    acct_id, _ = scenario
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    income = _label(body, "INCOME")
    spy = next(s for s in income["symbols"] if s["symbol"] == "SPY")
    assert spy["measured"] is False and spy["current_value"] is None and spy["pct_of_label"] is None
    assert income["complete"] is False and income["unmeasured_symbols"] == ["SPY"]
    assert income["current_value"] is None and income["current_pct_of_base"] is None
    assert income["current_value_measured_part"] == 1650.0
    assert "label:INCOME" in body["unmeasured"]


def test_snapshot_failure_marks_the_base_unmeasured(monkeypatch):
    def install(account):
        monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda account_id: account)
    acct_id, _ = build_scenario(install, snapshot_error=ConnectionError("down"))
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    assert body["base_notional"] is None and body["account_value"] is None
    assert {"base_notional", "available_buying_power", "account_value"} <= set(body["unmeasured"])
    assert _label(body, "CORE")["current_pct_of_base"] is None and _label(body, "CORE")["target_value"] is None


def test_no_dividend_payments_is_said_not_zeroed(monkeypatch):
    def install(account):
        monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda account_id: account)
    acct_id, _ = build_scenario(install, dividends=False)
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    assert body["dividends"]["payments"] == 0 and "failed fetch" in body["dividends"]["note"]
    assert _label(body, "INCOME")["pnl"]["with_dividends_amount"] is None


def test_get_is_pure_no_facts_refresh_no_income_sync_no_writes(scenario):
    from sqlmodel import select

    from ba2_trade_platform.core.db import get_db
    acct_id, account = scenario
    v1_client().get(f"/accounts/{acct_id}/portfolio")
    assert account.margin_requests == [] and account.calls == []
    with get_db() as session:
        assert session.exec(select(AccountSymbolFacts)).all() == []


def test_stored_facts_are_returned_with_their_age(scenario):
    from ba2_trade_platform.core.db import add_instance
    acct_id, _ = scenario
    add_instance(AccountSymbolFacts(account_id=acct_id, symbol="AAPL", fractionable=True, marginable=True),
                 expunge_after_flush=True)
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    aapl = next(s for s in _label(body, "CORE")["symbols"] if s["symbol"] == "AAPL")
    assert aapl["facts"]["fractionable"] is True and aapl["facts"]["fetched_at"]
    assert body["stored"]["symbol_facts_fetched_at"] == aapl["facts"]["fetched_at"]


def test_no_portfolio_config_is_404_and_creates_nothing(monkeypatch):
    from sqlmodel import select

    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import PortfolioAllocationConfig
    d = create_account_definition(name="Bare", provider="MockAccount")
    r = v1_client().get(f"/accounts/{d.id}/portfolio")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert r.json()["message"] == "no portfolio on this account"
    with get_db() as session:
        assert session.exec(select(PortfolioAllocationConfig)).all() == []
    assert v1_client().get("/accounts/999/portfolio").status_code == 404


def test_failed_position_fetch_is_503_not_an_empty_book(monkeypatch):
    def install(account):
        monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda account_id: account)
    acct_id, _ = build_scenario(install, positions=None)
    r = v1_client().get(f"/accounts/{acct_id}/portfolio")
    assert r.status_code == 503 and r.json()["error"] == "broker_unreachable"


def test_broker_error_in_prices_maps_to_503(scenario):
    acct_id, account = scenario

    def boom(*a, **k):
        raise ConnectionError("net down")
    account.get_instrument_current_price = boom
    r = v1_client().get(f"/accounts/{acct_id}/portfolio")
    assert r.status_code == 503 and r.json()["error"] == "broker_unreachable"


def test_response_is_scrubbed(scenario):
    from ba2_trade_platform.core.portfolio_allocation_store import set_managed_label
    from tests.api_helpers import SK
    acct_id, _ = scenario
    set_managed_label(acct_id, "CORE", comment="note " + SK + "ABCDEFGH12345678")
    r = v1_client().get(f"/accounts/{acct_id}/portfolio")
    assert "ABCDEFGH12345678" not in r.text and "sk-[redacted]" in r.text
