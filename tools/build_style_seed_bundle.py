#!/usr/bin/env python
"""Resolve a style-seed POLICY into a BUNDLE of stage-1 genomes (read-only DB extraction).

    python tools/build_style_seed_bundle.py --policy P.json --out BUNDLE.json [--db DB] [--allow-running]

Policy (schema 1): ``{"schema":1, "style":"mid", "individuals":[{"contributions":[{"member":"O_LC",
"source":{"opt_id":117,"pick":"distinct","rank":1}}], "shared_from":0, "other_members":"off"}],
"fill":{"<glob>": value}, "out_of_range":"clamp"}``.  ``source`` is exactly one of ``opt_id`` /
``opt_name`` (with ``pick`` "best" or "distinct"+``rank``) or ``{"backtest_id": N}``.  "distinct" uses
``app.services.distinct_topn.select_behaviour_distinct`` with its default tolerances and min-trades.

The bundle is the policy plus, per contribution, ``genes`` and ``resolved``; plus ``policy_sha256``,
``built_at``, ``db_path``.  The DB is opened ``mode=ro``; nothing is written to it.
Exit 0 ok, 2 refused (message on stderr).
Plan: docs/plans/2026-11-04-stage2-jobA-seeding-and-gate-counter.md T6.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(os.path.expanduser("~"), "Documents", "ba2", "test", "dl_forecasting.db")
GENE_KEY = re.compile(r"^(cond|entry|exit|optsel|model|schedule|screener):")
BACKTEST_EXTRAS = ("entryRules", "exitRules", "expertFixedSettings")
OTHER_MEMBERS = ("authored", "off")
OUT_OF_RANGE = ("clamp", "refuse")
_SOURCE_KEYS = {"opt_id", "opt_name", "pick", "rank", "backtest_id"}


class Refused(Exception):
    """Printed to stderr by ``main``; exit 2."""


def _need(cond: bool, field: str, msg: str) -> None:
    if not cond:
        raise Refused(f"policy field {field}: {msg}")


def validate_policy(p: Any) -> None:
    _need(isinstance(p, dict), "<root>", "must be an object")
    _need(p.get("schema") == 1, "schema", f"must be 1, got {p.get('schema')!r}")
    _need(isinstance(p.get("style"), str) and bool(p["style"]), "style", "must be a non-empty string")
    inds = p.get("individuals")
    _need(isinstance(inds, list) and bool(inds), "individuals", "must be a non-empty list")
    for i, ind in enumerate(inds):
        f = f"individuals[{i}]"
        _need(isinstance(ind, dict), f, "must be an object")
        cs = ind.get("contributions")
        _need(isinstance(cs, list) and bool(cs), f"{f}.contributions", "must be a non-empty list")
        sf = ind.get("shared_from", 0)
        _need(isinstance(sf, int) and not isinstance(sf, bool) and 0 <= sf < len(cs),
              f"{f}.shared_from", f"must be an index 0..{len(cs) - 1}, got {sf!r}")
        om = ind.get("other_members", "authored")
        _need(om in OTHER_MEMBERS, f"{f}.other_members", f"must be one of {OTHER_MEMBERS}, got {om!r}")
        seen = set()
        for j, c in enumerate(cs):
            g = f"{f}.contributions[{j}]"
            _need(isinstance(c, dict), g, "must be an object")
            _need(isinstance(c.get("member"), str) and bool(c["member"]), f"{g}.member",
                  "must be a non-empty string")
            _need(c["member"] not in seen, f"{g}.member", f"{c['member']!r} appears twice in one individual")
            seen.add(c["member"])
            s = c.get("source")
            _need(isinstance(s, dict), f"{g}.source", "must be an object")
            extra = set(s) - _SOURCE_KEYS
            _need(not extra, f"{g}.source", f"unknown keys {sorted(extra)}")
            if "backtest_id" in s:
                _need(set(s) == {"backtest_id"} and isinstance(s["backtest_id"], int),
                      f"{g}.source", 'a backtest source is exactly {"backtest_id": int}')
                continue
            _need(("opt_id" in s) != ("opt_name" in s), f"{g}.source",
                  "give exactly one of opt_id / opt_name (or backtest_id)")
            pick = s.get("pick")
            _need(pick in ("best", "distinct"), f"{g}.source.pick",
                  f"must be 'best' or 'distinct', got {pick!r}")
            if pick == "distinct":
                r = s.get("rank")
                _need(isinstance(r, int) and not isinstance(r, bool) and r >= 1, f"{g}.source.rank",
                      f"a distinct pick needs an integer rank >= 1, got {r!r}")
            else:
                _need("rank" not in s, f"{g}.source.rank", "a 'best' pick takes no rank")
    _need(isinstance(p.get("fill", {}), dict), "fill", "must be an object of glob -> value")
    _need(p.get("out_of_range", "clamp") in OUT_OF_RANGE, "out_of_range",
          f"must be one of {OUT_OF_RANGE}, got {p.get('out_of_range')!r}")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.execute("select 1 from sqlite_master limit 1")
        return con
    except sqlite3.Error as e:
        raise Refused(f"cannot open {db_path} read-only: {e}") from e


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def _check_genome(genes: Any, space_keys: Optional[set], label: str) -> Dict[str, Any]:
    if not isinstance(genes, dict) or not genes:
        raise Refused(f"{label}: genome is empty or not an object")
    bad = [k for k in genes if not GENE_KEY.match(k)]
    pins = [k for k in bad if k.startswith("_")]
    if pins:
        raise Refused(f"{label}: row-pin key(s) {pins} would override the genome on a re-run; refused")
    if bad:
        raise Refused(f"{label}: non-gene key(s) {bad}")
    if space_keys is not None and set(genes) != space_keys:
        miss, extra = sorted(space_keys - set(genes)), sorted(set(genes) - space_keys)
        raise Refused(f"{label}: genome keys differ from the source parameter_ranges "
                      f"(missing {miss[:5]}, extra {extra[:5]})")
    return dict(genes)


_OPT_COLS = ("id,name,status,parameter_ranges,best_params,best_fitness,all_results,"
             "optimization_config")


def _load_opt(con, src: Dict[str, Any], allow_running: bool, label: str):
    if "opt_id" in src:
        rows = con.execute(f"select {_OPT_COLS} from strategy_optimizations where id=?",
                           (src["opt_id"],)).fetchall()
        what = f"opt_id={src['opt_id']}"
    else:
        rows = con.execute(f"select {_OPT_COLS} from strategy_optimizations where name=?",
                           (src["opt_name"],)).fetchall()
        what = f"opt_name={src['opt_name']!r}"
    if not rows:
        raise Refused(f"{label}: no optimization with {what}")
    if len(rows) > 1:
        raise Refused(f"{label}: {len(rows)} optimizations match {what}; use opt_id")
    row = rows[0]
    if row[2] == "running":
        if not allow_running:
            raise Refused(f"{label}: optimization {row[0]} is still running (pass --allow-running)")
    elif row[2] != "completed":
        raise Refused(f"{label}: optimization {row[0]} has status {row[2]!r}, not completed")
    return row


def _years(cfg: Any) -> Optional[float]:
    try:
        bt = cfg["backtest"]
        d = (datetime.fromisoformat(str(bt["end_date"])[:10])
             - datetime.fromisoformat(str(bt["start_date"])[:10])).days
        return d / 365.25 if d > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def resolve_contribution(con, c: Dict[str, Any], allow_running: bool, label: str) -> Dict[str, Any]:
    src = c["source"]
    if "backtest_id" in src:
        r = con.execute("select id,name,status,strategy_params,total_trades,ga_fitness,optimization_id "
                        "from backtests where id=?", (src["backtest_id"],)).fetchone()
        if r is None:
            raise Refused(f"{label}: no backtest id={src['backtest_id']}")
        params = _j(r[3]) or {}
        genes = _check_genome({k: v for k, v in params.items() if k not in BACKTEST_EXTRAS}, None, label)
        return {"genes": genes, "resolved": {
            "opt_id": r[6], "opt_name": None, "status": r[2], "pick": "backtest", "rank": None,
            "fitness": r[5], "trades": r[4], "params_key": None, "backtest_id": r[0]}}
    row = _load_opt(con, src, allow_running, label)
    space = _j(row[3])
    if not isinstance(space, dict) or not space:
        raise Refused(f"{label}: optimization {row[0]} has no parameter_ranges")
    space_keys = set(space)
    if src["pick"] == "best":
        genes = _check_genome(_j(row[4]), space_keys, label)
        return {"genes": genes, "resolved": {
            "opt_id": row[0], "opt_name": row[1], "status": row[2], "pick": "best", "rank": None,
            "fitness": row[5], "trades": None, "params_key": None, "backtest_id": None}}
    be = os.path.join(_REPO, "testplatform", "backend")
    if be not in sys.path:
        sys.path.insert(0, be)
    from app.services.distinct_topn import params_key, select_behaviour_distinct
    picks = select_behaviour_distinct(_j(row[6]), src["rank"], years=_years(_j(row[7])))
    if len(picks) < src["rank"]:
        raise Refused(f"{label}: optimization {row[0]} has only {len(picks)} behaviour-distinct genomes; "
                      f"rank {src['rank']} requested")
    pk = picks[src["rank"] - 1]
    genes = _check_genome(pk.params, space_keys, label)
    return {"genes": genes, "resolved": {
        "opt_id": row[0], "opt_name": row[1], "status": row[2], "pick": "distinct", "rank": pk.rank,
        "fitness": pk.fitness, "trades": pk.trades, "params_key": params_key(pk.params),
        "backtest_id": None}}


def build_bundle(policy: Dict[str, Any], db_path: str, allow_running: bool = False) -> Dict[str, Any]:
    validate_policy(policy)
    con = _connect_ro(db_path)
    try:
        bundle = json.loads(json.dumps(policy))
        for i, ind in enumerate(bundle["individuals"]):
            for j, c in enumerate(ind["contributions"]):
                c.update(resolve_contribution(con, c, allow_running,
                                              f"individuals[{i}].contributions[{j}] ({c['member']})"))
    finally:
        con.close()
    bundle["policy_sha256"] = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    bundle["built_at"] = datetime.now(timezone.utc).isoformat()
    bundle["db_path"] = os.path.abspath(db_path)
    return bundle


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--policy", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--allow-running", action="store_true")
    a = ap.parse_args(argv)
    try:
        try:
            with open(a.policy, encoding="utf-8") as fh:
                policy = json.load(fh)
        except (OSError, ValueError) as e:
            raise Refused(f"cannot read policy {a.policy}: {e}") from e
        bundle = build_bundle(policy, a.db, a.allow_running)
    except Refused as e:
        print(f"build_style_seed_bundle: refused: {e}", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=1, sort_keys=True)
    n = sum(len(i["contributions"]) for i in bundle["individuals"])
    print(f"wrote {a.out}: {len(bundle['individuals'])} individuals, {n} contributions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
