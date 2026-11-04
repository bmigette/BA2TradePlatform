"""The strict, raise-on-failure variants of get_dividends / get_filled_trades / get_balance_history on the in-tree
adapters (the management API uses them; the lenient methods keep answering [] for the Overview page).

A strict read answers [] ONLY when the broker said there is nothing; any failure raises."""
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from tests.ibkr_helpers import make_account  # noqa: F401
from tests.test_tastytrade_account import _bare_account as tasty_account
from tests.test_tastytrade_account import _history_by_type, _money_movement, _snapshot


# ----------------------------------------------------------------------------------------------- Alpaca
def _alpaca(routes, authenticated=True):
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct._authentication_error = "no key"
    acct.client = MagicMock() if authenticated else None
    calls = []

    def get(path, params=None):
        calls.append((path, dict(params or {})))
        answer = routes[path.rsplit("/", 1)[1]]
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer
    if authenticated:
        acct.client.get.side_effect = get
    acct._calls = calls
    return acct


def _div(i, symbol="KO", net="1.00", day="2026-08-03"):
    return {"id": f"{i}", "symbol": symbol, "net_amount": net, "date": day}


def test_alpaca_strict_dividends_match_the_lenient_ones_on_a_healthy_broker():
    acct = _alpaca({"DIV": [_div(1)], "DIVNRA": [{"symbol": "KO", "net_amount": "-0.15", "date": "2026-08-03"}]})
    assert acct.get_dividends_strict() == acct.get_dividends()
    assert acct.get_dividends_strict()[0]["tax_withheld"] == pytest.approx(0.15)


def test_alpaca_strict_dividends_read_every_page():
    pages = {}

    def div(params):
        token = (params or {}).get("page_token")
        if token is None:
            return [_div(i) for i in range(100)]
        assert token == "99"
        return [_div(100), _div(101)]
    acct = _alpaca({"DIV": div, "DIVNRA": []})
    rows = acct.get_dividends_strict()
    assert len(rows) == 102 and acct._calls[0][1]["page_size"] == 100 and pages == {}
    assert len(acct.get_dividends()) == 100                  # the lenient read still stops at the first page


def test_alpaca_strict_dividends_raise_where_the_lenient_ones_answer_empty():
    broken = _alpaca({"DIV": ConnectionError("down"), "DIVNRA": []})
    assert broken.get_dividends() == []
    with pytest.raises(ConnectionError):
        broken.get_dividends_strict()
    nra_down = _alpaca({"DIV": [_div(1)], "DIVNRA": TimeoutError("slow")})
    assert len(nra_down.get_dividends()) == 1                # lenient: tax silently unknown
    with pytest.raises(TimeoutError):
        nra_down.get_dividends_strict()
    anon = _alpaca({}, authenticated=False)
    assert anon.get_dividends() == []
    with pytest.raises(ConnectionError):
        anon.get_dividends_strict()


def test_alpaca_strict_filled_trades_raise_when_unauthenticated_and_otherwise_match():
    anon = _alpaca({}, authenticated=False)
    assert anon.get_filled_trades() == []
    with pytest.raises(ConnectionError):
        anon.get_filled_trades_strict()
    acct = _alpaca({})
    acct._fetch_raw_alpaca_orders = lambda status=None, fetch_all=False, full_history=False: [SimpleNamespace(
        id="o", side="OrderSide.BUY", filled_qty=2, filled_avg_price=10.0, symbol="KO",
        filled_at=datetime(2026, 9, 1, tzinfo=timezone.utc), created_at=None, legs=None)]
    assert acct.get_filled_trades_strict() == acct.get_filled_trades() and len(acct.get_filled_trades_strict()) == 1
    acct._fetch_raw_alpaca_orders = MagicMock(side_effect=OSError("down"))
    with pytest.raises(OSError):
        acct.get_filled_trades_strict()


def test_alpaca_strict_balance_history_raises_on_failures_the_lenient_one_hides(monkeypatch):
    history = SimpleNamespace(timestamp=[1788000000, 1788086400], equity=[100.0, None])
    acct = _alpaca({"CSD": [], "CSW": []})
    acct.client.get_portfolio_history = MagicMock(return_value=history)
    assert acct.get_balance_history()[1]["net_liquidating_value"] == 0.0        # lenient: None -> 0.0
    with pytest.raises(ValueError):
        acct.get_balance_history_strict()
    good = _alpaca({"CSD": [], "CSW": []})
    good.client.get_portfolio_history = MagicMock(return_value=SimpleNamespace(timestamp=[1788000000], equity=[100.0]))
    assert good.get_balance_history_strict()[0]["net_liquidating_value"] == 100.0
    transfers_down = _alpaca({"CSD": ConnectionError("down"), "CSW": []})
    transfers_down.client.get_portfolio_history = MagicMock(return_value=SimpleNamespace(timestamp=[1788000000],
                                                                                       equity=[100.0]))
    assert len(transfers_down.get_balance_history()) == 1                        # lenient: transfer-blind
    with pytest.raises(ConnectionError):
        transfers_down.get_balance_history_strict()
    portfolio_down = _alpaca({"CSD": [], "CSW": []})
    portfolio_down.client.get_portfolio_history = MagicMock(side_effect=OSError("down"))
    assert portfolio_down.get_balance_history() == []
    with pytest.raises(OSError):
        portfolio_down.get_balance_history_strict()


# --------------------------------------------------------------------------------------------- TastyTrade
def test_tasty_strict_reads_match_lenient_on_a_healthy_broker_and_raise_on_failure():
    acct = tasty_account()
    acct._account.get_history = _history_by_type(money_movement=[
        _money_movement(9101, "Dividend", "1.57", underlying_symbol="TIDL"),
        _money_movement(9102, "Dividend", "-0.24", underlying_symbol="TIDL")])
    assert acct.get_dividends_strict() == acct.get_dividends() and len(acct.get_dividends_strict()) == 1
    acct._account.get_history = AsyncMock(side_effect=RuntimeError("gateway timeout"))
    assert acct.get_dividends() == []
    with pytest.raises(RuntimeError):
        acct.get_dividends_strict()
    acct._account.get_balance_snapshots = AsyncMock(side_effect=RuntimeError("down"))
    assert acct.get_balance_history() == [] and acct.get_filled_trades() == []
    with pytest.raises(RuntimeError):
        acct.get_balance_history_strict()
    with pytest.raises(RuntimeError):
        acct.get_filled_trades_strict()


def test_tasty_strict_dividends_refuse_to_guess_when_the_drip_leg_fails():
    def answer(session, **kwargs):
        if kwargs.get("types") == ["Receive Deliver"]:
            raise RuntimeError("gateway timeout")
        return [_money_movement(9101, "Dividend", "1.33", underlying_symbol="TIDL")]
    acct = tasty_account()
    acct._account.get_history = AsyncMock(side_effect=answer)
    assert len(acct.get_dividends()) == 1                     # lenient: DRIP unknown, treated as cash
    with pytest.raises(RuntimeError):
        acct.get_dividends_strict()


def test_tasty_strict_balance_history_refuses_a_snapshot_without_nlv_and_unauthenticated_raises():
    acct = tasty_account()
    snap = _snapshot()
    acct._account.get_balance_snapshots = AsyncMock(return_value=[snap])
    assert acct.get_balance_history_strict() == acct.get_balance_history()
    bad = SimpleNamespace(snapshot_date=date(2026, 8, 3), cash_balance=Decimal("1"), net_liquidating_value=None)
    acct._account.get_balance_snapshots = AsyncMock(return_value=[bad])
    assert acct.get_balance_history()[0]["net_liquidating_value"] == 0.0
    with pytest.raises(ValueError):
        acct.get_balance_history_strict()
    acct._session = None
    assert acct.get_filled_trades() == []
    with pytest.raises(ConnectionError):
        acct.get_filled_trades_strict()


# ------------------------------------------------------------------------------------------------- IBKR
def test_ibkr_strict_filled_trades_raise_where_the_lenient_one_answers_empty(monkeypatch):
    from datetime import timedelta
    account, fake = make_account(monkeypatch)
    try:
        aapl = fake.add_stock("AAPL", 265598)
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        fake.make_fill_record(aapl, "BOT", 1, 150.0, when=datetime.now(timezone.utc))
        assert account.get_filled_trades_strict(start_date=recent) == account.get_filled_trades(start_date=recent)
        assert account.get_filled_trades_strict(start_date=recent)
        fake.fail_calls["reqExecutionsAsync"] = ConnectionError("x")
        assert account.get_filled_trades() == []
        with pytest.raises(Exception):
            account.get_filled_trades_strict(start_date=recent)
    finally:
        account.close()


def test_ibkr_strict_flex_reads_raise_when_unconfigured_or_unreachable_and_match_when_healthy(monkeypatch):
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
    from tests.test_ibkr_account_reads import FLEX_SEND, FLEX_STATEMENT
    account, _ = make_account(monkeypatch)
    try:
        assert account.get_dividends() == []
        with pytest.raises(ValueError):
            account.get_dividends_strict()
        with pytest.raises(ValueError):
            account.get_balance_history_strict()
    finally:
        account.close()

    monkeypatch.setattr(IBKRAccount, "_flex_http_get",
                        staticmethod(lambda url: FLEX_SEND if "SendRequest" in url else FLEX_STATEMENT))
    account, _ = make_account(monkeypatch, extra={"flex_token": "TOK", "flex_query_id": "42"})
    try:
        assert account.get_dividends_strict() == account.get_dividends() and len(account.get_dividends_strict()) == 1
        assert account.get_balance_history_strict()[0]["net_liquidating_value"] == 100.0
    finally:
        account.close()

    def boom(url):
        raise OSError("network unreachable")
    monkeypatch.setattr(IBKRAccount, "_flex_http_get", staticmethod(boom))
    account, _ = make_account(monkeypatch, extra={"flex_token": "TOK", "flex_query_id": "42"})
    try:
        assert account.get_dividends() == []
        with pytest.raises(ConnectionError):
            account.get_dividends_strict()
    finally:
        account.close()


def test_ibkr_strict_filled_trades_refuse_a_window_older_than_the_api_keeps(monkeypatch):
    from datetime import timedelta
    account, fake = make_account(monkeypatch)
    try:
        aapl = fake.add_stock("AAPL", 265598)
        now = datetime.now(timezone.utc)
        fake.make_fill_record(aapl, "BOT", 1, 150.0, when=now)
        assert len(account.get_filled_trades_strict(start_date=now - timedelta(days=2))) == 1
        with pytest.raises(ValueError, match="only the last 7 days"):
            account.get_filled_trades_strict(start_date=now - timedelta(days=60))
        with pytest.raises(ValueError):
            account.get_filled_trades_strict()                       # no start = "everything": not available
        assert len(account.get_filled_trades()) == 1                 # the lenient read still answers (and warns)
    finally:
        account.close()
