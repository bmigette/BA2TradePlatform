"""Fixed Account Growth inputs for the golden tests: positions, daily closes (with gaps), fills, dividends (cash and
DRIP) and a two-account balance history. Dates are ``datetime`` objects, as the brokers return them."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.types import InstrumentType

RANGE_START = date(2026, 9, 15)


def seed_instruments():
    for name, labels in (("AAPL", ["Growth", "Core"]), ("MSFT", ["Growth"]), ("XLE", ["Energy"])):
        add_instance(Instrument(name=name, instrument_type=InstrumentType.STOCK, categories=[], labels=labels),
                     expunge_after_flush=True)       # BND deliberately has no Instrument row: 'Unlabeled'


def positions():
    return [SimpleNamespace(symbol="AAPL", qty=10.0, cost_basis=1000.0),
            SimpleNamespace(symbol="AAPL", qty=2.0, cost_basis=240.0),         # a second lot of the same symbol
            SimpleNamespace(symbol="MSFT", qty=5.0, cost_basis=1500.0),
            SimpleNamespace(symbol="XLE", qty=20.0, cost_basis=1600.0),
            SimpleNamespace(symbol="BND", qty=8.0, cost_basis=640.0)]


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def historical_prices():
    days = _weekdays(date(2026, 9, 1), 45)
    prices = {}
    for sym, base, drift in (("AAPL", 100.0, 0.7), ("MSFT", 300.0, -0.4), ("XLE", 80.0, 0.2), ("BND", 72.0, 0.05)):
        prices[sym] = {d.isoformat(): round(base + drift * i + (i % 3) * 0.5, 2)
                       for i, d in enumerate(days) if not (sym == "AAPL" and i in (7, 8, 20))}   # gaps: forward-fill
    return prices


def trades():
    def t(day, sym, side, qty, price, acct=1):
        return {"date": datetime.combine(day, datetime.min.time()), "symbol": sym, "side": side, "qty": qty,
                "price": price, "account_id": acct}
    return [t(date(2026, 8, 20), "AAPL", "BUY", 12.0, 95.0), t(date(2026, 9, 10), "MSFT", "BUY", 3.0, 310.0),
            t(date(2026, 9, 18), "MSFT", "BUY", 4.0, 305.0), t(date(2026, 9, 24), "MSFT", "SELL", 2.0, 320.0),
            t(date(2026, 10, 2), "XLE", "BUY", 20.0, 80.0, 2), t(date(2026, 10, 6), "AAPL", "SELL", 4.0, 130.0),
            t(date(2026, 10, 9), "AAPL", "BUY", 4.0, 128.0), t(date(2026, 10, 13), "BND", "BUY", 8.0, 80.0, 2),
            t(date(2026, 10, 15), "BND", "SELL", 1.0, 81.0, 2)]


def dividends():
    def d(day, sym, amount, acct=1, drip_qty=None, drip_price=None):
        return {"date": datetime.combine(day, datetime.min.time()), "symbol": sym, "amount": amount,
                "account_id": acct, "account_name": f"acct{acct}", "drip_quantity": drip_qty,
                "drip_price": drip_price, "tax_withheld": 0.1, "gross_amount": amount + 0.1}
    return [d(date(2026, 9, 5), "AAPL", 2.4), d(date(2026, 9, 29), "AAPL", 2.5, drip_qty=0.02, drip_price=125.0),
            d(date(2026, 10, 3), "XLE", 7.0, 2), d(date(2026, 10, 12), "MSFT", 3.1),
            d(date(2026, 10, 20), "XLE", 7.5, 2, drip_qty=0.09, drip_price=82.0)]


def balance_history():
    out = []
    for i, day in enumerate(_weekdays(date(2026, 9, 1), 40)):
        stamp = datetime.combine(day, datetime.min.time())
        # account 1 reports a daily P/L (Alpaca-like); account 2 only cash (TastyTrade-like)
        out.append({"date": stamp, "account_id": 1,
                    "net_liquidating_value": 10000.0 + 40.0 * i + (500.0 if i == 12 else 0),
                    "cash_balance": 2000.0 - 10.0 * i, "profit_loss": 40.0 if i != 12 else 0.0})
        if i % 2 == 0:
            out.append({"date": stamp, "account_id": 2, "net_liquidating_value": 5000.0 + 15.0 * i,
                        "cash_balance": 1000.0 + (800.0 if i == 20 else 0) - 3.0 * i})
    return out
