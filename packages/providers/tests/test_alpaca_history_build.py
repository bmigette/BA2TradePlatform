"""Alpaca history cache build (build.py) and the download step of tools/prepare_history.py, on synthetic stores."""
import csv
import datetime as dt
import gzip
import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history import build as bld

REPO = Path(__file__).resolve().parents[3]
NY = "America/New_York"


# ---------------------------------------------------------------------------
# synthetic stores
# ---------------------------------------------------------------------------
def ny_to_utc(ts):
    return pd.Timestamp(ts).tz_localize(NY).tz_convert("UTC")


def daily_frame(days, close=10.0, volume=1000):
    rows = [{"t": ny_to_utc(pd.Timestamp(d)), "o": close, "h": close + 1, "l": close - 1, "c": close, "v": volume, "n": 5, "vw": close}
            for d in days]
    return pd.DataFrame(rows)


def intraday_frame(days, close=10.0, volume=100, extended=True):
    """78 regular bars (09:30..15:55) per day plus extended-hours bars at 04:00, 09:25 and 16:00, 19:55."""
    rows = []
    for d in days:
        starts = list(pd.date_range(f"{d} 09:30", f"{d} 15:55", freq="5min"))
        if extended:
            starts = [pd.Timestamp(f"{d} 04:00"), pd.Timestamp(f"{d} 09:25")] + starts + [pd.Timestamp(f"{d} 16:00"), pd.Timestamp(f"{d} 19:55")]
        rows += [{"t": ny_to_utc(s), "o": close, "h": close + 0.5, "l": close - 0.5, "c": close, "v": volume, "n": 1, "vw": close} for s in starts]
    return pd.DataFrame(rows)


def write_symbol(daily_root, intraday_root, sym, daily_days, intraday_days=(), gaps=None, close=10.0, volume=1000, d_status="complete"):
    p = Path(daily_root) / "bars" / "1Day" / f"symbol={sym}"
    p.mkdir(parents=True, exist_ok=True)
    daily_frame(daily_days, close, volume).to_parquet(p / "all.parquet", index=False)
    (Path(daily_root) / "manifest").mkdir(exist_ok=True)
    (Path(daily_root) / "manifest" / f"symbol={sym}.json").write_text(json.dumps(
        {"chunks": {"1Day/all": {"status": d_status, "sha256": "d" * 8, "bars": len(daily_days)}}}))
    if intraday_days:
        chunks = {}
        by_year = {}
        for d in intraday_days:
            by_year.setdefault(pd.Timestamp(d).year, []).append(d)
        q = Path(intraday_root) / "bars" / "5Min" / f"symbol={sym}"
        q.mkdir(parents=True, exist_ok=True)
        for y, ds in by_year.items():
            intraday_frame(ds, close, 100).to_parquet(q / f"{y}.parquet", index=False)
            chunk = {"status": "complete", "sha256": f"i{y}", "bars": 82 * len(ds)}
            if gaps and y in gaps:
                chunk["days_without_intraday_bars"] = {"count": len(gaps[y]), "of": len(ds) + len(gaps[y]), "days": gaps[y]}
            chunks[f"5Min/{y}"] = chunk
        (Path(intraday_root) / "manifest").mkdir(exist_ok=True)
        (Path(intraday_root) / "manifest" / f"symbol={sym}.json").write_text(json.dumps({"chunks": chunks}))


def _sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_table(out, splits=None, spinoffs=None, breaks=None, run_id="RUN1", csv_run_id=None, asof="2026-10-12", overrides_path=None):
    from ba2_providers.ohlcv.alpaca_history.split_table import OVERRIDES_PATH
    meta = Path(out) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "split_table.json").write_text(json.dumps({
        "run_id": run_id, "asof": asof, "inputs_sha256": "a", "overrides_sha256": _sha(overrides_path or OVERRIDES_PATH), "splits": splits or {}, "spinoffs": spinoffs or {},
        "breaks": breaks or {}}))
    for name in bld.RUN_ID_CSVS:
        with open(meta / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "run_id"])
            w.writerow(["X", csv_run_id or run_id])


@pytest.fixture
def env(tmp_path):
    daily, intr, out = tmp_path / "daily", tmp_path / "intra", tmp_path / "out"
    for s in (daily, intr):
        s.mkdir()
        (s / "STORE_INFO.json").write_text(json.dumps({"asof": "2026-10-12"}))
        (s / "bars" / "1Day").mkdir(parents=True)
        (s / "bars" / "5Min").mkdir(parents=True)
    out.mkdir()
    return daily, intr, out


def build(env, symbols=None, **kw):
    daily, intr, out = env
    return bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), symbols=symbols, workers=1, **kw)


def sessions(first, last):
    return [d.date().isoformat() for d in pd.bdate_range(first, last)]


# ---------------------------------------------------------------------------
# adjustment
# ---------------------------------------------------------------------------
def test_split_halves_prices_and_doubles_volume(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days[:6])
    make_table(out, splits={"ABC": [["2024-01-08", 2.0]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before, on_after = d[d["Date"] < "2024-01-08"], d[d["Date"] >= "2024-01-08"]
    assert (before["Close"] == 5.0).all() and (before["Volume"] == 2000).all()
    assert (on_after["Close"] == 10.0).all() and (on_after["Volume"] == 1000).all()      # the event day is on the new basis
    m = pd.read_parquet(out / "ABC_5min.parquet")
    assert (m[m["Date"] < "2024-01-08"]["Close"] == 5.0).all() and (m[m["Date"] < "2024-01-08"]["Volume"] == 200).all()
    assert (m[m["Date"] >= "2024-01-08"]["Close"] == 10.0).all()


def test_reverse_split_multiplies_prices_and_divides_volume(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, splits={"ABC": [["2024-01-08", 0.1]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before = d[d["Date"] < "2024-01-08"]
    assert np.allclose(before["Close"], 100.0) and (before["Volume"] == 100).all()


def test_two_splits_compound(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, splits={"ABC": [["2024-01-05", 2.0], ["2024-01-09", 3.0]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet").set_index("Date")
    assert d.loc["2024-01-04", "Close"] == pytest.approx(10 / 6) and d.loc["2024-01-08", "Close"] == pytest.approx(10 / 3)
    assert d.loc["2024-01-10", "Close"] == 10.0


def test_spinoff_divides_prices_and_leaves_volume(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, spinoffs={"ABC": [["2024-01-08", 1.25]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before = d[d["Date"] < "2024-01-08"]
    assert np.allclose(before["Close"], 8.0) and (before["Volume"] == 1000).all()


def test_split_and_spinoff_together(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days)
    make_table(out, splits={"ABC": [["2024-01-08", 2.0]]}, spinoffs={"ABC": [["2024-01-08", 1.25]]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    before = d[d["Date"] < "2024-01-08"]
    assert np.allclose(before["Close"], 4.0) and (before["Volume"] == 2000).all()


def test_break_drops_earlier_bars_and_is_recorded(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out, breaks={"ABC": ["2024-01-04", "2024-01-08"]})
    build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    m = pd.read_parquet(out / "ABC_5min.parquet")
    assert d["Date"].min() == pd.Timestamp("2024-01-08") and m["Date"].min() == pd.Timestamp("2024-01-08 09:30")
    e = json.loads((out / "_meta" / "build_manifest.json").read_text())["symbols"]["ABC"]
    assert e["break_date"] == "2024-01-08" and e["dropped_by_break"] == {"1d": 4, "5min": 4 * 78}
    assert e["1d"]["first"] == "2024-01-08"


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def test_regular_session_only_and_dst_keeps_78_bars(env):
    daily, intr, out = env
    march = ["2024-03-08", "2024-03-11"]            # DST starts Sunday 2024-03-10
    nov = ["2024-11-01", "2024-11-04"]              # DST ends Sunday 2024-11-03
    days = march + nov
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out)
    build(env)
    m = pd.read_parquet(out / "ABC_5min.parquet")
    assert m.groupby(m["Date"].dt.normalize()).size().tolist() == [78, 78, 78, 78]
    t = m["Date"]
    assert t.dt.strftime("%H:%M").min() == "09:30" and t.dt.strftime("%H:%M").max() == "15:55"


def test_half_day_keeps_what_exists_and_gap_day_stays_empty(env):
    daily, intr, out = env
    days = ["2024-11-27", "2024-11-29", "2024-12-02"]
    # 2024-11-29 is the half day: only bars up to 12:55; 2024-12-02 is a listed gap day (daily bar, no intraday bars)
    write_symbol(daily, intr, "ABC", days, ["2024-11-27", "2024-11-29"], gaps={2024: ["2024-12-02"]})
    p = intr / "bars" / "5Min" / "symbol=ABC" / "2024.parquet"
    f = pd.read_parquet(p)
    local = f["t"].dt.tz_convert(NY)
    f = f[~((local.dt.normalize() == pd.Timestamp("2024-11-29", tz=NY)) & (local.dt.hour >= 13))]
    f.to_parquet(p, index=False)
    make_table(out)
    build(env)
    m = pd.read_parquet(out / "ABC_5min.parquet")
    per_day = m.groupby(m["Date"].dt.normalize()).size()
    assert per_day[pd.Timestamp("2024-11-27")] == 78 and per_day[pd.Timestamp("2024-11-29")] == 42
    assert pd.Timestamp("2024-12-02") not in per_day.index                       # never filled from the daily bar
    e = json.loads((out / "_meta" / "build_manifest.json").read_text())["symbols"]["ABC"]
    assert e["days_without_intraday_bars"]["2024"]["days"] == ["2024-12-02"]
    assert pd.read_parquet(out / "ABC_1d.parquet")["Date"].iloc[-1] == pd.Timestamp("2024-12-02")


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------
FMP_DTYPES_1D = {"Date": "datetime64[ns]", "Open": "float64", "High": "float64", "Low": "float64", "Close": "float64",
                 "Volume": "int64", "effective_date": "datetime64[ns]"}
FMP_DTYPES_5MIN = dict(FMP_DTYPES_1D, Volume="float64")


def test_columns_and_dtypes_match_the_fmp_files(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out, splits={"ABC": [["2024-01-04", 1.5]]})
    build(env)
    for name, spec in (("ABC_1d.parquet", FMP_DTYPES_1D), ("ABC_5min.parquet", FMP_DTYPES_5MIN)):
        df = pd.read_parquet(out / name)
        assert list(df.columns) == list(spec) and {c: str(t) for c, t in df.dtypes.items()} == spec
        assert (df["effective_date"] == df["Date"]).all() and df["Date"].dt.tz is None
    # and against a real FMP cache file when this machine has one
    real = Path(os.path.expanduser("~/Documents/ba2/common/cache/FMPOHLCVProvider/AAPL_1d.parquet"))
    if real.is_file():
        assert {c: str(t) for c, t in pd.read_parquet(real).dtypes.items()} == FMP_DTYPES_1D


def test_file_names_follow_the_reader_rule():
    assert bld.file_names("BRK-B") == ("BRK-B_1d.parquet", "BRK-B_5min.parquet")
    assert bld.file_names("brk-b") == ("BRK-B_1d.parquet", "BRK-B_5min.parquet")


def test_share_class_symbol_is_written_with_a_dash_and_dotted_request_maps_to_it(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "BRK-B", days)
    make_table(out)
    build(env, symbols=["BRK.B"])
    assert (out / "BRK-B_1d.parquet").is_file()


def test_no_file_for_a_symbol_without_bars(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "ABC", days)
    (daily / "bars" / "1Day" / "symbol=EMPTY").mkdir()
    make_table(out)
    s = build(env)
    assert s["skipped_no_bars"] == 1 and not (out / "EMPTY_1d.parquet").exists() and (out / "ABC_1d.parquet").is_file()
    assert not (out / "ABC_5min.parquet").exists()                                # no 5-minute bars, no 5-minute file


# ---------------------------------------------------------------------------
# refusals and atomicity
# ---------------------------------------------------------------------------
def test_run_id_mismatch_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out, run_id="RUN1", csv_run_id="RUN0")
    with pytest.raises(bld.BuildError, match="run_id"):
        build(env)
    assert not (out / "ABC_1d.parquet").exists()


def test_asof_mismatch_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out, asof="2026-10-11")
    with pytest.raises(bld.BuildError, match="asof"):
        build(env)


def test_daily_not_at_new_york_midnight_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f["t"] = f["t"] + pd.Timedelta(hours=1)
    f.to_parquet(p, index=False)
    make_table(out)
    with pytest.raises(bld.BuildError, match="ABC"):
        build(env)
    assert not (out / "ABC_1d.parquet").exists()
    assert "00:00 New York" in json.loads((out / "_meta" / "build_manifest.json").read_text())["failed"]["ABC"]


def test_incomplete_chunk_refuses(env):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"), d_status="failed")
    make_table(out)
    with pytest.raises(bld.BuildError):
        build(env)


def test_write_is_atomic(env, monkeypatch):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out)

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises((bld.BuildError, OSError)):
        build(env)
    assert [n for n in os.listdir(out) if n != "_meta"] == []                      # no partial file, no temp file


def test_old_file_survives_a_failed_rewrite(env, monkeypatch):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out)
    build(env)
    before = (out / "ABC_1d.parquet").read_bytes()
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises((bld.BuildError, OSError)):
        build(env)
    assert (out / "ABC_1d.parquet").read_bytes() == before


def test_manifest_records_inputs_and_events(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days[:3])
    make_table(out, splits={"ABC": [["2024-01-08", 2.0], ["2019-01-01", 4.0]]})
    s = build(env)
    m = json.loads((out / "_meta" / "build_manifest.json").read_text())
    assert m["store_asof"] == "2026-10-12" and m["split_table"]["run_id"] == "RUN1" and len(m["split_table"]["sha256"]) == 64
    e = m["symbols"]["ABC"]
    assert e["events"]["splits"] == [["2024-01-08", 2.0]]                          # the 2019 event predates every bar
    assert e["1d"]["bars"] == 9 and e["5min"]["bars"] == 3 * 78 and e["5min"]["first"].startswith("2024-01-02 09:30")
    assert s["built"] == 1 and s["files_1d"] == 1 and s["files_5min"] == 1


def test_subset_run_merges_into_the_manifest_of_the_same_table(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "AAA", days)
    write_symbol(daily, intr, "BBB", days)
    make_table(out)
    build(env, symbols=["AAA"])
    build(env, symbols=["BBB"])
    assert sorted(json.loads((out / "_meta" / "build_manifest.json").read_text())["symbols"]) == ["AAA", "BBB"]


def test_rebuild_without_5min_removes_the_stale_5min_file(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "ABC", days, days)
    make_table(out)
    build(env)
    assert (out / "ABC_5min.parquet").is_file()
    import shutil
    shutil.rmtree(intr / "bars" / "5Min" / "symbol=ABC")
    build(env)
    assert not (out / "ABC_5min.parquet").exists()


# ---------------------------------------------------------------------------
# the pipeline: download step
# ---------------------------------------------------------------------------
def load_prepare_history():
    spec = importlib.util.spec_from_file_location("prepare_history_under_test", REPO / "tools" / "prepare_history.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ph_args(ph, tmp_path, *extra):
    daily, intr, out = tmp_path / "daily", tmp_path / "intra", tmp_path / "out"
    for d in (daily, intr, out / "_meta"):
        d.mkdir(parents=True, exist_ok=True)
    (daily / "symbols.txt").write_text("AAA\nBBB\n")
    (intr / "symbols.txt").write_text("AAA\n")
    (daily / ph.SPINCO_SYMBOLS_FILE).write_text("SPIN1\nSPIN2\n")
    (out / "_meta" / ph.SPINCO_MISSING_FILE).write_text("SPIN2\nSPIN3\n")
    argv = ["--source", "alpaca", "--store", str(daily), "--store-5min", str(intr), "--out", str(out), "--db", str(tmp_path / "k.db"), *extra]
    return ph.build_parser().parse_args(argv)


def test_download_step_uses_the_recorded_arguments(tmp_path, monkeypatch):
    ph = load_prepare_history()
    a = ph_args(ph, tmp_path)
    calls = []

    class FakeDl:
        @staticmethod
        def main(argv):
            sym = Path(argv[argv.index("--symbols-file") + 1])
            calls.append((argv, sym.read_text().split()))
            return 0
    monkeypatch.setattr(ph, "_load_downloader", lambda: FakeDl)
    res = ph.step_alpaca_download(a, lambda m: None)
    assert len(calls) == 3 and all(r["exit_code"] == 0 for r in res["runs"])
    common = ["--max-gap-days", "5", "--keys-db", str(tmp_path / "k.db"), "--account-id", "1", "--rpm", "150", "--min-remaining", "60"]
    daily, intr = str(tmp_path / "daily"), str(tmp_path / "intra")
    assert calls[0][0] == ["--store", intr, "--symbols-file", os.path.join(intr, "symbols.txt"), "--start", "2019-01-01", "--end", "2026-10-13"] + common
    assert calls[1][0] == ["--store", daily, "--symbols-file", os.path.join(daily, "symbols.txt"), "--start", "2016-01-01", "--end", "2026-10-13",
                           "--asof", "2026-10-12", "--phases", "corporate_actions,daily"] + common
    argv3 = calls[2][0]
    assert argv3[argv3.index("--phases") + 1] == "daily" and argv3[argv3.index("--asof") + 1] == "2026-10-12" and argv3[1] == daily
    assert calls[2][1] == ["SPIN1", "SPIN2", "SPIN3"]                               # merged, de-duplicated, order kept
    assert calls[0][1] == ["AAA"] and calls[1][1] == ["AAA", "BBB"]


def test_download_step_fails_on_a_nonzero_exit(tmp_path, monkeypatch):
    ph = load_prepare_history()
    a = ph_args(ph, tmp_path)
    monkeypatch.setattr(ph, "_load_downloader", lambda: type("D", (), {"main": staticmethod(lambda argv: 1)}))
    with pytest.raises(RuntimeError, match="exited 1"):
        ph.step_alpaca_download(a, lambda m: None)


def test_resume_over_complete_stores_makes_no_data_requests(tmp_path, monkeypatch):
    """The three recorded runs over empty-but-complete stores: the second pass asks the vendor for nothing."""
    import gzip as gz
    import io
    ph = load_prepare_history()
    a = ph_args(ph, tmp_path)
    dl = ph._load_downloader()
    counts = []

    class Resp:
        headers = {"Content-Encoding": "gzip", "X-RateLimit-Remaining": "150", "X-RateLimit-Reset": "0"}

        def __init__(self, payload):
            self._raw = gz.compress(json.dumps(payload).encode())

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def opener(request, timeout):
        counts.append(request.full_url)
        if "corporate-actions" in request.full_url:
            return Resp({"corporate_actions": {"forward_splits": []}, "next_page_token": None})
        return Resp({"bars": {}, "next_page_token": None})

    real_transport = dl.Transport
    monkeypatch.setattr(dl, "Transport", lambda k, s, **kw: real_transport(k, s, opener=opener, sleep=lambda _x: None, **dict(kw, rpm=1e9)))
    monkeypatch.setattr(dl, "load_credentials", lambda db, account: ("KEY", "SECRET"))
    monkeypatch.setattr(ph, "_load_downloader", lambda: dl)
    ph.step_alpaca_download(a, lambda m: None)
    first = len(counts)
    assert first > 0
    counts.clear()
    ph.step_alpaca_download(a, lambda m: None)
    assert counts == []


def test_fmp_download_wraps_fetch_cache_and_needs_its_arguments(tmp_path, monkeypatch):
    ph = load_prepare_history()
    a = ph.build_parser().parse_args(["--source", "fmp"])
    with pytest.raises(ValueError, match="--fmp-symbols-file"):
        ph.fmp_download_argv(a)
    sym = tmp_path / "u.txt"
    sym.write_text("AAPL\n")
    a = ph.build_parser().parse_args(["--source", "fmp", "--fmp-symbols-file", str(sym), "--fmp-start", "2020-01-01", "--fmp-end", "2025-12-31"])
    argv = ph.fmp_download_argv(a)
    assert argv[2:] == ["fetch-cache", "--provider", "fmp", "--timeframes", "5min,1d", "--start", "2020-01-01", "--end", "2025-12-31",
                        "--symbols", "@" + os.path.abspath(sym), "--workers", "5"] and argv[1].endswith("ba2test_launcher.py")
    seen = []
    monkeypatch.setattr(ph.subprocess, "run", lambda argv, cwd: seen.append(argv) or type("P", (), {"returncode": 0})())
    assert ph.step_fmp_download(a, lambda m: None)["exit_code"] == 0 and seen == [argv]
    monkeypatch.setattr(ph.subprocess, "run", lambda argv, cwd: type("P", (), {"returncode": 3})())
    with pytest.raises(RuntimeError, match="exited 3"):
        ph.step_fmp_download(a, lambda m: None)


# ---------------------------------------------------------------------------
# the pipeline: market-conditions step (the last step of BOTH sources)
# ---------------------------------------------------------------------------
import sys


def mc_setup(ph, tmp_path, source="alpaca"):
    """A cache root with the source's provider folder, a universe file, and the CLI args to run the step on it."""
    root = tmp_path / "cache"
    folder = root / ("AlpacaHistoryOHLCVProvider" if source == "alpaca" else "FMPOHLCVProvider")
    (folder / "_meta").mkdir(parents=True)
    uni = tmp_path / "uni.txt"
    uni.write_text("AAA\nBBB\n")
    flag = "--out" if source == "alpaca" else "--fmp-daily-dir"
    other = ["--out", str(tmp_path / "out")] if source == "fmp" else []
    argv = ["--source", source, flag, str(folder), *other, "--mc-cache-root", str(root), "--universe-file", str(uni),
            "--start", "2024-06-03", "--end", "2024-06-28", "--report-dir", str(tmp_path / "rep"), "--workers", "3"]
    return argv, root


def _real_warm_parser():
    spec = importlib.util.spec_from_file_location("warm_market_conditions_real", REPO / "tools" / "warm_market_conditions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_parser()


class FakeWarm:
    """Stands in for tools/warm_market_conditions.py: records every argv, prints what the real CLI prints."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def main(self, argv):
        self.calls.append(list(argv))
        _real_warm_parser().parse_args(argv)           # every argv must be accepted by the REAL tool's parser
        cmd = argv[0]
        profile = argv[argv.index("--profile") + 1] if "--profile" in argv else None
        if profile is None and "--plan" in argv:
            profile = os.path.basename(argv[argv.index("--plan") + 1]).split(".")[1]
        if self.fail == (cmd, profile):
            print("something is missing", file=sys.stderr if cmd == "build" else sys.stdout)
            return 1
        if cmd == "build":
            print(json.dumps({"ok": True}), file=sys.stderr)
            print("d" * 8 + profile)                   # --print-digest: the digest and nothing else on stdout
        else:
            print(json.dumps({"ok": True}))
        return 0


def test_market_conditions_is_the_last_step_of_both_sources(tmp_path):
    ph = load_prepare_history()
    assert ph.SOURCE_STEPS["alpaca"][-1] == "market-conditions" and ph.SOURCE_STEPS["fmp"][-1] == "market-conditions"
    assert ph.SOURCE_STEPS["alpaca"] == ("download", "split-table", "ipo-dates", "build", "verify", "market-conditions")
    assert ph.SOURCE_STEPS["fmp"] == ("download", "verify", "market-conditions")


@pytest.mark.parametrize("source", ["alpaca", "fmp"])
def test_market_conditions_is_refused_without_universe_start_and_end(tmp_path, source, capsys):
    ph = load_prepare_history()
    argv, _root = mc_setup(ph, tmp_path, source)
    for name in ("--universe-file", "--start", "--end"):
        i = argv.index(name)
        cut = argv[:i] + argv[i + 2:]
        assert ph.main([*cut, "--steps", "market-conditions"]) == ph.EXIT_CONFIG, name
        assert name in capsys.readouterr().err
        assert not (tmp_path / "rep").exists()                      # refused before anything ran


def test_market_conditions_refuses_a_cache_that_is_not_the_one_the_warm_tool_reads(tmp_path, capsys):
    ph = load_prepare_history()
    argv, root = mc_setup(ph, tmp_path)
    argv[argv.index("--out") + 1] = str(tmp_path / "elsewhere")
    assert ph.main([*argv, "--steps", "market-conditions"]) == ph.EXIT_CONFIG
    assert "--mc-cache-root" in capsys.readouterr().err


@pytest.mark.parametrize("source", ["alpaca", "fmp"])
def test_market_conditions_runs_plan_build_verify_prepare_host_for_both_profiles_and_records_digests(
        tmp_path, monkeypatch, source):
    ph = load_prepare_history()
    argv, root = mc_setup(ph, tmp_path, source)
    warm = FakeWarm()
    monkeypatch.setattr(ph, "_load_warm", lambda: warm)
    assert ph.main([*argv, "--steps", "market-conditions"]) == ph.EXIT_OK
    assert [c[0] for c in warm.calls] == ["plan", "build", "verify", "prepare-host"] * 2
    for c in warm.calls:                                            # the vendor is stated on EVERY call, nothing defaulted
        assert c[c.index("--ohlcv-history-source") + 1] == source
        if c[0] == "build":                          # build reads the plan's own cache_root and has no such flag
            assert "--cache-root" not in c
        else:
            assert c[c.index("--cache-root") + 1] == str(root)
    plan0 = warm.calls[0]
    assert plan0[plan0.index("--profile") + 1] == "ohlcv-v1" and plan0[plan0.index("--start") + 1] == "2024-06-03"
    assert plan0[plan0.index("--end") + 1] == "2024-06-28" and plan0[plan0.index("--universe-file") + 1].endswith("uni.txt")
    assert "--cache-only" in warm.calls[1] and "--fetch-missing" not in warm.calls[1] and "--print-digest" in warm.calls[1]
    assert warm.calls[2][warm.calls[2].index("--manifest") + 1] == "ddddddddohlcv-v1"
    prep = warm.calls[7]
    assert prep[prep.index("--profile") + 1] == "ta-structure-v1" and prep[prep.index("--jobs") + 1] == "3"
    report = json.loads((tmp_path / "rep" / f"prepare_history_{source}_report.json").read_text())
    res = report["steps"][-1]["result"]
    assert report["steps"][-1]["step"] == "market-conditions" and report["steps"][-1]["status"] == "ok"
    assert res["digests"] == {"ohlcv-v1": "ddddddddohlcv-v1", "ta-structure-v1": "ddddddddta-structure-v1"}


@pytest.mark.parametrize("source,expected", [("alpaca", "previous"), ("fmp", None)])
def test_market_conditions_plan_uses_gap_fill_previous_for_alpaca_only(tmp_path, monkeypatch, source, expected):
    ph = load_prepare_history()
    argv, _root = mc_setup(ph, tmp_path, source)
    warm = FakeWarm()
    monkeypatch.setattr(ph, "_load_warm", lambda: warm)
    assert ph.main([*argv, "--steps", "market-conditions"]) == ph.EXIT_OK
    plans = [c for c in warm.calls if c[0] == "plan"]
    assert len(plans) == 2
    for c in plans:
        assert (c[c.index("--gap-fill") + 1] if "--gap-fill" in c else None) == expected
    assert all("--gap-fill" not in c for c in warm.calls if c[0] != "plan")      # build must match the plan, it never overrides it


def test_market_conditions_exit_is_nonzero_when_any_profile_fails_but_both_are_reported(tmp_path, monkeypatch):
    ph = load_prepare_history()
    argv, _root = mc_setup(ph, tmp_path)
    warm = FakeWarm(fail=("plan", "ohlcv-v1"))      # the second profile must still run and be reported
    monkeypatch.setattr(ph, "_load_warm", lambda: warm)
    assert ph.main([*argv, "--steps", "market-conditions"]) == ph.EXIT_FAILED
    assert [c[0] for c in warm.calls] == ["plan", "plan", "build", "verify", "prepare-host"]
    entry = json.loads((tmp_path / "rep" / "prepare_history_alpaca_report.json").read_text())["steps"][-1]
    assert entry["status"] == "failed" and "ohlcv-v1 at plan" in entry["error"]
    res = entry["result"]
    assert res["digests"] == {"ohlcv-v1": None, "ta-structure-v1": "ddddddddta-structure-v1"}
    assert res["profiles"]["ohlcv-v1"]["failed"] == "plan" and "failed" not in res["profiles"]["ta-structure-v1"]


def test_market_conditions_dry_run_lists_the_eight_commands_and_writes_nothing(tmp_path, capsys):
    ph = load_prepare_history()
    argv, _root = mc_setup(ph, tmp_path)
    assert ph.main([*argv, "--steps", "market-conditions", "--dry-run"]) == ph.EXIT_OK
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("market-conditions:")]
    assert len(lines) == 8 and all("--ohlcv-history-source alpaca" in ln for ln in lines)
    assert not (tmp_path / "rep").exists()


# ---------------------------------------------------------------------------
# bad daily prints
# ---------------------------------------------------------------------------
BAD_DAY = "2024-01-04"


def repair_env(env, tmp_path, daily_high=20.0, daily_low=None, fmp_high=10.5, fmp_low=9.5, fmp_scale=1.0, with_fmp=True, close=10.0):
    """One symbol, five sessions; on BAD_DAY the daily High (and Low) is set to a (bad) print.  5-minute envelope: 9.5 .. 10.5."""
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-08")
    write_symbol(daily, intr, "ABC", days, days)
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    i = f.index[f["t"] == ny_to_utc(pd.Timestamp(BAD_DAY))][0]
    f.loc[i, "h"] = daily_high
    if daily_low is not None:
        f.loc[i, "l"] = daily_low
    f.to_parquet(p, index=False)
    make_table(out)
    fmp = tmp_path / "fmp"
    fmp.mkdir(parents=True)
    if with_fmp:
        rows = [{"Date": pd.Timestamp(d), "High": fmp_high * fmp_scale, "Low": fmp_low * fmp_scale, "Close": close * fmp_scale, "Volume": 1000} for d in days]
        pd.DataFrame(rows).to_parquet(fmp / "ABC_1d.parquet", index=False)
    s = bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, fmp_daily_dir=str(fmp))
    return pd.read_parquet(out / "ABC_1d.parquet").set_index("Date"), s


def test_bad_high_with_fmp_witness_is_repaired_and_recorded(env, tmp_path):
    d, s = repair_env(env, tmp_path)
    out = env[2]
    row = d.loc[BAD_DAY]
    assert row["High"] == 10.5 and row["Open"] == 10.0 and row["Close"] == 10.0 and row["Volume"] == 1000
    assert s["bar_repairs"] >= 1


def test_repair_row_is_recorded_in_the_csv(env, tmp_path):
    repair_env(env, tmp_path)
    r = pd.read_csv(env[2] / "_meta" / "bar_repairs.csv", keep_default_na=False)
    row = r[r["date"] == BAD_DAY].iloc[0]
    assert (row["symbol"], row["field"], row["old"], row["new"], row["envelope"], row["fmp"], row["run_id"]) == ("ABC", "High", 20.0, 10.5, 10.5, 10.5, "RUN1")
    assert (env[2] / "_meta" / "envelope" / "ABC.parquet").is_file()
    assert list(r.columns) == bld.REPAIR_COLUMNS


def test_no_repair_without_the_fmp_witness(env, tmp_path):
    d, s = repair_env(env, tmp_path, with_fmp=False)
    assert d.loc[BAD_DAY, "High"] == 20.0 and s["bar_repairs"] == 0


def test_no_repair_when_fmp_disagrees_with_the_envelope(env, tmp_path):
    d, s = repair_env(env, tmp_path, fmp_high=20.0)                                  # FMP shows the same high: not a bad print we can witness
    assert d.loc[BAD_DAY, "High"] == 20.0


def test_witness_uses_the_days_ratio_to_fmp(env, tmp_path):
    d, _ = repair_env(env, tmp_path, fmp_scale=2.0)                                  # FMP on another basis: x2 everywhere
    assert d.loc[BAD_DAY, "High"] == 10.5


def test_a_high_explained_by_the_close_is_not_repaired(env, tmp_path):
    # Close == High outside the 5-minute envelope (missing late bars / closing auction): High' = max(env, Open, Close) = Close
    sub = (tmp_path / "e2")
    for n in ("daily", "intra", "out"):
        (sub / n).mkdir(parents=True)
        if n != "out":
            (sub / n / "STORE_INFO.json").write_text(json.dumps({"asof": "2026-10-12"}))
            (sub / n / "bars" / "1Day").mkdir(parents=True)
            (sub / n / "bars" / "5Min").mkdir(parents=True)
    days = sessions("2024-01-02", "2024-01-08")
    write_symbol(sub / "daily", sub / "intra", "ABC", days, days, close=10.0)
    p = sub / "daily" / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f.loc[:, ["o", "h", "c"]] = 12.0
    f.loc[:, "l"] = 11.9
    f.to_parquet(p, index=False)
    make_table(sub / "out")
    fmp = tmp_path / "fmp2"
    fmp.mkdir()
    pd.DataFrame([{"Date": pd.Timestamp(x), "High": 10.5, "Low": 9.5, "Close": 12.0, "Volume": 1000} for x in days]).to_parquet(fmp / "ABC_1d.parquet", index=False)
    s = bld.build_cache(daily_store=str(sub / "daily"), intraday_store=str(sub / "intra"), out=str(sub / "out"), workers=1, fmp_daily_dir=str(fmp))
    assert s["bar_repairs"] == 0


def test_a_high_within_two_percent_is_left_alone(env, tmp_path):
    d, s = repair_env(env, tmp_path, daily_high=10.6)
    assert d.loc[BAD_DAY, "High"] == 10.6


def test_bad_low_is_repaired_symmetrically(env, tmp_path):
    d, _ = repair_env(env, tmp_path, daily_high=10.5, daily_low=1.0)
    assert d.loc[BAD_DAY, "Low"] == 9.5 and d.loc[BAD_DAY, "High"] == 10.5
    r = pd.read_csv(env[2] / "_meta" / "bar_repairs.csv", keep_default_na=False)
    assert "Low" in set(r["field"])


# ---------------------------------------------------------------------------
# Yahoo as the second witness
# ---------------------------------------------------------------------------
def yahoo_witness(tmp_path, high=None, low=None, available=True):
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    days = [pd.Timestamp(d) for d in sessions("2024-01-02", "2024-01-08")]
    frame = pd.DataFrame({"day": days, "Close": 10.0, "High": 10.5 if high is None else high, "Low": 9.5 if low is None else low})
    return ver.YahooDaily(str(tmp_path / "yd"), fetch=lambda t: frame if available else None, sleep=lambda s: None)


def second_witness(env, tmp_path, yahoo, fmp_high, daily_high=20.0):
    """FMP carries ``fmp_high`` on the bad day (not the envelope value: the first witness does not settle it); the daily High is bad."""
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-08")
    write_symbol(daily, intr, "ABC", days, days)
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f.loc[f["t"] == ny_to_utc(pd.Timestamp(BAD_DAY)), "h"] = daily_high
    f["h"] = f["h"].where(f["t"] == ny_to_utc(pd.Timestamp(BAD_DAY)), 10.5)       # the other days are clean
    f["l"] = 9.5
    f.to_parquet(p, index=False)
    make_table(out)
    fmp = tmp_path / "fmp"
    fmp.mkdir(parents=True)
    rows = [{"Date": pd.Timestamp(d), "High": fmp_high if d == BAD_DAY else 10.5, "Low": 9.5, "Close": 10.0, "Volume": 1000} for d in days]
    pd.DataFrame(rows).to_parquet(fmp / "ABC_1d.parquet", index=False)
    s = bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, fmp_daily_dir=str(fmp), yahoo=yahoo)
    return pd.read_parquet(out / "ABC_1d.parquet").set_index("Date"), s


def test_yahoo_agreeing_with_the_envelope_repairs_and_is_recorded_as_the_witness(env, tmp_path):
    d, s = second_witness(env, tmp_path, yahoo_witness(tmp_path, high=10.5), fmp_high=20.0)
    assert d.loc[BAD_DAY, "High"] == 10.5 and s["bar_repairs"] == 1 and s["unwitnessed_outliers"] == 0
    r = pd.read_csv(env[2] / "_meta" / "bar_repairs.csv", keep_default_na=False)
    assert list(r.columns) == bld.REPAIR_COLUMNS and r["witness"].tolist() == ["yahoo"]
    assert r["old"].iloc[0] == 20.0 and r["new"].iloc[0] == 10.5 and r["yahoo"].iloc[0] == 10.5 and r["fmp"].iloc[0] == 20.0
    assert (env[2] / "_meta" / "build_yahoo_inputs.csv").is_file()


def test_yahoo_and_fmp_both_carrying_the_print_keep_it_as_a_consolidated_print(env, tmp_path):
    d, s = second_witness(env, tmp_path, yahoo_witness(tmp_path, high=20.0), fmp_high=20.0)
    assert d.loc[BAD_DAY, "High"] == 20.0 and s["bar_repairs"] == 0 and s["consolidated_prints"] == 1 and s["unwitnessed_outliers"] == 0
    c = pd.read_csv(env[2] / "_meta" / "consolidated_prints.csv", keep_default_na=False)
    assert c["date"].tolist() == [BAD_DAY] and c["field"].tolist() == ["High"] and c["daily"].tolist() == [20.0]


def test_a_witness_inside_the_envelope_that_does_not_settle_it_leaves_the_bar_unwitnessed(env, tmp_path):
    # Yahoo 10.65: not within 1 % of the bound (no repair) and not beyond it by 2 % (not a consolidated print): left as it is
    d, s = second_witness(env, tmp_path, yahoo_witness(tmp_path, high=10.65), fmp_high=20.0)
    assert d.loc[BAD_DAY, "High"] == 20.0 and s["unwitnessed_outliers"] == 1 and s["consolidated_prints"] == 0 and s["bar_repairs"] == 0


def test_a_print_only_yahoo_carries_is_not_consolidated_when_fmp_is_inside_the_envelope(env, tmp_path):
    d, s = second_witness(env, tmp_path, yahoo_witness(tmp_path, high=20.0), fmp_high=10.0)
    assert d.loc[BAD_DAY, "High"] == 20.0 and s["consolidated_prints"] == 0 and s["unwitnessed_outliers"] == 1


def test_every_available_witness_beyond_the_envelope_is_a_consolidated_print(env, tmp_path):
    # Yahoo unavailable (no frame at all): FMP alone, beyond the envelope on the same side
    d, s = second_witness(env, tmp_path, yahoo_witness(tmp_path, available=False), fmp_high=20.0)
    assert d.loc[BAD_DAY, "High"] == 20.0 and s["consolidated_prints"] == 1 and s["unwitnessed_outliers"] == 0 and s["bar_repairs"] == 0
    c = pd.read_csv(env[2] / "_meta" / "consolidated_prints.csv", keep_default_na=False)
    assert list(c.columns) == bld.CONSOLIDATED_COLUMNS
    assert c["rule"].tolist() == [bld.RULE_ALL_BEYOND] and c["witnesses"].tolist() == ["fmp"]


def test_both_witnesses_beyond_the_envelope_but_neither_equal_to_the_daily_value_is_consolidated(env, tmp_path):
    # CRDF 2018-12-31: FMP 3.391, Yahoo 3.42, daily 3.44, bound 3.246 -> here FMP 15, Yahoo 12, daily 20, bound 10.5
    d, s = second_witness(env, tmp_path, yahoo_witness(tmp_path, high=12.0), fmp_high=15.0)
    assert d.loc[BAD_DAY, "High"] == 20.0 and s["consolidated_prints"] == 1 and s["unwitnessed_outliers"] == 0
    c = pd.read_csv(env[2] / "_meta" / "consolidated_prints.csv", keep_default_na=False)
    assert c["rule"].tolist() == [bld.RULE_ALL_BEYOND] and c["witnesses"].tolist() == ["fmp+yahoo"]


def test_yahoo_only_witness_with_no_fmp_bar_beyond_the_envelope_is_consolidated(env, tmp_path):
    # CIRC / SUIG: Yahoo = daily, no FMP bar that day
    import numpy as np
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-08")
    write_symbol(daily, intr, "ABC", days, days)
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f["h"] = np.where(f["t"] == ny_to_utc(pd.Timestamp(BAD_DAY)), 20.0, 10.5)
    f["l"] = 9.5
    f.to_parquet(p, index=False)
    make_table(out)
    fmp = tmp_path / "fmp"
    fmp.mkdir()
    pd.DataFrame([{"Date": pd.Timestamp(d), "High": 10.5, "Low": 9.5, "Close": 10.0, "Volume": 1000} for d in days if d != BAD_DAY]).to_parquet(fmp / "ABC_1d.parquet", index=False)
    s = bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, fmp_daily_dir=str(fmp),
                        yahoo=yahoo_witness(tmp_path, high=20.0))
    assert s["consolidated_prints"] == 1 and s["unwitnessed_outliers"] == 0
    c = pd.read_csv(out / "_meta" / "consolidated_prints.csv", keep_default_na=False)
    assert c["witnesses"].tolist() == ["yahoo"]


def test_a_low_beyond_the_envelope_with_every_witness_beyond_it_is_consolidated():
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        df = pd.DataFrame({"Date": [pd.Timestamp("2019-10-01")], "Open": [17.5], "High": [17.6], "Low": [15.55], "Close": [17.5],
                           "Volume": [1], "effective_date": [pd.Timestamp("2019-10-01")]})
        df.to_parquet(os.path.join(t, "FNWB_1d.parquet"), index=False)
        y = pd.DataFrame({"day": [pd.Timestamp("2019-10-01")], "Close": [17.5], "High": [17.6], "Low": [13.86]})
        o = {"date": "2019-10-01", "field": "Low", "old": 15.55, "bound": 17.007, "fmp": 15.55}
        rep, cons, left = bld.yahoo_second_pass(t, "FNWB", [o], y)
        assert rep == [] and left == [] and [c["rule"] for c in cons] == [bld.RULE_ALL_BEYOND]


# ---------------------------------------------------------------------------
# placeholder daily bars (v == 0 and n == 0) are dropped
# ---------------------------------------------------------------------------
def test_placeholder_daily_bars_are_dropped_counted_and_never_backfilled(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days)
    write_symbol(daily, intr, "XYZ", days, days[:2])
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    ph = f["t"].isin([ny_to_utc(pd.Timestamp(d)) for d in days[2:5]])
    f.loc[ph, ["v", "n"]] = 0                                       # a flat no-trade run
    f.loc[f["t"] == ny_to_utc(pd.Timestamp(days[6])), "v"] = 0      # volume 0 but trades counted: a real bar, kept
    f.to_parquet(p, index=False)
    make_table(out)
    s = build(env)
    d = pd.read_parquet(out / "ABC_1d.parquet")
    assert set(d["Date"].dt.strftime("%Y-%m-%d")) == set(days) - set(days[2:5])
    assert len(pd.read_parquet(out / "XYZ_1d.parquet")) == len(days)
    m = json.loads((out / "_meta" / "build_manifest.json").read_text())
    assert m["symbols"]["ABC"]["dropped_placeholders"] == 3 and m["symbols"]["XYZ"]["dropped_placeholders"] == 0
    assert m["dropped_placeholders"] == 3 and s["dropped_placeholders"] == 3 and s["symbols_with_placeholders"] == 1
    assert s["bars_1d"] == len(d) + len(days)


def test_placeholder_count_covers_only_bars_after_the_series_break(env):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-12")
    write_symbol(daily, intr, "ABC", days, days)
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f.loc[f["t"].isin([ny_to_utc(pd.Timestamp(d)) for d in (days[0], days[7])]), ["v", "n"]] = 0
    f.to_parquet(p, index=False)
    make_table(out, breaks={"ABC": [days[3]]})
    build(env)
    m = json.loads((out / "_meta" / "build_manifest.json").read_text())
    assert m["symbols"]["ABC"]["dropped_placeholders"] == 1


# ---------------------------------------------------------------------------
# bar_cap overrides
# ---------------------------------------------------------------------------
def _overrides_file(tmp_path, entries):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return p


def _cap(field, day=BAD_DAY, reviewed_by="owner (B. Migette) 2026-10-21", symbol="ABC"):
    return {"symbol": symbol, "event_date": day, "kind": "bar_cap", "field": field, "decision": "accept", "reason": "bad print",
            "added": "2026-10-21", "reviewed_by": reviewed_by}


def _cap_env(env, tmp_path, entries, **kw):
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-08")
    write_symbol(daily, intr, "ABC", days, days)
    p = daily / "bars" / "1Day" / "symbol=ABC" / "all.parquet"
    f = pd.read_parquet(p)
    f["h"], f["l"] = 10.5, 9.5                                                       # the other days are clean
    i = f.index[f["t"] == ny_to_utc(pd.Timestamp(BAD_DAY))][0]
    f.loc[i, "h"], f.loc[i, "l"] = 20.0, 1.0
    f.to_parquet(p, index=False)
    ov = _overrides_file(tmp_path, entries)
    make_table(out, overrides_path=ov)
    return build(env, overrides_path=str(ov), **kw), out


def test_bar_cap_override_caps_the_field_at_the_envelope_and_records_witness_override(env, tmp_path):
    s, out = _cap_env(env, tmp_path, [_cap("High")])
    d = pd.read_parquet(out / "ABC_1d.parquet").set_index("Date")
    assert d.loc[BAD_DAY, "High"] == 10.5 and d.loc[BAD_DAY, "Low"] == 1.0          # only the named field
    r = pd.read_csv(out / "_meta" / "bar_repairs.csv", keep_default_na=False)
    assert r["witness"].tolist() == ["override"] and r["field"].tolist() == ["High"] and r["old"].iloc[0] == 20.0 and r["new"].iloc[0] == 10.5
    assert s["bar_repairs"] == 1 and s["unwitnessed_outliers"] == 1                  # the Low is still an unwitnessed outlier


def test_bar_cap_removes_the_value_from_the_unwitnessed_list_and_caps_a_low(env, tmp_path):
    s, out = _cap_env(env, tmp_path, [_cap("High"), _cap("Low")])
    d = pd.read_parquet(out / "ABC_1d.parquet").set_index("Date")
    assert d.loc[BAD_DAY, "High"] == 10.5 and d.loc[BAD_DAY, "Low"] == 9.5 and s["unwitnessed_outliers"] == 0 and s["bar_repairs"] == 2


def test_bar_cap_on_a_day_without_a_bar_stops_the_build_and_a_changed_overrides_file_is_refused(env, tmp_path):
    with pytest.raises(bld.BuildError, match="no built daily bar"):
        _cap_env(env, tmp_path, [_cap("High", day="2024-02-01")])
    daily, intr, out = env
    ov = _overrides_file(tmp_path, [_cap("High")])
    make_table(out, overrides_path=ov)
    ov.write_text(ov.read_text() + " ", encoding="utf-8")
    with pytest.raises(bld.BuildError, match="changed since the split table"):
        build(env, overrides_path=str(ov))


def test_a_pending_bar_cap_refuses_the_build(env, tmp_path):
    from ba2_providers.ohlcv.alpaca_history.split_table import SplitOverridesPending
    with pytest.raises(SplitOverridesPending):
        _cap_env(env, tmp_path, [_cap("High", reviewed_by="pending")])


def test_enov_split_and_spinoff_on_one_day_adjust_to_0_574_of_the_split_only_series():
    d = pd.DataFrame({"Date": pd.to_datetime(["2022-04-04", "2022-04-05"]), "Open": [30.0, 51.6], "High": [30.0, 51.6], "Low": [30.0, 51.6],
                      "Close": [30.0, 51.6], "Volume": [100, 100]})
    both = bld.adjust(d, [("2022-04-05", 1 / 3)], [("2022-04-05", 1.743)])
    split_only = bld.adjust(d, [("2022-04-05", 1 / 3)])
    assert (both["Close"].iloc[0] / split_only["Close"].iloc[0]) == pytest.approx(1 / 1.743) and 1 / 1.743 == pytest.approx(0.5737, abs=1e-4)
    assert both["Volume"].iloc[0] == split_only["Volume"].iloc[0]                     # a spin-off leaves the share count alone
    assert both["Close"].iloc[1] == 51.6


def test_the_gate_certificate_reads_the_manifest_the_build_writes(env):
    # the certificate is tied to the manifest a REAL build writes (a guessed schema once crashed the full gate: KeyError run_id)
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    daily, intr, out = env
    days = sessions("2024-01-02", "2024-01-05")
    write_symbol(daily, intr, "ABC", days)
    make_table(out)
    build(env)
    meta = str(out / "_meta")
    cert = ver.write_gate_certificate(meta, {"structural": {"status": ver.STATUS_PASS}})
    assert cert["status"] == ver.STATUS_PASS and cert["build_run_id"].startswith("RUN1 built ")
    assert ver.read_gate_certificate(meta)["build_manifest_sha256"] == cert["build_manifest_sha256"]
    write_symbol(daily, intr, "ABD", days)                                # a rebuild after the gate with other content (a rebuild of
    build(env)                                                            # identical bytes in the same second IS the certified build)
    with pytest.raises(ver.GateCertificateError, match="re-run the gate"):
        ver.read_gate_certificate(meta)
    ver.write_gate_certificate(meta, {"yahoo": {"status": ver.STATUS_FAIL}})
    with pytest.raises(ver.GateCertificateError, match="FAILED"):
        ver.read_gate_certificate(meta)


# ---------------------------------------------------------------------------
# exchange suspensions / delistings (_meta/suspensions.json)
# ---------------------------------------------------------------------------
def _susp_env(env, tmp_path, sym, drop, fmp_days="all", fmp_volume=500, end="2026-10-12", fmp_tz=None, ipo="absent", intraday=False):
    """One symbol with bars every session 2024-01-02..``end`` except the ``drop`` days; an FMP file over ``fmp_days`` (all sessions).
    ``ipo``: "absent" = no ipo_dates.json; else the profile ipoDate of the symbol (None = a profile without one) in that file."""
    daily, intr, out = env
    if ipo != "absent":
        _write_ipo(out, {sym: ipo})
    all_days = sessions("2024-01-02", end)
    i_days = sessions("2024-02-01", "2024-02-29") + sessions("2024-05-16", "2024-06-14") if intraday else ()
    write_symbol(daily, intr, sym, [d for d in all_days if d not in set(drop)], intraday_days=i_days)
    make_table(out)
    fmp = tmp_path / "fmp"
    fmp.mkdir(exist_ok=True)
    if fmp_days is not None:
        fd = all_days if fmp_days == "all" else fmp_days
        dates = pd.to_datetime(fd) if fmp_tz is None else pd.to_datetime(fd).tz_localize(fmp_tz)
        pd.DataFrame({"Date": dates, "Volume": [fmp_volume] * len(fd)}).to_parquet(fmp / f"{sym}_1d.parquet", index=False)
    s = bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, fmp_daily_dir=str(fmp))
    f = json.loads((out / "_meta" / "suspensions.json").read_text())
    return f, s


def _write_ipo(out, symbols):
    (out / "_meta").mkdir(parents=True, exist_ok=True)
    (out / "_meta" / "ipo_dates.json").write_text(json.dumps({"fetched_at": "x", "source": "test", "symbols": symbols}))


def test_an_otc_run_with_fmp_trading_is_a_suspension(env, tmp_path):
    drop = sessions("2024-03-01", "2024-05-15")                         # 55 sessions with no Alpaca bar, FMP traded every day
    f, s = _susp_env(env, tmp_path, "NESR", drop)
    assert f["build"].startswith("RUN1 built ") and f["rule"]["min_sessions"] == 20
    (last, resume, kind, missing, share), = f["symbols"]["NESR"]
    assert (last, resume, kind, share) == ("2024-02-29", "2024-05-16", "otc_suspension", 1.0) and missing == len(drop) - 1     # Good Friday 03-29 is no session
    m = json.loads((env[2] / "_meta" / "build_manifest.json").read_text())
    assert m["suspension_events"] == 1 and s["suspension_events"] == 1 and s["symbols_with_suspensions"] == 1


def test_a_gap_without_fmp_trading_or_fmp_file_is_an_exchange_gap_event(env, tmp_path):
    # owner decision 2026-10-22: EVERY >= 20-session run without an Alpaca bar is an event; otc_suspension only when FMP traded through it
    drop = sessions("2024-03-01", "2024-05-15")
    want = ["2024-02-29", "2024-05-16", "exchange_gap", len(drop) - 1]
    f, _ = _susp_env(env, tmp_path, "THIN", drop, fmp_volume=0)         # FMP has rows but no volume: judged, share 0.0
    assert f["symbols"]["THIN"] == [want + [0.0]]
    f, _ = _susp_env(env, tmp_path, "NOF", drop, fmp_days=None)         # no FMP file at all: share unknown
    assert f["symbols"]["NOF"] == [want + [None]]
    f, _ = _susp_env(env, tmp_path, "PART", drop, fmp_days=sessions("2024-01-02", "2024-03-20"))   # FMP does not cover the run
    assert f["symbols"]["PART"] == [want + [None]]
    f, _ = _susp_env(env, tmp_path, "FULL", drop, fmp_volume=500)       # FMP traded every session: the stricter kind
    assert f["symbols"]["FULL"][0][2] == "otc_suspension"
    assert "undecided" not in f


def test_a_short_gap_is_not_a_suspension(env, tmp_path):
    f, _ = _susp_env(env, tmp_path, "SHORT", sessions("2024-03-01", "2024-03-26"))    # 18 sessions < 20
    assert f["symbols"] == {}
    f, _ = _susp_env(env, tmp_path, "SHORT", sessions("2024-03-01", "2024-03-28"))    # 20 sessions (Good Friday is 03-29)
    assert [e[3] for e in f["symbols"]["SHORT"]] == [20]


def test_a_symbol_whose_bars_stop_long_before_the_store_end_is_delisted(env, tmp_path):
    f, _ = _susp_env(env, tmp_path, "GONE", sessions("2024-06-03", "2026-10-12"), fmp_days=None)
    (last, resume, kind, missing, share), = f["symbols"]["GONE"]
    assert (last, resume, kind, share) == ("2024-05-31", None, "delisted", None) and missing > 5
    # a tail quiet for <= 5 sessions is inside the slack
    f, _ = _susp_env(env, tmp_path, "QUIET", sessions("2026-10-06", "2026-10-12"), fmp_days=None)
    assert "QUIET" not in f["symbols"]


def test_subset_build_merges_suspensions_like_the_manifest(env, tmp_path):
    drop = sessions("2024-03-01", "2024-05-15")
    _susp_env(env, tmp_path, "AAA", drop)
    daily, intr, out = env
    write_symbol(daily, intr, "BBB", sessions("2024-01-02", "2026-10-12"))
    bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, symbols=["BBB"], fmp_daily_dir=str(tmp_path / "fmp"))
    f = json.loads((out / "_meta" / "suspensions.json").read_text())
    assert sorted(f["symbols"]) == ["AAA"] and f["symbols"]["AAA"][0][2] == "otc_suspension"      # AAA kept, BBB has none


def test_an_fmp_file_with_utc_midnight_labels_reads_as_its_wall_dates(env, tmp_path):
    # 11k FMP daily files (945 symbols this cache builds, mostly ETFs: SPMO, BRKH...) carry tz-aware UTC midnights; compared
    # with naive days they once matched nothing (witness / step / gate silently off) and crashed the suspension detector
    drop = sessions("2024-03-01", "2024-05-15")
    f, _ = _susp_env(env, tmp_path, "SPMO", drop, fmp_tz="UTC")
    (last, resume, kind, _missing, share), = f["symbols"]["SPMO"]
    assert (last, resume, kind, share) == ("2024-02-29", "2024-05-16", "otc_suspension", 1.0)


def test_fmp_naive_dates_keeps_daily_labels_converts_intraday_and_refuses_a_timed_daily_stamp():
    from ba2_providers.ohlcv.alpaca_history.split_build import fmp_naive_dates
    utc = pd.Series(pd.to_datetime(["2024-03-01", "2024-03-04"]).tz_localize("UTC"))
    assert list(fmp_naive_dates(utc, False, "x")) == list(pd.to_datetime(["2024-03-01", "2024-03-04"]))     # not 02-29 20:00
    bar = pd.Series(pd.to_datetime(["2024-03-01 14:30"]).tz_localize("UTC"))
    assert list(fmp_naive_dates(bar, True, "x")) == [pd.Timestamp("2024-03-01 09:30")]
    with pytest.raises(ValueError, match="not a midnight"):
        fmp_naive_dates(bar, False, "x")
    naive = pd.Series(pd.to_datetime(["2024-03-01"]))
    assert fmp_naive_dates(naive, False, "x") is naive


def test_detect_exchange_events_returns_a_flat_event_list():
    dates = pd.to_datetime(sessions("2024-01-02", "2024-02-29") + sessions("2024-05-16", "2026-10-12"))
    ev = bld.detect_exchange_events(dates, None, "2026-10-12")
    assert ev == [["2024-02-29", "2024-05-16", "exchange_gap", ev[0][3], None]] and ev[0][3] >= 20


def test_event_counts_by_kind_are_in_the_summary_and_the_manifest(env, tmp_path):
    f, s = _susp_env(env, tmp_path, "THIN", sessions("2024-03-01", "2024-05-15"), fmp_days=None)
    m = json.loads((env[2] / "_meta" / "build_manifest.json").read_text())
    assert s["suspension_events"] == 1 and s["suspension_events_by_kind"] == {"exchange_gap": 1} and m["suspension_events"] == 1


# ---------------------------------------------------------------------------
# reused tickers (_meta/reused_tickers.json, input _meta/ipo_dates.json)
# ---------------------------------------------------------------------------
GAP = sessions("2024-03-01", "2024-05-15")                    # resume 2024-05-16


def _reused(env):
    return json.loads((env[2] / "_meta" / "reused_tickers.json").read_text())


def test_a_ticker_whose_company_listed_at_the_end_of_the_gap_drops_the_bars_before_it(env, tmp_path):
    f, s = _susp_env(env, tmp_path, "VG", GAP, ipo="2024-05-16", intraday=True, fmp_days=None)
    daily, intr, out = env
    d1 = pd.read_parquet(out / "VG_1d.parquet")
    d5 = pd.read_parquet(out / "VG_5min.parquet")
    assert str(d1["Date"].iloc[0].date()) == "2024-05-16" and str(d5["Date"].iloc[0].date()) == "2024-05-16"      # both series start at the relist
    assert d5["Date"].dt.normalize().nunique() == len(sessions("2024-05-16", "2024-06-14")) and not (d5["Date"].dt.normalize() < "2024-05-16").any()
    r = _reused(env)["reused"]["VG"]
    assert r["gap"] == ["2024-02-29", "2024-05-16"] and r["missing_sessions"] == len(GAP) - 1
    assert r["dropped"]["last"] == "2024-02-29" and r["dropped"]["first"] == "2024-01-02" and r["dropped"]["1d"] > 0 and r["dropped"]["5min"] > 0
    assert r["evidence"]["ipo_date"] == "2024-05-16" and r["evidence"]["days_from_resume"] == 0
    assert "VG" not in f["symbols"] and _reused(env)["ambiguous"] == {}                  # the gap is gone with the old company's bars
    assert s["reused_tickers"] == ["VG"]
    m = json.loads((out / "_meta" / "build_manifest.json").read_text())
    assert m["reused_tickers"] == 1 and m["symbols"]["VG"]["1d"]["first"] == "2024-05-16"


def test_the_ipo_date_may_be_a_few_days_either_side_of_the_resume(env, tmp_path):
    _susp_env(env, tmp_path, "VG", GAP, ipo="2024-05-20")                                 # +4 days
    assert "VG" in _reused(env)["reused"]
    _susp_env(env, tmp_path, "VG", GAP, ipo="2024-05-12")                                 # -4 days
    assert "VG" in _reused(env)["reused"]
    _susp_env(env, tmp_path, "VG", GAP, ipo="2024-05-09")                                 # -7 days: the tolerance edge
    assert "VG" in _reused(env)["reused"]
    _susp_env(env, tmp_path, "VG", GAP, ipo="2024-05-08")                                 # -8 days: ambiguous, not reused
    assert _reused(env)["reused"] == {}


def test_the_same_company_after_a_long_suspension_keeps_its_history_and_the_event(env, tmp_path):
    f, _ = _susp_env(env, tmp_path, "NBIS", GAP, fmp_days=None, ipo="2011-05-24")
    d1 = pd.read_parquet(env[2] / "NBIS_1d.parquet")
    assert str(d1["Date"].iloc[0].date()) == "2024-01-02"
    assert f["symbols"]["NBIS"][0][:3] == ["2024-02-29", "2024-05-16", "exchange_gap"]
    assert _reused(env)["reused"] == {} and _reused(env)["ambiguous"] == {}               # judged: not listed anywhere


@pytest.mark.parametrize("ipo,reason", [(None, "no_ipo_date"), ("2024-04-10", "ipo_inside_gap"), ("2024-08-01", "ipo_after_resume"),
                                         ("absent", "no_ipo_dates_file")])
def test_ambiguous_evidence_is_not_truncated_and_is_listed_for_review(env, tmp_path, ipo, reason):
    f, s = _susp_env(env, tmp_path, "AMB", GAP, ipo=ipo)
    d1 = pd.read_parquet(env[2] / "AMB_1d.parquet")
    assert str(d1["Date"].iloc[0].date()) == "2024-01-02"                                # never guess: nothing dropped
    assert f["symbols"]["AMB"][0][2] in ("exchange_gap", "otc_suspension")             # the gap is still an event
    assert _reused(env)["reused"] == {} and _reused(env)["ambiguous"]["AMB"] == [["2024-02-29", "2024-05-16", len(GAP) - 1, reason]]
    assert s["gaps_for_review"]["AMB"][0][3] == reason and (s["ipo_dates_file"] is None) == (ipo == "absent")


def test_only_gaps_after_the_reuse_cut_are_events_and_the_old_ones_go_with_the_old_company(env, tmp_path):
    daily, intr, out = env
    all_days = sessions("2024-01-02", "2026-10-12")
    drop = set(sessions("2024-03-01", "2024-05-15")) | set(sessions("2025-03-03", "2025-05-30"))
    write_symbol(daily, intr, "TWO", [d for d in all_days if d not in drop])
    make_table(out)
    _write_ipo(out, {"TWO": "2025-06-02"})                                                # the SECOND gap's end
    bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1)
    f = json.loads((out / "_meta" / "suspensions.json").read_text())
    assert _reused(env)["reused"]["TWO"]["gap"][1] == "2025-06-02" and "TWO" not in f["symbols"]      # both old gaps are before the cut
    _write_ipo(out, {"TWO": "2024-05-16"})                                                # the FIRST gap's end: the second gap is after the cut
    bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1)
    f = json.loads((out / "_meta" / "suspensions.json").read_text())
    assert _reused(env)["reused"]["TWO"]["gap"][1] == "2024-05-16"
    assert [e[:2] for e in f["symbols"]["TWO"]] == [["2025-02-28", "2025-06-02"]] and _reused(env)["ambiguous"] == {}   # ipo <= its first bar: same company


def test_a_malformed_ipo_dates_file_stops_the_build(env, tmp_path):
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out)
    (out / "_meta").mkdir(exist_ok=True)
    (out / "_meta" / "ipo_dates.json").write_text(json.dumps({"symbols": {"ABC": "not-a-date"}}))
    with pytest.raises(Exception, match="malformed"):
        bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1)


def test_subset_build_does_not_merge_entries_judged_against_another_fmp_dir(env, tmp_path):
    drop = sessions("2024-03-01", "2024-05-15")
    _susp_env(env, tmp_path, "AAA", drop)
    daily, intr, out = env
    write_symbol(daily, intr, "BBB", sessions("2024-01-02", "2026-10-12"))
    other = tmp_path / "fmp_other"
    other.mkdir()
    bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, symbols=["BBB"], fmp_daily_dir=str(other))
    m = json.loads((out / "_meta" / "build_manifest.json").read_text())
    assert sorted(m["symbols"]) == ["BBB"] and m["fmp_daily_dir"] == str(other)       # AAA's entries are not carried over
    f = json.loads((out / "_meta" / "suspensions.json").read_text())
    assert f["symbols"] == {}


def test_subset_build_does_not_merge_an_old_manifest_without_reused_ticker_entries(env, tmp_path):
    _susp_env(env, tmp_path, "AAA", sessions("2024-03-01", "2024-05-15"))
    daily, intr, out = env
    mp = out / "_meta" / "build_manifest.json"
    m = json.loads(mp.read_text())
    del m["symbols"]["AAA"]["reused_ticker"]
    mp.write_text(json.dumps(m))
    write_symbol(daily, intr, "BBB", sessions("2024-01-02", "2026-10-12"))
    bld.build_cache(daily_store=str(daily), intraday_store=str(intr), out=str(out), workers=1, symbols=["BBB"], fmp_daily_dir=str(tmp_path / "fmp"))
    assert sorted(json.loads(mp.read_text())["symbols"]) == ["BBB"]


def test_the_gate_certificate_also_certifies_the_suspension_events(env):
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out)
    build(env)
    meta = out / "_meta"
    cert = ver.write_gate_certificate(str(meta), {"structural": {"status": ver.STATUS_PASS}})
    assert len(cert["suspensions_sha256"]) == 64 and ver.read_gate_certificate(str(meta)) == cert
    f = json.loads((meta / "suspensions.json").read_text())
    f["symbols"]["ABC"] = [["2024-01-03", None, "delisted", 9, None]]
    (meta / "suspensions.json").write_text(json.dumps(f))                      # edited / older copy after the gate
    with pytest.raises(ver.GateCertificateError, match="suspension events.*re-run the gate"):
        ver.read_gate_certificate(str(meta))
    (meta / "suspensions.json").unlink()
    with pytest.raises(ver.GateCertificateError, match="re-run the gate"):
        ver.read_gate_certificate(str(meta))
    with pytest.raises(ver.GateCertificateError, match="suspensions.json not found"):
        ver.write_gate_certificate(str(meta), {"structural": {"status": ver.STATUS_PASS}})


def test_a_certificate_without_suspensions_sha256_is_refused(env):
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    daily, intr, out = env
    write_symbol(daily, intr, "ABC", sessions("2024-01-02", "2024-01-05"))
    make_table(out)
    build(env)
    meta = out / "_meta"
    ver.write_gate_certificate(str(meta), {"structural": {"status": ver.STATUS_PASS}})
    cp = meta / "gate_certificate.json"
    c = json.loads(cp.read_text())
    del c["suspensions_sha256"]
    cp.write_text(json.dumps(c))
    with pytest.raises(ver.GateCertificateError, match="predates"):
        ver.read_gate_certificate(str(meta))
