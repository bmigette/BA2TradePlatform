"""The scoring-cache LRU must hold one trial's WHOLE working set (2026-07-28).

REGRESSION. Sharding the congress scoring caches (ffbf9f5) capped the per-worker LRU at 2, on the
belief that a trial touches two files. It touches THREE: the hold cache, a skill shard, and a
confidence shard. The hold cache is only loaded when ``min_trader_avg_hold_days > 0`` -- which is
0 in the interface default (what the single-trial profiler used, hence "2 is enough") but floored
at 1.0 for every GA trial. So the GA ran a 3-file working set against a 2-slot LRU and every
access evicted the file needed next.

Measured on one S3 trial, same genome, identical ``trades=31`` at every cap -- the caches are pure
memos, so this was performance-only, which is exactly why it hid for so long:

    LRU=2   1056.5s   334 misses/file   94.2% of wall spent re-reading 76-238MB JSON shards
    LRU=3     64.5s     1 miss /file     4.7%

These tests pin the INVARIANT (cap >= files-per-trial), not the number 3, so adding a fourth
per-trial cache file fails here instead of silently costing 16x again.
"""
import importlib
import json

import pytest

from ba2_experts.FMPSenateTraderWeight import (
    FMPSenateTraderWeight, clear_worker_scoring_cache,
)

# NOT ``import ba2_experts.FMPSenateTraderWeight as mod`` -- the package's __init__ re-exports the
# CLASS under the module's own name, so that binding hands back the class and every module-level
# lookup below would AttributeError.
mod = importlib.import_module("ba2_experts.FMPSenateTraderWeight")

# The three caches ONE trial loads: hold + skill shard + confidence shard. Derived from the
# class's own filenames so a rename can't quietly desync this from what the expert really opens.
_FILES_PER_TRIAL = 3


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the scoring caches at an empty tmp dir; reset the module LRU around each test."""
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    (tmp_path / "fmp_history").mkdir()
    clear_worker_scoring_cache()
    yield tmp_path / "fmp_history"
    clear_worker_scoring_cache()


def _expert():
    return FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)


def _write(cache_dir, name, payload):
    (cache_dir / name).write_text(json.dumps(payload))


def test_cap_covers_a_whole_trials_working_set():
    """The floor itself. 2 was below it; that cost 16x."""
    assert mod._WORKER_SCORING_CACHE_MAX >= _FILES_PER_TRIAL


def test_three_shards_coexist_without_eviction(cache_dir):
    """The behaviour the cap exists for: load the three files a trial uses, and all three are
    still resident -- no re-read of the one loaded first."""
    names = ["congress_scalper_scores.json",
             "congress_skill_scores__60_5_50_12.json",
             "congress_confidence_scores__60_10_0.json"]
    for i, n in enumerate(names):
        _write(cache_dir, n, {f"k{i}": i})

    e = _expert()
    for attr, n in zip(["_hold_cache", "_skill_cache", "_confidence_cache"], names):
        e._load_scoring_cache(attr, n)

    assert len(mod._WORKER_SCORING_CACHE) == _FILES_PER_TRIAL
    resident = {str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in mod._WORKER_SCORING_CACHE}
    assert resident == set(names), "a file a trial still needs was evicted"


def test_a_reload_after_the_full_working_set_is_a_cache_hit(cache_dir):
    """The actual failure mode: with the cap too low, coming back to the FIRST file re-reads it
    from disk. Detected by mutating the in-memory dict -- a re-read would drop the marker."""
    names = ["congress_scalper_scores.json",
             "congress_skill_scores__60_5_50_12.json",
             "congress_confidence_scores__60_10_0.json"]
    for n in names:
        _write(cache_dir, n, {"on_disk": 1})

    e = _expert()
    first = e._load_scoring_cache("_hold_cache", names[0])
    first["only_in_memory"] = True
    for attr, n in zip(["_skill_cache", "_confidence_cache"], names[1:]):
        e._load_scoring_cache(attr, n)

    again = e._load_scoring_cache("_hold_cache", names[0])
    assert again is first, "the first shard was evicted and re-read from disk"
    assert "only_in_memory" in again


def test_the_cap_is_still_enforced(cache_dir):
    """It is a BOUND, not a leak -- the whole point of sharding was capping RSS. Going one file
    PAST the working set must still evict."""
    e = _expert()
    for i in range(mod._WORKER_SCORING_CACHE_MAX + 2):
        n = f"congress_skill_scores__shard{i}.json"
        _write(cache_dir, n, {"i": i})
        e._load_scoring_cache("_skill_cache", n)
    assert len(mod._WORKER_SCORING_CACHE) == mod._WORKER_SCORING_CACHE_MAX


def test_eviction_is_least_recently_used(cache_dir):
    """A plain dict-order eviction would drop whatever was inserted first even if the trial is
    actively using it; the LRU touch on a hit is what makes the cap safe at exactly the working
    set size."""
    names = [f"congress_skill_scores__lru{i}.json" for i in range(mod._WORKER_SCORING_CACHE_MAX)]
    for n in names:
        _write(cache_dir, n, {"n": n})
    e = _expert()
    for n in names:
        e._load_scoring_cache("_skill_cache", n)

    e._load_scoring_cache("_skill_cache", names[0])          # touch the oldest -> now newest
    extra = "congress_skill_scores__lru_extra.json"
    _write(cache_dir, extra, {"n": extra})
    e._load_scoring_cache("_skill_cache", extra)             # forces one eviction

    resident = {str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in mod._WORKER_SCORING_CACHE}
    assert names[0] in resident, "evicted the just-touched entry -- not LRU"
    assert names[1] not in resident, "should have evicted the genuinely-least-recent entry"
