"""Deterministic scenario + normaliser for the portfolio loader parity test (and the API tests).

``build_scenario`` seeds a fixed DB (two managed labels, one excluded symbol, one unpriced holding, stored weights,
a reserve, income events, a symbol-facts row, a stats row) and returns the fake account. ``normalise`` turns any
loader result into plain JSON (timestamps replaced, dataclasses flattened), so the golden file is comparable."""
import dataclasses
from datetime import date, datetime
from enum import Enum
from types import SimpleNamespace

from ba2_trade_platform.core.account_types import AccountSnapshot, MarginInfo
from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import SymbolMarketStats
from ba2_trade_platform.core.portfolio_allocation_store import (
    set_allocation_config, set_managed_label, set_symbol_weight, upsert_income_event)
from ba2_trade_platform.core.utils import add_label_to_instruments
from ba2_trade_platform.core import allocator_exclusion as exclusion_store
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_expert_instance


class GoldenAccount(MockAccount):
    def __init__(self, account_id, positions, prices, dividends=(), buying_power=5_000.0, snapshot_error=None,
                 snapshot_all_none=False):
        super().__init__(account_id)
        self._settings_cache = {"manual_trading_enabled": True}
        self._positions = positions
        self._prices = dict(prices)
        self.dividends = list(dividends)
        self.buying_power = buying_power
        self.snapshot_error = snapshot_error
        self.snapshot_all_none = snapshot_all_none     # AlpacaAccount's auth-failure answer: no raise, no figures
        self.dividend_calls = 0
        self.margin_requests = []
        self.calls = []

    def get_positions(self):
        return self._positions

    def get_instrument_current_price(self, symbol_or_symbols, price_type=None):
        return {s: self._prices.get(s) for s in symbol_or_symbols}

    def get_account_snapshot(self):
        if self.snapshot_error is not None:
            raise self.snapshot_error
        if self.snapshot_all_none:
            return AccountSnapshot()
        return AccountSnapshot(cash=5_000.0, equity=20_000.0, net_liquidation=20_000.0,
                               buying_power=self.buying_power, margin_multiplier=1.0, is_margin_account=False,
                               supports_fractional=True)

    def get_symbol_margin_info(self, symbols):
        self.calls.append("get_symbol_margin_info")
        self.margin_requests.append(list(symbols))
        return {s: MarginInfo(symbol=s, bp_factor=1.0, fractionable=True) for s in symbols if s == "AAPL"}

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        self.dividend_calls += 1
        return list(self.dividends)

    def get_cash_transfers(self, start_date=None, end_date=None):
        self.calls.append("get_cash_transfers")
        return []


def _pos(symbol, qty, cost, mv):
    return {"symbol": symbol, "qty": qty, "cost_basis": cost, "market_value": mv, "side": "BUY"}


def build_scenario(use_account, *, dividends=True, snapshot_error=None, positions="default", extra_prices=None,
                   snapshot_all_none=False):
    """Seed the DB and install the account. ``use_account(account)`` makes the factory return it."""
    acct_id = create_account_definition(name="Manual", provider="MockAccount").id
    create_expert_instance(account_id=acct_id, alias="Alpha", enabled=False)
    set_managed_label(acct_id, "CORE", target_pct=60.0, comment="core", color="#0072B2")
    set_managed_label(acct_id, "INCOME", target_pct=30.0)
    add_label_to_instruments(["AAPL", "MSFT", "KO"], "CORE")
    add_label_to_instruments(["O", "SPY"], "INCOME")
    set_symbol_weight(acct_id, "CORE", "AAPL", weight_pct=50.0)
    set_symbol_weight(acct_id, "CORE", "MSFT", weight_pct=30.0)
    set_allocation_config(acct_id, valuation_mode="market", unallocated_pct=10.0)
    exclusion_store.exclude_symbol(acct_id, "KO", note="paused")
    upsert_income_event(acct_id, "ev-1", date.today(), "DIVIDEND", 12.5, symbol="O")
    upsert_income_event(acct_id, "ev-2", date.today(), "DEPOSIT", 100.0)
    add_instance(SymbolMarketStats(symbol="AAPL", dividend_yield_pct=0.5, total_return_1y_pct=12.0), expunge_after_flush=True)
    book = [_pos("AAPL", 10, 1500.0, 2000.0), _pos("MSFT", 5, 1800.0, 2000.0), _pos("KO", 20, 1100.0, 1200.0),
            _pos("O", 30, 1700.0, 1650.0), _pos("SPY", 4, 1900.0, 2100.0)] if positions == "default" else positions
    divs = [{"symbol": "O", "amount": 40.0, "drip_quantity": None, "date": datetime(2026, 9, 1)},
            {"symbol": "AAPL", "amount": 3.0, "drip_quantity": 0.01, "date": datetime(2026, 9, 2)}] if dividends else []
    # SPY has no quote: a held, unmeasurable holding in market mode.
    account = GoldenAccount(acct_id, book, {"AAPL": 200.0, "MSFT": 400.0, "KO": 60.0, "O": 55.0, **(extra_prices or {})},
                            dividends=divs, snapshot_error=snapshot_error, snapshot_all_none=snapshot_all_none)
    use_account(account)
    return acct_id, account


_DROP = "<dt>"


def normalise(value):
    """JSON-safe, deterministic: timestamps -> placeholder, dataclass/SQLModel -> dict, tuple keys -> text."""
    if value is None or isinstance(value, (bool, int, str)):
        return value.value if isinstance(value, Enum) else value
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, (datetime, date)):
        return _DROP
    if isinstance(value, Enum):
        return normalise(value.value)
    if hasattr(value, "model_dump"):
        return normalise(value.model_dump())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return normalise({f.name: getattr(value, f.name) for f in dataclasses.fields(value)})
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else "|".join(map(str, k))): normalise(v) for k, v in sorted(
            value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (set, frozenset)):
        return sorted(normalise(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [normalise(v) for v in value]
    return repr(value)
