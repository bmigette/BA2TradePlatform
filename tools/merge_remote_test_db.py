#!/usr/bin/env python
"""Merge a REMOTE master's test-platform DB (a copy of its dl_forecasting.db) into the local one.

Why: grid masters run on remote hosts (remote227: stage 2, goal2027) with their own test DB; the
results are analysed and deployed from the owner PC. The 2026-10-24 stage-1 merge was done by hand
(optimization ids +100, a loose mapping file). This tool makes it repeatable and incremental.

What is copied, with every reference remapped:
  strategies                          (those the selected optimizations/backtests use)
  strategy_optimizations              strategy_id, backtest_id; optimization_config ids:
                                      chainSeedFromOptimizationId, warmStartFromOptimizationId,
                                      seededFrom.optimization_id (any depth)
  strategy_optimization_populations   optimization_id (the chain-seed source populations)
  backtests                           strategy_id, optimization_id; results/trades/equity blobs
                                      copied verbatim (no ids inside); labels kept (ForwardTest)
  robustness_runs                     backtest_id, variant_backtest_ids
Per-run trading DB files are keyed by each run's own config id, not by these table ids: copy
them separately if wanted.

IDs: every copied row gets ``source id + offset`` (one offset per table, default the next multiple of
1000 above the target's max id), so a remote opt 5 becomes e.g. 1005 and the mapping stays readable.
Offsets and every mapped pair are RECORDED in the target (table ``merge_provenance``) under
``--source-tag``: a later copy of the same remote DB merges only the NEW rows with the SAME offsets
(phase A in December, phase B in January). A row already merged is skipped; ``--refresh`` rewrites it
from the source (a job that was still running at the previous copy). A collision is refused.

Rows still running/pending in the source are skipped (listed); ``--running-as-cancelled`` imports them
as ``cancelled`` with a note. Everything runs in ONE transaction: a refusal changes nothing.
``--dry-run`` prints the plan and rolls back. The mapping is written as JSON (``--mapping-out``).
``--rewrite-reports DIR`` rewrites the ids inside walk-forward report JSONs copied from the remote
(``oos_backtest_id``, ``backtest_id``, ``train_jobs[].id``) with the same mapping.

Usage:
  python tools/merge_remote_test_db.py --source remote227_goal2027.db --source-tag remote227-goal2027 \\
      [--target ~/Documents/ba2/test/dl_forecasting.db] [--name-like '%goal2027%'] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_ID_TABLES = ("strategies", "strategy_optimizations", "backtests", "robustness_runs")
_RUNNING = ("running", "pending", "queued")
_OPT_ID_KEYS = ("chainSeedFromOptimizationId", "warmStartFromOptimizationId")


class MergeError(Exception):
    """A refusal. Nothing is written (the transaction is rolled back)."""


def default_target() -> str:
    return os.path.join(os.environ.get("BA2_HOME", os.path.expanduser("~/Documents/ba2")), "test",
                        "dl_forecasting.db")


def _columns(con: sqlite3.Connection, schema: str, table: str) -> List[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info('{table}')")]


def _ensure_provenance(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS main.merge_provenance (source_tag TEXT NOT NULL, tbl TEXT NOT NULL, "
                "source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, merged_at TEXT NOT NULL, "
                "PRIMARY KEY (source_tag, tbl, source_id))")
    con.execute("CREATE TABLE IF NOT EXISTS main.merge_offsets (source_tag TEXT NOT NULL, tbl TEXT NOT NULL, "
                "id_offset INTEGER NOT NULL, PRIMARY KEY (source_tag, tbl))")


def _remap_opt_ids(node: Any, resolve) -> Any:
    """Every optimization id inside an optimization_config, passed through ``resolve`` (source id -> target id)."""
    def m(v):
        return resolve(v) if isinstance(v, int) and not isinstance(v, bool) else v

    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _OPT_ID_KEYS:
                out[k] = m(v)
            elif k == "seededFrom" and isinstance(v, dict):
                out[k] = {kk: (m(vv) if kk == "optimization_id" else _remap_opt_ids(vv, resolve))
                          for kk, vv in v.items()}
            else:
                out[k] = _remap_opt_ids(v, resolve)
        return out
    if isinstance(node, list):
        return [_remap_opt_ids(v, resolve) for v in node]
    return node


def load_prior_mapping(path: str) -> Dict[str, Any]:
    """A mapping of source rows merged BEFORE, not under this tag: this tool's ``--mapping-out`` JSON
    (``{"tables": {table: {src: tgt}}}``) or the 2026-10-24 hand-merge file (``{"opts": {...}, "strategies":
    {...}, "backtests_kept_ids": true}``). Returns ``{"tables": {...}, "identity": set(tables)}``."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if "tables" in doc:
        tables = {t: {int(k): int(v) for k, v in m.items()} for t, m in doc["tables"].items()}
        return {"tables": tables, "identity": set()}
    tables = {"strategy_optimizations": {int(k): int(v) for k, v in (doc.get("opts") or {}).items()},
              "strategies": {int(k): int(v) for k, v in (doc.get("strategies") or {}).items()}}
    return {"tables": tables, "identity": {"backtests"} if doc.get("backtests_kept_ids") else set()}


class Merger:
    def __init__(self, target: str, source: str, tag: str, *, name_like: Optional[str] = None,
                 refresh: bool = False, running_as_cancelled: bool = False,
                 offsets: Optional[Dict[str, int]] = None, prior: Optional[Dict[str, Any]] = None,
                 allow_outside_refs: bool = False, log=print):
        if not os.path.isfile(source):
            raise MergeError(f"source DB not found: {source}")
        if not os.path.isfile(target):
            raise MergeError(f"target DB not found: {target}")
        if os.path.samefile(source, target):
            raise MergeError("source and target are the same file")
        if not tag or any(c.isspace() for c in tag):
            raise MergeError("--source-tag must be a non-empty word, e.g. remote227-goal2027")
        from pathlib import Path
        # URI connections so the source can be attached READ-ONLY: this tool never writes to it.
        self.con = sqlite3.connect(Path(target).resolve().as_uri(), uri=True, isolation_level=None)
        self.con.execute("PRAGMA foreign_keys=OFF")
        self.con.execute("ATTACH DATABASE ? AS src", (Path(source).resolve().as_uri() + "?mode=ro",))
        self.tag, self.name_like, self.refresh = tag, name_like, refresh
        self.running_as_cancelled = running_as_cancelled
        self.forced_offsets = dict(offsets or {})
        self.prior = prior or {"tables": {}, "identity": set()}
        self.allow_outside_refs = allow_outside_refs
        self._outside: List[Tuple[str, int, Optional[str]]] = []
        self.log = log
        self.maps: Dict[str, Dict[int, int]] = {t: {} for t in _ID_TABLES}
        self.offsets: Dict[str, int] = {}
        self.report: Dict[str, Any] = {"inserted": {}, "refreshed": {}, "skipped_merged": {}, "skipped_running": [],
                                       "imported_cancelled": [], "dangling_opt_ids": [], "outside_refs": []}

    # ----------------------------------------------------------------------------------- plan
    def _check_schema(self) -> None:
        for t in (*_ID_TABLES, "strategy_optimization_populations"):
            src = _columns(self.con, "src", t)
            if not src:
                continue                                        # the source never had this table
            tgt = set(_columns(self.con, "main", t))
            extra = [c for c in src if c not in tgt]
            if extra:
                raise MergeError(f"{t}: the source has columns the target lacks {extra}: migrate the target "
                                 f"first (scripts/migrate_db.py) -- copying would drop them")

    def _offset(self, table: str) -> int:
        row = self.con.execute("SELECT id_offset FROM main.merge_offsets WHERE source_tag=? AND tbl=?",
                               (self.tag, table)).fetchone()
        if row:
            if table in self.forced_offsets and self.forced_offsets[table] != row[0]:
                raise MergeError(f"{table}: --offset {self.forced_offsets[table]} differs from the recorded offset "
                                 f"{row[0]} of tag {self.tag!r} (one tag keeps one offset)")
            return row[0]
        if table in self.forced_offsets:
            off = self.forced_offsets[table]
        else:
            mx = self.con.execute(f"SELECT COALESCE(MAX(id), 0) FROM main.{table}").fetchone()[0]
            off = int(math.ceil((mx + 1) / 1000.0)) * 1000
        self.con.execute("INSERT INTO main.merge_offsets VALUES (?,?,?)", (self.tag, table, off))
        return off

    def _merged(self, table: str) -> Dict[int, int]:
        return {s: t for s, t in self.con.execute(
            "SELECT source_id, target_id FROM main.merge_provenance WHERE source_tag=? AND tbl=?", (self.tag, table))}

    def _selected_opts(self) -> List[Tuple[int, str, str]]:
        sql = "SELECT id, name, status FROM src.strategy_optimizations"
        args: tuple = ()
        if self.name_like:
            sql += " WHERE name LIKE ?"
            args = (self.name_like,)
        return list(self.con.execute(sql + " ORDER BY id", args))

    # ----------------------------------------------------------------------------------- copy
    def _copy_rows(self, table: str, ids: Iterable[int], transform) -> None:
        ids = sorted(set(ids))
        if not ids:
            return
        cols = _columns(self.con, "src", table)
        tcols = set(_columns(self.con, "main", table))
        cols = [c for c in cols if c in tcols]
        done = self._merged(table)
        off = self.offsets[table]
        ins = upd = skip = 0
        now = datetime.now(timezone.utc).isoformat()
        sel = f"SELECT {', '.join(cols)} FROM src.{table} WHERE id = ?"
        for sid in ids:
            row = self.con.execute(sel, (sid,)).fetchone()
            if row is None:
                continue
            rec = dict(zip(cols, row))
            if sid in done and not self.refresh:
                self.maps[table][sid] = done[sid]
                skip += 1
                continue
            tid = done.get(sid, sid + off)
            self.maps[table][sid] = tid
            rec = transform(rec)
            rec["id"] = tid
            if sid in done:
                sets = ", ".join(f"{c} = ?" for c in cols if c != "id")
                self.con.execute(f"UPDATE main.{table} SET {sets} WHERE id = ?",
                                 [rec[c] for c in cols if c != "id"] + [tid])
                upd += 1
                continue
            if self.con.execute(f"SELECT 1 FROM main.{table} WHERE id = ?", (tid,)).fetchone():
                raise MergeError(f"{table}: target id {tid} (source {sid} + offset {off}) is already taken by a row "
                                 f"this tag did not merge; pick another --offset {table}=N")
            self.con.execute(f"INSERT INTO main.{table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [rec[c] for c in cols])
            self.con.execute("INSERT INTO main.merge_provenance VALUES (?,?,?,?,?)", (self.tag, table, sid, tid, now))
            ins += 1
        self.report["inserted"][table] = ins
        self.report["refreshed"][table] = upd
        self.report["skipped_merged"][table] = skip

    def _src_name(self, table: str, sid: int) -> Optional[str]:
        r = self.con.execute(f"SELECT name FROM src.{table} WHERE id = ?", (sid,)).fetchone()
        return r[0] if r else None

    def _ref(self, table: str, sid: Optional[int]) -> Optional[int]:
        """A reference to a source row, resolved in order: copied in this run; merged earlier under this tag;
        a --prior-mapping (verified: the local row must carry the source row's name); --allow-outside-refs (its
        tag-stable id, reported). Anything else is collected and REFUSED at the end of the run: a reference mapped
        to an id that does not exist locally would point at the wrong row or at nothing."""
        if sid is None:
            return None
        if sid in self.maps[table]:
            return self.maps[table][sid]
        done = self._merged(table)
        if sid in done:
            return done[sid]
        name = self._src_name(table, sid)
        if name is None and table == "strategy_optimizations":
            self.report["dangling_opt_ids"].append(sid)      # not in the source either: kept as is, reported
            return sid
        prior = self.prior["tables"].get(table, {})
        tid = prior.get(sid, sid if table in self.prior["identity"] else None)
        if tid is not None:
            local = self.con.execute(f"SELECT name FROM main.{table} WHERE id = ?", (tid,)).fetchone()
            if local is None or local[0] != name:
                raise MergeError(f"--prior-mapping maps {table} {sid} ({name!r}) to local {tid}, which is "
                                 f"{'missing' if local is None else repr(local[0])}")
            return tid
        if self.allow_outside_refs:
            tid = sid + self.offsets[table]
            if (table, sid, name, tid) not in self.report["outside_refs"]:
                self.report["outside_refs"].append((table, sid, name, tid))
            return tid
        if (table, sid, name) not in self._outside:
            self._outside.append((table, sid, name))
        return sid

    def _map(self, table: str, sid: Optional[int]) -> Optional[int]:
        if sid is None:
            return None
        if sid not in self.maps[table]:
            raise MergeError(f"reference to {table} id {sid} that is not part of this merge")
        return self.maps[table][sid]

    def run(self) -> Dict[str, Any]:
        self._check_schema()
        self.con.execute("BEGIN")
        try:
            _ensure_provenance(self.con)
            for t in _ID_TABLES:
                self.offsets[t] = self._offset(t)
            opts = []
            for oid, name, status in self._selected_opts():
                if status in _RUNNING and not self.running_as_cancelled:
                    self.report["skipped_running"].append((oid, name, status))
                    continue
                if status in _RUNNING:
                    self.report["imported_cancelled"].append((oid, name, status))
                opts.append(oid)
            opt_set = set(opts)
            if self.name_like:
                bts = [r[0] for r in self.con.execute("SELECT id FROM src.backtests WHERE optimization_id IN "
                                                      f"({','.join('?' * len(opt_set))})", tuple(opt_set))] \
                    if opt_set else []
            else:
                bts = [r[0] for r in self.con.execute("SELECT id FROM src.backtests")]
            # an optimization's own backtest_id may point at a row outside its optimization_id group
            bts += [r[0] for r in self.con.execute(
                f"SELECT backtest_id FROM src.strategy_optimizations WHERE backtest_id IS NOT NULL AND id IN "
                f"({','.join('?' * len(opt_set)) or 'NULL'})", tuple(opt_set))]
            running_bts = {r[0] for r in self.con.execute(
                f"SELECT id FROM src.backtests WHERE status IN ({','.join('?' * len(_RUNNING))})", _RUNNING)}
            bts = [b for b in bts if b not in running_bts or self.running_as_cancelled]
            strategies = {r[0] for r in self.con.execute(
                f"SELECT strategy_id FROM src.strategy_optimizations WHERE strategy_id IS NOT NULL AND id IN "
                f"({','.join('?' * len(opt_set)) or 'NULL'})", tuple(opt_set))}
            strategies |= {r[0] for r in self.con.execute(
                f"SELECT strategy_id FROM src.backtests WHERE strategy_id IS NOT NULL AND id IN "
                f"({','.join('?' * len(bts)) or 'NULL'})", tuple(bts))}

            # optimization ids are reserved FIRST: backtests and configs point at them. ANY source optimization
            # maps to its tag-stable id (merged id, else source id + offset), also one skipped now (still
            # running, or outside --name-like): a later merge of this tag gives it exactly that id.
            omerged = self._merged("strategy_optimizations")
            ooff = self.offsets["strategy_optimizations"]
            for oid in opts:
                self.maps["strategy_optimizations"][oid] = omerged.get(oid, oid + ooff)

            self._copy_rows("strategies", strategies, lambda r: r)

            bt_cancel = self.running_as_cancelled

            def bt_row(r):
                r["strategy_id"] = self._map("strategies", r.get("strategy_id"))
                r["optimization_id"] = self._ref("strategy_optimizations", r.get("optimization_id"))
                for k in ("model_id", "prediction_dataset_id", "execution_dataset_id"):
                    if r.get(k) is not None:
                        raise MergeError(f"backtest {r['id']}: {k}={r[k]} references a table this tool does not "
                                         f"merge (ML datasets/models)")
                if bt_cancel and r.get("status") in _RUNNING:
                    r["status"] = "cancelled"
                return r
            self._copy_rows("backtests", bts, bt_row)


            def opt_row(r):
                r["strategy_id"] = self._map("strategies", r.get("strategy_id"))
                r["backtest_id"] = self._map("backtests", r.get("backtest_id"))
                if r.get("optimization_config"):
                    cfg = json.loads(r["optimization_config"])
                    r["optimization_config"] = json.dumps(
                        _remap_opt_ids(cfg, lambda v: self._ref("strategy_optimizations", v)))
                if r.get("status") in _RUNNING:
                    was = r["status"]
                    r["status"] = "cancelled"
                    r["error_message"] = ((r.get("error_message") or "") +
                                          f" [imported as cancelled by merge_remote_test_db ({self.tag}): still "
                                          f"{was} at copy time]").strip()
                return r
            self._copy_rows("strategy_optimizations", opts, opt_row)
            self.report["dangling_opt_ids"] = sorted(set(self.report["dangling_opt_ids"]))

            # chain-seed populations: keyed by optimization_id (no id column)
            pops = 0
            if _columns(self.con, "src", "strategy_optimization_populations"):
                pcols = [c for c in _columns(self.con, "src", "strategy_optimization_populations")
                         if c in set(_columns(self.con, "main", "strategy_optimization_populations"))]
                for row in self.con.execute(f"SELECT {', '.join(pcols)} FROM src.strategy_optimization_populations"
                                            ).fetchall():
                    rec = dict(zip(pcols, row))
                    if rec["optimization_id"] not in opt_set:
                        continue
                    rec["optimization_id"] = self.maps["strategy_optimizations"][rec["optimization_id"]]
                    self.con.execute(f"INSERT OR REPLACE INTO main.strategy_optimization_populations "
                                     f"({', '.join(pcols)}) VALUES ({', '.join('?' * len(pcols))})",
                                     [rec[c] for c in pcols])
                    pops += 1
            self.report["inserted"]["strategy_optimization_populations"] = pops

            if _columns(self.con, "src", "robustness_runs"):
                rr = [r[0] for r in self.con.execute(
                    f"SELECT id FROM src.robustness_runs WHERE backtest_id IN ({','.join('?' * len(bts)) or 'NULL'})",
                    tuple(bts))]

                def rr_row(r):
                    r["backtest_id"] = self._map("backtests", r.get("backtest_id"))
                    if r.get("variant_backtest_ids"):
                        ids = json.loads(r["variant_backtest_ids"])
                        r["variant_backtest_ids"] = json.dumps([self.maps["backtests"].get(i, i) for i in ids])
                    return r
                self._copy_rows("robustness_runs", rr, rr_row)
            if self._outside:
                shown = ", ".join(f"{t} {sid} ({name!r})" for t, sid, name in self._outside[:12])
                raise MergeError(
                    f"{len(self._outside)} reference(s) to source rows outside this merge with no known local id: "
                    f"{shown}{' ...' if len(self._outside) > 12 else ''}. Pass --prior-mapping <json> (rows merged "
                    f"before, e.g. the 2026-10-24 file), widen --name-like, or --allow-outside-refs.")
            return self.report
        except BaseException:
            self.con.execute("ROLLBACK")
            raise

    def commit(self) -> None:
        self.con.execute("COMMIT")

    def rollback(self) -> None:
        self.con.execute("ROLLBACK")

    def mapping(self) -> Dict[str, Any]:
        return {"source_tag": self.tag, "offsets": self.offsets,
                "tables": {t: {str(k): v for k, v in sorted(m.items())} for t, m in self.maps.items()}}


def rewrite_reports(directory: str, mapping: Dict[str, Any], log=print) -> int:
    """Remap the ids inside walk-forward report JSONs (``oos_backtest_id``/``backtest_id`` -> backtests,
    ``train_jobs[].id`` -> strategy_optimizations). Returns the number of files rewritten."""
    bt = {int(k): v for k, v in mapping["tables"]["backtests"].items()}
    opt = {int(k): v for k, v in mapping["tables"]["strategy_optimizations"].items()}

    def walk(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in ("oos_backtest_id", "backtest_id") and isinstance(v, int):
                    out[k] = bt.get(v, v)
                elif k == "train_jobs" and isinstance(v, list):
                    out[k] = [({**j, "id": opt.get(j["id"], j["id"])} if isinstance(j, dict) and
                               isinstance(j.get("id"), int) else j) for j in v]
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    n = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if not f.endswith(".json"):
                continue
            p = os.path.join(root, f)
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
            new = walk(doc)
            if new != doc:
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump(new, fh, indent=2)
                n += 1
    log(f"rewrote ids in {n} report file(s) under {directory}")
    return n


def _parse_offsets(items: List[str]) -> Dict[str, int]:
    out = {}
    for it in items:
        t, _, v = it.partition("=")
        if t not in _ID_TABLES or not v.isdigit():
            raise SystemExit(f"--offset {it!r}: expected <table>=<int>, table in {_ID_TABLES}")
        out[t] = int(v)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="Copy of the remote master's dl_forecasting.db (read only).")
    ap.add_argument("--source-tag", required=True, help="Stable name of that source, e.g. remote227-goal2027.")
    ap.add_argument("--target", default=default_target(), help="Local test DB (default: $BA2_HOME/test/...).")
    ap.add_argument("--name-like", default=None, help="Only optimizations whose name matches (SQL LIKE).")
    ap.add_argument("--refresh", action="store_true", help="Rewrite rows this tag merged before.")
    ap.add_argument("--running-as-cancelled", action="store_true",
                    help="Import still-running source rows as cancelled (default: skip them).")
    ap.add_argument("--offset", action="append", default=[], metavar="TABLE=N",
                    help="Force a table's id offset on the FIRST merge of this tag.")
    ap.add_argument("--prior-mapping", default=None, metavar="JSON",
                    help="Rows of this source merged BEFORE, not under this tag (this tool's mapping JSON, or the "
                         "2026-10-24 hand-merge file dl_forecasting.db.merge227_mapping.json). References to them "
                         "resolve to their local ids, checked by name.")
    ap.add_argument("--allow-outside-refs", action="store_true",
                    help="Resolve references to unmerged, unmapped source rows to their tag-stable ids (reported) "
                         "instead of refusing.")
    ap.add_argument("--mapping-out", default=None, help="Mapping JSON (default: <target>.merge_<tag>.json).")
    ap.add_argument("--rewrite-reports", default=None, metavar="DIR",
                    help="Also remap ids inside the walk-forward report JSONs under DIR (copied from the remote).")
    ap.add_argument("--dry-run", action="store_true", help="Plan only: roll back, write nothing.")
    a = ap.parse_args(argv)
    try:
        m = Merger(a.target, a.source, a.source_tag, name_like=a.name_like, refresh=a.refresh,
                   running_as_cancelled=a.running_as_cancelled, offsets=_parse_offsets(a.offset),
                   prior=load_prior_mapping(a.prior_mapping) if a.prior_mapping else None,
                   allow_outside_refs=a.allow_outside_refs)
        rep = m.run()
    except MergeError as e:
        print(f"merge_remote_test_db: REFUSED: {e}")
        return 2
    print(json.dumps({"offsets": m.offsets, **rep}, indent=2, default=str))
    if a.dry_run:
        m.rollback()
        print("--dry-run: rolled back, nothing written")
        return 0
    m.commit()
    out = a.mapping_out or f"{a.target}.merge_{a.source_tag}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(m.mapping(), fh, indent=2)
    print(f"merged; mapping written to {out}")
    if a.rewrite_reports:
        rewrite_reports(a.rewrite_reports, m.mapping())
    return 0


if __name__ == "__main__":
    sys.exit(main())
