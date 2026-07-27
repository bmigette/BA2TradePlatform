"""Scoring caches are SHARDED by settings suffix (2026-07-27).

WHY. ``_load_scoring_cache`` json.load()s a whole cache into ``_WORKER_SCORING_CACHE``, a
module-level dict -- one copy PER POOL CHILD. Measured on remote150 during Senate S4:
skill 3,406,775 entries = 2.22GB, confidence 1,778,046 = 2.95GB, so 5.17GB per process and
~20.7GB across four children. That drove the box to 99.2% memory, trials swapped, and six
remote trials blew past the master's 1800s budget.

The fix exploits the key shape. Both caches carry their SETTINGS in the key suffix, and one GA
trial uses exactly ONE settings combination:

    skill       "<trader>|<n>|<as_of>|<horizon>|<min_past>|<max_past>|<lookback>"
    confidence  "<trader>|<n>|<SYM>|<side>|<as_of>|<max_exec_days>|<focus_cap_pct>"

Measured on the real files: 6 suffixes each. skill splits evenly (~567,798 each); confidence
unevenly (655,130 / 604,424 / 300,002 / 141,925 / 76,275 / 300). So a trial's working set is
one shard -- ~370MB + ~1.09GB worst case = ~1.46GB against 5.17GB -- as a PLAIN DICT, with no
lookup slowdown. (A columnar mmapped store was built and rejected first: 20.19x slower per hit,
see test_files/bench_scoring_store.py and commit ecfe14c.)

Sharding also bounds memory as the cache GROWS, which the monolithic file never did: every new
parameter combination the GA explores used to inflate every process's RSS.
"""
import importlib
import json
import os

import pytest

from ba2_experts.FMPSenateTraderWeight import (
    FMPSenateTraderWeight, _shard_filename, clear_worker_scoring_cache,
)

# NOT `import ba2_experts.FMPSenateTraderWeight as m` -- the package re-exports the CLASS under
# that same dotted name, so the plain import binds the class and monkeypatching module globals
# (_WORKER_SCORING_CACHE_MAX) would silently target the wrong object.
MOD = importlib.import_module("ba2_experts.FMPSenateTraderWeight")


@pytest.fixture(autouse=True)
def _clean():
    clear_worker_scoring_cache()
    yield
    clear_worker_scoring_cache()


# --------------------------------------------------------------------------- #
# the shard-name mapping
# --------------------------------------------------------------------------- #
def test_shard_filename_is_filesystem_safe():
    """'|' is illegal in a Windows filename and '.' would fake an extension."""
    n = _shard_filename("congress_skill_scores.json", "60|5|50|12")
    assert "|" not in n
    assert n.endswith(".json")
    assert n == "congress_skill_scores__60_5_50_12.json"


def test_float_suffix_survives():
    """confidence's focus-cap is a float ('20.0'); its '.' must not split the extension."""
    n = _shard_filename("congress_confidence_scores.json", "105|20.0")
    assert n == "congress_confidence_scores__105_20_0.json"
    assert n.count(".") == 1


def test_distinct_settings_get_distinct_shards():
    a = _shard_filename("congress_skill_scores.json", "60|5|50|12")
    b = _shard_filename("congress_skill_scores.json", "60|5|50|6")
    c = _shard_filename("congress_skill_scores.json", "90|5|50|12")
    assert len({a, b, c}) == 3


# --------------------------------------------------------------------------- #
# isolation: the whole point is that one shard never pulls in another
# --------------------------------------------------------------------------- #
def _write(tmp_path, name, payload):
    d = tmp_path / "fmp_history"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def test_loading_one_shard_does_not_load_another(tmp_path, monkeypatch):
    monkeypatch.setattr("ba2_common.config.CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(MOD.FMPSenateTraderWeight, "_scoring_cache_path",
                        lambda self, fn: os.path.join(str(tmp_path), "fmp_history", fn))

    _write(tmp_path, "congress_skill_scores__60_5_50_12.json", {"A|1|d|60|5|50|12": {"skill_score": 1.0}})
    _write(tmp_path, "congress_skill_scores__90_5_50_12.json", {"B|1|d|90|5|50|12": {"skill_score": 2.0}})

    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    got = e._load_scoring_cache("_skill_cache", "congress_skill_scores__60_5_50_12.json")

    assert "A|1|d|60|5|50|12" in got
    assert "B|1|d|90|5|50|12" not in got, "loaded a shard it was never asked for"
    assert len(got) == 1


def test_each_shard_is_memoized_separately(tmp_path, monkeypatch):
    """Two shards in one process must be two entries, not one clobbering the other."""
    monkeypatch.setattr(MOD.FMPSenateTraderWeight, "_scoring_cache_path",
                        lambda self, fn: os.path.join(str(tmp_path), "fmp_history", fn))
    _write(tmp_path, "congress_skill_scores__60_5_50_12.json", {"A|1": {"skill_score": 1.0}})
    _write(tmp_path, "congress_skill_scores__90_5_50_12.json", {"B|1": {"skill_score": 2.0}})

    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    a = e._load_scoring_cache("_skill_cache", "congress_skill_scores__60_5_50_12.json")
    b = e._load_scoring_cache("_skill_cache", "congress_skill_scores__90_5_50_12.json")
    assert a is not b
    assert a["A|1"]["skill_score"] == 1.0
    assert b["B|1"]["skill_score"] == 2.0


def test_missing_shard_starts_empty_not_fatal(tmp_path, monkeypatch):
    """A shard that was never built must not crash the trial. Scores are a pure function of
    their inputs, so an empty shard is merely slower -- never wrong."""
    monkeypatch.setattr(MOD.FMPSenateTraderWeight, "_scoring_cache_path",
                        lambda self, fn: os.path.join(str(tmp_path), "fmp_history", fn))
    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    got = e._load_scoring_cache("_skill_cache", "congress_skill_scores__1_2_3_4.json")
    assert got == {}


# --------------------------------------------------------------------------- #
# the LRU cap -- a long-lived child must not re-accumulate every shard
# --------------------------------------------------------------------------- #
def test_worker_cache_evicts_beyond_the_cap(tmp_path, monkeypatch):
    """A pool child is reused across trials with DIFFERENT genomes. Without a cap it would end
    up holding all 6 shards and we would be back where we started."""
    monkeypatch.setattr(MOD, "_WORKER_SCORING_CACHE_MAX", 2)
    monkeypatch.setattr(MOD.FMPSenateTraderWeight, "_scoring_cache_path",
                        lambda self, fn: os.path.join(str(tmp_path), "fmp_history", fn))
    for i in range(4):
        _write(tmp_path, f"congress_skill_scores__{i}_5_50_12.json", {f"K{i}": {"skill_score": float(i)}})

    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    for i in range(4):
        e._load_scoring_cache("_skill_cache", f"congress_skill_scores__{i}_5_50_12.json")

    assert len(MOD._WORKER_SCORING_CACHE) <= 2, "shard cache grew past its cap"


def test_eviction_keeps_the_most_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "_WORKER_SCORING_CACHE_MAX", 2)
    monkeypatch.setattr(MOD.FMPSenateTraderWeight, "_scoring_cache_path",
                        lambda self, fn: os.path.join(str(tmp_path), "fmp_history", fn))
    for i in range(3):
        _write(tmp_path, f"congress_skill_scores__{i}_5_50_12.json", {f"K{i}": {"skill_score": float(i)}})

    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    e._load_scoring_cache("_skill_cache", "congress_skill_scores__0_5_50_12.json")
    e._load_scoring_cache("_skill_cache", "congress_skill_scores__1_5_50_12.json")
    e._load_scoring_cache("_skill_cache", "congress_skill_scores__0_5_50_12.json")  # touch 0
    e._load_scoring_cache("_skill_cache", "congress_skill_scores__2_5_50_12.json")  # evicts 1

    paths = [os.path.basename(p) for p in MOD._WORKER_SCORING_CACHE]
    assert "congress_skill_scores__0_5_50_12.json" in paths
    assert "congress_skill_scores__1_5_50_12.json" not in paths


# --------------------------------------------------------------------------- #
# writes land in the shard they belong to
# --------------------------------------------------------------------------- #
def test_delta_is_written_per_shard(tmp_path, monkeypatch):
    """Each shard gets its OWN delta file -- a shared delta would replay another shard's
    entries into this one on next load, silently mixing settings."""
    monkeypatch.setattr(MOD.FMPSenateTraderWeight, "_scoring_cache_path",
                        lambda self, fn: os.path.join(str(tmp_path), "fmp_history", fn))
    (tmp_path / "fmp_history").mkdir(parents=True, exist_ok=True)

    e = FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)
    a = e._scoring_cache_delta_path("congress_skill_scores__60_5_50_12.json")
    b = e._scoring_cache_delta_path("congress_skill_scores__90_5_50_12.json")
    assert a != b
