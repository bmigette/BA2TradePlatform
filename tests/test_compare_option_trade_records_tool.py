"""Smoke test of tools/compare_option_trade_records.py against two tiny sqlite DBs.

The DBs carry only the columns the tool reads, with enums stored by NAME as SQLModel writes
them (``OPTION`` / ``BUY`` / ``FILLED`` / ``CALL``). One single-leg long call on each side, the
same decision (data_session 2024-05-03, a Friday): live fills on Monday 2024-05-06, the
backtest stamps Friday. Plus one backtest-only structure, which must be reported unmatched.
"""
from __future__ import annotations

import csv
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from ba2_common.core.option_trade_record import LEG_SNAPSHOT_FIELDS, STRUCTURE_SNAPSHOT_FIELDS

tool = importlib.import_module("tools.compare_option_trade_records")

REPO = Path(__file__).resolve().parent.parent
C = "MSFT240621C00420000"


def _snap(**over):
    leg = {k: None for k in LEG_SNAPSHOT_FIELDS}
    leg.update(contract_symbol=C, side="buy", ratio_qty=1, position_intent="buy_to_open",
               right="call", strike=420.0, expiry="2024-06-21", dte=45,
               data_session="2024-05-03", spot=406.66, moneyness_pct=3.28, bid=8.1, ask=8.4,
               mid=8.25, spread_pct=3.6, last=8.2, iv=0.22, delta=0.38, gamma=0.01,
               theta=-0.12, vega=0.6, rho=0.2, open_interest=5000, volume=900,
               greeks_source="broker", quote_time="2024-05-06T13:35:00+00:00")
    leg.update(over)
    return leg


def _structure():
    s = {k: None for k in STRUCTURE_SNAPSHOT_FIELDS}
    s.update(strategy="long_call", quantity=1, multiplier=100, net_price=8.3, leg_count=1,
             max_loss=830.0, max_loss_state="MEASURED", max_profit=None,
             max_profit_state="UNBOUNDED", breakevens=[428.3])
    return s


def _build_live_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE accountdefinition (id INTEGER PRIMARY KEY, name TEXT, provider TEXT)")
    con.execute("INSERT INTO accountdefinition VALUES (1, 'AlpacaOptions', 'Alpaca')")
    con.execute('CREATE TABLE "transaction" (id INTEGER PRIMARY KEY, symbol TEXT, '
                'close_reason TEXT, status TEXT, option_strategy TEXT, open_date TEXT, '
                'close_date TEXT)')
    con.execute('INSERT INTO "transaction" VALUES (5, \'MSFT\', \'take_profit\', \'CLOSED\', '
                '\'long_call\', \'2024-05-06 13:36:00\', \'2024-05-14 14:00:00\')')
    cols = tool.LIVE_ORDER_COLUMNS
    con.execute(f"CREATE TABLE tradingorder ({', '.join(cols)})")
    entry = {"entry_record": {"version": "option_trade_record_v1", "legs": [_snap()],
                              "legs_without_quote": [], "structure": _structure()}}
    exit_ = {"exit_record": {"version": "option_trade_record_v1", "trigger": "take_profit",
                             "rule_id": 9, "rule_name": "tp",
                             "legs": [_snap(side="sell", position_intent="sell_to_close",
                                            data_session="2024-05-13", bid=12.0, ask=12.3,
                                            mid=12.15)],
                             "legs_without_quote": []}}
    base = dict(account_id=1, symbol=C, quantity=1, filled_qty=1, transaction_id=5,
                parent_order_id=None, asset_class="OPTION", contract_symbol=C,
                option_type="CALL", strike=420.0, expiry="2024-06-21", underlying_symbol="MSFT",
                multiplier=100)
    rows = [
        dict(base, id=1, side="BUY", status="FILLED", open_price=8.3,
             created_at="2024-05-06 13:35:00", data=json.dumps(entry),
             position_intent="buy_to_open", option_strategy="long_call"),
        dict(base, id=2, side="SELL", status="FILLED", open_price=12.1,
             created_at="2024-05-14 14:00:00", data=json.dumps(exit_),
             position_intent="sell_to_close", option_strategy="close"),
        # an equity order on the same account must be ignored
        dict(base, id=3, side="BUY", status="FILLED", open_price=400.0, asset_class="EQUITY",
             contract_symbol=None, created_at="2024-05-06 13:35:00", data=None,
             position_intent=None, option_strategy=None),
    ]
    con.executemany(f"INSERT INTO tradingorder ({', '.join(cols)}) VALUES "
                    f"({', '.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    con.close()


def _build_test_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE backtests (id INTEGER PRIMARY KEY, name TEXT, start_date TEXT, "
                "end_date TEXT, trades JSON)")
    row = {"symbol": "MSFT", "entry_time": "2024-05-03T00:00:00", "exit_time":
           "2024-05-13T00:00:00", "direction": "buy", "entry_price": 8.3, "exit_price": 12.1,
           "size": 1.0, "pnl": 379.0, "pnl_pct": 0.4, "bars_held": 6,
           "exit_reason": "take_profit", "contract_symbol": C, "underlying_symbol": "MSFT",
           "option_type": "call", "strike": 420.0, "expiry": "2024-06-21", "transaction_id": 1,
           "multiplier": 100.0, "option_strategy": "long_call",
           "recommendation_confidence": 80.0,
           "entry_record": {"version": "option_trade_record_v1", "structure": _structure(),
                            "legs_without_quote": [],
                            "leg": _snap(greeks_source="bs_from_close", delta=0.36,
                                         quote_time=None)},
           "exit_record": {"trigger": "take_profit", "rule_id": 2, "rule_name": "r-1",
                           "leg": _snap(side="sell", position_intent="sell_to_close",
                                        data_session="2024-05-13", bid=12.0, ask=12.3,
                                        mid=12.15, greeks_source="bs_from_close",
                                        quote_time=None)}}
    extra = dict(row, symbol="NVDA", underlying_symbol="NVDA",
                 contract_symbol="NVDA240621C00900000", transaction_id=2)
    equity = {"symbol": "SPY", "entry_time": "2024-05-03T00:00:00", "contract_symbol": None,
              "transaction_id": 3}
    con.execute("INSERT INTO backtests VALUES (42, 'TOP1-test', '2024-01-01', '2024-12-31', ?)",
                (json.dumps([row, extra, equity]),))
    con.commit()
    con.close()


def test_cli_smoke(tmp_path, capsys):
    live_db, test_db = tmp_path / "live.sqlite", tmp_path / "test.db"
    _build_live_db(live_db)
    _build_test_db(test_db)
    out_json, out_csv = tmp_path / "r.json", tmp_path / "r.csv"
    argv = ["--live-db", str(live_db), "--account-id", "1", "--test-db", str(test_db),
            "--backtest-id", "42", "--start", "2024-05-01", "--end", "2024-05-31"]
    assert tool.main(argv + ["--out", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert "paired 1" in text
    assert "unmatched backtest (1)" in text and "NVDA" in text
    assert "sources differ" in text           # delta: broker vs bs_from_close
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["summary"]["paired"] == 1
    assert report["pairs"][0]["data_session"] == "2024-05-03"
    assert report["pairs"][0]["live"]["key_source"] == "record"
    delta = [d for d in report["diffs"] if d["field"] == "delta" and d["phase"] == "entry"][0]
    assert delta["source_mismatch"] is True
    assert tool.main(argv + ["--out", str(out_csv)]) == 0
    with out_csv.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    kinds = {r["kind"] for r in rows}
    assert {"diff", "unmatched_backtest"} <= kinds


def test_cli_opens_databases_read_only(tmp_path):
    live_db, test_db = tmp_path / "live.sqlite", tmp_path / "test.db"
    _build_live_db(live_db)
    _build_test_db(test_db)
    before = (live_db.read_bytes(), test_db.read_bytes())
    con = tool.open_ro(str(live_db))
    try:
        try:
            con.execute("DELETE FROM tradingorder")
            raised = False
        except sqlite3.OperationalError:
            raised = True
    finally:
        con.close()
    assert raised
    tool.main(["--live-db", str(live_db), "--account-id", "1", "--test-db", str(test_db),
               "--backtest-id", "42"])
    assert (live_db.read_bytes(), test_db.read_bytes()) == before


def test_cli_refuses_schema_drift_and_unknown_ids(tmp_path, capsys):
    live_db, test_db = tmp_path / "live.sqlite", tmp_path / "test.db"
    _build_live_db(live_db)
    _build_test_db(test_db)
    base = ["--live-db", str(live_db), "--test-db", str(test_db)]
    assert tool.main(base + ["--account-id", "99", "--backtest-id", "42"]) == 2
    assert tool.main(base + ["--account-id", "1", "--backtest-id", "7"]) == 2
    bad = tmp_path / "bad.sqlite"
    sqlite3.connect(bad).execute("CREATE TABLE tradingorder (id INTEGER)").connection.commit()
    assert tool.main(["--live-db", str(bad), "--test-db", str(test_db), "--account-id", "1",
                      "--backtest-id", "42"]) == 2
    assert "lacks column" in capsys.readouterr().err


def test_cli_runs_as_a_script(tmp_path):
    live_db, test_db = tmp_path / "live.sqlite", tmp_path / "test.db"
    _build_live_db(live_db)
    _build_test_db(test_db)
    env = dict(os.environ, PYTHONPATH=str(REPO / "packages" / "common"))
    proc = subprocess.run(
        [sys.executable, str(REPO / "tools" / "compare_option_trade_records.py"),
         "--live-db", str(live_db), "--account-id", "1", "--test-db", str(test_db),
         "--backtest-id", "42", "--symbols", "MSFT"],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "paired 1" in proc.stdout
