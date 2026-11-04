"""GET /accounts/{id}/portfolio."""
import pytest

from ba2_trade_platform.core.models import AccountSymbolFacts
from ba2_trade_platform.services import accounts as accounts_svc
from tests.api_helpers import v1_client
from tests.factories import create_account_definition
from tests.portfolio_golden import build_scenario

pytestmark = pytest.mark.api_db


@pytest.fixture(autouse=True)
def _fresh_dividends_cache():
    """The dividends TTL cache is keyed by account id and every test's first account is id 1."""
    from ba2_trade_platform.services import portfolio as portfolio_svc
    portfolio_svc._DIVIDENDS_CACHE.clear()
    yield
    portfolio_svc._DIVIDENDS_CACHE.clear()


def _installer(monkeypatch):
    def install(account):
        monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda account_id: account)
    return install


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
    # SPY is held and unpriced: compute_base_notional values it at 0, so 10650 is only a LOWER BOUND.
    assert body["base_notional"] is None and body["base_notional_lower_bound"] == 10650.0
    assert "base_notional" in body["unmeasured"] and body["unpriced_managed_holdings"] == ["SPY"]
    assert body["unallocated_pct"] == 10.0
    core = _label(body, "CORE")
    assert core["target_pct"] == 60.0 and core["complete"] is True
    assert core["current_value"] == 4000.0 and core["invested"] == 3300.0
    assert core["current_pct_of_base"] is None and core["target_value"] is None      # base-derived
    assert core["pnl"]["amount"] == 700.0 and core["pnl"]["with_dividends_amount"] is None
    assert core["symbol_counts"] == {"total": 3, "held": 3, "excluded": 1, "unmeasured": 0}
    aapl = next(s for s in core["symbols"] if s["symbol"] == "AAPL")
    assert aapl["target_share_pct"] == 50.0 and aapl["current_value"] == 2000.0
    assert aapl["target_value"] is None and aapl["pct_of_label_target"] is None   # base-derived
    assert aapl["pct_of_label"] is not None                                        # CORE is complete
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


def test_complete_book_reports_the_base_and_every_derived_figure(monkeypatch):
    acct_id, _ = build_scenario(_installer(monkeypatch), extra_prices={"SPY": 525.0})
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    assert body["base_notional"] == 12750.0 and body["base_notional_lower_bound"] is None
    assert body["unmeasured"] == [] and body["unpriced_managed_holdings"] == []
    core = _label(body, "CORE")
    assert core["target_value"] is not None and core["current_pct_of_base"] is not None
    aapl = next(s for s in core["symbols"] if s["symbol"] == "AAPL")
    assert aapl["pct_of_total"] is not None and aapl["pct_of_label"] is not None
    assert aapl["target_value"] is not None and aapl["pct_of_label_target"] is not None


def test_all_none_snapshot_is_listed_as_unmeasured(monkeypatch):
    """AlpacaAccount.get_account_snapshot returns an all-None snapshot on auth failure WITHOUT raising."""
    acct_id, _ = build_scenario(_installer(monkeypatch), snapshot_all_none=True, extra_prices={"SPY": 525.0})
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    assert body["base_notional"] is None and body["base_notional_lower_bound"] is None
    assert body["available_buying_power"] is None and body["account_value"] is None
    assert {"base_notional", "available_buying_power", "account_value"} <= set(body["unmeasured"])


def test_pct_of_label_is_null_for_every_row_of_an_incomplete_label(scenario):
    acct_id, _ = scenario
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    income = _label(body, "INCOME")
    assert all(r["pct_of_label"] is None for r in income["symbols"])
    assert next(r for r in income["symbols"] if r["symbol"] == "O")["measured"] is True   # measured, yet no share


def test_pct_of_total_is_null_for_every_row_when_any_symbol_is_unmeasured(scenario):
    acct_id, _ = scenario
    body = v1_client().get(f"/accounts/{acct_id}/portfolio").json()
    assert all(r["pct_of_total"] is None for lb in body["labels"] for r in lb["symbols"])
    assert all(lb["pct_of_managed"] is None for lb in body["labels"])


def test_dividends_are_cached_for_60s_and_the_age_is_reported(scenario, monkeypatch):
    from ba2_trade_platform.services import portfolio as portfolio_svc
    acct_id, account = scenario
    clock = [1000.0]
    monkeypatch.setattr(portfolio_svc.time, "monotonic", lambda: clock[0])
    c = v1_client()
    first = c.get(f"/accounts/{acct_id}/portfolio").json()["dividends"]
    clock[0] += 30.0
    second = c.get(f"/accounts/{acct_id}/portfolio").json()["dividends"]
    assert account.dividend_calls == 1
    assert first["cached"] is False and first["age_s"] == 0.0
    assert second["cached"] is True and second["age_s"] == 30.0 and second["payments"] == first["payments"]
    clock[0] += 31.0
    third = c.get(f"/accounts/{acct_id}/portfolio").json()["dividends"]
    assert account.dividend_calls == 2 and third["cached"] is False


def test_an_empty_dividend_answer_is_never_cached(monkeypatch):
    acct_id, account = build_scenario(_installer(monkeypatch), dividends=False)
    c = v1_client()
    c.get(f"/accounts/{acct_id}/portfolio")
    c.get(f"/accounts/{acct_id}/portfolio")
    assert account.dividend_calls == 2


@pytest.mark.parametrize("model_name", ["AccountSymbolFacts", "SymbolMarketStats"])
def test_unreadable_stored_rows_are_503_not_empty(scenario, monkeypatch, model_name):
    import ba2_trade_platform.core.models as models
    acct_id, _ = scenario
    monkeypatch.setattr(models, model_name, object())        # select(<object>) raises inside the strict reader
    r = v1_client().get(f"/accounts/{acct_id}/portfolio")
    assert r.status_code == 503 and r.json()["error"] == "unmeasurable"
    assert r.json()["detail"]["stored_read_failed"] in ("symbol_facts", "symbol_stats")
