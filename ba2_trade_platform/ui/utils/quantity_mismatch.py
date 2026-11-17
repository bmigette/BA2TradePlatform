"""Overview quantity-mismatch check: the broker's SHARE positions against the open EQUITY transactions.

An option transaction is filed under its underlying's ticker with a quantity in CONTRACTS, so it must
never be added to the share count: a buy-write (100 shares plus one short covered call) is 100 shares at
the broker and 100 shares in the transactions, not 101. Option positions are not compared here; they
carry contract symbols at the broker and legs on the orders, which this check does not read.
"""
from typing import Any, Dict, Iterable, List

#: Share difference below which the two quantities are the same (float dust).
TOLERANCE = 0.01


def _is_option(asset_class: Any) -> bool:
    """True for an option asset class, whichever shape it arrives in: the platform's ``AssetClass``
    enum, a broker enum (``us_option``), or their plain strings. Absent means shares (rows from
    before options existed)."""
    value = getattr(asset_class, "value", asset_class)
    return "option" in str(value or "").lower()


def find_quantity_mismatches(positions: Iterable[Any], transactions: Iterable[Any]) -> List[Dict[str, Any]]:
    """Tickers whose broker share position is SMALLER than the open equity transactions say.

    ``positions`` are the account's broker positions (dicts or dict-like, with ``symbol``, ``qty`` and
    ``asset_class``); ``transactions`` are its OPENED transactions. Each result carries the equity
    transactions of that ticker only, so a correction never touches an option transaction.
    """
    broker: Dict[str, float] = {}
    for pos in positions:
        row = pos if isinstance(pos, dict) else dict(pos)
        if _is_option(row.get("asset_class")):
            continue
        symbol, qty = row.get("symbol"), row.get("qty")
        if symbol and qty:
            broker[symbol] = float(qty)

    shares: Dict[str, List[Any]] = {}
    for txn in transactions:
        if _is_option(txn.asset_class):
            continue
        if txn.symbol and txn.quantity:
            shares.setdefault(txn.symbol, []).append(txn)

    mismatches = []
    for symbol, broker_qty in broker.items():
        symbol_txns = shares.get(symbol, [])
        txn_qty = sum(float(t.quantity) for t in symbol_txns)
        if abs(broker_qty) < abs(txn_qty) - TOLERANCE:
            mismatches.append({"symbol": symbol, "broker_qty": broker_qty, "transaction_qty": txn_qty,
                               "difference": txn_qty - broker_qty, "transactions": symbol_txns})
    return mismatches
