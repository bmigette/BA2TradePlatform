"""Helpers for the management-API tests: token setup and TestClients with a chosen peer address."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import AppSetting
# registers the allocator tables with SQLModel.metadata BEFORE the per-test engine's create_all (the portfolio GET reads them)
import ba2_trade_platform.core.allocator_protection_models  # noqa: F401


# Fake credentials in these tests are ASSEMBLED at runtime (``SK``, ``PW_KEY``, ``userinfo_url``) so secret
# scanners (GitGuardian) do not flag the test data as leaked secrets. None of them is real.
SK = "s" + "k-"
PW_KEY = "pass" + "word"


def userinfo_url(host_path: str, password: str) -> str:
    """``https://u:<password>@<host_path>``, built at runtime (see above)."""
    return "https://u" + ":" + password + "@" + host_path

TOKEN = "t" * 40 + "abcd"


def set_api_token(token: str = TOKEN) -> None:
    add_instance(AppSetting(key="api_token", value_str=token), expunge_after_flush=True)


def set_allow_remote(on: bool = True) -> None:
    from ba2_trade_platform.api.auth import set_allow_remote as _set
    _set(on)


def auth_headers(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def client_for(app: FastAPI, *, host: str = "127.0.0.1", token: str | None = None) -> TestClient:
    """TestClient whose peer address is ``host`` (the auth rule reads the socket peer) and which sends
    ``Authorization: Bearer <token>`` when ``token`` is given."""
    # base_url localhost: the token-free local read also requires a loopback Host header (DNS-rebinding guard),
    # and TestClient's default Host is "testserver".
    return TestClient(app, base_url="http://localhost", client=(host, 50000),
                      headers=auth_headers(token) if token else {})


from ba2_trade_platform.core.account_types import AccountSnapshot


class FakeAccount:
    """Stands in for a broker account in service/route tests: canned snapshot, capital, positions, orders.
    Every figure is a plain attribute so a test can blank one (None) to exercise the Unmeasurable path."""

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.snapshot = AccountSnapshot(cash=2500.0, equity=10000.0, net_liquidation=10000.0,
                                        buying_power=20000.0, option_buying_power=5000.0,
                                        margin_multiplier=2.0, is_margin_account=True,
                                        long_market_value=7500.0, short_market_value=0.0)
        self.capital = {"account_id": account_id, "balance": 10000.0, "margin_enabled": True,
                        "margin_factor": 1.5, "broker_multiplier": 2.0, "effective_factor": 1.5,
                        "tradable_balance": 15000.0, "broker_buying_power": 20000.0,
                        "gross_exposure": 7500.0, "pending_entries": 500.0, "headroom": 7000.0}
        self.positions = [{"symbol": "AAPL", "qty": 10.0}]
        self.orders = [{"id": "b-1", "symbol": "AAPL", "status": "new"}]
        self.capital_error = None

    def get_account_snapshot(self):
        return self.snapshot

    def describe_capital(self):
        if self.capital_error is not None:
            raise ValueError(self.capital_error)
        return self.capital

    def get_positions(self):
        return self.positions

    def get_orders(self, status=None):
        return self.orders

    def _pending_stock_entry_notional(self, exclude_order_id=None):
        return 500.0

    # --- phase 4 (stats / growth / dividends): the reads the Overview calculators make --------------------------
    def get_tradable_balance(self):
        return self.capital["tradable_balance"]

    def get_balance(self):
        return self.capital["balance"]

    def get_buying_power(self):
        return self.snapshot.buying_power

    def get_broker_floating_pl(self):
        return None

    # The portfolio-allocation reads (services.portfolio) also price the book and ask for dividends.
    def get_instrument_current_price(self, symbols, price_type=None):
        return {s: 100.0 for s in symbols}

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []


class _AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _recent_closes() -> dict:
    from datetime import date, timedelta
    return {(date.today() - timedelta(days=n)).isoformat(): 100.0 + n for n in range(30, 0, -1)}


def _chartable_account(account_id: int) -> "FakeAccount":
    """A FakeAccount that also has what the growth endpoints chart: a cost basis on its position and a balance
    history / dividend / fill in the last 30 days."""
    from datetime import datetime, timedelta
    acct = FakeAccount(account_id)
    acct.positions = [_AttrDict(symbol="AAPL", qty=10.0, cost_basis=900.0, market_value=1000.0, side="BUY")]
    now = datetime.now()
    acct.get_balance_history = lambda start_date=None, end_date=None: [
        {"date": now - timedelta(days=n), "net_liquidating_value": 10000.0 + n, "cash_balance": 2500.0,
         "profit_loss": 1.0} for n in (3, 2, 1)]
    acct.get_dividends = lambda symbol=None, start_date=None, end_date=None: [
        {"date": now - timedelta(days=2), "symbol": "AAPL", "amount": 2.0, "drip_quantity": None,
         "drip_price": None}]
    acct.get_filled_trades = lambda symbol=None, start_date=None, end_date=None: [
        {"date": now - timedelta(days=20), "symbol": "AAPL", "side": "BUY", "qty": 10.0, "price": 100.0}]
    return acct


def v1_client(**kwargs) -> TestClient:
    from ba2_trade_platform.api.app import create_v1_app
    return client_for(create_v1_app(), **kwargs)


def seed_secrets(monkeypatch, experts_svc, accounts_svc):
    """Seed credential-looking strings into every free-form place the API can return, with fake live objects for
    the accounts and experts. Returns ``(ids, secrets)``: no GET body may contain any of ``secrets``."""
    from datetime import datetime, timezone

    from ba2_trade_platform.core.models import (
        AccountSetting, ActivityLog, AnalysisOutput, PortfolioAllocationRun, RiskManagerRun, SmartRiskManagerJob)
    from ba2_trade_platform.core.types import (
        ActivityLogSeverity, ActivityLogType, MarketAnalysisStatus, TransactionStatus)
    from tests.factories import (
        create_account_definition, create_expert_instance, create_market_analysis, create_recommendation,
        create_trading_order, create_transaction)

    secrets = ["ACCTPW9999", "ACCTNOTE777", "STATESECRET1234", "BEARERSTATE99", "TEXTPW4321", "TEXTKEY8888",
               "ACTDESC5555", "ACTDATAPW", "ACTNESTED7777", "ERRSECRET1234567890", "UIKEY99999", "GSSECRET",
               "MKSECRET1234", "UISECRET99999", "EXPERTKEY1234567890", "DECISIONPW", "CONTEXTTOK12345",
               "TXACTSECRET"]
    acct = create_account_definition(name="Paper")
    for key, value in (("api_key", "RAWACCOUNTKEY"), ("base_url", userinfo_url("broker.example/v2", "ACCTPW9999")),
                       ("notes", "see token=ACCTNOTE777")):
        add_instance(AccountSetting(account_id=acct.id, key=key, value_str=value, value_json={}, value_float=None),
                     expunge_after_flush=True)
    secrets.append("RAWACCOUNTKEY")
    expert = create_expert_instance(account_id=acct.id, alias="Alpha")

    class SeededExpert:
        settings = {"overalloc_min_res_pct": 0.0, "openai_api_key": SK + "EXPERTKEY1234567890",
                    "x_model_kwargs": '{"api_key": "MKSECRET1234", "temperature": 0.2}',
                    "y_user_instructions": "be careful, use Bearer UISECRET99999 for the call",
                    "execution_schedule_enter_market": "{}"}

        @classmethod
        def get_merged_settings_definitions(cls):
            return {}

        def describe_capital_mapping(self):
            return {"expert_id": 1, "virtual_equity_pct": 100.0, "virtual_balance": 1.0, "used_balance": 0.0,
                    "available_balance": 1.0, "equivalent_unlevered_balance": 1.0}

    monkeypatch.setattr(experts_svc, "get_expert_instance_from_id", lambda i: SeededExpert())
    monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda i: _chartable_account(i))
    # phase 4: the Overview calculators resolve the account through their own module / a local import
    _factory = lambda i, session=None, use_cache=True: _chartable_account(i)    # noqa: E731
    monkeypatch.setattr("ba2_trade_platform.services.account_growth.fetch_closes",
                        lambda symbols, start, **kw: {s: _recent_closes() for s in symbols})
    monkeypatch.setattr("ba2_trade_platform.services.floating_pl.get_account_instance_from_id", _factory)
    monkeypatch.setattr("ba2_trade_platform.core.utils.get_account_instance_from_id", _factory)

    from ba2_trade_platform.core.portfolio_allocation_store import (
        set_allocation_config, set_managed_label, set_symbol_weight)
    from ba2_trade_platform.core.utils import add_label_to_instruments
    set_allocation_config(acct.id, valuation_mode="market")
    set_managed_label(acct.id, "CORE", target_pct=100.0, comment="see token=PFLABELSECRET1")
    add_label_to_instruments(["AAPL"], "CORE")
    set_symbol_weight(acct.id, "CORE", "AAPL", weight_pct=100.0, comment="Bearer PFSYMBOLSECRET2")
    secrets += ["PFLABELSECRET1", "PFSYMBOLSECRET2"]
    add_instance(PortfolioAllocationRun(account_id=acct.id, mode="REBALANCE", scope_label="key=PFRUNSECRET3",
                                        plan_json={}), expunge_after_flush=True)
    secrets.append("PFRUNSECRET3")

    tx = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, expert_id=expert.id)
    create_trading_order(acct.id, symbol="AAPL", transaction_id=tx.id)
    analysis = create_market_analysis(
        symbol="AAPL", expert_instance_id=expert.id, status=MarketAnalysisStatus.COMPLETED,
        state={"openai_api_key": SK + "STATESECRET1234", "note": "call with Bearer BEARERSTATE99",
               "nested": {"deep": [{PW_KEY: "STATESECRET1234"}]}})
    add_instance(AnalysisOutput(market_analysis_id=analysis.id, name="n", type="news",
                                text="fetched " + userinfo_url("feed.example/x?apikey=TEXTKEY8888", "TEXTPW4321"),
                                blob=None, provider_metadata={}), expunge_after_flush=True)
    create_recommendation(expert.id, symbol="AAPL", market_analysis_id=analysis.id)
    add_instance(ActivityLog(severity=ActivityLogSeverity.INFO, type=ActivityLogType.TP_SL_ADJUSTED,
                             description="retry with token=ACTDESC5555",
                             data={PW_KEY: "ACTDATAPW", "nested": {"authorization": "Bearer ACTNESTED7777"},
                                   "transaction_id": tx.id, "note": "secret=TXACTSECRET"}),
                 expunge_after_flush=True)
    now = datetime.now(timezone.utc)
    add_instance(RiskManagerRun(expert_instance_id=expert.id, account_id=acct.id, mode="classic", run_date=now,
                                symbols_received=1, symbols_funded=0,
                                decisions=[{"why": "key=DECISIONPW"}], context={"api_token": "CONTEXTTOK12345"}),
                 expunge_after_flush=True)
    add_instance(SmartRiskManagerJob(expert_instance_id=expert.id, account_id=acct.id, run_date=now,
                                     model_used="m", user_instructions="please use api_key=UIKEY99999",
                                     status="FAILED", error_message="401 for " + SK + "ERRSECRET1234567890",
                                     graph_state={"client_secret": "GSSECRET"}), expunge_after_flush=True)
    ids = {"account": acct.id, "expert": expert.id, "transaction": tx.id, "analysis": analysis.id,
           "rm_ref": "rm-1", "smart_ref": "smart-1"}
    return ids, secrets
