#!/usr/bin/env python
"""Export stored backtests as self-contained HTML files (same renderer as the web UI's "Save as HTML").

    .venv/Scripts/python.exe tools/export_backtest_html.py 1107
    .venv/Scripts/python.exe tools/export_backtest_html.py 1107 1108 --out exports/
    .venv/Scripts/python.exe tools/export_backtest_html.py 1107 --out report.html --db path/to/dl_forecasting.db

No server needed. The DB is opened READ-ONLY (sqlite ``mode=ro``): nothing is written to it. Default DB is the
test platform's own (``$DATABASE_URL`` if set, else ``<BA2_HOME>/test/dl_forecasting.db``), the same one
``ba2-test`` and ``tools/rerun_stored_row.py`` use. ``--out`` is a directory (one ``backtest-<id>.html`` per id)
or, for a single id, a ``.html`` file path. Default: the current directory.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))

from ba2test_launcher import _enter_backend  # noqa: E402

CALLER_CWD = os.getcwd()
_enter_backend()


def default_db_path() -> Path:
    url = os.environ.get("DATABASE_URL")
    if url and url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///"):])
    from ba2_common.config import TEST_DIR
    return Path(TEST_DIR) / "dl_forecasting.db"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="+", type=int, metavar="backtest_id")
    ap.add_argument("--out", help="output directory, or a .html file path when exporting a single id")
    ap.add_argument("--db", help="test-platform SQLite file (default: the platform's own DB)")
    ns = ap.parse_args(argv)

    db_path = Path(ns.db) if ns.db else default_db_path()
    if not db_path.is_absolute():
        db_path = Path(CALLER_CWD) / db_path
    db_path = db_path.resolve()
    if not db_path.is_file():
        ap.error(f"DB {db_path} does not exist")

    out = None
    if ns.out:
        out = Path(ns.out)
        if not out.is_absolute():
            out = Path(CALLER_CWD) / out
    single_file = out is not None and out.suffix.lower() == ".html"
    if single_file and len(ns.ids) != 1:
        ap.error("--out FILE.html takes exactly one backtest id; pass a directory for several")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.backtest import Backtest
    from app.models.strategy import Strategy
    from app.services.backtest.html_export import render_backtest_html

    engine = create_engine(f"sqlite:///file:{db_path.as_posix()}?mode=ro&uri=true")
    db = sessionmaker(bind=engine)()
    rc = 0
    try:
        for bid in ns.ids:
            bt = db.query(Backtest).filter(Backtest.id == bid).first()
            if bt is None:
                print(f"backtest {bid} not found in {db_path}", file=sys.stderr)
                rc = 1
                continue
            strat = db.query(Strategy).filter(Strategy.id == bt.strategy_id).first() if bt.strategy_id else None
            target = out if single_file else (out or Path(CALLER_CWD)) / f"backtest-{bid}.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_backtest_html(bt, strat), encoding="utf-8")
            print(target)
    finally:
        db.close()
        engine.dispose()
    return rc


if __name__ == "__main__":
    sys.exit(main())
