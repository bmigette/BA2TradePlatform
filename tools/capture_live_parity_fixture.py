"""Capture a LIVE classic-RM expert window into a hermetic JSON fixture for the
golden live<->backtest parity harness (Phase 0 of
docs/plans/2026-07-02-live-backtest-engine-unification.md).

Reads the LIVE db READ-ONLY (~/Documents/ba2_trade_platform/db.sqlite) for one expert
instance and serialises: the instance + its enter/open rulesets (+ eventactions), the
recorded ExpertRecommendations, the resulting TradingOrders, and the expert settings.
The parity harness/test consume ONLY the committed fixture (never the live DB), so they
are deterministic + committable.

Usage:  ~/ba2-venvs/test/bin/python tools/capture_live_parity_fixture.py --instance 13
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys

LIVE_DB = os.path.expanduser("~/Documents/ba2_trade_platform/db.sqlite")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "testplatform", "backend",
                       "tests", "backtest", "fixtures")


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _eventactions_for_ruleset(cur, ruleset_id):
    """Eventactions linked to a ruleset via ruleset_eventaction_link (schema-introspected)."""
    if ruleset_id is None:
        return []
    link_cols = [c[1] for c in cur.execute("PRAGMA table_info(ruleset_eventaction_link)")]
    rs_col = next((c for c in link_cols if "ruleset" in c), None)
    ea_col = next((c for c in link_cols if "event" in c or "action" in c), None)
    if not rs_col or not ea_col:
        return []
    order_col = next((c for c in link_cols if "order" in c or "index" in c or "position" in c), None)
    order_by = f" order by {order_col}" if order_col else ""
    links = _rows(cur, f"select * from ruleset_eventaction_link where {rs_col}=?{order_by}", (ruleset_id,))
    ea_ids = [r[ea_col] for r in links]  # already in ruleset evaluation order (first-match-wins)
    if not ea_ids:
        return []
    by_id = {r["id"]: r for r in _rows(cur, f"select * from eventaction where id in ({','.join('?' * len(ea_ids))})", ea_ids)}
    return [by_id[i] for i in ea_ids if i in by_id]  # preserve link order


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", type=int, required=True)
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    inst = _rows(cur, "select * from expertinstance where id=?", (args.instance,))
    if not inst:
        sys.exit(f"instance {args.instance} not found")
    inst = inst[0]

    settings = _rows(cur, "select instance_id,key,value_str,value_json,value_float "
                          "from expertsetting where instance_id=?", (args.instance,))
    recs = _rows(cur, "select * from expertrecommendation where instance_id=? order by created_at",
                 (args.instance,))
    rec_ids = [r["id"] for r in recs]
    orders = []
    if rec_ids:
        qm = ",".join("?" * len(rec_ids))
        orders = _rows(cur, f"select * from tradingorder where expert_recommendation_id in ({qm})", rec_ids)

    fixture = {
        "instance": inst,
        "settings": settings,
        "enter_ruleset": (_rows(cur, "select * from ruleset where id=?", (inst["enter_market_ruleset_id"],)) or [None])[0],
        "enter_eventactions": _eventactions_for_ruleset(cur, inst["enter_market_ruleset_id"]),
        "open_ruleset": (_rows(cur, "select * from ruleset where id=?", (inst["open_positions_ruleset_id"],)) or [None])[0],
        "open_eventactions": _eventactions_for_ruleset(cur, inst["open_positions_ruleset_id"]),
        "recommendations": recs,
        "orders": orders,
        "account": (_rows(cur, "select * from accountdefinition where id=?", (inst["account_id"],)) or [None])[0],
        "captured_from": "live db (read-only)", "window": "2026-06-01..06-14",
    }
    con.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.abspath(os.path.join(OUT_DIR, f"live_parity_inst{args.instance}.json"))
    with open(out, "w") as fh:
        json.dump(fixture, fh, indent=1, default=str)
    print(f"wrote {out}: {len(recs)} recs, {len(orders)} orders, "
          f"{len(fixture['enter_eventactions'])} enter-eventactions, "
          f"expert={inst.get('expert')}")
    # quick summary of funded orders (qty>0) for harness scoping
    funded = [o for o in orders if (o.get("quantity") or 0) > 0]
    print(f"  funded orders (qty>0): {len(funded)}; sides={sorted({o.get('side') for o in funded})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
