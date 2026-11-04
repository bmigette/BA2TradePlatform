"""``tools/build_style_seed_bundle.py``: policy -> bundle, read-only, on a hand-built sqlite DB."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("build_style_seed_bundle", str(_REPO / "tools" / "build_style_seed_bundle.py"))
T = importlib.util.module_from_spec(_spec)
sys.modules["build_style_seed_bundle"] = T
_spec.loader.exec_module(T)

SPACE = {"cond:a:x": {"type": "int", "min": 1, "max": 9}, "entry:a-entry:enabled": {"type": "int", "min": 0, "max": 1}}


def _rec(fit, ret, dd, trades, x):
    return {"params": {"cond:a:x": x, "entry:a-entry:enabled": 1}, "fitness": fit, "key": f"k{x}",
            "trades": trades, "total_return": ret, "max_drawdown": dd}


RESULTS = [_rec(10.0, 500.0, -20.0, 300, 1), _rec(10.0, 500.0, -20.0, 300, 2),   # clone
           _rec(8.0, 200.0, -10.0, 100, 3), _rec(5.0, 90.0, -5.0, 60, 4)]


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.execute("create table strategy_optimizations (id integer primary key, name text, status text, "
              "parameter_ranges text, best_params text, best_fitness real, all_results text, optimization_config text)")
    c.execute("create table backtests (id integer primary key, name text, status text, strategy_params text, "
              "total_trades int, ga_fitness real, optimization_id int)")
    cfg = json.dumps({"backtest": {"start_date": "2020-01-01", "end_date": "2025-12-31"}})
    c.execute("insert into strategy_optimizations values (1,'optA','completed',?,?,10.0,?,?)",
              (json.dumps(SPACE), json.dumps(RESULTS[0]["params"]), json.dumps(RESULTS), cfg))
    c.execute("insert into strategy_optimizations values (2,'optRun','running',?,?,1.0,?,?)",
              (json.dumps(SPACE), json.dumps(RESULTS[0]["params"]), json.dumps(RESULTS), cfg))
    sp = dict(RESULTS[0]["params"], entryRules=[1], exitRules=[2], expertFixedSettings={"z": 1})
    c.execute("insert into backtests values (7,'bt','completed',?,300,9.0,1)", (json.dumps(sp),))
    c.execute("insert into backtests values (8,'pin','completed',?,300,9.0,1)",
              (json.dumps(dict(RESULTS[0]["params"], _atr_swap_migration=True)),))
    c.commit(); c.close()
    return p


def _policy(source, **kw):
    return {"schema": 1, "style": "mid", "fill": {"a": 1}, "out_of_range": "clamp",
            "individuals": [{"contributions": [{"member": "O_LC", "source": source}], "shared_from": 0,
                             "other_members": "off"}], **kw}


def _run(tmp_path, db, policy, *extra):
    pf, out = tmp_path / "p.json", tmp_path / "b.json"
    pf.write_text(json.dumps(policy))
    rc = T.main(["--policy", str(pf), "--out", str(out), "--db", str(db), *extra])
    return rc, out


def test_best_pick_reads_best_params(tmp_path, db):
    rc, out = _run(tmp_path, db, _policy({"opt_id": 1, "pick": "best"}))
    assert rc == 0
    c = json.loads(out.read_text())["individuals"][0]["contributions"][0]
    assert c["genes"] == RESULTS[0]["params"]
    assert c["resolved"]["opt_name"] == "optA" and c["resolved"]["pick"] == "best"


def test_distinct_pick_skips_clones_and_resolves_by_name(tmp_path, db):
    rc, out = _run(tmp_path, db, _policy({"opt_name": "optA", "pick": "distinct", "rank": 2}))
    assert rc == 0
    c = json.loads(out.read_text())["individuals"][0]["contributions"][0]
    assert c["genes"]["cond:a:x"] == 3 and c["resolved"]["rank"] == 2 and c["resolved"]["trades"] == 100
    assert c["resolved"]["params_key"]


def test_missing_rank_is_refused_naming_the_field(tmp_path, db, capsys):
    rc, _ = _run(tmp_path, db, _policy({"opt_id": 1, "pick": "distinct"}))
    assert rc == 2 and "individuals[0].contributions[0].source.rank" in capsys.readouterr().err


def test_rank_beyond_available_is_refused(tmp_path, db, capsys):
    rc, _ = _run(tmp_path, db, _policy({"opt_id": 1, "pick": "distinct", "rank": 4}))
    assert rc == 2 and "behaviour-distinct" in capsys.readouterr().err


def test_backtest_pick_strips_only_the_named_extras(tmp_path, db):
    rc, out = _run(tmp_path, db, _policy({"backtest_id": 7}))
    assert rc == 0
    c = json.loads(out.read_text())["individuals"][0]["contributions"][0]
    assert c["genes"] == RESULTS[0]["params"] and c["resolved"]["backtest_id"] == 7


def test_a_row_pin_key_is_refused(tmp_path, db, capsys):
    rc, _ = _run(tmp_path, db, _policy({"backtest_id": 8}))
    assert rc == 2 and "_atr_swap_migration" in capsys.readouterr().err


def test_genome_keys_must_equal_source_space(tmp_path, db, capsys):
    c = sqlite3.connect(db)
    c.execute("update strategy_optimizations set best_params=? where id=1", (json.dumps({"cond:a:x": 1}),))
    c.commit(); c.close()
    rc, _ = _run(tmp_path, db, _policy({"opt_id": 1, "pick": "best"}))
    assert rc == 2 and "entry:a-entry:enabled" in capsys.readouterr().err


def test_running_source_needs_allow_running(tmp_path, db, capsys):
    rc, _ = _run(tmp_path, db, _policy({"opt_id": 2, "pick": "best"}))
    assert rc == 2 and "running" in capsys.readouterr().err
    rc, _ = _run(tmp_path, db, _policy({"opt_id": 2, "pick": "best"}), "--allow-running")
    assert rc == 0


def test_missing_source_is_refused(tmp_path, db, capsys):
    rc, _ = _run(tmp_path, db, _policy({"opt_id": 99, "pick": "best"}))
    assert rc == 2 and "no optimization" in capsys.readouterr().err


def test_the_tool_never_writes_the_db(tmp_path, db):
    before = db.read_bytes()
    _run(tmp_path, db, _policy({"opt_id": 1, "pick": "distinct", "rank": 1}))
    assert db.read_bytes() == before


@pytest.mark.parametrize("mut,field", [
    (lambda p: p.update(schema=2), "schema"),
    (lambda p: p.update(style=""), "style"),
    (lambda p: p.update(individuals=[]), "individuals"),
    (lambda p: p["individuals"][0].update(shared_from=5), "shared_from"),
    (lambda p: p["individuals"][0].update(other_members="x"), "other_members"),
    (lambda p: p.update(out_of_range="maybe"), "out_of_range"),
    (lambda p: p["individuals"][0]["contributions"][0]["source"].update(opt_name="x"), "source"),
])
def test_policy_schema_errors_name_the_field(tmp_path, db, capsys, mut, field):
    p = _policy({"opt_id": 1, "pick": "best"})
    mut(p)
    rc, _ = _run(tmp_path, db, p)
    assert rc == 2 and field in capsys.readouterr().err


def test_bundle_carries_policy_hash_and_db_path(tmp_path, db):
    rc, out = _run(tmp_path, db, _policy({"opt_id": 1, "pick": "best"}))
    b = json.loads(out.read_text())
    assert len(b["policy_sha256"]) == 64 and b["built_at"] and b["db_path"].endswith("t.db")
    assert b["style"] == "mid" and b["fill"] == {"a": 1}
