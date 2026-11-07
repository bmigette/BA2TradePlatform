"""Backfill ``hold_assigned_stock`` / ``hold_assigned_stock_tags`` onto existing LIVE instances.

Live now decides, per instance, who keeps the stock a short-option assignment delivers (the same
shared function the backtest uses: ``ba2_common.core.assignment_hold``). An instance deployed before
that has NEITHER setting stored; live treats it as UNDECIDED (stock held + an ERROR at every
assignment). This tool sets the policy it should have been deployed with.

DRY RUN by default (reads the live DB read-only and prints a table). ``--apply`` writes through the
expert class's ``save_settings``; run it only with the live app stopped or follow it with
``POST /api/reload`` (settings are cached on the live instance objects).

Where the answer comes from, in order:
  1. SOURCE BACKTEST. The instance description holds "(backtest N)" (written by
     import_deploy_payload) or the alias ends "(btN)" / "-btN"; with ``--test-db`` the row's optimization run config supplies the
     account settings the backtest ran with -> ``hold_assigned_stock_tags`` when it had them, else
     ``hold_assigned_stock``. This is the only source that reproduces the backtest exactly.
  2. STRUCTURE. A ruleset that writes cash-secured puts AND covered calls is a wheel -> hold; any
     other option action -> liquidate (what every non-wheel backtest ran). An instance with no
     option action at all is skipped.
An instance that already stores either key is never touched. A backtest/structure DISAGREEMENT
is printed (the backtest wins).

    python tools/backfill_assigned_stock_policy.py --live-db PATH [--test-db PATH]
    python tools/backfill_assigned_stock_policy.py --live-db PATH [--test-db PATH] --apply
"""
import argparse
import json
import os
import re
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "packages", "common"),
           os.path.join(_ROOT, "packages", "providers"), os.path.join(_ROOT, "packages", "experts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HOLD = "hold_assigned_stock"
TAGS = "hold_assigned_stock_tags"
_OPTION_WORDS = ("put", "call", "option", "spread", "condor", "straddle", "strangle", "lizard",
                 "ratio", "butterfly")


def _ro(path: str) -> sqlite3.Connection:
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def structure_policy(action_texts):
    """``("hold"|"liquidate"|None, why)`` from the instance's ruleset action JSON strings."""
    blob = " ".join(action_texts).lower()
    if not any(w in blob for w in _OPTION_WORDS):
        return None, "no option action in its rulesets"
    if "sell_cash_secured_put" in blob and "sell_covered_call" in blob:
        return "hold", "writes cash-secured puts AND covered calls (wheel)"
    return "liquidate", "option actions without a covered-call overlay"


def backtest_policy(test_con, backtest_id):
    """``({HOLD:..}|{TAGS:..}|None, why)`` from the source backtest's run-level account settings."""
    row = test_con.execute("select optimization_id from backtests where id=?",
                           (backtest_id,)).fetchone()
    if row is None:
        return None, f"backtest {backtest_id} not in the test DB"
    if row[0] is None:
        return None, f"backtest {backtest_id} has no optimization run (no account settings)"
    cfg = test_con.execute("select optimization_config from strategy_optimizations where id=?",
                           (row[0],)).fetchone()
    if cfg is None or cfg[0] is None:
        return None, f"optimization {row[0]} missing"
    block = (json.loads(cfg[0]) if isinstance(cfg[0], str) else cfg[0]).get("backtest")
    if not isinstance(block, dict):
        return None, f"optimization {row[0]} has no backtest block"
    acct = block.get("account_settings") or {}
    if acct.get(TAGS) is not None:
        return {TAGS: sorted(int(t) for t in acct[TAGS])}, f"backtest {backtest_id} ran {TAGS}"
    held = bool(acct.get(HOLD, False))
    return {HOLD: held}, f"backtest {backtest_id} ran {HOLD}={held}"


def plan(live_con, test_con):
    """One ``(id, alias, expert, action, decision|None, why)`` row per instance."""
    rows = []
    for inst_id, alias, expert, desc, enter_rs, open_rs in live_con.execute(
            "select id, alias, expert, user_description, enter_market_ruleset_id, "
            "open_positions_ruleset_id from expertinstance order by id").fetchall():
        stored = {k for (k,) in live_con.execute(
            "select key from expertsetting where instance_id=? and key in (?,?)",
            (inst_id, HOLD, TAGS)).fetchall()}
        if stored:
            rows.append((inst_id, alias, expert, "already set: " + ",".join(sorted(stored)),
                         None, ""))
            continue
        texts = []
        for rs in (enter_rs, open_rs):
            if rs is None:
                continue
            for (a,) in live_con.execute(
                    "select ea.actions from eventaction ea join ruleset_eventaction_link l "
                    "on l.eventaction_id=ea.id where l.ruleset_id=?", (rs,)).fetchall():
                texts.append(a if isinstance(a, str) else json.dumps(a))
        struct, struct_why = structure_policy(texts)
        decision, source, why = None, "", ""
        m = re.search(r"\(backtest (\d+)\)|\(bt(\d+)\)|-bt(\d+)", f"{desc or ''} {alias or ''}")
        if m and test_con is not None:
            decision, why = backtest_policy(test_con, int(next(g for g in m.groups() if g)))
            source = "backtest" if decision else ""
        if decision is None:
            if struct is None:
                rows.append((inst_id, alias, expert, "SKIP", None, struct_why))
                continue
            decision = {HOLD: struct == "hold"}
            source, why = "structure", struct_why
        elif (struct is not None and TAGS not in decision
              and decision[HOLD] != (struct == "hold")):
            why += f"  [NOTE disagrees with structure: {struct_why}]"
        rows.append((inst_id, alias, expert, f"SET ({source})", decision, why))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live-db", required=True, help="the trade DB to read (and write with --apply)")
    ap.add_argument("--test-db", help="the test platform DB (source backtest facts)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ns = ap.parse_args()

    live_con = _ro(ns.live_db)
    test_con = _ro(ns.test_db) if ns.test_db else None
    rows = plan(live_con, test_con)
    print(f"live DB = {ns.live_db}\nmode    = {'APPLY' if ns.apply else 'DRY RUN'}\n")
    print(f"{'inst':>5}  {'expert':<24} {'action':<26} policy / why")
    for inst_id, alias, expert, action, decision, why in rows:
        print(f"{inst_id:>5}  {str(expert):<24} {action:<26} "
              f"{json.dumps(decision) if decision else ''}  {why}  [{alias}]")
    todo = [r for r in rows if r[4]]
    print(f"\n{len(todo)} instance(s) to set.")
    live_con.close()
    if not ns.apply or not todo:
        return 0

    from ba2_common.core import db as _db
    _db.configure_db(ns.live_db)
    from ba2_trade_platform.modules.experts import experts as live_experts
    for inst_id, alias, expert, action, decision, why in todo:
        cls = next((c for c in live_experts if c.__name__ == expert), None)
        if cls is None:
            print(f"  !! inst {inst_id}: expert {expert!r} not in the live registry; NOT written")
            continue
        cls(inst_id).save_settings({k: (v, None) for k, v in decision.items()})
        print(f"  wrote inst {inst_id}: {decision}")
    print("POST /api/reload so the instance/settings caches are dropped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
