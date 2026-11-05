"""Entry-gate counter: counting on hand-made ``rule_evaluations``, seeding registry, row plumbing,
and the report tool."""
from __future__ import annotations

import importlib.util
import inspect
import json
import sqlite3
from pathlib import Path

from app.services import strategy_optimization_handler as soh
from app.services.backtest import entry_gate_bt as eg
from app.services.backtest.entry_gate_bt import EntryGateRecord, register_entry_rule


def _c(key, ok):
    return {"trigger_key": key, "condition_result": ok}


def _ev(rid, name, conds, met, **extra):
    return {"rule_name": name, "rule_id": rid, "conditions": conds, "all_conditions_met": met,
            "executed": met, **extra}


def test_counts_met_short_circuited_and_unmapped_leaf():
    register_entry_rule(9001, "ea-a", "rule-a", {"cond_0": "shared-gate_confidence", "cond_2": "o_lc-market-adx"})
    rec = EntryGateRecord()
    # met
    rec.note_rules([_ev(9001, "ea-a", [_c("cond_0", True), _c("cond_2", True)], True)])
    # short-circuited at the first leaf (second never evaluated)
    rec.note_rules([_ev(9001, "ea-a", [_c("cond_0", False)], False)])
    # blocked at the second leaf
    rec.note_rules([_ev(9001, "ea-a", [_c("cond_0", True), _c("cond_2", False)], False)])
    # unregistered rule with an unmapped trigger key
    rec.note_rules([_ev(1, "plain", [_c("bullish", True), _c("gate_x", False)], False)])
    # stale id (name mismatch) must not borrow another rule's labels
    rec.note_rules([_ev(9001, "other-name", [_c("cond_0", False)], False)])
    out = rec.record()
    a = out["rule-a"]
    assert (a["n"], a["met"], a["b"], a["order"]) == (
        3, 1, {"shared-gate_confidence": 1, "o_lc-market-adx": 1},
        ["shared-gate_confidence", "o_lc-market-adx"])
    assert out["ea:plain"]["b"] == {"?gate_x": 1} and out["ea:plain"]["order"] == ["?bullish", "?gate_x"]
    assert out["ea:other-name"]["b"] == {"?cond_0": 1}
    json.dumps(out)


def test_error_rule_and_empty():
    rec = EntryGateRecord()
    assert rec.record() is None
    rec.note_rules([_ev(5, "boom", [], False, error="x")])
    assert rec.record()["ea:boom"]["b"] == {"?error": 1}


def test_entry_gate_field():
    assert soh._entry_gate_field({}) == {}
    assert soh._entry_gate_field({"entry_gate_record": None}) == {}
    blob = {"r": {"n": 1, "met": 0, "b": {"x": 1}, "order": ["x"]}}
    assert soh._entry_gate_field({"entry_gate_record": blob}) == {"eg": blob}


def test_all_three_row_builders_carry_eg():
    src = inspect.getsource(soh)
    assert src.count("**_entry_gate_field(results)") == 1          # worker result dict
    assert src.count("**_entry_gate_field(results or {})") == 1    # in-process row
    assert src.count('{"eg": out["eg"]} if out.get("eg")') == 1    # pooled/distributed row


def test_report_tool(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "report_entry_gates", Path(__file__).resolve().parents[3] / "tools" / "report_entry_gates.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    eg_a = {"r": {"n": 10, "met": 2, "b": {"g1": 5, "g2": 3}, "order": ["g1", "g2"]}}
    eg_b = {"r": {"n": 10, "met": 4, "b": {"g1": 2, "g2": 4}, "order": ["g1", "g2"]}}
    trials = [{"trades": 0, "eg": eg_a}, {"trades": 3, "eg": eg_b}, {"trades": 1}]
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE strategy_optimizations (id INTEGER PRIMARY KEY, all_results TEXT)")
    con.execute("INSERT INTO strategy_optimizations VALUES (7, ?)", (json.dumps(trials),))
    con.commit()
    con.close()
    assert tool.sum_eg(trials)["r"] == {"n": 20, "met": 6, "b": {"g1": 7, "g2": 7}, "order": ["g1", "g2"]}
    assert tool.main(["--opt-id", "7", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "zero-trade trials: 1 trial(s)" in out and "trading trials: 2 trial(s)" in out
    assert "rule r: evaluated=10 met=2" in out and "reached=5" in out  # g2 reached 10-5
    assert tool.main(["--opt-id", "99", "--db", str(db)]) == 1
