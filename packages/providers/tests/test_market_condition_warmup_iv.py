"""The warmup for the CHAIN profile ``iv-v1`` (docs/plans/2026-11-15-option-iv-conditions.md).

A throwaway cache holds FMP-layout daily bars (the same fixture the OHLCV warmup tests use) and a
ThetaData-layout option tree (``ThetaDataOptionsProvider/<SYM>/exp=YYYY-MM-DD/*.parquet``) priced
by Black-76, with a vendor ``iv`` column full of garbage that must never be read. Pinned:

* the plan inventories the chain per symbol (a symbol without one is a non-fetchable blocking
  item; with ``allow_exclusions`` it is LEFT OUT, never published as a run of unknown rows);
* every published row equals ``compute_iv_v1`` on the same session chain and the same window;
* a session whose chain has no rows is ``missing_session`` and is named in the coverage record;
* a second build reuses every row (zero computed, the same manifest), and editing ONE quote
  rebuilds exactly the rows of that session -- a row's identity covers bars AND quotes;
* the manifest records ``option_source``; OHLCV-profile manifests never carry the key;
* an option tree fetched only through a date before the last row is a blocking stale tail.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import implied_vol as ivm
from ba2_common.core import market_conditions as mc
from ba2_common.core.market_condition_source import assemble_window, read_fmp_daily_cache
from ba2_common.core.market_condition_store import MarketConditionStore
from ba2_common.core.market_conditions import (
    FIELD_ATM_IV_30, FIELD_IV_RV_RATIO, FIELD_IV_TERM_SLOPE, IV_PROFILE, STATUS_MISSING_SESSION,
    STATUS_VALID,
)
from ba2_providers.market_conditions import warmup as W
from ba2_providers.market_conditions.option_chain_source import (
    OPTION_SOURCE_THETADATA, ThetaDataParquetChainSource,
)

from .test_market_condition_warmup import CERT, TRUTH, FakeSource, _sessions, _write

START, END = date(2025, 3, 3), date(2025, 4, 30)
CHAIN_SYMBOLS = ("AAA", "BBB")          # CCC has bars but no option tree
HOLE = ("BBB", date(2025, 3, 20))       # an interior session with no chain rows


def _fridays(a: date, b: date):
    d = a + timedelta(days=(4 - a.weekday()) % 7)
    while d <= b:
        yield d
        d += timedelta(days=7)


def _write_tree(root: Path, symbol: str, sessions, *, fetched_through: date, skip=(), bump=None,
                provider="ThetaDataOptionsProvider"):
    """One parquet per expiry, every session with 7 <= dte <= 130, strikes around 100."""
    sdir = root / provider / symbol
    by_exp = {}
    for s in sessions:
        if s in skip:
            continue
        for e in _fridays(s + timedelta(days=7), s + timedelta(days=130)):
            dte = (e - s).days
            F = 100.0 + 0.05 * (s - sessions[0]).days
            vol = 0.22 + 0.0008 * dte + (0.03 if symbol == "BBB" else 0.0)
            for K in np.arange(80.0, 125.0, 2.5):
                for typ in ("call", "put"):
                    p = ivm.black76_price(F, float(K), dte / 365.0, vol, typ == "call")
                    bid, ask = max(p - 0.02, 0.0), p + 0.02
                    if bump is not None and bump == (s, e, float(K), typ):
                        bid, ask = bid + 0.01, ask + 0.01
                    by_exp.setdefault(e, []).append(
                        {"option_type": typ, "strike": float(K), "expiry": e.isoformat(),
                         "bar_date": s.isoformat(), "bid": bid, "ask": ask, "iv": 9.99})
    for e, rows in by_exp.items():
        pdir = sdir / f"exp={e.isoformat()}"
        pdir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(pdir / f"{symbol}_{e.isoformat()}_1d.parquet", index=False)
        (pdir / "_manifest.json").write_text(json.dumps(
            {"status": "complete", "start": "2024-01-01", "end": fetched_through.isoformat()}))


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "cache"
    for sym, df in TRUTH.items():
        _write(r, sym, df)
    for sym, df in CERT.items():
        _write(r, sym, df)
    rows = _sessions(START - timedelta(days=7), END)
    for sym in CHAIN_SYMBOLS:
        _write_tree(r, sym, rows, fetched_through=END + timedelta(days=30),
                    skip={HOLE[1]} if sym == HOLE[0] else ())
    return r


@pytest.fixture(autouse=True)
def fast_claims(monkeypatch):
    monkeypatch.setattr(W, "CLAIM_POLL_S", 0.01)
    monkeypatch.setenv("BA2_MC_MAX_HOST_BUILDERS", "2")


def _plan(root, universe=("AAA", "BBB", "CCC"), **kw):
    kw.setdefault("option_source", "thetadata")
    p = W.plan(IV_PROFILE, universe, START, END, cache_root=str(root), source=FakeSource(str(root)), **kw)
    return W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json()))


def _build(plan, **kw):
    return W.build(plan, fetch_missing=False, concurrency=1, source=None, **kw)


def test_plan_inventories_the_chain_and_refuses_a_symbol_without_one(root):
    plan = _plan(root)
    assert plan.option_source == OPTION_SOURCE_THETADATA
    assert plan.option_root == os.path.abspath(str(root / "ThetaDataOptionsProvider"))
    assert plan.symbol("AAA").option_chain is True
    assert plan.symbol("CCC").option_chain is False
    kinds = {(i["symbol"], i["kind"]) for i in plan.blocking_inventory()}
    assert ("CCC", "no_option_chain") in kinds
    rep = _build(plan)
    assert not rep.ok and rep.manifest_digest is None
    assert any(i["kind"] == "no_option_chain" for i in rep.inventory)


def test_an_ohlcv_profile_refuses_an_option_root(root):
    with pytest.raises(W.WarmupConfigError, match="only for chain profiles"):
        W.plan("ohlcv-v1", ["AAA"], START, END, cache_root=str(root), source=FakeSource(str(root)),
               option_root=str(root / "ThetaDataOptionsProvider"))


def test_a_stale_option_tree_is_a_blocking_tail(root):
    _write_tree(root, "AAA", _sessions(START - timedelta(days=7), END), fetched_through=END - timedelta(days=10))
    plan = _plan(root, universe=("AAA",))
    assert plan.symbol("AAA").option_chain_stale is True
    assert [i["kind"] for i in plan.blocking_inventory()] == ["option_chain_stale_tail"]


def test_build_publishes_rows_equal_to_the_calculator_and_reuses_them(root):
    plan = _plan(root)
    rep = _build(plan, allow_exclusions=True)
    assert rep.ok, rep
    assert "CCC" in rep.excluded and rep.excluded["CCC"][0]["kind"] == "no_option_chain"
    store = MarketConditionStore(str(root))
    m = store.read_manifest(rep.manifest_digest)
    assert m["profile"] == IV_PROFILE and m["option_source"] == OPTION_SOURCE_THETADATA
    assert m["calc_version"] == "iv-v1/calc-1"
    assert sorted({o["symbol"] for o in m["objects"]}) == ["AAA", "BBB"]

    # every row == the calculator on the SAME session chain and the SAME 128-bar window
    src = ThetaDataParquetChainSource(str(root / "ThetaDataOptionsProvider"))
    for sym in ("AAA", "BBB"):
        chains = src.chains(sym, date(2025, 1, 1), date(2025, 12, 31))
        path = root / "FMPOHLCVProvider" / f"{sym}_1d.parquet"
        bars = read_fmp_daily_cache(str(path))
        rows = list(store.iter_rows(m, sym))
        assert len(rows) == plan.row_sessions
        for session, row in rows:
            win = assemble_window(*bars, session)
            want = mc.compute_iv_v1(session, chains.get(session), win.arrays() if win.ok else None,
                                    win.status if not win.ok else STATUS_VALID, win.reason)
            assert dict(row.by_field()) == dict(want.by_field()), (sym, session)
    by = dict(store.iter_rows(m, "AAA"))
    assert all(o.status == STATUS_VALID for o in by[START].by_field().values())
    assert by[START].by_field()[FIELD_IV_TERM_SLOPE].value < 1.0      # upward term structure

    # the hole: missing_session on the implied fields, recorded in the coverage
    hole = dict(store.iter_rows(m, "BBB"))[HOLE[1]].by_field()
    assert hole[FIELD_ATM_IV_30].status == STATUS_MISSING_SESSION
    assert hole[FIELD_IV_RV_RATIO].status == STATUS_MISSING_SESSION
    exc = [e for e in m["coverage"]["BBB"]["exceptions"] if e["kind"] == "option_chain_missing"]
    assert exc and exc[0]["first_session"] == HOLE[1].isoformat() and exc[0]["reaches_last_row"] is False

    # a second build: nothing recomputed, the same manifest
    rep2 = _build(_plan(root), allow_exclusions=True)
    assert rep2.ok and rep2.manifest_digest == rep.manifest_digest
    assert rep2.counters["rows_computed"] == 0
    assert rep2.counters["rows_reused"] == rep.counters["rows_computed"]


def test_editing_one_quote_rebuilds_exactly_that_sessions_rows(root):
    plan = _plan(root, universe=("AAA",))
    rep = _build(plan)
    assert rep.ok, rep
    target = date(2025, 4, 8)
    expiry = next(_fridays(target + timedelta(days=7), target + timedelta(days=130)))
    _write_tree(root, "AAA", _sessions(START - timedelta(days=7), END), fetched_through=END + timedelta(days=30),
                bump=(target, expiry, 100.0, "call"))
    rep2 = _build(_plan(root, universe=("AAA",)))
    assert rep2.ok, rep2
    assert rep2.counters["rows_computed"] == 1
    assert rep2.manifest_digest != rep.manifest_digest


def test_an_ohlcv_manifest_never_carries_option_source(root):
    p = W.plan("ohlcv-v1", ["AAA"], START, END, cache_root=str(root), source=FakeSource(str(root)))
    assert p.option_root is None and p.option_source is None
    assert p.symbol("AAA").option_chain is None
    rep = W.build(W.MarketConditionWarmPlan.from_dict(json.loads(p.to_json())), fetch_missing=False,
                  concurrency=1, source=None)
    assert rep.ok, rep
    m = MarketConditionStore(str(root)).read_manifest(rep.manifest_digest)
    assert "option_source" not in m


def test_the_mapped_reader_serves_the_published_iv_rows(root):
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader

    rep = _build(_plan(root, universe=("AAA",)))
    assert rep.ok, rep
    reader = MappedMarketConditionReader(str(root), rep.manifest_digest, IV_PROFILE)
    store = MarketConditionStore(str(root))
    want = dict(store.iter_rows(store.read_manifest(rep.manifest_digest), "AAA"))
    for session in (START, date(2025, 4, 1), END):
        assert dict(reader.observe("AAA", session).by_field()) == dict(want[session].by_field())


# --------------------------------------------------------------------------- chain quality (review 2026-11-15)
def _verify_cli():
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "tools" / "warm_market_conditions.py"
    spec = importlib.util.spec_from_file_location("warm_mc_cli_iv", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_build_records_chain_quality_and_verify_refuses_a_late_chain(root, capsys):
    """AAA's tree starts 20 sessions AFTER the window does while its bars run throughout: a leading
    missing_session run that window coverage exempts as "pre-listing". The build records it as a
    chain-quality run, and verify refuses the symbol unless it is accepted by name."""
    from types import SimpleNamespace

    rows = _sessions(START - timedelta(days=7), END)
    late = set(rows[:22])
    _write_tree(root, "AAA", rows, fetched_through=END + timedelta(days=30), skip=late)
    plan = _plan(root, universe=("AAA", "BBB"))
    rep = _build(plan)
    assert rep.ok, rep
    q = rep.iv_quality
    expected = sum(1 for d in late if d >= date.fromisoformat(plan.first_row_session))
    assert expected > 15
    assert q["AAA"][FIELD_ATM_IV_30]["longest_run"] == expected
    assert q["AAA"][FIELD_ATM_IV_30]["run_first"] == plan.first_row_session
    assert q["BBB"][FIELD_ATM_IV_30]["longest_run"] == 1             # the single interior hole
    m = MarketConditionStore(str(root)).read_manifest(rep.manifest_digest)
    kinds = {e["kind"] for e in m["coverage"]["AAA"]["exceptions"]}
    assert {"iv_quality", "chain_quality"} <= kinds

    cli = _verify_cli()
    args = SimpleNamespace(manifest=rep.manifest_digest, iv_max_run=20, iv_max_rate=0.40, iv_accept_symbols=None)
    verdict = cli._chain_quality_verdict(str(root), args)
    assert [r.split(":")[0] for r in verdict["refused"]] == ["AAA"]
    assert cli.main(["--quiet", "verify", "--manifest", rep.manifest_digest, "--cache-root", str(root),
                         "--ohlcv-history-source", "fmp"]) == 1
    assert cli.main(["--quiet", "verify", "--manifest", rep.manifest_digest, "--cache-root", str(root),
                         "--ohlcv-history-source", "fmp", "--iv-accept-symbols", "AAA"]) == 0
    assert cli.main(["--quiet", "verify", "--manifest", rep.manifest_digest, "--cache-root", str(root),
                         "--ohlcv-history-source", "fmp", "--iv-max-run", "30", "--iv-max-rate", "0.5"]) == 0


def test_conflicting_duplicate_quotes_are_counted_in_coverage(root):
    import pandas as pd

    sdir = root / "ThetaDataOptionsProvider" / "AAA"
    part = sorted(p for p in sdir.iterdir() if p.name.startswith("exp="))[3]
    f = next(part.glob("*.parquet"))
    df = pd.read_parquet(f)
    dup = df[df["bar_date"] == START.isoformat()].iloc[:5].copy()
    assert len(dup) == 5
    dup["bid"] = dup["bid"] + 0.5
    pd.concat([df, dup]).to_parquet(f, index=False)
    rep = _build(_plan(root, universe=("AAA",)))
    assert rep.ok, rep
    m = MarketConditionStore(str(root)).read_manifest(rep.manifest_digest)
    cq = [e for e in m["coverage"]["AAA"]["exceptions"] if e["kind"] == "chain_quality"][0]
    assert cq["conflicting_quotes_dropped"] >= 10



# --------------------------------------------------------------------------- the Alpha Vantage store
def _av_tree(root: Path, symbol: str, sessions, *, staged=()):
    """The AV store layout: the same partitions, a ledger of fetched days, and ``staged`` days that a
    running download has not compacted yet (written to ``_days/`` with DIFFERENT quotes, which must
    REPLACE the partition rows of that day)."""
    import pandas as pd

    _write_tree(root, symbol, sessions, fetched_through=END + timedelta(days=30), provider="AlphaVantageOptionsProvider")
    sdir = root / "AlphaVantageOptionsProvider" / symbol
    lines = [json.dumps({"d": d.isoformat(), "s": "ok", "rows": 1}) for d in sessions]
    (sdir / "_ledger.jsonl").write_text(chr(10).join(lines) + chr(10) + '{"d": "torn', encoding="utf-8")
    if staged:
        (sdir / "_days").mkdir(exist_ok=True)
        for d in staged:
            rows = []
            for e in _fridays(d + timedelta(days=7), d + timedelta(days=130)):
                dte = (e - d).days
                for K in np.arange(80.0, 125.0, 2.5):
                    for typ in ("call", "put"):
                        p = ivm.black76_price(100.0, float(K), dte / 365.0, 0.45, typ == "call")
                        rows.append({"option_type": typ, "strike": float(K), "expiry": e.isoformat(),
                                     "bar_date": d.isoformat(), "bid": max(p - 0.02, 0.0), "ask": p + 0.02,
                                     "iv": 9.99})
            pd.DataFrame(rows).to_parquet(sdir / "_days" / f"{d.isoformat()}.parquet", index=False)


def test_the_option_source_must_be_named(root):
    with pytest.raises(W.WarmupConfigError, match="name it explicitly"):
        W.plan(IV_PROFILE, ["AAA"], START, END, cache_root=str(root), source=FakeSource(str(root)))


def test_an_alphavantage_build_reads_partitions_staged_days_and_the_ledger(root):
    from ba2_providers.market_conditions.option_chain_source import (
        OPTION_SOURCE_ALPHAVANTAGE, AlphaVantageParquetChainSource,
    )

    rows = _sessions(START - timedelta(days=7), END)
    staged = date(2025, 4, 1)
    _av_tree(root, "AAA", rows, staged=(staged,))
    src = AlphaVantageParquetChainSource(str(root / "AlphaVantageOptionsProvider"))
    assert src.fetched_through("AAA") == rows[-1]          # the ledger, torn last line ignored
    plan = _plan(root, universe=("AAA",), option_source="alphavantage")
    assert plan.option_source == OPTION_SOURCE_ALPHAVANTAGE
    assert plan.option_root.endswith("AlphaVantageOptionsProvider")
    rep = _build(plan)
    assert rep.ok, rep
    m = MarketConditionStore(str(root)).read_manifest(rep.manifest_digest)
    assert m["option_source"] == "alphavantage"
    by = dict(MarketConditionStore(str(root)).iter_rows(m, "AAA"))
    # the staged day's quotes (vol 0.45) replaced the partition's (vol ~0.25): IV30 ~ 0.45
    assert by[staged].by_field()[FIELD_ATM_IV_30].value == pytest.approx(0.45, abs=5e-3)
    assert by[date(2025, 4, 2)].by_field()[FIELD_ATM_IV_30].value < 0.30
    # identical quotes give identical rows whichever store holds them
    th = _build(_plan(root, universe=("AAA",), option_source="thetadata"))
    mt = MarketConditionStore(str(root)).read_manifest(th.manifest_digest)
    bt = dict(MarketConditionStore(str(root)).iter_rows(mt, "AAA"))
    same = [d for d in by if d != staged]
    assert all(dict(by[d].by_field()) == dict(bt[d].by_field()) for d in same)
    assert th.manifest_digest != rep.manifest_digest        # option_source is part of the identity
