"""services.portfolio read-only mode: a GET-side load never writes the DB nor asks the broker to refresh."""
import pytest
from sqlmodel import select

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountSymbolFacts, PortfolioAllocationConfig, PortfolioIncomeEvent
from ba2_trade_platform.services import portfolio as svc
from tests.portfolio_golden import build_scenario

pytestmark = pytest.mark.api_db


def _use(monkeypatch):
    import ba2_trade_platform.core.utils as core_utils

    def install(account):
        monkeypatch.setattr(core_utils, "get_account_instance_from_id", lambda account_id: account)
    return install


def _count(model):
    with get_db() as session:
        return len(session.exec(select(model)).all())


def test_read_only_load_does_not_refresh_facts_or_touch_the_broker_writers(monkeypatch):
    acct_id, account = build_scenario(_use(monkeypatch))
    config = svc.peek_allocation_config(acct_id)
    payload = svc.load_view_payload(acct_id, config.valuation_mode, read_only=True, config=config)
    assert account.margin_requests == []                       # refresh_symbol_facts never ran
    assert "get_symbol_margin_info" not in account.calls
    assert payload["symbol_facts"] == {}                       # stored rows only (none stored here)
    assert _count(AccountSymbolFacts) == 0                     # ...and nothing was written


def test_the_page_mode_still_refreshes_the_facts(monkeypatch):
    acct_id, account = build_scenario(_use(monkeypatch))
    payload = svc.load_view_payload(acct_id, "market")
    assert account.margin_requests and set(payload["symbol_facts"]) == {"AAPL"}
    assert _count(AccountSymbolFacts) == 1


def test_peek_never_creates_the_config_row(monkeypatch):
    from tests.factories import create_account_definition
    acct = create_account_definition(name="Bare", provider="MockAccount")
    assert svc.peek_allocation_config(acct.id) is None
    assert _count(PortfolioAllocationConfig) == 0
    svc.load_valuation_mode(acct.id)                           # the page's call DOES create it
    assert _count(PortfolioAllocationConfig) == 1


def test_read_income_panel_does_not_sync(monkeypatch):
    acct_id, account = build_scenario(_use(monkeypatch))
    before = _count(PortfolioIncomeEvent)
    events, open_total, note = svc.read_income_panel(acct_id)
    assert "get_cash_transfers" not in account.calls and _count(PortfolioIncomeEvent) == before
    assert open_total == 112.5 and len(events) == 2 and note is None
    svc.load_income_panel(acct_id)                             # the page's call syncs
    assert "get_cash_transfers" in account.calls


def test_snapshot_failure_is_flagged_not_zeroed(monkeypatch):
    acct_id, _ = build_scenario(_use(monkeypatch), snapshot_error=ConnectionError("down"))
    payload = svc.load_view_payload(acct_id, "market", read_only=True, config=svc.peek_allocation_config(acct_id))
    assert payload["snapshot_available"] is False
    assert payload["base_notional"] is None and payload["account_value"] is None
