"""tools/merge_remote_test_db.py: a remote master's test DB merged into the local one, every reference remapped,
incremental per source tag, one transaction, the real schema (``create_all`` of the app models)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import app.models  # noqa: E402,F401
from app.models.database import Base  # noqa: E402

import merge_remote_test_db as MR  # noqa: E402


def _db(path):
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    eng.dispose()
    return str(path)


def _insert(c, table, **vals):
    """Insert with every other NOT NULL column filled by a type-appropriate dummy (the real schema)."""
    for _, name, typ, notnull, default, pk in c.execute(f"PRAGMA table_info('{table}')").fetchall():
        if notnull and not pk and default is None and name not in vals:
            t = (typ or "").upper()
            vals[name] = (0 if any(k in t for k in ("INT", "FLOAT", "REAL", "NUM", "BOOL")) else
                          "2024-01-01 00:00:00" if any(k in t for k in ("DATE", "TIME")) else "x")
    cols = list(vals)
    c.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
              [vals[k] for k in cols])


def _strategy(c, sid, name):
    _insert(c, "strategies", id=sid, name=name, entry_rules="[]", exit_rules="[]")


def _opt(c, oid, name, sid, status="completed", cfg=None, bt=None):
    _insert(c, "strategy_optimizations", id=oid, strategy_id=sid, backtest_id=bt, name=name, status=status,
            fitness_metric="sharpe", optimization_type="genetic", optimization_config=json.dumps(cfg or {}),
            all_results="[]", best_fitness=1.0)


def _bt(c, bid, name, sid, oid, status="completed", labels=("Grid",)):
    _insert(c, "backtests", id=bid, name=name, engine_type="daily", strategy_id=sid, status=status,
            optimization_id=oid, labels=json.dumps(list(labels)), trades='[{"pnl": 1}]',
            results='{"total_return": 5}')


@pytest.fixture
def dbs(tmp_path):
    src, tgt = _db(tmp_path / "remote.db"), _db(tmp_path / "local.db")
    with sqlite3.connect(tgt) as c:                               # the local DB already holds rows 1..3
        _strategy(c, 1, "local-s")
        _opt(c, 1, "local-opt", 1)
        _bt(c, 3, "local-bt", 1, 1)
    with sqlite3.connect(src) as c:
        _strategy(c, 1, "remote-s1")
        _strategy(c, 2, "remote-s2")
        _opt(c, 1, "cell-goal2027-wf1", 1)
        _opt(c, 2, "cell-goal2027-wf2", 2, cfg={"ga": {"chainSeedFromOptimizationId": 1},
                                               "seededFrom": {"optimization_id": 1, "name": "cell-goal2027-wf1"}})
        _opt(c, 3, "other-job", 1)
        _bt(c, 1, "TOP1-cell-goal2027-wf1", 1, 1, labels=("Grid", "ForwardTest"))
        _bt(c, 2, "WF1-OOS-R1-cell-goal2027-wf1", 1, 1)
        _bt(c, 3, "WF2-OOS-R1-cell-goal2027-wf2", 2, 2)
        _insert(c, "strategy_optimization_populations", optimization_id=1, gene_space_signature="[]",
                population="[]")
    return src, tgt


def _merge(src, tgt, tag="remote227-goal2027", **kw):
    m = MR.Merger(tgt, src, tag, log=lambda *_: None, **kw)
    rep = m.run()
    m.commit()
    return m, rep


def _q(db, sql, *a):
    with sqlite3.connect(db) as c:
        return c.execute(sql, a).fetchall()


def test_rows_are_copied_with_offset_ids_and_every_reference_remapped(dbs):
    src, tgt = dbs
    m, rep = _merge(src, tgt)
    assert m.offsets == {"strategies": 1000, "strategy_optimizations": 1000, "backtests": 1000,
                         "robustness_runs": 1000}
    assert _q(tgt, "SELECT id, strategy_id, name FROM strategy_optimizations WHERE id >= 1000 ORDER BY id") == [
        (1001, 1001, "cell-goal2027-wf1"), (1002, 1002, "cell-goal2027-wf2"), (1003, 1001, "other-job")]
    assert _q(tgt, "SELECT id, optimization_id, strategy_id FROM backtests WHERE id >= 1000 ORDER BY id") == [
        (1001, 1001, 1001), (1002, 1001, 1001), (1003, 1002, 1002)]
    cfg = json.loads(_q(tgt, "SELECT optimization_config FROM strategy_optimizations WHERE id=1002")[0][0])
    assert cfg["ga"]["chainSeedFromOptimizationId"] == 1001 and cfg["seededFrom"]["optimization_id"] == 1001
    assert _q(tgt, "SELECT optimization_id FROM strategy_optimization_populations") == [(1001,)]
    assert _q(tgt, "SELECT id, name FROM backtests WHERE id = 3") == [(3, "local-bt")]      # untouched


def test_blobs_and_forwardtest_labels_are_kept(dbs):
    src, tgt = dbs
    _merge(src, tgt)
    labels, trades, results = _q(tgt, "SELECT labels, trades, results FROM backtests WHERE id=1001")[0]
    assert "ForwardTest" in json.loads(labels)
    assert json.loads(trades) == [{"pnl": 1}] and json.loads(results) == {"total_return": 5}


def test_name_filter_merges_only_those_jobs_and_their_rows(dbs):
    src, tgt = dbs
    m, _ = _merge(src, tgt, name_like="%goal2027%")
    assert sorted(m.maps["strategy_optimizations"]) == [1, 2]
    assert _q(tgt, "SELECT count(*) FROM strategy_optimizations WHERE name='other-job'") == [(0,)]


def test_a_second_merge_adds_only_new_rows_with_the_same_offsets(dbs):
    src, tgt = dbs
    _merge(src, tgt)
    with sqlite3.connect(src) as c:                              # January: the remote ran more
        _opt(c, 4, "cell-goal2027-refit", 2, cfg={"ga": {"chainSeedFromOptimizationId": 2}})
        _bt(c, 4, "TOP1-cell-goal2027-refit", 2, 4)
    with sqlite3.connect(tgt) as c:                              # and the local DB grew meanwhile
        _bt(c, 1500, "local-later", 1, 1)
    m, rep = _merge(src, tgt)
    assert rep["inserted"]["strategy_optimizations"] == 1 and rep["skipped_merged"]["strategy_optimizations"] == 3
    assert m.offsets["backtests"] == 1000                        # recorded, not recomputed from the new max
    cfg = json.loads(_q(tgt, "SELECT optimization_config FROM strategy_optimizations WHERE id=1004")[0][0])
    assert cfg["ga"]["chainSeedFromOptimizationId"] == 1002
    assert _q(tgt, "SELECT optimization_id FROM backtests WHERE id=1004") == [(1004,)]


def test_running_jobs_are_skipped_then_merged_later_under_their_reserved_id(dbs):
    src, tgt = dbs
    with sqlite3.connect(src) as c:
        _opt(c, 5, "cell-goal2027-wf3", 2, status="running")
        _bt(c, 5, "TOP1-x", 2, 5, status="running")
    _, rep = _merge(src, tgt)
    assert [r[0] for r in rep["skipped_running"]] == [5]
    assert _q(tgt, "SELECT count(*) FROM strategy_optimizations WHERE id=1005") == [(0,)]
    with sqlite3.connect(src) as c:
        c.execute("UPDATE strategy_optimizations SET status='completed' WHERE id=5")
        c.execute("UPDATE backtests SET status='completed' WHERE id=5")
    _merge(src, tgt)
    assert _q(tgt, "SELECT status FROM strategy_optimizations WHERE id=1005") == [("completed",)]
    assert _q(tgt, "SELECT optimization_id FROM backtests WHERE id=1005") == [(1005,)]


def test_running_as_cancelled_imports_with_a_note(dbs):
    src, tgt = dbs
    with sqlite3.connect(src) as c:
        _opt(c, 5, "cell-goal2027-wf3", 2, status="running")
    _merge(src, tgt, running_as_cancelled=True)
    status, err = _q(tgt, "SELECT status, error_message FROM strategy_optimizations WHERE id=1005")[0]
    assert status == "cancelled" and "still running at copy time" in err


def test_refresh_rewrites_an_already_merged_row(dbs):
    src, tgt = dbs
    _merge(src, tgt)
    with sqlite3.connect(src) as c:
        c.execute("UPDATE strategy_optimizations SET best_fitness=9.5 WHERE id=1")
    _merge(src, tgt)
    assert _q(tgt, "SELECT best_fitness FROM strategy_optimizations WHERE id=1001") == [(1.0,)]
    _, rep = _merge(src, tgt, refresh=True)
    assert _q(tgt, "SELECT best_fitness FROM strategy_optimizations WHERE id=1001") == [(9.5,)]
    assert rep["refreshed"]["strategy_optimizations"] == 3


def test_a_collision_refuses_and_writes_nothing(dbs):
    src, tgt = dbs
    with sqlite3.connect(tgt) as c:
        _bt(c, 2002, "squatter", 1, 1)
    with pytest.raises(MR.MergeError, match="already taken"):
        _merge(src, tgt, offsets={"backtests": 2000})
    assert _q(tgt, "SELECT count(*) FROM strategy_optimizations") == [(1,)]
    assert _q(tgt, "SELECT count(*) FROM sqlite_master WHERE name='merge_provenance'") == [(0,)]


def test_a_source_column_the_target_lacks_is_refused(dbs):
    src, tgt = dbs
    with sqlite3.connect(src) as c:
        c.execute("ALTER TABLE backtests ADD COLUMN future_col TEXT")
    with pytest.raises(MR.MergeError, match="future_col"):
        MR.Merger(tgt, src, "t", log=lambda *_: None).run()


def test_dry_run_writes_nothing(dbs, capsys):
    src, tgt = dbs
    assert MR.main(["--source", src, "--target", tgt, "--source-tag", "t", "--dry-run"]) == 0
    assert _q(tgt, "SELECT count(*) FROM backtests") == [(1,)]
    assert not os.path.exists(f"{tgt}.merge_t.json")


def test_cli_writes_the_mapping_and_rewrites_walk_forward_reports(dbs, tmp_path):
    src, tgt = dbs
    rep_dir = tmp_path / "goal2027_wf" / "cell"
    rep_dir.mkdir(parents=True)
    (rep_dir / "fold1.json").write_text(json.dumps({
        "key": {"train_jobs": [{"id": 1, "completed_at": "2026-12-01"}]},
        "genomes": [{"fold": 1, "rank": 1, "oos_backtest_id": 2}]}), encoding="utf-8")
    assert MR.main(["--source", src, "--target", tgt, "--source-tag", "t",
                    "--rewrite-reports", str(tmp_path / "goal2027_wf")]) == 0
    mapping = json.loads(open(f"{tgt}.merge_t.json", encoding="utf-8").read())
    assert mapping["tables"]["backtests"]["2"] == 1002 and mapping["offsets"]["backtests"] == 1000
    doc = json.loads((rep_dir / "fold1.json").read_text(encoding="utf-8"))
    assert doc["key"]["train_jobs"][0]["id"] == 1001 and doc["genomes"][0]["oos_backtest_id"] == 1002


def test_same_file_is_refused(dbs):
    src, _ = dbs
    with pytest.raises(MR.MergeError, match="same file"):
        MR.Merger(src, src, "t")


def test_the_source_is_attached_read_only(dbs):
    src, tgt = dbs
    m = MR.Merger(tgt, src, "t", log=lambda *_: None)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        m.con.execute("DELETE FROM src.backtests")


# ------------------------------------------------------------------ references outside the merge (stage 2 seeded
# from stage-1 jobs that were hand-merged on 2026-10-24 with opt ids +100)


@pytest.fixture
def seeded(dbs, tmp_path):
    src, tgt = dbs
    with sqlite3.connect(src) as c:
        _opt(c, 6, "stage2-jobC", 2, cfg={"ga": {"chainSeedFromOptimizationId": 3},
                                         "seededFrom": {"optimization_id": 3, "name": "other-job"}})
    with sqlite3.connect(tgt) as c:                              # the hand-merged stage-1 row: 3 -> 103
        _opt(c, 103, "other-job", 1)
    legacy = tmp_path / "merge227_mapping.json"
    legacy.write_text(json.dumps({"opt_offset": 100, "opts": {"3": 103}, "strategies": {},
                                  "backtests_kept_ids": True}), encoding="utf-8")
    return src, tgt, str(legacy)


def test_a_reference_outside_the_merge_is_refused_without_a_mapping(seeded):
    src, tgt, _ = seeded
    with pytest.raises(MR.MergeError, match="outside this merge.*other-job"):
        _merge(src, tgt, name_like="stage2-%")
    assert _q(tgt, "SELECT count(*) FROM strategy_optimizations WHERE name='stage2-jobC'") == [(0,)]


def test_the_2026_10_24_hand_merge_file_resolves_it_checked_by_name(seeded):
    src, tgt, legacy = seeded
    _merge(src, tgt, name_like="stage2-%", prior=MR.load_prior_mapping(legacy))
    cfg = json.loads(_q(tgt, "SELECT optimization_config FROM strategy_optimizations WHERE name='stage2-jobC'")[0][0])
    assert cfg["ga"]["chainSeedFromOptimizationId"] == 103 and cfg["seededFrom"]["optimization_id"] == 103


def test_a_prior_mapping_pointing_at_another_row_is_refused(seeded, tmp_path):
    src, tgt, _ = seeded
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"opts": {"3": 1}, "strategies": {}}), encoding="utf-8")   # local 1 = local-opt
    with pytest.raises(MR.MergeError, match="local-opt"):
        _merge(src, tgt, name_like="stage2-%", prior=MR.load_prior_mapping(str(bad)))


def test_allow_outside_refs_maps_to_the_tag_stable_id_and_reports_it(seeded):
    src, tgt, _ = seeded
    _, rep = _merge(src, tgt, name_like="stage2-%", allow_outside_refs=True)
    assert rep["outside_refs"] == [("strategy_optimizations", 3, "other-job", 1003)]


def test_this_tools_own_mapping_file_works_as_a_prior_mapping(dbs, tmp_path):
    src, tgt = dbs
    assert MR.main(["--source", src, "--target", tgt, "--source-tag", "first", "--name-like", "cell-%"]) == 0
    with sqlite3.connect(src) as c:
        _opt(c, 7, "later-job", 1, cfg={"seededFrom": {"optimization_id": 2}})
    prior = MR.load_prior_mapping(f"{tgt}.merge_first.json")
    _merge(src, tgt, tag="second", name_like="later-%", prior=prior)
    cfg = json.loads(_q(tgt, "SELECT optimization_config FROM strategy_optimizations WHERE name='later-job'")[0][0])
    assert cfg["seededFrom"]["optimization_id"] == 1002
