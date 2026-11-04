"""A call-recording fake broker account for the management-API write tests (and the UI/API parity tests)."""
from typing import Any, List, Optional, Tuple


class RecordingAccount:
    """Records every mutating call as ``(method, args...)``. ``raises`` makes the named method raise;
    ``returns`` makes it return the given value instead of True."""

    def __init__(self, account_id: int = 1, price: Optional[float] = 100.0):
        self.id = account_id
        self.calls: List[Tuple[Any, ...]] = []
        self.raises = {}
        self.returns = {}
        self.price = price
        self.close_result = {"success": True, "message": "Close order submitted", "canceled_count": 0,
                             "deleted_count": 0, "close_order_id": 99}

    def _do(self, name: str, *args):
        self.calls.append((name,) + args)
        if name in self.raises:
            raise self.raises[name]
        return self.returns[name] if name in self.returns else True

    def get_instrument_current_price(self, symbol):
        if self.price is None:
            raise ValueError("price unavailable")
        return self.price

    def adjust_tp(self, txn, price, source=""):
        return self._do("adjust_tp", txn.id, price, source)

    def adjust_sl(self, txn, price, source=""):
        return self._do("adjust_sl", txn.id, price, source)

    def adjust_tp_sl(self, txn, tp, sl, source=""):
        return self._do("adjust_tp_sl", txn.id, tp, sl, source)

    def cancel_order(self, order_id):
        return self._do("cancel_order", order_id)

    async def close_transaction_async(self, transaction_id):
        self.calls.append(("close_transaction_async", transaction_id))
        if "close_transaction_async" in self.raises:
            raise self.raises["close_transaction_async"]
        return self.close_result


def install_account(monkeypatch, account: RecordingAccount) -> None:
    """Make every service lookup (provider class, cached instance, transaction's account) return ``account``."""
    from ba2_trade_platform.services import accounts as accounts_svc
    from ba2_trade_platform.services import trades as trades_svc
    monkeypatch.setattr(trades_svc, "get_account_class", lambda provider: (lambda account_id: account))
    monkeypatch.setattr(trades_svc, "get_account_instance_from_transaction", lambda tid: account, raising=False)
    monkeypatch.setattr(trades_svc, "get_account_instance_from_id", lambda aid: account, raising=False)
    monkeypatch.setattr(accounts_svc, "get_account_instance_from_id", lambda aid: account)
