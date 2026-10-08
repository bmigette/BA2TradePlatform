"""Launch- and job-start preflight: an intraday-reading job REFUSES a universe holding a symbol whose
intraday cache is on a different price level than its daily cache.

THE DEFECT (measured 2026-10-08): 7 of 250 random symbols had a 5-minute history a constant multiple (0.10 ..
6.25) of the daily one for a whole year; an intraday-clock backtest reads its decision price from the 5-minute
bars and its history (indicators, ATR, highs, TP/SL anchors) from the daily bars, so such a symbol was
silently wrong, and no launch check compared the two intervals' price levels.

WHO IS CHECKED, and what is not:
  * an intraday-clock job (``execution_interval`` sub-daily)                 -> checked, ``execution_interval``
  * a daily-clock job that prices OPTIONS (``options_cache_db``)             -> checked, 5m (the post-hoc
    intraday drawdown refinement re-prices flagged trades on the underlying's 5-minute bars)
  * a daily-clock EQUITY job                                                 -> reads no intraday bar: untouched
"""
from __future__ import annotations

import inspect
import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from app.services.backtest import intraday_basis_preflight as pf
from app.services.job_fatal import JOB_FATAL_ERROR_TYPES, job_fatal
from ba2_common.core import native_cache, split_basis
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.split_basis import IntradayBasisMismatch, IntradayBasisStale
from ba2_providers.ohlcv import cross_interval_basis as cib

PROV = "FMPOHLCVProvider"


def _pair(first=date(2023, 1, 3), last=date(2023, 12, 29), seed=5):
    rng = np.random.default_rng(seed)
    price = 40.0
    d, i = [], []
    for _o, c in nyse_regular_sessions(first, last):
        local = c.astimezone(NY_TZ)
        n = (local.hour * 60 + local.minute - 570) // 5
        path = price * np.exp(np.cumsum(rng.normal(0.0001, 0.0006, n)))
        price = float(path[-1])
        stamps = [pd.Timestamp(local.date()) + pd.Timedelta(minutes=570 + 5 * k) for k in range(n)]
        i.append(pd.DataFrame({"Date": stamps, "Open": path, "High": path * 1.0003, "Low": path * 0.9997,
                               "Close": path, "Volume": 1000.0}))
        d.append({"Date": pd.Timestamp(local.date()), "Open": float(path[0]), "High": float(path.max() * 1.0003),
                  "Low": float(path.min() * 0.9997), "Close": float(path[-1]), "Volume": 1e6})
    return pd.DataFrame(d), pd.concat(i, ignore_index=True)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    (root / PROV).mkdir(parents=True)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(root))
    pf.reset_job_memo()
    cib._STORES.clear()
    yield root / PROV
    pf.reset_job_memo()
    cib._STORES.clear()


def _put(folder, symbol, factor=1.0):
    daily, intra = _pair()
    for c in ("Open", "High", "Low", "Close"):
        intra[c] = intra[c] * factor
    daily.assign(effective_date=daily["Date"]).to_parquet(folder / f"{symbol}_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(folder / f"{symbol}_5min.parquet", index=False)


def cfg(symbols, interval="5min", options=False):
    c = {"enabled_instruments": list(symbols), "execution_interval": interval,
         "start_date": datetime(2023, 3, 1), "end_date": datetime(2023, 12, 29), "warmup_days": 30}
    if options:
        c["options_cache_db"] = "/tmp/x.db"
    return c


# ------------------------------------------------------------------------------------------ who is checked
def test_a_daily_clock_equity_job_reads_no_intraday_bar_and_is_untouched(cache):
    _put(cache, "BAD", 0.5)
    assert pf.interval_to_check(cfg(["BAD"], "1d")) is None
    assert pf.require_for_config(cfg(["BAD"], "1d")) is None          # even with a wrong-basis 5-minute file


def test_an_intraday_clock_job_and_a_daily_clock_options_job_are_checked(cache):
    assert pf.interval_to_check(cfg(["A"], "5min")) == "5min"
    assert pf.interval_to_check(cfg(["A"], "15min")) == "15min"
    assert pf.interval_to_check(cfg(["A"], "1d", options=True)) == "5m"
    _put(cache, "OPT", 0.5)
    with pytest.raises(IntradayBasisMismatch, match="OPT"):
        pf.require_for_config(cfg(["OPT"], "1d", options=True))


# ------------------------------------------------------------------------------------------ the refusal
def test_a_healthy_universe_passes_and_the_report_is_recorded_on_the_result(cache):
    _put(cache, "GOOD1")
    _put(cache, "GOOD2")
    rep = pf.require_for_config(cfg(["GOOD1", "GOOD2"]))
    assert rep is not None and not rep.refused and rep.counts["ok"] == 2
    d = rep.to_dict()
    assert d["symbols"] == 2 and d["interval"] == "5min" and d["unjudged"] == [] and d["unjudged_count"] == 0


def test_a_mismatched_symbol_refuses_with_symbol_factor_and_class(cache):
    _put(cache, "GOOD")
    _put(cache, "DDLIKE", 1 / 3)
    _put(cache, "SIRILIKE", 0.1)
    with pytest.raises(IntradayBasisMismatch) as ei:
        pf.require_for_config(cfg(["GOOD", "DDLIKE", "SIRILIKE"]))
    msg = str(ei.value)
    assert "2 of 3 symbols" in msg
    assert "DDLIKE: constant_factor x0.333" in msg and "SIRILIKE: constant_factor x0.1" in msg
    assert "GOOD:" not in msg
    assert "--exclude-symbols" in msg            # the recorded way to run without them: the only escape


def test_the_warmup_range_is_part_of_the_window(cache):
    # bad ONLY in January 2023; the job window starts 2023-03-01 with 30 warm-up days -> 2023-01-30..
    daily, intra = _pair()
    jan = intra["Date"] < "2023-02-15"
    for c in ("Open", "High", "Low", "Close"):
        intra.loc[jan, c] = intra.loc[jan, c] * 4.0
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / "EARLY_1d.parquet", index=False)
    intra.assign(effective_date=intra["Date"]).to_parquet(cache / "EARLY_5min.parquet", index=False)
    pf.reset_job_memo()
    with pytest.raises(IntradayBasisMismatch, match="EARLY"):
        pf.require_for_config({**cfg(["EARLY"]), "warmup_days": 45})   # reads back into the defect
    pf.reset_job_memo()
    assert pf.require_for_config({**cfg(["EARLY"]), "warmup_days": 0}) is not None  # starts after it


def test_a_stale_marker_refuses_even_when_the_prices_agree(cache):
    _put(cache, "MARKED")
    split_basis.write_intraday_stale(str(cache / "MARKED_5min.parquet"), reason="daily history replaced")
    with pytest.raises(IntradayBasisStale, match="MARKED"):
        pf.require_for_config(cfg(["MARKED"]))


def test_symbols_that_cannot_be_judged_are_reported_never_ok_and_not_refused(cache):
    _put(cache, "GOOD")
    rep = pf.require_for_config(cfg(["GOOD", "NOFILES"]))
    assert rep.counts["ok"] == 1 and rep.counts["no_daily"] == 1
    assert [r.symbol for r in rep.unjudged] == ["NOFILES"]
    assert rep.to_dict()["unjudged"][0]["class"] == "no_daily"


# ------------------------------------------------------------------------------------------ cost
def test_the_verdict_is_computed_once_per_process_per_job_not_per_trial(cache):
    _put(cache, "AAA")
    _put(cache, "BBB")
    store = cib.store_for(PROV)
    pf.require_for_config(cfg(["AAA", "BBB"]))
    reads = store.reads
    assert reads == 2
    for _ in range(50):                                            # 50 GA trials of the same job
        pf.require_for_config(cfg(["AAA", "BBB"]))
    assert store.reads == reads and store.mem_hits == 0            # not even re-statted: the job memo answered
    # a job on the same universe with a different window needs no parquet read either (per-file memo)
    pf.require_for_config({**cfg(["AAA", "BBB"]), "end_date": datetime(2023, 11, 30)})
    assert store.reads == reads and store.mem_hits == 2


def test_a_rewritten_file_is_rescanned_never_answered_from_a_stale_verdict(cache):
    _put(cache, "AAA")
    pf.require_for_config(cfg(["AAA"]))
    _put(cache, "AAA", 0.5)                                        # the 5-minute file is replaced
    pf.reset_job_memo()                                            # a new job
    with pytest.raises(IntradayBasisMismatch):
        pf.require_for_config(cfg(["AAA"]))


# ------------------------------------------------------------------------------------------ job-fatal path
def test_both_refusals_are_job_fatal_by_name():
    for exc in (IntradayBasisMismatch("x"), IntradayBasisStale("x")):
        assert job_fatal(exc) and job_fatal(type(exc).__name__)
    assert {"IntradayBasisMismatch", "IntradayBasisStale"} <= JOB_FATAL_ERROR_TYPES


def test_the_job_start_refuses_before_any_bar_is_loaded(cache, monkeypatch):
    """Runs the real ``run_daily_backtest`` on a config whose universe holds a mismatched symbol: it raises
    ``IntradayBasisMismatch`` and NO bar was loaded (neither the price source's preload nor any series read)."""
    from app.models.database import Base, engine
    from app.services.backtest import daily_backtest_handler as H
    from app.services.backtest import price_source as PS
    Base.metadata.create_all(bind=engine)
    _put(cache, "BADONE", 0.5)
    loaded = []
    monkeypatch.setattr(PS.AsOfPriceSource, "preload", lambda self, *a, **k: loaded.append("preload"))
    monkeypatch.setattr(PS.MemoizedOHLCVProvider, "_load", lambda self, *a, **k: loaded.append("load"))
    payload = {"backtest_id": 987001, "name": "pf", "enabled_instruments": ["BADONE"], "experts": ["FMPEarningsDrift"],
               "start_date": "2023-03-01", "end_date": "2023-12-29", "initial_capital": 100_000.0, "commission": 1.0,
               "slippage": 0.0, "fill_model": "next_bar_open", "seed": 1, "execution_interval": "5min",
               "warmup_days": 30}
    cfg = H._build_config(payload)
    with pytest.raises(IntradayBasisMismatch, match="BADONE: constant_factor x0.5"):
        H.run_daily_backtest(cfg)
    assert loaded == []


def test_a_healthy_universe_reaches_the_preload(cache, monkeypatch):
    from app.models.database import Base, engine
    from app.services.backtest import daily_backtest_handler as H
    Base.metadata.create_all(bind=engine)
    _put(cache, "GOODONE")
    class Reached(Exception):
        pass
    def stop(self, *a, **k):
        raise Reached()
    from app.services.backtest import price_source as PS
    monkeypatch.setattr(PS.AsOfPriceSource, "preload", stop)
    payload = {"backtest_id": 987002, "name": "pf", "enabled_instruments": ["GOODONE"], "experts": ["FMPEarningsDrift"],
               "start_date": "2023-03-01", "end_date": "2023-12-29", "initial_capital": 100_000.0, "commission": 1.0,
               "slippage": 0.0, "fill_model": "next_bar_open", "seed": 1, "execution_interval": "5min", "warmup_days": 30}
    with pytest.raises(Reached):
        H.run_daily_backtest(H._build_config(payload))


def test_a_trial_that_hits_the_refusal_is_reported_fatal_to_the_master(cache):
    """The worker's trial wrapper classifies by the exception's NAME (the object does not cross the process
    boundary): ``fatal`` + ``error_type`` ride the result, and the master aborts the job on the first one."""
    _put(cache, "BADONE", 0.5)
    try:
        pf.require_for_config(cfg(["BADONE"]))
    except Exception as e:  # noqa: BLE001 -- reproducing the wrapper's classification
        out = {"ok": False, "fatal": job_fatal(e), "error_type": type(e).__name__, "error": str(e)}
    assert out["fatal"] and out["error_type"] == "IntradayBasisMismatch"
    from app.services.strategy_optimization_handler import _FatalTrialError, _abort_on_fatal_trial
    with pytest.raises(_FatalTrialError, match="BADONE"):
        _abort_on_fatal_trial(out, {"msg": None}, key=0, flat={})


# ------------------------------------------------------------------------------------------ launch refusal
def test_the_launcher_refuses_early_with_the_list_and_has_no_skip_flag(cache):
    import ba2test_launcher as L
    _put(cache, "GOOD")
    _put(cache, "SPLIT", 0.5)
    block = {**cfg(["GOOD", "SPLIT"]), "start_date": "2023-03-01", "end_date": "2023-12-29"}
    with pytest.raises(SystemExit) as ei:
        L._refuse_intraday_basis_mismatch("optimize", block)
    assert "REFUSED" in str(ei.value) and "SPLIT: constant_factor x0.5" in str(ei.value)
    # the recorded way out: exclude the symbol; the check then passes
    pf.reset_job_memo()
    ok = {**block, "enabled_instruments": ["GOOD"]}
    L._refuse_intraday_basis_mismatch("optimize", ok)
    # a daily-clock equity job is not asked at all
    L._refuse_intraday_basis_mismatch("optimize", {**block, "execution_interval": "1d"})
    src = inspect.getsource(L)
    assert "BA2_SKIP_BASIS" not in src and "skip-basis-check" not in src.lower()



# ------------------------------------------------------------------------------------------ review round
def test_M5_a_repaired_or_newly_defective_file_is_seen_without_resetting_the_memo(cache):
    _put(cache, "LIVE1")
    c = cfg(["LIVE1"])
    pf.require_for_config(c)                                         # judged ok, memoised
    _put(cache, "LIVE1", 0.5)                                        # a defect appears in a long-lived worker
    with pytest.raises(IntradayBasisMismatch, match="LIVE1"):
        pf.require_for_config(c)
    _put(cache, "LIVE1", 1.0)                                        # ... and is repaired
    assert pf.require_for_config(c).counts["ok"] == 1
    split_basis.write_intraday_stale(str(cache / "LIVE1_5min.parquet"), reason="x")     # a marker appears
    with pytest.raises(IntradayBasisStale):
        pf.require_for_config(c)
    split_basis.clear_intraday_stale(str(cache / "LIVE1_5min.parquet"))
    assert pf.require_for_config(c) is not None


def test_M7_the_judged_window_is_what_the_engine_reads(cache):
    # a short job window: the provider's read guard looks 90 days back; so does the preflight
    _put(cache, "SHORTWIN")
    rep = pf.require_for_config({**cfg(["SHORTWIN"]), "start_date": datetime(2023, 12, 1), "warmup_days": 0})
    lo = pd.Timestamp(rep.window_start)
    assert lo == pd.Timestamp("2023-12-29") - pd.Timedelta(days=cib.MIN_JUDGED_DAYS)


def test_an_insufficient_symbol_with_enough_daily_sessions_is_a_coverage_refusal(cache):
    daily, intra = _pair()
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / "THIN_1d.parquet", index=False)
    few = intra[(intra["Date"] >= "2023-03-01") & (intra["Date"] < "2023-03-08")]     # 5 sessions inside the window
    few.assign(effective_date=few["Date"]).to_parquet(cache / "THIN_5min.parquet", index=False)
    with pytest.raises(IntradayBasisMismatch) as ei:
        pf.require_for_config(cfg(["THIN"]))
    assert "THIN: intraday COVERAGE" in str(ei.value) and "daily sessions in the window" in str(ei.value)


def test_no_intraday_file_is_not_a_basis_refusal_for_an_intraday_clock(cache):
    daily, _ = _pair()
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / "NOINTRA_1d.parquet", index=False)
    rep = pf.require_for_config(cfg(["NOINTRA"]))                     # BacktestCacheMiss's business
    assert [r.symbol for r in rep.unjudged] == ["NOINTRA"]


def test_options_job_with_no_intraday_bars_is_a_recorded_warning_with_the_sessions(cache):
    daily, _ = _pair()
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / "OPTNO_1d.parquet", index=False)
    _put(cache, "OPTOK")
    rep = pf.require_for_config(cfg(["OPTNO", "OPTOK"], "1d", options=True))
    d = rep.to_dict()["option_refinement_uncovered"]
    assert d["count"] == 1 and d["symbols"][0]["symbol"] == "OPTNO" and d["symbols"][0]["class"] == "no_intraday"
    assert d["daily_sessions_without_intraday"] > 150 and d["symbols"][0]["sessions_without_intraday"] > 150
    assert rep.job_kind == "options"


def test_rebased_symbols_and_remaining_bursts_are_recorded_in_the_result(cache):
    _put(cache, "REB1")
    split_basis.write_intraday_rebase(str(cache / "REB1_5min.parquet"), {
        "applied_utc": "2026-10-08", "source": "mixed", "segments": [{"volume_unadjusted": True}]})
    daily, intra = _pair()
    odd = intra.copy()
    for d in ("2023-05-02", "2023-06-13", "2023-07-20", "2023-09-05"):             # 4 sessions 15% off, far apart
        m = odd["Date"].dt.normalize() == pd.Timestamp(d)
        for c in ("Open", "High", "Low", "Close"):
            odd.loc[m, c] = odd.loc[m, c] * 1.15
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / "BUR_1d.parquet", index=False)
    odd.assign(effective_date=odd["Date"]).to_parquet(cache / "BUR_5min.parquet", index=False)
    rep = pf.require_for_config(cfg(["REB1", "BUR"]))
    d = rep.to_dict()
    assert d["rebased_intraday"] == {"count": 1, "symbols": ["REB1"], "volume_unadjusted": ["REB1"], "sources": ["mixed"]}
    assert d["remaining_bursts"]["count"] == 1 and d["remaining_bursts"]["symbols"][0]["symbol"] == "BUR"
    assert d["counts"]["rebased_intraday"] == 1


def test_the_provider_is_derived_not_hardcoded(cache, tmp_path, monkeypatch):
    assert pf.default_provider_name() == PROV
    other = tmp_path / "cache" / "OtherProvider"
    other.mkdir(parents=True)
    _put(cache, "SAMEBAD", 0.5)                                         # bad under the default provider only
    _put(other, "SAMEBAD", 1.0)
    pf.reset_job_memo()
    assert pf.require_for_config(cfg(["SAMEBAD"]), provider="OtherProvider").counts["ok"] == 1
    with pytest.raises(IntradayBasisMismatch):
        pf.require_for_config(cfg(["SAMEBAD"]))


def test_job_start_reports_reviewed_exclusions_applied_at_launch(cache):
    _put(cache, "GOODX")
    rep = pf.require_for_config({**cfg(["GOODX"]), "intraday_basis_exclusions": [{"symbol": "GONEX", "reason": "r",
                                 "added": "2026-10-08", "reviewed_by": "Bastien"}]})
    assert rep.to_dict()["excluded_by_reviewed_list"][0]["symbol"] == "GONEX"


def test_job_start_refuses_a_listed_symbol_still_in_the_universe(cache, tmp_path, monkeypatch):
    from ba2_providers.ohlcv import intraday_exclusions as ix
    f = tmp_path / "ex.json"
    f.write_text(json.dumps({"entries": [{"symbol": "LISTED", "reason": "vendor serves two bases", "added": "2026-10-08",
                                          "reviewed_by": "Bastien"}]}))
    monkeypatch.setattr(ix, "EXCLUSIONS_PATH", str(f))
    _put(cache, "LISTED", 0.5)
    with pytest.raises(IntradayBasisMismatch, match="remove it at launch"):
        pf.require_for_config(cfg(["LISTED"]))


# ------------------------------------------------------------------------------------------ the launcher
def _exclusion_file(tmp_path, monkeypatch, reviewed_by):
    from ba2_providers.ohlcv import intraday_exclusions as ix
    f = tmp_path / "ex.json"
    f.write_text(json.dumps({"entries": [{"symbol": "LST", "reason": "vendor serves old dates as-traded", "added": "2026-10-08",
                                          "reviewed_by": reviewed_by}]}))
    monkeypatch.setattr(ix, "EXCLUSIONS_PATH", str(f))


def _block(symbols):
    return {**cfg(symbols), "start_date": "2023-03-01", "end_date": "2023-12-29"}


def test_launcher_removes_a_REVIEWED_listed_symbol_with_a_printed_line_and_a_results_entry(cache, tmp_path, monkeypatch, capsys):
    import ba2test_launcher as L
    _exclusion_file(tmp_path, monkeypatch, "Bastien")
    _put(cache, "GOOD")
    _put(cache, "LST", 0.5)
    block = _block(["GOOD", "LST"])
    L._refuse_intraday_basis_mismatch("optimize", block)
    out = capsys.readouterr().out
    assert "EXCLUDING 1 symbols on the reviewed intraday-basis exclusion list" in out and "LST (vendor serves" in out
    assert block["enabled_instruments"] == ["GOOD"]
    assert block["excluded_instruments"] == ["LST"]
    assert block["intraday_basis_exclusions"][0]["symbol"] == "LST" and block["intraday_basis_exclusions"][0]["reviewed_by"] == "Bastien"
    # the result of a job started from this block records it
    assert pf.require_for_config(block).to_dict()["excluded_by_reviewed_list"][0]["symbol"] == "LST"


def test_launcher_still_refuses_a_PENDING_or_unlisted_mismatched_symbol(cache, tmp_path, monkeypatch):
    import ba2test_launcher as L
    _exclusion_file(tmp_path, monkeypatch, "pending owner review")
    _put(cache, "LST", 0.5)
    _put(cache, "OTHER", 0.5)
    with pytest.raises(SystemExit) as ei:
        L._refuse_intraday_basis_mismatch("optimize", _block(["LST", "OTHER"]))
    msg = str(ei.value)
    assert "LST: constant_factor" in msg and "'pending' review" in msg and "OTHER: constant_factor" in msg


def test_launcher_prints_a_listed_symbol_that_no_longer_needs_its_entry(cache, tmp_path, monkeypatch, capsys):
    import ba2test_launcher as L
    _exclusion_file(tmp_path, monkeypatch, "Bastien")
    _put(cache, "LST", 1.0)                                              # repaired since
    block = _block(["LST"])
    L._refuse_intraday_basis_mismatch("optimize", block)
    assert "no longer needed" in capsys.readouterr().out and block["enabled_instruments"] == ["LST"]


def test_both_launch_paths_run_the_basis_preflight_then_the_coverage_preflight():
    import inspect
    import ba2test_launcher as L
    for fn, tag in ((L._cmd_optimize, "optimize"), (L._cmd_optimize_batch, "optimize-batch")):
        src = inspect.getsource(fn)
        i_basis = src.index(f'_refuse_intraday_basis_mismatch("{tag}"')
        i_cov = src.index(f'_coverage_preflight("{tag}"')
        assert i_basis < i_cov, tag



# ------------------------------------------------------------------------------------------ fetch-cache (extend_ohlcv_cache)
class _Stub:
    """FMP provider with the network replaced; bound to the backend cache layer exactly as fetch-cache does."""

    @staticmethod
    def make(tail_factor):
        from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
        from app.services.ohlcv_cache_provider import wrap_with_cache
        daily, intra = _pair()

        class FMPOHLCVProvider_(FMPOHLCVProvider):
            def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
                df = daily if interval in ("1d", "daily") else intra.copy()
                if interval not in ("1d", "daily"):
                    late = df["Date"] >= "2023-12-20"
                    for c in ("Open", "High", "Low", "Close"):
                        df.loc[late, c] = df.loc[late, c] * tail_factor
                lo, hi = pd.Timestamp(start_date), pd.Timestamp(end_date) + pd.Timedelta(days=1)
                return df[(df["Date"] >= lo) & (df["Date"] < hi)].reset_index(drop=True).copy()

            def _split_calendar(self, symbol, interval):
                return []
        FMPOHLCVProvider_.__name__ = PROV
        return wrap_with_cache(FMPOHLCVProvider_(api_key="k"))


def _legacy_file(cache, symbol):
    daily, intra = _pair()
    old = intra.copy()
    early = old["Date"] < "2023-12-01"
    for c in ("Open", "High", "Low", "Close"):
        old.loc[early, c] = old.loc[early, c] * 1.325              # a LEGACY x1.325 segment, like T
    head = old[old["Date"] < "2023-12-20"]
    daily.assign(effective_date=daily["Date"]).to_parquet(cache / f"{symbol}_1d.parquet", index=False)
    head.assign(effective_date=head["Date"]).to_parquet(cache / f"{symbol}_5min.parquet", index=False)
    return str(cache / f"{symbol}_5min.parquet")


def test_fetch_cache_extends_a_legacy_defective_file_with_a_correct_tail_and_refuses_a_wrong_one(cache):
    path = _legacy_file(cache, "EXT1")
    n0 = len(pd.read_parquet(path))
    good = _Stub.make(1.0)
    got = good.extend_ohlcv_cache("EXT1", datetime(2023, 1, 3), datetime(2023, 12, 29), "5min")
    assert len(pd.read_parquet(path)) > n0 and got["Date"].max() >= pd.Timestamp("2023-12-28")      # accepted
    path2 = _legacy_file(cache, "EXT2")
    before = open(path2, "rb").read()
    bad = _Stub.make(2.0)                                                                           # tail on another basis
    with pytest.raises(IntradayBasisMismatch, match="incoming bars"):
        bad.extend_ohlcv_cache("EXT2", datetime(2023, 1, 3), datetime(2023, 12, 29), "5min")
    assert open(path2, "rb").read() == before
