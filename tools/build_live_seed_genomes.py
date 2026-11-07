#!/usr/bin/env python
"""Build a ``--seed-genomes`` file from the settings an expert runs LIVE.

    python tools/build_live_seed_genomes.py --live-db ~/Documents/ba2_trade_platform-prod/db.sqlite \\
        --instance 7 --source-row 1107 --source-db <test db holding the row> --out seeds_i7.json

The genome = the deployed backtest row's genes (its ``strategy_params``: rules, screener, schedule and model
genes, i.e. what was exported to the instance), with every ``model:<key>`` replaced by the instance's LIVE
setting ``<key>`` where the expert declares that setting -- so a value changed by hand after the deploy (a cap, a
mode) is what the GA starts from. Live settings the expert declares but the row never searched are added as
``model:<key>`` too; the launcher keeps those that are genes of the new job and reports the rest as dropped.

Read-only on both databases (sqlite ``mode=ro``). Prints every model gene whose live value differs from the row.
"""
import argparse
import json
import os
import sqlite3
import sys

GENE_NAMESPACES = ("model:", "screener:", "schedule:", "cond:", "entry:", "exit:", "market:", "optsel:", "rm:")


def _ro(path):
    return sqlite3.connect(f"file:{os.path.expanduser(path)}?mode=ro", uri=True)


def live_settings(live_db, instance_id):
    """{key: typed value} of an ExpertInstance's settings (value_json when set, else value_float, else value_str)."""
    con = _ro(live_db)
    row = con.execute("select expert from expertinstance where id=?", (instance_id,)).fetchone()
    if row is None:
        sys.exit(f"instance {instance_id} not found in {live_db}")
    out = {}
    for key, s, f, j in con.execute(
            "select key, value_str, value_float, value_json from expertsetting where instance_id=?", (instance_id,)):
        if j not in (None, "", "{}", "null"):
            try:
                out[key] = json.loads(j)
                continue
            except ValueError:
                pass
        out[key] = f if f is not None else s
    return row[0], out


def coerce(value, definition):
    """The live value read the way the expert declares the setting (bool/int/float/str)."""
    t = (definition or {}).get("type")
    if value is None:
        return None
    if t == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on") if not isinstance(value, bool) else value
    if t == "int":
        return int(float(value))
    if t == "float":
        return float(value)
    return value


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live-db", required=True)
    ap.add_argument("--instance", type=int, required=True)
    ap.add_argument("--source-row", type=int, required=True)
    ap.add_argument("--source-db", required=True, help="test-platform DB holding the deployed row")
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="GENE=VALUE",
                    help="override one gene of the seed (JSON value, else string), e.g. schedule:time=10:00 when the "
                         "live time is not one of the job's choices. Repeatable; every override is printed.")
    args = ap.parse_args()

    expert, live = live_settings(args.live_db, args.instance)
    row = _ro(args.source_db).execute("select name, expert_name, strategy_params from backtests where id=?",
                                      (args.source_row,)).fetchone()
    if row is None:
        sys.exit(f"row {args.source_row} not found in {args.source_db}")
    name, row_expert, sp = row
    if row_expert != expert:
        sys.exit(f"row {args.source_row} is a {row_expert} row, instance {args.instance} runs {expert}")
    params = json.loads(sp) if isinstance(sp, str) else sp
    genes = {k: v for k, v in params.items() if k.startswith(GENE_NAMESPACES)}

    from ba2_experts import get_expert_class
    defs = get_expert_class(expert).get_settings_definitions()
    changed, added = [], []
    for key, value in live.items():
        if key not in defs:
            continue
        v = coerce(value, defs[key])
        gene = f"model:{key}"
        if gene in genes:
            if genes[gene] != v and not (isinstance(v, bool) and genes[gene] == int(v)):
                changed.append((gene, genes[gene], v))
                genes[gene] = v
        else:
            genes[gene] = v
            added.append(gene)
    # The ENTRY days the instance runs live (execution_schedule_enter_market) -> the schedule:<day> genes.
    sched = live.get("execution_schedule_enter_market")
    if isinstance(sched, dict) and isinstance(sched.get("days"), dict):
        for day, on in sched["days"].items():
            gene = f"schedule:{day}"
            v = 1 if on else 0
            if gene in genes and genes[gene] != v:
                changed.append((gene, genes[gene], v))
            genes[gene] = v
        print(f"live entry schedule: days {[d for d, on in sched['days'].items() if on]} times {sched.get('times')}")
    for item in args.set:
        gene, _, raw = item.partition("=")
        try:
            val = json.loads(raw)
        except ValueError:
            val = raw
        print(f"override: {gene}: {genes.get(gene)!r} -> {val!r}")
        genes[gene] = val
    for gene, old, new in changed:
        print(f"live differs from row: {gene}: row {old!r} -> live {new!r}")
    print(f"{expert} instance {args.instance} <- row {args.source_row} ({name}): {len(genes)} genes, "
          f"{len(changed)} model genes changed to the live value, {len(added)} live settings added as model genes")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"individuals": [{"label": f"live-inst{args.instance}-row{args.source_row}", "genes": genes}],
                   "source": {"live_db": args.live_db, "instance": args.instance, "row": args.source_row,
                              "row_name": name}}, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
