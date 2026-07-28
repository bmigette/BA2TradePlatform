"""Prewarm must persist EVERY shard it computed, to the path it read each from (2026-07-28).

REGRESSION, and a silent one. ``_do_senate_scores`` scores with ``is_live=False``, which disables
the throttled delta writes, so its ONLY persistence was a single
``_save_scoring_cache("_skill_cache", _SKILL_CACHE_FILE)``. After sharding (ffbf9f5) that call was
wrong twice over:

  1. it wrote whichever ONE shard ``self._skill_cache`` last pointed at -- the loops walk
     horizon x lookback shards, so the other combos' scores were discarded; and
  2. it wrote to the UNSHARDED ``congress_skill_scores.json``, a filename no trial reads and which
     is not even on disk (the one-off splitter 9c96d34 moved the originals into shards).

So a multi-hour prewarm persisted a fraction of its work to a dead file and still printed
"prewarm done: N scores". Nothing errored, because a scoring cache is a pure memo -- being wrong
about it costs only time. Same shape as the ATR tz bug and the LRU cap.
"""
import json

import importlib

import pytest

from ba2_experts.FMPSenateTraderWeight import (
    FMPSenateTraderWeight, clear_worker_scoring_cache, flush_all_scoring_caches,
    set_scoring_cache_max,
)

mod = importlib.import_module("ba2_experts.FMPSenateTraderWeight")


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    (tmp_path / "fmp_history").mkdir()
    original_max = mod._WORKER_SCORING_CACHE_MAX
    clear_worker_scoring_cache()
    yield tmp_path / "fmp_history"
    clear_worker_scoring_cache()
    mod._WORKER_SCORING_CACHE_MAX = original_max


def _expert():
    return FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)


SHARDS = [f"congress_skill_scores__{h}_5_50_{lb}.json" for h in (30, 60, 90) for lb in (6, 12)]


def _read_flushed(cache_dir, name):
    """Read back what a flush wrote. Flushes land in the streamable JSON-Lines sibling (one
    ``{"k":…,"v":…}`` per line), not the legacy single-object ``.json`` — see
    test_scoring_cache_jsonl.py."""
    p = cache_dir / (name[:-len(".json")] + ".jsonl")
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            out[e["k"]] = e["v"]
    return out


def test_every_resident_shard_is_written_to_its_own_path(cache_dir):
    """The core fix: 6 shards in, 6 shards out, each where it came from."""
    set_scoring_cache_max(len(SHARDS))
    e = _expert()
    for i, name in enumerate(SHARDS):
        (cache_dir / name).write_text("{}")
        cache = e._load_scoring_cache("_skill_cache", name)
        cache[f"trader|{i}|2024-01-01"] = {"skill_score": float(i)}

    assert flush_all_scoring_caches() == len(SHARDS)

    for i, name in enumerate(SHARDS):
        assert _read_flushed(cache_dir, name) == {f"trader|{i}|2024-01-01": {"skill_score": float(i)}}, (
            f"{name} did not receive its own scores")


def test_nothing_is_written_to_the_unsharded_filename(cache_dir):
    """The dead file the old flush targeted. Trials read shards; a write here helps nobody."""
    set_scoring_cache_max(len(SHARDS))
    e = _expert()
    for name in SHARDS:
        (cache_dir / name).write_text("{}")
        e._load_scoring_cache("_skill_cache", name)["k"] = {"skill_score": 1.0}
    flush_all_scoring_caches()
    assert not (cache_dir / FMPSenateTraderWeight._SKILL_CACHE_FILE).exists()


def test_an_evicted_shard_is_silently_absent_from_the_flush(cache_dir):
    """WHY prewarm must raise the cap. The flush can only persist what is still RESIDENT, so a
    shard the LRU dropped mid-loop takes its scores with it -- and the flush reports success for
    the ones that survived. This test documents the loss so the launcher's count-check has a
    reason to exist."""
    mod._WORKER_SCORING_CACHE_MAX = 2
    e = _expert()
    for name in SHARDS:
        (cache_dir / name).write_text("{}")
        e._load_scoring_cache("_skill_cache", name)["k"] = {"skill_score": 1.0}

    assert flush_all_scoring_caches() == 2, "only the still-resident shards can be written"
    written = [n for n in SHARDS if _read_flushed(cache_dir, n)]
    assert len(written) == 2 and written == SHARDS[-2:], "the surviving shards are the newest two"


def test_raising_the_cap_lets_every_shard_survive_to_the_flush(cache_dir):
    """The launcher's fix, end to end."""
    mod._WORKER_SCORING_CACHE_MAX = 2
    set_scoring_cache_max(len(SHARDS))
    e = _expert()
    for name in SHARDS:
        (cache_dir / name).write_text("{}")
        e._load_scoring_cache("_skill_cache", name)["k"] = {"skill_score": 1.0}
    assert flush_all_scoring_caches() == len(SHARDS)


def test_set_scoring_cache_max_never_lowers_the_cap():
    """The default is a correctness FLOOR (a trial's 3-file working set), not a preference."""
    original = mod._WORKER_SCORING_CACHE_MAX
    try:
        assert set_scoring_cache_max(1) == original
        assert mod._WORKER_SCORING_CACHE_MAX == original
    finally:
        mod._WORKER_SCORING_CACHE_MAX = original


def test_flush_folds_away_the_delta(cache_dir):
    """The flush is COMPACTING -- it writes the merged dict, so a stale delta left behind would
    be re-applied on the next load and could resurrect superseded entries."""
    set_scoring_cache_max(3)
    name = SHARDS[0]
    (cache_dir / name).write_text("{}")
    delta = cache_dir / (name + ".delta.jsonl")
    delta.write_text(json.dumps({"k": "old", "v": {"skill_score": 0.0}}) + "\n")
    e = _expert()
    e._load_scoring_cache("_skill_cache", name)["new"] = {"skill_score": 1.0}
    flush_all_scoring_caches()
    assert not delta.exists()


def test_flush_with_nothing_resident_is_zero_not_an_error(cache_dir):
    assert flush_all_scoring_caches() == 0
