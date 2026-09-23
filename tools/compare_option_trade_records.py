"""Compare a live account's option trades with a backtest's, structure by structure.

BT/live option parity, plan Part C5 (docs/plans/2026-09-22-bt-live-option-parity.md). The live
path stores ``TradingOrder.data["entry_record"]`` / ``["exit_record"]`` (schema
``option_trade_record_v1``); a persisted backtest carries the same records on its option rows
in ``Backtest.trades``. This tool reads both, READ-ONLY, pairs each live structure with its
backtest twin and prints where they differ.

PAIRING: ``(underlying, option_strategy, data_session)``. ``data_session`` is the session whose
data the decision read, written into every entry leg snapshot by the same code on both paths:
live decides during N(D) and reads D, the backtest decides on bar D and reads D. When a record
is missing or an error, the session is derived from the fill (live: prior session of the order's
``created_at``; backtest: the entry bar) and the report says so. See
``ba2_common.core.option_trade_compare`` for the full rules.

BOTH DATABASES ARE OPENED READ-ONLY (``file:...?mode=ro`` plus ``PRAGMA query_only``). The tool
never writes to either.

Usage
-----
    python tools/compare_option_trade_records.py \\
        --live-db "%USERPROFILE%/Documents/ba2_trade_platform-opt/db.sqlite" --account-id 1 \\
        --test-db "%USERPROFILE%/Documents/ba2/test/dl_forecasting.db" --backtest-id 1712 \\
        --start 2026-10-01 --end 2026-12-31 [--symbols AAPL,MSFT] \\
        [--abs-tol 1e-6] [--rel-tol 0] [--tol spot=0.05:0.001 --tol iv=0.02:0] \\
        [--out diff.csv | --out report.json]

``--out *.csv`` writes one row per diff plus one per unmatched structure (``kind`` column);
``--out *.json`` writes the whole report (summary, pairs, diffs, unmatched, issues).
Exit code: 0 when the comparison ran (whatever it found), 2 on a usage / schema error.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO = Path(__file__).resolve().parent.parent
# This checkout's ba2_common (the venv's editable install may point at another worktree).
_COMMON = str(_REPO / "packages" / "common")
if _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)

from ba2_common.core.option_trade_compare import (  # noqa: E402
    Tolerances, backtest_structures, compare, live_structures, summarize,
)

#: The columns the tool reads. Checked up front so a schema drift fails loudly, not as a
#: silently empty comparison.
LIVE_ORDER_COLUMNS = (
    "id", "account_id", "symbol", "quantity", "side", "status", "filled_qty", "open_price",
    "created_at", "transaction_id", "data", "parent_order_id", "asset_class",
    "contract_symbol", "option_type", "strike", "expiry", "underlying_symbol", "multiplier",
    "position_intent", "option_strategy",
)
LIVE_TXN_COLUMNS = ("id", "symbol", "close_reason", "status", "option_strategy",
                    "open_date", "close_date")
BACKTEST_COLUMNS = ("id", "name", "start_date", "end_date", "trades")


class SchemaError(RuntimeError):
    pass


def open_ro(path: str) -> sqlite3.Connection:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SchemaError(f"database not found: {p}")
    conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _require_columns(conn: sqlite3.Connection, table: str, needed: Sequence[str]) -> None:
    have = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if not have:
        raise SchemaError(f"table {table!r} does not exist")
    missing = [c for c in needed if c not in have]
    if missing:
        raise SchemaError(f"table {table!r} lacks column(s) {missing}")


def _json(value: Any, what: str, issues: List[str]) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as e:
        issues.append(f"{what}: unreadable JSON ({e}); treated as absent")
        return None


def load_live(conn: sqlite3.Connection, account_id: int
              ) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]], List[str]]:
    """(option order rows, their transactions by id, load issues) for one account."""
    _require_columns(conn, "tradingorder", LIVE_ORDER_COLUMNS)
    _require_columns(conn, "transaction", LIVE_TXN_COLUMNS)
    _require_columns(conn, "accountdefinition", ("id",))
    if conn.execute("SELECT 1 FROM accountdefinition WHERE id = ?", (account_id,)).fetchone() is None:
        raise SchemaError(f"live account {account_id} does not exist")
    issues: List[str] = []
    cols = ", ".join(LIVE_ORDER_COLUMNS)
    cur = conn.execute(
        f"SELECT {cols} FROM tradingorder WHERE account_id = ? "
        f"AND lower(asset_class) = 'option' ORDER BY id", (account_id,))
    orders = []
    for row in cur.fetchall():
        o = dict(zip(LIVE_ORDER_COLUMNS, row))
        o["data"] = _json(o["data"], f"live order {o['id']} data", issues)
        orders.append(o)
    txn_ids = sorted({o["transaction_id"] for o in orders if o["transaction_id"] is not None})
    txns: Dict[int, Dict[str, Any]] = {}
    tcols = ", ".join(LIVE_TXN_COLUMNS)
    for i in range(0, len(txn_ids), 500):
        chunk = txn_ids[i:i + 500]
        q = f'SELECT {tcols} FROM "transaction" WHERE id IN ({",".join("?" * len(chunk))})'
        for row in conn.execute(q, chunk):
            t = dict(zip(LIVE_TXN_COLUMNS, row))
            txns[t["id"]] = t
    return orders, txns, issues


def load_backtest(conn: sqlite3.Connection, backtest_id: int
                  ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    """(backtest meta, trades rows, load issues)."""
    _require_columns(conn, "backtests", BACKTEST_COLUMNS)
    row = conn.execute(f"SELECT {', '.join(BACKTEST_COLUMNS)} FROM backtests WHERE id = ?",
                       (backtest_id,)).fetchone()
    if row is None:
        raise SchemaError(f"backtest {backtest_id} does not exist")
    meta = dict(zip(BACKTEST_COLUMNS, row))
    issues: List[str] = []
    trades = _json(meta.pop("trades"), f"backtest {backtest_id} trades", issues)
    if trades is None:
        issues.append(f"backtest {backtest_id} has no trades blob")
        trades = []
    if not isinstance(trades, list):
        raise SchemaError(f"backtest {backtest_id} trades is not a list")
    return meta, trades, issues


def parse_tol(specs: Sequence[str]) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    for spec in specs or ():
        try:
            name, val = spec.split("=", 1)
            a, _, r = val.partition(":")
            out[name.strip()] = (float(a), float(r) if r else 0.0)
        except ValueError:
            raise SchemaError(f"--tol {spec!r}: expected FIELD=ABS[:REL]")
    return out


_CSV_COLUMNS = ("kind", "pair_id", "underlying", "strategy", "data_session", "phase", "leg",
                "field", "live", "backtest", "abs_delta", "rel_delta", "within_tolerance",
                "live_source", "backtest_source", "source_mismatch", "note", "reason", "ref")


def write_out(report: Dict[str, Any], path: str) -> None:
    p = Path(path)
    if p.suffix.lower() == ".json":
        p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return
    if p.suffix.lower() != ".csv":
        raise SchemaError(f"--out {path!r}: use a .csv or .json file")
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for d in report["diffs"]:
            w.writerow({"kind": "diff", **{k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                           for k, v in d.items()}})
        for kind in ("unmatched_live", "unmatched_backtest"):
            for u in report[kind]:
                w.writerow({"kind": kind, "underlying": u["underlying"],
                            "strategy": u["strategy"], "data_session": u["data_session"],
                            "reason": u["reason"], "ref": json.dumps(u["ref"])})
        for issue in report["issues"]:
            w.writerow({"kind": "issue", "note": issue})


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--live-db", required=True, help="live platform db.sqlite (opened read-only)")
    ap.add_argument("--account-id", required=True, type=int, help="live AccountDefinition id")
    ap.add_argument("--test-db", required=True, help="testplatform DB (opened read-only)")
    ap.add_argument("--backtest-id", required=True, type=int, help="testplatform Backtest id")
    ap.add_argument("--start", help="first data_session to compare (YYYY-MM-DD, inclusive)")
    ap.add_argument("--end", help="last data_session to compare (YYYY-MM-DD, inclusive)")
    ap.add_argument("--symbols", help="comma-separated underlyings (default: all)")
    ap.add_argument("--abs-tol", type=float, default=1e-6)
    ap.add_argument("--rel-tol", type=float, default=0.0)
    ap.add_argument("--tol", action="append", default=[],
                    help="per-field tolerance FIELD=ABS[:REL], repeatable")
    ap.add_argument("--out", help="write the full diff to a .csv or .json file")
    ap.add_argument("--limit", type=int, default=20, help="rows per section in the summary")
    args = ap.parse_args(argv)
    try:
        tol = Tolerances(abs_tol=args.abs_tol, rel_tol=args.rel_tol, fields=parse_tol(args.tol))
        live_conn = open_ro(args.live_db)
        try:
            orders, txns, live_issues = load_live(live_conn, args.account_id)
        finally:
            live_conn.close()
        test_conn = open_ro(args.test_db)
        try:
            meta, trades, bt_issues = load_backtest(test_conn, args.backtest_id)
        finally:
            test_conn.close()
    except SchemaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    issues: List[str] = live_issues + bt_issues
    live = live_structures(orders, txns, issues)
    bt = backtest_structures(trades, issues)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    report = compare(live, bt, tol, start=args.start, end=args.end, symbols=symbols,
                     extra_issues=issues)
    report["sources"] = {
        "live_db": os.path.abspath(os.path.expanduser(args.live_db)),
        "account_id": args.account_id, "live_option_orders": len(orders),
        "test_db": os.path.abspath(os.path.expanduser(args.test_db)),
        "backtest": {k: (str(v) if v is not None else None) for k, v in meta.items()},
        "backtest_trade_rows": len(trades),
    }
    print(f"live: {report['sources']['live_db']} account {args.account_id} "
          f"({len(orders)} option orders)")
    print(f"backtest: {args.backtest_id} {meta.get('name')!r} "
          f"{meta.get('start_date')} .. {meta.get('end_date')} ({len(trades)} trade rows)")
    print(summarize(report, limit=args.limit))
    if args.out:
        try:
            write_out(report, args.out)
        except SchemaError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
