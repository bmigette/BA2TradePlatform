"""Rename successors (Alpaca keys history by the CURRENT ticker): the split-table step's per-store lists and download runs 4 / 5 of
tools/prepare_history.py."""
from __future__ import annotations

import gzip
import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd

from ba2_providers.ohlcv.alpaca_history import split_build as sb, split_table as st

REPO = Path(__file__).resolve().parents[3]


def d(s):
    return date.fromisoformat(s)


RENAMES = {"OLD": [(d("2026-09-24"), "NEW")], "CHAIN": [(d("2026-05-01"), "MID")], "MID": [(d("2026-06-01"), "END")],
           "REUSED": [(d("2020-01-10"), "ELSE")], "LONG": [(d("2026-09-24"), "LATE")], "OTCX": [(d("2026-08-07"), "OTCXY")]}


def test_successor_rows_follow_the_chain_to_its_end_and_respect_the_window():
    last = {"OLD": d("2026-09-23"), "CHAIN": d("2026-04-30"), "REUSED": d("2026-10-01"), "LONG": d("2026-08-01"),
            "OTCX": d("2026-08-06"), "PLAIN": d("2026-10-01")}
    rows = st.rename_successor_rows(last, RENAMES, lambda n: n in {"NEW", "END"}, lambda n: n == "END")
    assert [(r["old"], r["new"], r["process_date"], r["old_last_bar"], r["new_in_store"], r["new_in_5min"]) for r in rows] == [
        ("CHAIN", "END", "2026-05-01", "2026-04-30", "yes", "yes"),    # the FINAL ticker of the chain, the first rename's date
        ("OLD", "NEW", "2026-09-24", "2026-09-23", "yes", "no"),       # in the daily store, NOT in the 5-minute one
        ("OTCX", "OTCXY", "2026-08-07", "2026-08-06", "no", "no")]     # an OTC destination is listed too (a delisting is decided later)
    # REUSED: its series goes on long after the rename (a reused ticker); LONG: ended 54 days before it; PLAIN: never renamed
    assert st.rename_successor_rows({"OLD": d("2026-09-09")}, RENAMES, lambda n: True)[0]["new_in_5min"] == "unknown"
    assert st.rename_successor_rows({"OLD": d("2026-09-09")}, RENAMES, lambda n: True)[0]["old"] == "OLD"     # exactly 15 days: in
    assert st.rename_successor_rows({"OLD": d("2026-09-08")}, RENAMES, lambda n: True) == []                    # 16 days: out


def _frame(upto):
    sessions = [x for x in pd.bdate_range("2021-01-04", "2021-03-31") if x.date() <= upto]
    return pd.DataFrame({"t": [pd.Timestamp(x).tz_localize("America/New_York").tz_convert("UTC") for x in sessions],
                         "o": 10.0, "c": 10.0, "h": 10.0, "l": 10.0, "v": 5.0, "n": 3})


def _write_stores(tmp_path):
    store, intr = tmp_path / "store", tmp_path / "intr"
    for sym, upto in (("OLD", d("2021-03-10")), ("KEEP", d("2021-03-31")), ("NEW", d("2021-03-31")), ("OLD2", d("2021-03-10")),
                      ("NEW2", d("2021-03-31"))):
        if sym == "NEW2":
            continue
        p = store / "bars" / "1Day" / f"symbol={sym}"
        p.mkdir(parents=True)
        _frame(upto).to_parquet(p / "all.parquet")
    (store / "STORE_INFO.json").write_text(json.dumps({"asof": "2021-03-31"}), encoding="utf-8")
    (store / "corporate_actions").mkdir()
    with gzip.open(store / "corporate_actions" / "ym=2021-03.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"forward_splits": [{"symbol": "KEEP", "ex_date": "2021-03-31", "old_rate": 1, "new_rate": 2}],
                   "name_changes": [{"old_symbol": "OLD", "new_symbol": "NEW", "process_date": "2021-03-12"},
                                    {"old_symbol": "OLD2", "new_symbol": "NEW2", "process_date": "2021-03-12"}]}, f)
    (intr / "bars" / "5Min" / "symbol=KEEP").mkdir(parents=True)                # NEW and NEW2 have no 5-minute bars; NEW has daily ones
    return store, intr


def test_split_table_writes_the_per_store_lists_a_successor_can_be_daily_only(tmp_path):
    store, intr = _write_stores(tmp_path)
    fmpd = tmp_path / "fmpd"
    fmpd.mkdir()
    out = tmp_path / "out"
    cal = out / "_meta" / "fmp_split_calendar"
    cal.mkdir(parents=True)
    for w in ("2021-01-01_2021-01-30", "2021-01-31_2021-03-01", "2021-03-02_2021-03-31", "2021-04-01_2021-04-01"):
        (cal / f"{w}.json").write_text("[]", encoding="utf-8")
    kw = dict(store=str(store), out=str(out), db="unused", fmp_daily_dir=str(fmpd), fmp_from="2021-01-01", fmp_to="2021-04-01",
              log=lambda m: None, yahoo_fetch=lambda t: [], yahoo_sleep=lambda s: None)
    res = sb.build_split_table(intraday_store=str(intr), **kw)
    meta = out / "_meta"
    table = json.loads((meta / "split_table.json").read_text(encoding="utf-8"))
    rid = table["run_id"]
    assert (meta / "rename_successors.csv").read_text(encoding="utf-8").splitlines() == [
        "old,new,process_date,old_last_bar,new_in_store,new_in_5min,run_id",
        f"OLD,NEW,2021-03-12,2021-03-10,yes,no,{rid}", f"OLD2,NEW2,2021-03-12,2021-03-10,no,no,{rid}"]

    def listed(name):
        lines = (meta / name).read_text(encoding="utf-8").splitlines()
        assert lines[0] == f"# run_id: {rid}"
        return [x for x in lines if not x.startswith("#")]
    assert listed("rename_successors_missing_daily.txt") == ["NEW2"]
    assert listed("rename_successors_missing_5min.txt") == ["NEW", "NEW2"]       # NEW: in the daily store, absent from the 5-minute one
    assert res["rename_successors"] == 2 and res["rename_successors_missing_daily"] == 1 and res["rename_successors_missing_5min"] == 2
    # without a 5-minute store the 5-minute columns are "unknown" and that list is not (re)written
    out2 = tmp_path / "out2"
    (out2 / "_meta").mkdir(parents=True)
    import shutil
    shutil.copytree(cal, out2 / "_meta" / "fmp_split_calendar")
    sb.build_split_table(**{**kw, "out": str(out2)})
    assert "unknown" in (out2 / "_meta" / "rename_successors.csv").read_text(encoding="utf-8")
    assert not (out2 / "_meta" / "rename_successors_missing_5min.txt").exists()


# ---- the download runs 4 and 5 ---------------------------------------------------------------------------------------------
def load_ph():
    spec = importlib.util.spec_from_file_location("prepare_history_renames_test", REPO / "tools" / "prepare_history.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dl_args(ph, tmp_path, daily_text, m5_text):
    daily, intr, out = tmp_path / "daily", tmp_path / "intra", tmp_path / "out"
    for p in (daily, intr, out / "_meta"):
        p.mkdir(parents=True, exist_ok=True)
    (daily / "symbols.txt").write_text("AAA\n")
    (intr / "symbols.txt").write_text("AAA\n")
    (daily / ph.SPINCO_SYMBOLS_FILE).write_text("SP\n")
    for key, text in (("renames_daily", daily_text), ("renames_5min", m5_text)):
        if text is not None:
            (out / "_meta" / ph.RENAME_MISSING_FILES[key]).write_text(text)
    return ph.build_parser().parse_args(["--source", "alpaca", "--store", str(daily), "--store-5min", str(intr), "--out", str(out),
                                         "--db", str(tmp_path / "k.db")])


def _run_download(ph, a, monkeypatch):
    calls = []

    class FakeDl:
        @staticmethod
        def main(argv):
            calls.append((argv, Path(argv[argv.index("--symbols-file") + 1]).read_text().split()))
            return 0
    monkeypatch.setattr(ph, "_load_downloader", lambda: FakeDl)
    logs = []
    res = ph.step_alpaca_download(a, logs.append)
    return calls, res, logs


def test_download_runs_4_and_5_fetch_each_store_its_own_missing_successors(tmp_path, monkeypatch):
    ph = load_ph()
    a = _dl_args(ph, tmp_path, "# run_id: x\nNEW2\n", "# run_id: x\n# comment\nNEW\nNEW2\nNEW\n")
    calls, res, _ = _run_download(ph, a, monkeypatch)
    assert len(calls) == 5 and len(res["runs"]) == 5
    daily, intr = str(tmp_path / "daily"), str(tmp_path / "intra")
    common = ["--max-gap-days", "5", "--keys-db", str(tmp_path / "k.db"), "--account-id", "1", "--rpm", "150", "--min-remaining", "60"]
    argv4, argv5 = calls[3][0], calls[4][0]
    assert argv4[:2] == ["--store", intr] and argv4[argv4.index("--phases") + 1] == "daily,intraday" and "--asof" not in argv4
    assert argv4[argv4.index("--start") + 1] == "2019-01-01" and argv4[argv4.index("--end") + 1] == "2026-10-13" and argv4[-len(common):] == common
    assert argv5[:2] == ["--store", daily] and argv5[argv5.index("--phases") + 1] == "daily" and argv5[argv5.index("--asof") + 1] == "2026-10-12"
    assert argv5[argv5.index("--start") + 1] == "2016-01-01" and argv5[-len(common):] == common
    assert calls[3][1] == ["NEW", "NEW2"] and calls[4][1] == ["NEW2"]               # per store; comments dropped, de-duplicated
    # one store's list empty or absent: only that run is skipped, loudly
    sub = tmp_path / "one"
    sub.mkdir()
    calls, res, logs = _run_download(ph, _dl_args(ph, sub, "# only comments\n", "NEW\n"), monkeypatch)
    assert len(calls) == 4 and calls[3][0][1] == str(sub / "intra") and len(res["skipped"]) == 1 and len(res["runs"]) == 4
    assert sum("SKIPPED" in m and "_daily.txt" in m for m in logs) == 1
    sub = tmp_path / "none"
    sub.mkdir()
    calls, res, logs = _run_download(ph, _dl_args(ph, sub, None, None), monkeypatch)
    assert len(calls) == 3 and sum("SKIPPED" in m for m in logs) == 2


def test_download_plan_lists_runs_4_and_5_only_with_a_non_empty_list(tmp_path):
    ph = load_ph()
    lines = ph.plan_alpaca_download(_dl_args(ph, tmp_path, "NEW\n", "NEW\n"))
    assert len(lines) == 5 and not any("SKIPPED" in x for x in lines)
    sub = tmp_path / "none"
    sub.mkdir()
    lines = ph.plan_alpaca_download(_dl_args(ph, sub, None, "NEW\n"))
    assert len(lines) == 5 and sum("would be SKIPPED" in x for x in lines) == 1
