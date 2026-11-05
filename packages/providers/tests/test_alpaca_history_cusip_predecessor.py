"""CUSIP-only change whose new security restarted Alpaca's history: the predecessor series (key SYMBOL@asof) is merged by the build
strictly before the change, and refused when it overlaps or the seam does not line up.  Synthetic stores."""
import csv
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from ba2_providers.ohlcv.alpaca_history import build as bld
from ba2_providers.ohlcv.alpaca_history.split_table import (
    cusip_only_changes, cusip_predecessor_rows, predecessor_key, split_predecessor_key)

NY = "America/New_York"
CUT = dt.date(2026, 9, 14)
KEY = "TEVA@2026-09-13"


def ny_utc(ts):
    return pd.Timestamp(ts).tz_localize(NY).tz_convert("UTC")


def daily(days, close):
    return pd.DataFrame([{"t": ny_utc(d), "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 1000, "n": 5, "vw": close}
                         for d in days])


def intraday(days, close):
    rows = []
    for d in days:
        for s in pd.date_range(f"{d} 09:30", f"{d} 15:55", freq="5min"):
            rows.append({"t": ny_utc(s), "o": close, "h": close + .5, "l": close - .5, "c": close, "v": 100, "n": 1, "vw": close})
    return pd.DataFrame(rows)


def bdays(a, b):
    return [d.date().isoformat() for d in pd.bdate_range(a, b)]


def put(root, key, days, close, five=False, asof=None):
    p = Path(root) / "bars" / "1Day" / f"symbol={key}"
    p.mkdir(parents=True, exist_ok=True)
    daily(days, close).to_parquet(p / "all.parquet", index=False)
    (Path(root) / "manifest").mkdir(exist_ok=True)
    mpath = Path(root) / "manifest" / f"symbol={key}.json"
    m = json.loads(mpath.read_text()) if mpath.exists() else {"chunks": {}}
    m["chunks"]["1Day/all"] = {"status": "complete", "sha256": "d" + key, "bars": len(days), **({"asof": asof} if asof else {})}
    if five:
        q = Path(root) / "bars" / "5Min" / f"symbol={key}"
        q.mkdir(parents=True, exist_ok=True)
        intraday(days, close).to_parquet(q / "2026.parquet", index=False)
        m["chunks"]["5Min/2026"] = {"status": "complete", "sha256": "i" + key, "bars": 78 * len(days), **({"asof": asof} if asof else {})}
    mpath.write_text(json.dumps(m))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def table(out):
    from ba2_providers.ohlcv.alpaca_history.split_table import OVERRIDES_PATH
    meta = Path(out) / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "split_table.json").write_text(json.dumps({"run_id": "R", "asof": "2026-10-12", "inputs_sha256": "a",
                                                       "overrides_sha256": sha(OVERRIDES_PATH), "splits": {}, "spinoffs": {}, "breaks": {}}))
    for n in bld.RUN_ID_CSVS:
        with open(meta / n, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "run_id"])
            w.writerow(["X", "R"])


def actions(store, changes=(), extra=None):
    d = Path(store) / "corporate_actions"
    d.mkdir(exist_ok=True)
    month = {"name_changes": [{"old_symbol": s, "new_symbol": s, "old_cusip": "OLD", "new_cusip": "NEW", "process_date": p}
                              for s, p in changes], **(extra or {})}
    with gzip.open(d / "ym=2026-09.json.gz", "wt", encoding="utf-8") as f:
        json.dump(month, f)


def fmp(path, days, close):
    Path(path).mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"Date": pd.to_datetime(days), "Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1000.0,
                  "effective_date": pd.to_datetime(days)}).to_parquet(Path(path) / "TEVA_1d.parquet", index=False)


@pytest.fixture
def env(tmp_path):
    d, i, o, f = tmp_path / "daily", tmp_path / "intra", tmp_path / "out", tmp_path / "fmp"
    for s in (d, i):
        (s / "bars" / "1Day").mkdir(parents=True)
        (s / "bars" / "5Min").mkdir(parents=True)
        (s / "STORE_INFO.json").write_text(json.dumps({"asof": "2026-10-12"}))
    o.mkdir()
    table(o)
    actions(d, [("TEVA", "2026-09-14")])
    pre, cur = bdays("2026-08-03", "2026-09-11"), bdays("2026-09-14", "2026-09-30")
    put(d, "TEVA", cur, 20.0)
    put(i, "TEVA", cur, 20.0, five=True)
    put(d, KEY, pre, 20.0, asof="2026-09-13")
    put(i, KEY, pre, 20.0, five=True, asof="2026-09-13")
    put(i, "TEVA", cur, 20.0, five=True)
    fmp(f, pre + cur, 20.0)
    return d, i, o, f, pre, cur


def run(env_, **kw):
    d, i, o, f = env_[:4]
    return bld.build_cache(daily_store=str(d), intraday_store=str(i), out=str(o), workers=1, fmp_daily_dir=str(f), **kw)


# ---------------------------------------------------------------------------
# keys and the derived list
# ---------------------------------------------------------------------------
def test_predecessor_key_round_trip():
    assert predecessor_key("teva", CUT) == KEY
    assert split_predecessor_key(KEY) == ("TEVA", CUT)
    assert split_predecessor_key("TEVA") == ("TEVA", None)


def test_only_a_change_where_history_restarts_is_listed():
    months = [{"name_changes": [
        {"old_symbol": "TEVA", "new_symbol": "TEVA", "old_cusip": "A", "new_cusip": "B", "process_date": "2026-09-14"},
        {"old_symbol": "OLDY", "new_symbol": "OLDY", "old_cusip": "A", "new_cusip": "B", "process_date": "2022-01-10"},    # long history before
        {"old_symbol": "IPOX", "new_symbol": "IPOX", "old_cusip": "A", "new_cusip": "B", "process_date": "2024-06-05"},    # IPO before the change
        {"old_symbol": "SAME", "new_symbol": "SAME", "old_cusip": "A", "new_cusip": "A", "process_date": "2026-09-14"},   # not a CUSIP change
        {"old_symbol": "REN", "new_symbol": "NEWR", "old_cusip": "A", "new_cusip": "B", "process_date": "2026-09-14"}]}]   # a ticker rename
    changes = cusip_only_changes(months)
    assert sorted(changes) == ["IPOX", "OLDY", "TEVA"]
    first = {"TEVA": dt.date(2026, 9, 14), "OLDY": dt.date(2016, 1, 4), "IPOX": dt.date(2024, 5, 29)}
    rows = cusip_predecessor_rows(changes, first, lambda k: False)
    assert [(r["symbol"], r["key"], r["in_daily"], r["in_5min"]) for r in rows] == [("TEVA", KEY, "no", "unknown")]
    assert cusip_predecessor_rows(changes, first, lambda k: True, lambda k: False)[0]["in_daily"] == "yes"


def test_downloader_key_parsing_and_scope():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dl", Path(__file__).resolve().parents[3] / "tools" / "alpaca_history_download.py")
    dl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dl)
    assert dl.split_predecessor_key(KEY) == ("TEVA", "2026-09-13")
    assert dl.split_predecessor_key("BRK-B") == ("BRK-B", None)
    assert dl.alpaca_symbol(KEY) == "TEVA" and dl.alpaca_symbol("BRK-B@2020-01-01") == "BRK.B"
    with pytest.raises(dl.FatalError):
        dl.split_predecessor_key("TEVA@notadate")
    run_ = dl.Downloader(Path("."), None, start=dl.parse_day("2016-01-01"), end_exclusive=dl.parse_day("2026-10-13"), asof="2026-10-12",
                         retry_refused=False, max_consecutive_failures=5, max_gap_days=5)
    assert run_._scope("TEVA") == ("2026-10-12", dl.parse_day("2026-10-13"))
    assert run_._scope(KEY) == ("2026-09-13", dl.parse_day("2026-09-14"))          # the old series ends where the change takes effect
    with pytest.raises(dl.FatalError):
        run_._scope("TEVA@2026-10-20")


def test_a_predecessor_key_is_not_a_symbol_of_the_build(env):
    d, i = env[:2]
    assert bld.list_symbols(str(d), str(i)) == ["TEVA"]


# ---------------------------------------------------------------------------
# the merge
# ---------------------------------------------------------------------------
def test_old_and_new_series_are_one_file_with_no_overlap(env):
    d, i, o, f, pre, cur = env
    run(env)
    out = pd.read_parquet(o / "TEVA_1d.parquet")
    assert len(out) == len(pre) + len(cur)
    assert out["Date"].is_monotonic_increasing and not out["Date"].duplicated().any()
    assert str(out["Date"].iloc[0].date()) == pre[0] and str(out["Date"].iloc[len(pre) - 1].date()) == pre[-1]
    assert str(out["Date"].iloc[len(pre)].date()) == cur[0]
    m5 = pd.read_parquet(o / "TEVA_5min.parquet")
    assert len(m5) == 78 * (len(pre) + len(cur)) and not m5["Date"].duplicated().any()
    entry = json.loads((o / "_meta" / "build_manifest.json").read_text())["symbols"]["TEVA"]
    assert entry["predecessor"]["key"] == KEY and entry["predecessor"]["daily_bars"] == len(pre)
    assert entry["predecessor"]["seam"]["relative_difference"] == pytest.approx(0.0)
    assert not (o / "TEVA@2026-09-13_1d.parquet").exists()


def test_old_bars_on_or_after_the_change_are_cut_never_merged(env):
    d, i, o, f, pre, cur = env
    put(d, KEY, pre + cur[:3], 20.0, asof="2026-09-13")           # a fetch that ran past the change
    run(env)
    out = pd.read_parquet(o / "TEVA_1d.parquet")
    assert len(out) == len(pre) + len(cur) and not out["Date"].duplicated().any()
    entry = json.loads((o / "_meta" / "build_manifest.json").read_text())["symbols"]["TEVA"]
    assert entry["predecessor"]["dropped_on_or_after_cut"] == 3


def test_the_predecessor_changes_the_input_digest(env):
    d, i, o, f, pre, cur = env
    run(env)
    first = json.loads((o / "_meta" / "build_manifest.json").read_text())["symbols"]["TEVA"]["inputs_sha256"]
    m = json.loads((d / "manifest" / f"symbol={KEY}.json").read_text())
    m["chunks"]["1Day/all"]["sha256"] = "other"
    (d / "manifest" / f"symbol={KEY}.json").write_text(json.dumps(m))
    run(env)
    assert json.loads((o / "_meta" / "build_manifest.json").read_text())["symbols"]["TEVA"]["inputs_sha256"] != first


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
def test_refused_when_the_current_series_already_has_bars_before_the_change(env):
    d, i, o, f, pre, cur = env
    put(d, "TEVA", pre[-3:] + cur, 20.0)
    with pytest.raises(bld.BuildError, match="TEVA.*before the change|overlap"):
        run(env)


def test_refused_when_the_seam_does_not_line_up_with_fmp(env):
    d, i, o, f, pre, cur = env
    put(d, KEY, pre, 40.0, asof="2026-09-13")                      # the old security trades at twice the new one: a 2:1 share ratio
    put(i, KEY, pre, 40.0, five=True, asof="2026-09-13")
    with pytest.raises(bld.BuildError, match="seam does not line up"):
        run(env)


def test_refused_without_an_fmp_witness(env):
    d, i, o, f, pre, cur = env
    (f / "TEVA_1d.parquet").unlink()
    with pytest.raises(bld.BuildError, match="no FMP daily file"):
        run(env)


def test_refused_when_fmp_covers_too_few_sessions_of_a_side(env):
    d, i, o, f, pre, cur = env
    fmp(f, pre[:2] + cur, 20.0)
    with pytest.raises(bld.BuildError, match="need 3"):
        run(env)


def test_refused_when_the_key_is_not_a_cusip_change_in_the_corporate_actions(env):
    d, i, o, f, pre, cur = env
    actions(d, [("TEVA", "2026-09-20")])
    with pytest.raises(bld.BuildError, match="no CUSIP-only name change"):
        run(env)


def test_refused_when_a_predecessor_chunk_was_fetched_with_another_asof(env):
    d, i, o, f, pre, cur = env
    put(d, KEY, pre, 20.0, asof="2026-10-12")
    with pytest.raises(bld.BuildError, match="not fetched as of 2026-09-13"):
        run(env)


def test_refused_when_a_split_inside_the_old_series_was_never_judged(env):
    d, i, o, f, pre, cur = env
    actions(d, [("TEVA", "2026-09-14")], extra={"forward_splits": [{"symbol": "TEVA", "ex_date": "2026-08-20", "new_rate": 2, "old_rate": 1}]})
    with pytest.raises(bld.BuildError, match="never judged"):
        run(env)
