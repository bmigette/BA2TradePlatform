"""``tools/warm_market_conditions.py`` -- the plan/build/verify CLI (plan Task 6).

What is pinned here is the CONTRACT an operator and a job queue rely on: exit 0 only when the
work is done, 1 when there is an actionable inventory or a failed verification, 2 for a
configuration error (unknown profile, missing files, bad dates) -- and that ``--cache-only``
fetches nothing. A tiny fabricated cache root keeps it fast: three symbols, one month of
decisions, plus the AAPL/NVDA certification files the source preflight reads.

Run from ``testplatform/backend``:
    ...python.exe -m pytest tests/test_warm_market_conditions_cli.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "tools" / "warm_market_conditions.py"

UNIVERSE = ("AAA", "BBB", "CCC")
START, END = date(2024, 6, 3), date(2024, 6, 28)


def _tool():
    spec = importlib.util.spec_from_file_location("warm_market_conditions", str(_SCRIPT))
    m = importlib.util.module_from_spec(spec)
    sys.modules["warm_market_conditions"] = m
    spec.loader.exec_module(m)
    return m


def _sessions(a, b):
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(a, b)]


def _frame(sessions, seed, base=100.0):
    rng = np.random.default_rng(seed)
    n = len(sessions)
    c = base * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"Date": pd.to_datetime(sessions), "Open": o, "High": np.maximum(o, c) * 1.004,
                         "Low": np.minimum(o, c) * 0.996, "Close": c,
                         "Volume": rng.integers(1e6, 5e6, n).astype(float)})


def _write(root, sym, df):
    p = Path(root) / "FMPOHLCVProvider" / f"{sym}_1d.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["effective_date"] = out["Date"]
    out.to_parquet(p, index=False)
    return p


class _NoFetchSource:
    """A source with no network: the split calendar is empty and any fetch is a test failure."""

    calls = 0
    bytes = 0

    def split_calendar(self, symbol):
        return []

    def fetch_daily(self, symbol, start, end):
        raise AssertionError(f"the CLI fetched {symbol} {start}..{end}")

    def force_full_refetch(self, symbol):
        raise AssertionError(f"the CLI force-refetched {symbol}")


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "cache"
    for i, sym in enumerate(UNIVERSE):
        _write(r, sym, _frame(_sessions(date(2023, 9, 1), date(2024, 6, 28)), seed=i + 3))
    _write(r, "AAPL", _frame(_sessions(date(2020, 6, 1), date(2020, 10, 30)), seed=1, base=120.0))
    _write(r, "NVDA", _frame(_sessions(date(2024, 4, 1), date(2024, 8, 30)), seed=2, base=110.0))
    return str(r)


@pytest.fixture
def tool(monkeypatch):
    m = _tool()
    monkeypatch.setattr(m, "make_source", lambda cache_root, history_source, **kw: _NoFetchSource())
    # The flag is REQUIRED on the real CLI; these tests are about everything else, so they run on fmp.
    # ``main_raw`` is the real entry point (the flag tests below use it).
    m.main_raw = m.main
    m.main = lambda argv: m.main_raw(argv if "--ohlcv-history-source" in argv
                                     else [*argv, "--ohlcv-history-source", "fmp"])
    return m


@pytest.fixture
def universe_file(tmp_path):
    p = tmp_path / "universe.txt"
    p.write_text("# market-condition universe\n" + "\n".join(UNIVERSE) + "\n\n", encoding="utf-8")
    return str(p)


def _plan_args(root, universe_file, out, start=START, end=END, profile="ohlcv-v1"):
    return ["plan", "--profile", profile, "--universe-file", universe_file, "--start", start.isoformat(),
            "--end", end.isoformat(), "--cache-root", root, "--out", out, "--quiet"]


def test_plan_build_verify_round_trip(tool, root, universe_file, tmp_path, capsys):
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out)) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["plan"]["universe"] == 3 and printed["inventory"] == [] and not printed["preflight_errors"]
    saved = json.loads(Path(out).read_text())
    assert saved["profile"] == "ohlcv-v1" and len(saved["symbols"]) == 3

    assert tool.main(["--quiet", "build", "--plan", out, "--cache-only"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["manifest_digest"]
    assert report["counters"]["rows_computed"] == printed["plan"]["rows_required"]
    assert report["counters"]["provider_calls"] == 0

    digest = report["manifest_digest"]
    assert tool.main(["--quiet", "verify", "--manifest", digest, "--cache-root", root]) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["ok"] and v["objects_checked"] > 0 and v["raw_checked"] > 0

    # A corrupted object (same size) fails verification with exit 1.
    from ba2_common.core.market_condition_store import MarketConditionStore
    store = MarketConditionStore(root)
    obj = store.abspath(store.read_manifest(digest)["objects"][0]["path"])
    data = bytearray(obj.read_bytes())
    data[len(data) // 2] ^= 0xFF
    obj.write_bytes(bytes(data))
    assert tool.main(["--quiet", "verify", "--manifest", digest, "--cache-root", root]) == 1
    assert json.loads(capsys.readouterr().out)["corrupt"]


def test_print_digest_puts_only_the_digest_on_stdout(tool, root, universe_file, tmp_path, capsys):
    """The stage-1 launch script captures the digest with a plain command substitution, so stdout
    has to be the digest and nothing else -- while the counters still go somewhere auditable
    (stderr), because a build nobody can see the counters of is a build nobody can check."""
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out)) == 0
    capsys.readouterr()

    assert tool.main(["build", "--plan", out, "--cache-only", "--print-digest", "--quiet"]) == 0
    captured = capsys.readouterr()
    digest = captured.out.strip()
    assert digest and len(digest.splitlines()) == 1
    report = json.loads(captured.err)
    assert report["manifest_digest"] == digest and report["ok"]


def test_prepare_host_verifies_then_maps_and_refuses_a_corrupt_object(tool, root, universe_file,
                                                                      tmp_path, capsys):
    """prepare-host is the step every worker runs before a search dispatches (design 4.4 step 5).

    Order is the contract: VERIFY (re-hash every object and raw shard), and only then build the
    mapped arrays. A corrupt object must leave the host with no mapping at all -- an array set is
    immutable once published and is mapped by every worker process on the box.
    """
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out)) == 0
    capsys.readouterr()
    assert tool.main(["--quiet", "build", "--plan", out, "--cache-only"]) == 0
    digest = json.loads(capsys.readouterr().out)["manifest_digest"]

    assert tool.main(["--quiet", "prepare-host", "--manifest", digest, "--cache-root", root]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] and first["built"] and first["symbols"] == 3 and first["rows"] > 0

    # Second run on a warm host: opened, not rebuilt -- the "0 built / N opened" signal.
    assert tool.main(["--quiet", "prepare-host", "--manifest", digest, "--cache-root", root]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["ok"] and not second["built"] and second["opened"]

    from ba2_common.core.market_condition_reader import _derived_root, mapped_key
    from ba2_common.core.market_condition_store import MarketConditionStore
    store = MarketConditionStore(root)
    obj = store.abspath(store.read_manifest(digest)["objects"][0]["path"])
    size = obj.stat().st_size
    data = bytearray(obj.read_bytes())
    data[len(data) // 2] ^= 0xFF
    obj.write_bytes(bytes(data))
    assert obj.stat().st_size == size
    assert tool.main(["--quiet", "prepare-host", "--manifest", digest, "--cache-root", root]) == 1
    bad = json.loads(capsys.readouterr().out)
    assert not bad["ok"] and not bad["verified"] and any("corrupt" in e for e in bad["errors"])
    # The already-published mapping is untouched (nothing rewrites a published set); what the
    # failure guarantees is that no NEW mapping was built from the corrupt bytes.
    assert os.path.isdir(os.path.join(_derived_root(root), mapped_key("ohlcv-v1", digest)))


def test_cache_only_with_missing_coverage_exits_one_and_fetches_nothing(tool, root, universe_file, tmp_path, capsys):
    os.remove(Path(root) / "FMPOHLCVProvider" / "CCC_1d.parquet")
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out)) == 1
    assert [i["kind"] for i in json.loads(capsys.readouterr().out)["inventory"]] == ["missing_file"]
    assert tool.main(["--quiet", "build", "--plan", out, "--cache-only"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"] and report["manifest_digest"] is None
    assert report["inventory"][0]["symbol"] == "CCC"


def test_configuration_errors_exit_two(tool, root, universe_file, tmp_path, capsys):
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out, profile="ta-structure-v9")) == 2
    assert tool.main(["--quiet", "plan", "--profile", "ohlcv-v1", "--universe-file", str(tmp_path / "nope.txt"),
                      "--start", "2024-06-03", "--end", "2024-06-28", "--cache-root", root]) == 2
    assert tool.main(_plan_args(root, universe_file, out, start=END, end=START)) == 2
    assert tool.main(["--quiet", "plan", "--profile", "ohlcv-v1", "--universe-file", universe_file,
                      "--start", "not-a-date", "--end", "2024-06-28"]) == 2
    assert tool.main(["--quiet", "build", "--plan", str(tmp_path / "missing.json"), "--cache-only"]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"plan_version": 99}), encoding="utf-8")
    assert tool.main(["--quiet", "build", "--plan", str(bad), "--cache-only"]) == 2
    # build requires exactly one mode
    assert tool.main(["--quiet", "build", "--plan", out]) == 2
    # prepare-host without a digest has nothing to prepare: a configuration error.
    assert tool.main(["--quiet", "prepare-host"]) == 2
    capsys.readouterr()


def test_unknown_manifest_verification_exits_one(tool, root, capsys):
    assert tool.main(["--quiet", "verify", "--manifest", "0" * 64, "--cache-root", root]) == 1
    # Same for prepare-host: a digest that is not on this host is an ACTIONABLE failure (sync it,
    # then prepare), not a malformed command line.
    assert tool.main(["--quiet", "prepare-host", "--manifest", "0" * 64, "--cache-root", root]) == 1
    capsys.readouterr()


class _BrokenCalendarSource(_NoFetchSource):
    """The split calendar cannot be read: the warmup must refuse, not warm an unproven basis."""

    def split_calendar(self, symbol):
        if symbol == "BBB":
            raise RuntimeError("FMP split calendar unreachable")
        return []


def test_unreadable_split_calendar_exits_one_until_exclusions_are_allowed(tool, root, universe_file,
                                                                          tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tool, "make_source", lambda cache_root, history_source: _BrokenCalendarSource())
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out)) == 1
    printed = json.loads(capsys.readouterr().out)
    assert [i["kind"] for i in printed["inventory"]] == ["split_calendar_unavailable"]
    assert printed["inventory"][0]["symbol"] == "BBB"
    assert any(e.startswith("BBB:") for e in printed["preflight_errors"])

    assert tool.main(["--quiet", "build", "--plan", out, "--cache-only"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"] and report["manifest_digest"] is None and "BBB" in report["errors"][0]

    assert tool.main(["--quiet", "build", "--plan", out, "--cache-only", "--allow-exclusions"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["manifest_digest"]
    assert list(report["excluded"]) == ["BBB"]
    from ba2_common.core.market_condition_store import MarketConditionStore
    m = MarketConditionStore(root).read_manifest(report["manifest_digest"])
    assert m["coverage"]["BBB"]["exceptions"][0]["kind"] == "split_calendar_unavailable"


# ---------------------------------------------------------------------------
# --ohlcv-history-source: the vendor is a required, explicit choice and selects the source profile
ALPACA_DIR = "AlpacaHistoryOHLCVProvider"


def _write_alpaca(root, sym, df):
    p = Path(root) / ALPACA_DIR / f"{sym}_1d.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["effective_date"] = out["Date"]
    out.to_parquet(p, index=False)
    return p


def _write_build_manifest(root, events, certify=True, run_id="test-build"):
    """``events``: {symbol: [[iso date, factor], ...]} -- the splits the Alpaca build APPLIED."""
    meta = Path(root) / ALPACA_DIR / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "build_manifest.json").write_text(json.dumps(
        {"run_id": run_id, "symbols": {s: {"events": {"splits": ev, "spinoffs": []}} for s, ev in events.items()}}),
        encoding="utf-8")
    if certify:                          # the full gate's PASS certificate of this manifest (prepare_history --steps verify)
        from ba2_providers.ohlcv.alpaca_history import verify as ver
        ver.write_gate_certificate(str(meta), {"structural": {"status": ver.STATUS_PASS}})


@pytest.fixture
def alpaca_root(root):
    """``root`` (fmp cache) plus an Alpaca cache of the same symbols with DIFFERENT prices."""
    for i, sym in enumerate(UNIVERSE):
        _write_alpaca(root, sym, _frame(_sessions(date(2023, 9, 1), date(2024, 6, 28)), seed=i + 40))
    _write_alpaca(root, "AAPL", _frame(_sessions(date(2020, 6, 1), date(2020, 10, 30)), seed=1, base=120.0))
    _write_alpaca(root, "NVDA", _frame(_sessions(date(2024, 4, 1), date(2024, 8, 30)), seed=2, base=110.0))
    _write_build_manifest(root, {s: [] for s in UNIVERSE})
    return root


def _vendor_plan_args(root, universe_file, out, vendor, profile="ohlcv-v1"):
    return _plan_args(root, universe_file, out, profile=profile) + ["--ohlcv-history-source", vendor]


def test_the_history_source_flag_is_required_on_every_subcommand_and_typos_are_refused(tool, root, universe_file,
                                                                                       tmp_path, capsys):
    out = str(tmp_path / "plan.json")
    base = {"plan": ["plan", "--profile", "ohlcv-v1", "--universe-file", universe_file, "--start", "2024-06-03",
                     "--end", "2024-06-28", "--cache-root", root, "--out", out],
            "build": ["build", "--plan", out, "--cache-only"],
            "verify": ["verify", "--manifest", "0" * 64, "--cache-root", root],
            "prepare-host": ["prepare-host", "--manifest", "0" * 64, "--cache-root", root]}
    for name, argv in base.items():
        assert tool.main_raw(["--quiet", *argv]) == 2, f"{name} accepted a missing --ohlcv-history-source"
        for bad in ("alpca", "FMP", "", "both"):
            assert tool.main_raw(["--quiet", *argv, "--ohlcv-history-source", bad]) == 2, (name, bad)
    assert not Path(out).exists()
    capsys.readouterr()


def test_alpaca_round_trip_records_the_alpaca_source_profile_and_never_fetches(tool, monkeypatch, alpaca_root,
                                                                                universe_file, tmp_path, capsys):
    from ba2_common.core.market_condition_source import SOURCE_PROFILE_ALPACA_DAILY
    from ba2_common.core.market_condition_store import MarketConditionStore
    monkeypatch.undo()                      # the REAL make_source: the Alpaca source, no stub
    out = str(tmp_path / "plan.json")
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["plan"]["universe"] == 3 and not printed["preflight_errors"]
    plan = json.loads(Path(out).read_text())
    assert plan["source_profile"] == SOURCE_PROFILE_ALPACA_DAILY
    assert plan["certification"]["source_profile"] == SOURCE_PROFILE_ALPACA_DAILY and plan["certification"]["consistent"]
    assert all(s["cache_file"].replace("\\", "/").split("/")[-2] == ALPACA_DIR for s in plan["symbols"])

    assert tool.main_raw(["--quiet", "build", "--plan", out, "--cache-only", "--ohlcv-history-source", "alpaca"]) == 0
    report = json.loads(capsys.readouterr().out)
    digest = report["manifest_digest"]
    assert report["ok"] and report["counters"]["provider_calls"] == 0
    assert MarketConditionStore(alpaca_root).read_manifest(digest)["source_profile"] == SOURCE_PROFILE_ALPACA_DAILY

    for argv in (["verify", "--manifest", digest], ["prepare-host", "--manifest", digest, "--profile", "ohlcv-v1"]):
        assert tool.main_raw(["--quiet", *argv, "--cache-root", alpaca_root, "--ohlcv-history-source", "alpaca"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
        # ... and the same digest under the OTHER vendor is refused, not silently accepted.
        assert tool.main_raw(["--quiet", *argv, "--cache-root", alpaca_root, "--ohlcv-history-source", "fmp"]) == 1
        capsys.readouterr()


def test_a_plan_and_a_build_must_agree_on_the_vendor_and_alpaca_cannot_fetch(tool, monkeypatch, alpaca_root,
                                                                           universe_file, tmp_path, capsys):
    monkeypatch.undo()
    out = str(tmp_path / "plan.json")
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 0
    capsys.readouterr()
    assert tool.main_raw(["--quiet", "build", "--plan", out, "--cache-only", "--ohlcv-history-source", "fmp"]) == 2
    assert tool.main_raw(["--quiet", "build", "--plan", out, "--fetch-missing", "--ohlcv-history-source", "alpaca"]) == 2
    err = capsys.readouterr().err
    assert "prepare_history.py" in err


def test_alpaca_without_a_build_manifest_is_a_configuration_error(tool, monkeypatch, root, universe_file, tmp_path,
                                                                  capsys):
    """The split-basis preflight must not pass vacuously: no record of the applied splits, no plan."""
    monkeypatch.undo()
    _write_alpaca(root, "AAA", _frame(_sessions(date(2023, 9, 1), date(2024, 6, 28)), seed=1))
    assert tool.main_raw(_vendor_plan_args(root, universe_file, str(tmp_path / "plan.json"), "alpaca")) == 2
    assert "prepare_history.py" in capsys.readouterr().err


def test_alpaca_preflight_catches_a_split_the_build_claims_but_the_series_does_not_absorb(
        tool, monkeypatch, alpaca_root, universe_file, tmp_path, capsys):
    """Equivalent of the FMP split-basis preflight: the applied splits (build manifest) are checked against the
    stored series. A 4:1 split on 2024-03-15 that the manifest lists, with the close still dropping 4x there,
    is an un-absorbed split -> actionable plan, nothing built."""
    monkeypatch.undo()
    days = _sessions(date(2023, 9, 1), date(2024, 6, 28))
    df = _frame(days, seed=7, base=100.0)
    cut = pd.Timestamp("2024-03-15")
    for col in ("Open", "High", "Low", "Close"):
        df.loc[df["Date"] >= cut, col] = df.loc[df["Date"] >= cut, col] / 4.0
    _write_alpaca(alpaca_root, "BBB", df)
    _write_build_manifest(alpaca_root, {"AAA": [], "BBB": [["2024-03-15", 4.0]], "CCC": []})
    out = str(tmp_path / "plan.json")
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 1
    printed = json.loads(capsys.readouterr().out)
    bbb = [s for s in json.loads(Path(out).read_text())["symbols"] if s["symbol"] == "BBB"][0]
    assert bbb["split_events"] == [["2024-03-15", 4.0]]
    assert bbb["split_checks"] and printed["inventory"]


def test_alpaca_plan_needs_a_pass_gate_certificate_of_the_current_build(tool, monkeypatch, alpaca_root, universe_file,
                                                                         tmp_path, capsys):
    """The gate certifies the Alpaca cache's split basis: no certificate, a FAIL one, or one of another build -> refused."""
    from ba2_providers.ohlcv.alpaca_history import verify as ver
    monkeypatch.undo()
    out = str(tmp_path / "plan.json")
    meta = Path(alpaca_root) / ALPACA_DIR / "_meta"
    (meta / ver.GATE_CERTIFICATE_NAME).unlink()
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 1
    assert "gate certificate" in json.dumps(json.loads(capsys.readouterr().out)["preflight_errors"])
    ver.write_gate_certificate(str(meta), {"yahoo": {"status": ver.STATUS_FAIL}})
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 1
    assert "FAILED" in json.dumps(json.loads(capsys.readouterr().out)["preflight_errors"])
    ver.write_gate_certificate(str(meta), {"yahoo": {"status": ver.STATUS_PASS}})
    _write_build_manifest(alpaca_root, {s: [] for s in UNIVERSE}, certify=False, run_id="rebuilt")  # after the gate ran
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 1
    assert "re-run the gate" in json.dumps(json.loads(capsys.readouterr().out)["preflight_errors"])
    _write_build_manifest(alpaca_root, {s: [] for s in UNIVERSE})
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 0
    assert json.loads(Path(out).read_text())["certification"]["gate"]["build_run_id"] == "test-build"


def test_alpaca_split_checks_that_ask_for_a_refetch_are_informational_once_the_gate_passed(
        tool, monkeypatch, alpaca_root, universe_file, tmp_path, capsys):
    """A small applied factor (a 1.01 stock dividend) is ``undetectable`` from prices: on FMP that asks for a re-fetch,
    on the gate-certified Alpaca cache it is recorded and does not block (SCCO, ITUB 2026-10-21)."""
    monkeypatch.undo()
    _write_build_manifest(alpaca_root, {"AAA": [], "BBB": [["2024-03-15", 1.01]], "CCC": []})
    out = str(tmp_path / "plan.json")
    assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, "alpaca")) == 0
    bbb = [s for s in json.loads(Path(out).read_text())["symbols"] if s["symbol"] == "BBB"][0]
    assert [c["verdict"] for c in bbb["split_checks"]] == ["undetectable"] and bbb["refetch_required"] is False


def test_the_two_sources_never_collide_in_the_feature_store(tool, monkeypatch, alpaca_root, universe_file, tmp_path,
                                                            capsys):
    """Same cache root, same symbols/profile/window, different prices per vendor: the manifests, the feature
    objects and the raw shards are disjoint; neither build reuses a row of the other; both still verify."""
    from ba2_common.core.market_condition_store import MarketConditionStore
    digests, manifests = {}, {}
    for vendor in ("fmp", "alpaca"):
        if vendor == "alpaca":
            monkeypatch.undo()
        out = str(tmp_path / f"plan.{vendor}.json")
        assert tool.main_raw(_vendor_plan_args(alpaca_root, universe_file, out, vendor)) == 0
        capsys.readouterr()
        assert tool.main_raw(["--quiet", "build", "--plan", out, "--cache-only", "--ohlcv-history-source", vendor]) == 0
        rep = json.loads(capsys.readouterr().out)
        assert rep["counters"].get("rows_reused", 0) == 0, f"{vendor} reused rows it did not compute"
        digests[vendor] = rep["manifest_digest"]
        manifests[vendor] = MarketConditionStore(alpaca_root).read_manifest(digests[vendor])
    assert digests["fmp"] != digests["alpaca"]
    assert manifests["fmp"]["source_profile"] != manifests["alpaca"]["source_profile"]
    shas = {v: {o["sha256"] for o in m["objects"]} for v, m in manifests.items()}
    raws = {v: {o["sha256"] for o in m["raw_objects"]} for v, m in manifests.items()}
    assert shas["fmp"] and shas["alpaca"] and not (shas["fmp"] & shas["alpaca"])
    assert raws["fmp"] and raws["alpaca"] and not (raws["fmp"] & raws["alpaca"])
    for vendor, digest in digests.items():
        assert tool.main_raw(["--quiet", "verify", "--manifest", digest, "--cache-root", alpaca_root,
                              "--ohlcv-history-source", vendor]) == 0
        capsys.readouterr()


def test_identical_bars_of_two_sources_are_never_offered_as_reusable_rows(tool, monkeypatch, root, universe_file,
                                                                          tmp_path, capsys):
    """Even with byte-identical prices the plan of the second vendor reports no reusable rows from the first
    vendor's manifest: reuse is within one source profile (the store still dedups identical bytes)."""
    for i, sym in enumerate(UNIVERSE):
        _write_alpaca(root, sym, _frame(_sessions(date(2023, 9, 1), date(2024, 6, 28)), seed=i + 3))  # == fmp frames
    _write_alpaca(root, "AAPL", _frame(_sessions(date(2020, 6, 1), date(2020, 10, 30)), seed=1, base=120.0))
    _write_alpaca(root, "NVDA", _frame(_sessions(date(2024, 4, 1), date(2024, 8, 30)), seed=2, base=110.0))
    _write_build_manifest(root, {s: [] for s in UNIVERSE})
    out = str(tmp_path / "plan.fmp.json")
    assert tool.main_raw(_vendor_plan_args(root, universe_file, out, "fmp")) == 0
    capsys.readouterr()
    assert tool.main_raw(["--quiet", "build", "--plan", out, "--cache-only", "--ohlcv-history-source", "fmp"]) == 0
    capsys.readouterr()
    reusable_fmp_replan = None
    assert tool.main_raw(_vendor_plan_args(root, universe_file, str(tmp_path / "again.fmp.json"), "fmp")) == 0
    capsys.readouterr()
    reusable_fmp_replan = sum(s["rows_reusable"] for s in json.loads((tmp_path / "again.fmp.json").read_text())["symbols"])
    assert reusable_fmp_replan > 0                      # the control: the SAME vendor does reuse its rows
    monkeypatch.undo()
    out2 = str(tmp_path / "plan.alpaca.json")
    assert tool.main_raw(_vendor_plan_args(root, universe_file, out2, "alpaca")) == 0
    capsys.readouterr()
    assert sum(s["rows_reusable"] for s in json.loads(Path(out2).read_text())["symbols"]) == 0


# ----------------------------------------------------------------- repair (stage-2, 2026-09-26)
def test_repair_with_nothing_to_repair_exits_zero_and_fetches_nothing(tool, root, universe_file,
                                                                       tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tool, "us_session_open", lambda: False)
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out) + ["--ohlcv-history-source", "fmp"]) == 0
    capsys.readouterr()
    assert tool.main(["--quiet", "repair", "--ohlcv-history-source", "fmp", "--plan", out]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and rep["refetched"] == [] and rep["still_refused"] == {}


def test_repair_says_a_rerun_after_the_close_is_required_when_the_session_was_open(
        tool, root, universe_file, tmp_path, monkeypatch, capsys):
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out) + ["--ohlcv-history-source", "fmp"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(tool, "us_session_open", lambda: True)
    assert tool.main(["--quiet", "repair", "--ohlcv-history-source", "fmp", "--plan", out, "--allow-open-session"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["rerun_after_close_required"] is True


def test_repair_refuses_while_the_us_session_is_open_unless_allowed(tool, root, universe_file,
                                                                     tmp_path, monkeypatch, capsys):
    """A full re-fetch during the session stores that day's PARTIAL bar (stage-2 pass A). The
    repair waits for the close unless the operator says otherwise."""
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out) + ["--ohlcv-history-source", "fmp"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(tool, "us_session_open", lambda: True)
    assert tool.main(["--quiet", "repair", "--ohlcv-history-source", "fmp", "--plan", out]) == 2
    assert "session" in capsys.readouterr().err
    assert tool.main(["--quiet", "repair", "--ohlcv-history-source", "fmp", "--plan", out, "--allow-open-session"]) == 0


def test_repair_hands_the_tail_keep_to_the_source(tool, root, universe_file, tmp_path, monkeypatch, capsys):
    seen = []

    def make(cache_root, history_source, **kw):
        seen.append(kw)
        return _NoFetchSource()

    monkeypatch.setattr(tool, "make_source", make)
    monkeypatch.setattr(tool, "us_session_open", lambda: False)
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out) + ["--ohlcv-history-source", "fmp"]) == 0
    seen.clear()
    capsys.readouterr()
    assert tool.main(["--quiet", "repair", "--ohlcv-history-source", "fmp", "--plan", out, "--keep-newer-cached-tail", "sats,Xyz"]) == 0
    assert seen == [{"keep_newer_cached_tail": ["SATS", "XYZ"]}]
    rep = json.loads(capsys.readouterr().out)
    assert rep["keep_newer_cached_tail"] == ["SATS", "XYZ"] and rep["kept_tails"] == {}


def test_repair_refuses_alpaca_and_needs_the_flag(tool, root, universe_file, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tool, "us_session_open", lambda: False)
    out = str(tmp_path / "plan.json")
    assert tool.main(_plan_args(root, universe_file, out) + ["--ohlcv-history-source", "fmp"]) == 0
    capsys.readouterr()
    assert tool.main_raw(["--quiet", "repair", "--plan", out]) == 2                 # flag missing
    capsys.readouterr()
    assert tool.main(["--quiet", "repair", "--plan", out, "--ohlcv-history-source", "alpaca"]) == 2
    assert "prepare_history.py" in capsys.readouterr().err
