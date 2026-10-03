"""A tiny TWS simulator around the REAL ``ib_async`` 2.1.0 ``IB`` / ``Wrapper`` (socket stubbed, no network).

``FakeIB`` models IB's behaviour at the adapter's surface; this one feeds the REAL wrapper the callbacks TWS
sends, so the facts the fake encodes are pinned against the library itself. ``truth`` is what IB actually holds
per orderId; ``reqAllOpenOrders`` is answered the way TWS answers it: an ``openOrder`` AND an ``orderStatus`` per
working order, then ``openOrderEnd``.
"""
import asyncio
from types import SimpleNamespace

from ib_async import IB, Order, OrderState, Stock

ACCOUNT = "DU1234567"


class TwsSim:
    def __init__(self):
        self.ib = ib = IB()
        c = ib.client
        c.isReady = lambda: True
        c.isConnected = lambda: True
        c._reqIdSeq = 500
        ib.wrapper.clientId = 7
        self.truth = {}
        self.sent = []
        c.placeOrder = lambda oid, contract, order: self.sent.append(("place", oid, order.lmtPrice, order.auxPrice))
        #: how TWS answers a cancel: 'ok' (PendingCancel then Cancelled + error 202), 'refuse' (error 10148),
        #: 'lost' (nothing at all)
        self.cancel_mode = "lost"
        #: does TWS send an orderStatus after each openOrder on reqAllOpenOrders? (it is believed to)
        self.status_on_open = True
        #: other API clients' working orders: {orderId: {"client": n, "perm": n, ...}}
        self.foreign = {}
        c.cancelOrder = self._on_cancel
        c.reqAllOpenOrders = self._req_all_open
        c.reqOpenOrders = self._req_all_open

    def _req_all_open(self):
        asyncio.get_event_loop().call_later(0.05, self.deliver_open)

    def _on_cancel(self, *a, **k):
        self.sent.append(("cancel",) + a)
        oid = a[0]
        loop = asyncio.get_event_loop()
        w = self.ib.wrapper
        t = next((x for x in w.trades.values() if x.order.orderId == oid), None)
        if t is None or self.cancel_mode == "lost":
            return
        if self.cancel_mode == "refuse":
            loop.call_later(0.05, lambda: w.error(
                oid, 10148, f"OrderId {oid} that needs to be cancelled cannot be cancelled, state: PendingCancel.", ""))
            return

        def pending():
            self.truth[oid]["status"] = "PendingCancel"
            self.status(t, "PendingCancel")
        def done():
            self.truth[oid]["status"] = "Cancelled"
            self.status(t, "Cancelled")
            w.error(oid, 202, "Order Canceled - reason:", "")
        loop.call_later(0.03, pending)
        loop.call_later(0.08, done)

    def fill(self, order_id, qty, price):
        """IB fills ``qty`` of the order (a partial fill leaves it Submitted)."""
        t = next(x for x in self.ib.wrapper.trades.values() if x.order.orderId == order_id)
        tr = self.truth[order_id]
        done = tr.get("filled", 0.0) + qty
        tr["filled"] = done
        tr["status"] = "Filled" if done >= tr["qty"] - 1e-9 else "Submitted"
        self.ib.wrapper.orderStatus(order_id, tr["status"], done, tr["qty"] - done, price, t.order.permId,
                                    0, price, 7, "", 0.0)

    def deliver_open(self):
        w = self.ib.wrapper
        for oid, f in self.foreign.items():
            fo = Order(orderId=oid, clientId=f["client"], permId=f["perm"], action="BUY", totalQuantity=5,
                       orderType="LMT", lmtPrice=10.0, orderRef="other-app", account=ACCOUNT)
            w.openOrder(oid, AAPL, fo, OrderState(status="Submitted"))
            w.orderStatus(oid, "Submitted", 0.0, 5.0, 0.0, f["perm"], 0, 0.0, f["client"], "", 0.0)
        for t in list(w.trades.values()):
            tr = self.truth.get(t.order.orderId)
            if tr is None or tr["status"] in ("Filled", "Cancelled"):
                continue
            o = Order(orderId=t.order.orderId, clientId=7, permId=t.order.permId, action=t.order.action,
                      totalQuantity=tr["qty"], orderType=t.order.orderType, lmtPrice=tr["lmt"],
                      auxPrice=tr["aux"], orderRef=t.order.orderRef, account=ACCOUNT)
            w.openOrder(o.orderId, t.contract, o, OrderState(status=tr["status"]))
            if self.status_on_open:
                self.status(t, tr["status"])
        w.openOrderEnd()

    def status(self, t, st):
        o = t.order
        tr = self.truth[o.orderId]
        done = tr.get("filled", 0.0)
        self.ib.wrapper.orderStatus(o.orderId, st, done, tr["qty"] - done, 0.0, o.permId, 0, 0.0, 7, "", 0.0)


AAPL = Stock("AAPL", "SMART", "USD")
AAPL.conId = 265598


def make_shim(sim, rt, account_cls):
    """An ``IBKRAccount`` stand-in with only what the order-object coroutines need."""
    shim = account_cls.__new__(account_cls)
    shim.id = 1
    shim._runtime = lambda: rt

    async def _resolve_stock(_ib, _sym):
        return SimpleNamespace(contract=AAPL, details=None)

    async def _market_rules(_ib, _details):
        return [(0.0, 0.01)]
    shim._resolve_stock, shim._market_rules = _resolve_stock, _market_rules
    return shim


def resting_stop(sim, resting="Submitted", ref="ba2:1:5:abcd1234:SL"):
    """Place a resting STP LMT on the real wrapper and tell it IB acknowledged it."""
    ib = sim.ib
    o = Order(action="SELL", orderType="STP LMT", totalQuantity=10, auxPrice=90.0, lmtPrice=89.55,
              orderRef=ref, account=ACCOUNT)
    t = ib.placeOrder(AAPL, o)
    o.permId = 9000 + o.orderId
    sim.truth[o.orderId] = {"lmt": 89.55, "aux": 90.0, "qty": 10.0, "status": resting}
    sim.status(t, resting)
    return t, o, ref


def ib_applies_modifications(sim, order_id, delay=0.1):
    """IB applies a change ``delay`` seconds after it arrives (openOrder + orderStatus echo)."""
    ib = sim.ib
    orig = ib.client.placeOrder

    def on_place(oid, contract, order):
        orig(oid, contract, order)
        if oid == order_id:
            def apply():
                sim.truth[oid].update(lmt=order.lmtPrice, aux=order.auxPrice)
                sim.deliver_open()
            asyncio.get_event_loop().call_later(delay, apply)
    ib.client.placeOrder = on_place
