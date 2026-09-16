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
    monkeypatch.setattr(m, "make_source", lambda cache_root: _NoFetchSource())
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
    monkeypatch.setattr(tool, "make_source", lambda cache_root: _BrokenCalendarSource())
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
