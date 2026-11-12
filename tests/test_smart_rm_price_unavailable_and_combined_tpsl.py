"""Smart Risk Manager review findings (2026-11-09).

FINDING 1 -- no invented price. When the live price lookup for an open position failed,
``get_portfolio_status`` substituted the ENTRY price (``current_price = trans.open_price``), so
the LLM saw a stale price and a 0.00% P&L and decided closes / TP / SL on it. Now the position
is still listed (hiding it from the risk manager is worse) with price and P&L explicitly
unavailable, an ERROR names the symbol and transaction, and the aggregates say they are
partial. Every renderer of that structure handles the unavailable case.

FINDING 2 -- the combined TP+SL action bypassed the single-action validations. When the model
recommended both a take-profit and a stop-loss for one transaction, ``action_node`` merged them
and called ``account.adjust_tp_sl`` directly: no ownership check, no status check, no
side/price check. Now the combined action goes through ``toolkit.update_tp_sl``, which runs the
SAME checks as ``update_stop_loss`` / ``update_take_profit`` and then makes one broker call.
Also: a same-type duplicate for one transaction is logged and recorded as superseded instead
of being dropped silently, and transaction ids are normalised so "12" and 12 group together.
"""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from ba2_trade_platform.core import SmartRiskManagerGraph as graph_mod
from ba2_trade_platform.core import SmartRiskManagerToolkit as toolkit_mod
from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.factories import (
    create_account_definition, create_expert_instance, create_trading_order, create_transaction,
)


# --------------------------------------------------------------------------- fakes
class _RecLogger:
    """ba2 loggers have propagate=False, so caplog sees nothing: record calls directly."""

    def __init__(self):
        self.records = []

    def _rec(self, level):
        def f(msg, *a, **k):
            self.records.append((level, str(msg)))
        return f

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "critical", "exception"):
            return self._rec(name)
        raise AttributeError(name)

    def at(self, level):
        return [m for lv, m in self.records if lv == level]


class _FakeAccount:
    """Records every broker-side TP/SL call; price lookups are scripted per symbol."""

    def __init__(self, prices):
        self.prices = prices  # symbol -> float | None | Exception
        self.calls = []

    def get_instrument_current_price(self, symbol, price_type=None):
        p = self.prices.get(symbol)
        if isinstance(p, Exception):
            raise p
        return p

    def adjust_tp(self, transaction, new_tp_price, source=""):
        self.calls.append(("adjust_tp", transaction.id, new_tp_price))
        return True

    def adjust_sl(self, transaction, new_sl_price, source=""):
        self.calls.append(("adjust_sl", transaction.id, new_sl_price))
        return True

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        self.calls.append(("adjust_tp_sl", transaction.id, new_tp_price, new_sl_price))
        return True


def _toolkit(expert_id, account):
    tk = object.__new__(SmartRiskManagerToolkit)
    tk.expert_instance_id = expert_id
    tk.account_id = 1
    tk.account = account
    tk.expert = SimpleNamespace(get_virtual_balance=lambda: 10000.0,
                                get_available_balance=lambda: 5000.0)
    return tk


@pytest.fixture
def world():
    acct = create_account_definition()
    me = create_expert_instance(account_id=acct.id, alias="me")
    other = create_expert_instance(account_id=acct.id, alias="other")
    return SimpleNamespace(account_id=acct.id, me=me.id, other=other.id)


def _position(world, expert_id, symbol="AAPL", side=OrderDirection.BUY, open_price=100.0, qty=10.0):
    t = create_transaction(symbol=symbol, quantity=qty, side=side, open_price=open_price,
                           expert_id=expert_id, status=TransactionStatus.OPENED)
    create_trading_order(world.account_id, symbol=symbol, quantity=qty, side=side,
                         order_type=OrderType.MARKET, status=OrderStatus.FILLED,
                         filled_qty=qty, open_price=open_price, transaction_id=t.id)
    return t.id


@pytest.fixture
def rec_logs(monkeypatch):
    tk_log, gr_log = _RecLogger(), _RecLogger()
    monkeypatch.setattr(toolkit_mod, "logger", tk_log)
    monkeypatch.setattr(graph_mod, "logger", gr_log)
    return SimpleNamespace(toolkit=tk_log, graph=gr_log)


def test_the_tests_exercise_this_worktree():
    import pathlib
    here = pathlib.Path(__file__).resolve().parents[1]
    assert pathlib.Path(toolkit_mod.__file__).resolve().is_relative_to(here)
    assert pathlib.Path(graph_mod.__file__).resolve().is_relative_to(here)


# =========================================================================== FINDING 1
@pytest.mark.parametrize("failure", [RuntimeError("quote feed down"), None],
                         ids=["lookup-raises", "lookup-returns-none"])
class TestPriceUnavailable:
    def test_position_listed_with_unavailable_price_not_zero_pnl(self, world, rec_logs, failure):
        bad = _position(world, world.me, symbol="AAPL", open_price=100.0)
        good = _position(world, world.me, symbol="MSFT", open_price=200.0)
        tk = _toolkit(world.me, _FakeAccount({"AAPL": failure, "MSFT": 210.0}))

        status = tk.get_portfolio_status()

        by_id = {p["transaction_id"]: p for p in status["open_positions"]}
        assert set(by_id) == {bad, good}, "the position must still be listed"
        pos = by_id[bad]
        assert pos["current_price"] is None, "no invented price (was the entry price)"
        assert pos["unrealized_pnl"] is None and pos["unrealized_pnl_pct"] is None
        assert pos["position_value"] is None
        assert pos["price_available"] is False
        assert "PRICE UNAVAILABLE" in pos["price_status"]
        assert pos["entry_price"] == 100.0

        ok = by_id[good]
        assert ok["price_available"] is True
        assert ok["current_price"] == 210.0 and ok["unrealized_pnl"] == 100.0

        # Aggregates say they are partial instead of treating AAPL as flat.
        assert status["totals_partial"] is True
        assert [p["transaction_id"] for p in status["price_unavailable_positions"]] == [bad]
        assert status["total_unrealized_pnl"] == 100.0
        assert status["total_position_value"] == 2100.0
        assert "PARTIAL" in status["totals_note"] and "AAPL" in status["totals_note"]

        errors = rec_logs.toolkit.at("error")
        assert any("AAPL" in m and f"#{bad}" in m for m in errors), errors

    def test_markdown_report_renders_unavailable(self, world, rec_logs, failure):
        bad = _position(world, world.me, symbol="AAPL", open_price=100.0)
        tk = _toolkit(world.me, _FakeAccount({"AAPL": failure}))
        md = tk.get_all_transactions(format_type="markdown")
        assert md.startswith("# All Transactions Report"), md
        assert f"Transaction #{bad}: AAPL" in md
        assert "PRICE UNAVAILABLE" in md
        assert "0.00%" not in md
        assert "PARTIAL" in md

    def test_positions_summary_renders_unavailable(self, world, rec_logs, failure):
        bad = _position(world, world.me, symbol="AAPL", open_price=100.0)
        status = _toolkit(world.me, _FakeAccount({"AAPL": failure})).get_portfolio_status()
        text = graph_mod._build_positions_summary(status["open_positions"])
        assert f"Transaction #{bad}" in text
        assert "PRICE UNAVAILABLE" in text
        assert "P&L: UNKNOWN" in text
        assert "+0.0%" not in text

    def test_analyze_portfolio_prompt_renders_unavailable(self, world, rec_logs, failure, monkeypatch):
        bad = _position(world, world.me, symbol="AAPL", open_price=100.0)
        status = _toolkit(world.me, _FakeAccount({"AAPL": failure})).get_portfolio_status()

        class _Tk:
            def __init__(self, *a, **k):
                pass

            def get_last_risk_manager_summary(self):
                return {}

        seen = []

        class _Llm:
            def invoke(self, messages):
                seen.append(messages[-1].content)
                return AIMessage(content="ok")

        monkeypatch.setattr(graph_mod, "SmartRiskManagerToolkit", _Tk)
        monkeypatch.setattr(graph_mod, "create_llm", lambda *a, **k: _Llm())
        state = {"portfolio_status": status, "open_positions": status["open_positions"],
                 "risk_manager_model": "x", "backend_url": None, "api_key": None,
                 "expert_instance_id": world.me, "account_id": 1, "messages": [],
                 "agent_scratchpad": ""}
        graph_mod.analyze_portfolio(state)
        prompt = seen[0]
        assert f"Transaction #{bad}: AAPL" in prompt
        assert "PRICE UNAVAILABLE" in prompt
        assert "0.00%" not in prompt
        assert "PARTIAL" in prompt


def test_update_tools_refuse_when_price_unavailable(world, rec_logs):
    t = _position(world, world.me, symbol="AAPL")
    acct = _FakeAccount({"AAPL": None})
    tk = _toolkit(world.me, acct)
    for res in (tk.update_stop_loss(t, 90.0, "r"), tk.update_take_profit(t, 120.0, "r"),
                tk.update_tp_sl(t, 120.0, 90.0, "r")):
        assert res["success"] is False
        assert "Could not get current price for AAPL" in res["message"]
    assert acct.calls == []


# =========================================================================== FINDING 2
def _run_actions(monkeypatch, world, account, actions):
    monkeypatch.setattr(graph_mod, "SmartRiskManagerToolkit",
                        lambda eid, aid: _toolkit(eid, account))
    state = {"expert_instance_id": world.me, "account_id": world.account_id,
             "recommended_actions": actions, "actions_log": [], "iteration_count": 0,
             "messages": []}
    return graph_mod.action_node(state)


def _tp(tid, price, reason="tp"):
    return {"action_type": "update_take_profit", "reason": reason, "confidence": 70,
            "parameters": {"transaction_id": tid, "new_tp_price": price}}


def _sl(tid, price, reason="sl"):
    return {"action_type": "update_stop_loss", "reason": reason, "confidence": 70,
            "parameters": {"transaction_id": tid, "new_sl_price": price}}


class TestCombinedTpSl:
    def test_other_experts_transaction_is_refused(self, world, rec_logs, monkeypatch):
        theirs = _position(world, world.other, symbol="AAPL")
        acct = _FakeAccount({"AAPL": 100.0})
        out = _run_actions(monkeypatch, world, acct, [_tp(theirs, 110.0), _sl(theirs, 95.0)])
        assert acct.calls == [], "nothing may reach the broker"
        rec, = out["actions_log"]
        assert rec["action_type"] == "adjust_tp_sl"
        assert rec["success"] is False
        assert f"belongs to expert {world.other}" in rec["summary"]

    @pytest.mark.parametrize("side,tp,sl,needle", [
        (OrderDirection.BUY, 110.0, 105.0, "must be below current price"),   # long SL above
        (OrderDirection.BUY, 95.0, 90.0, "must be above current price"),     # long TP below
        (OrderDirection.SELL, 90.0, 95.0, "must be above current price"),    # short SL below
        (OrderDirection.SELL, 105.0, 110.0, "must be below current price"),  # short TP above
    ], ids=["long-sl-wrong-side", "long-tp-wrong-side", "short-sl-wrong-side",
            "short-tp-wrong-side"])
    def test_wrong_side_is_refused(self, world, rec_logs, monkeypatch, side, tp, sl, needle):
        t = _position(world, world.me, symbol="AAPL", side=side)
        acct = _FakeAccount({"AAPL": 100.0})
        out = _run_actions(monkeypatch, world, acct, [_tp(t, tp), _sl(t, sl)])
        assert acct.calls == []
        rec, = out["actions_log"]
        assert rec["action_type"] == "adjust_tp_sl" and rec["success"] is False
        assert needle in rec["summary"]

    def test_closed_transaction_is_refused(self, world, rec_logs, monkeypatch):
        t = create_transaction(symbol="AAPL", expert_id=world.me, status=TransactionStatus.CLOSED)
        acct = _FakeAccount({"AAPL": 100.0})
        out = _run_actions(monkeypatch, world, acct, [_tp(t.id, 110.0), _sl(t.id, 95.0)])
        assert acct.calls == []
        rec, = out["actions_log"]
        assert rec["success"] is False and "Cannot modify TP/SL" in rec["summary"]

    def test_valid_combined_makes_one_broker_call(self, world, rec_logs, monkeypatch):
        t = _position(world, world.me, symbol="AAPL")
        acct = _FakeAccount({"AAPL": 100.0})
        out = _run_actions(monkeypatch, world, acct, [_tp(t, 110.0), _sl(str(t), 95.0)])
        assert acct.calls == [("adjust_tp_sl", t, 110.0, 95.0)]
        rec, = out["actions_log"]
        assert rec["action_type"] == "adjust_tp_sl" and rec["success"] is True

    def test_valid_short_combined(self, world, rec_logs, monkeypatch):
        t = _position(world, world.me, symbol="AAPL", side=OrderDirection.SELL)
        acct = _FakeAccount({"AAPL": 100.0})
        _run_actions(monkeypatch, world, acct, [_tp(t, 90.0), _sl(t, 105.0)])
        assert acct.calls == [("adjust_tp_sl", t, 90.0, 105.0)]


class TestDuplicateSameType:
    def test_earlier_duplicate_is_warned_and_recorded_as_superseded(self, world, rec_logs, monkeypatch):
        t = _position(world, world.me, symbol="AAPL")
        acct = _FakeAccount({"AAPL": 100.0})
        out = _run_actions(monkeypatch, world, acct,
                           [_sl(str(t), 95.0, "first"), _sl(t, 96.0, "second")])
        # The later one still wins (unchanged), and "12" vs 12 is one key.
        assert acct.calls == [("adjust_sl", t, 96.0)]
        superseded = [r for r in out["actions_log"] if r.get("superseded")]
        executed = [r for r in out["actions_log"] if not r.get("superseded")]
        assert len(superseded) == 1 and len(executed) == 1
        s = superseded[0]
        assert s["action_type"] == "update_stop_loss"
        assert s["arguments"]["new_sl_price"] == 95.0
        assert s["success"] is False
        assert "superseded" in s["summary"].lower()
        assert executed[0]["success"] is True
        warns = rec_logs.graph.at("warning")
        assert any("supersed" in m.lower() and f"#{t}" in m for m in warns), warns
