"""JSON-Lines scoring shards: the format exists so LOAD PEAK stops being set by a dict.

The column table cut live memory 480MB -> 208MB but peak only 641MB -> 544MB, because
``json.load`` on the legacy single-object shard materialises every entry before the table can be
built -- and RSS follows peak, so most of the win was unrealised. A line-per-entry file is parsed
one entry at a time, so peak is the table itself.

Format is chosen by EXTENSION, not by sniffing: a legacy shard is a single line and is itself
valid JSON, so "read the first line and decide" costs a full parse -- exactly what this avoids.
"""
import json

import importlib

import pytest

from ba2_experts.FMPSenateTraderWeight import (
    FMPSenateTraderWeight, clear_worker_scoring_cache, flush_all_scoring_caches,
)
from ba2_experts.scoring_table import ScoringTable

mod = importlib.import_module("ba2_experts.FMPSenateTraderWeight")

SHARD = "congress_skill_scores__60_5_50_12.json"
ENTRIES = {
    "Alice|10|2024-01-02": {"skill_score": 0.5, "scored_trades": 3, "hit_rate": None,
                            "avg_fwd_return_pct": 1.25},
    "Bob|20|2024-01-02": {"skill_score": 0.0, "scored_trades": 0, "hit_rate": 0.33,
                          "avg_fwd_return_pct": -0.5},
}


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    (tmp_path / "fmp_history").mkdir()
    clear_worker_scoring_cache()
    yield tmp_path / "fmp_history"
    clear_worker_scoring_cache()


def _expert():
    return FMPSenateTraderWeight.__new__(FMPSenateTraderWeight)


def _write_jsonl(cache_dir, name, entries):
    with open(cache_dir / name, "w", encoding="utf-8") as f:
        for k, v in entries.items():
            f.write(json.dumps({"k": k, "v": v}) + "\n")


# --------------------------------------------------------------------------- #
# the streaming read
# --------------------------------------------------------------------------- #
def test_a_jsonl_shard_loads(cache_dir):
    _write_jsonl(cache_dir, "congress_skill_scores__60_5_50_12.jsonl", ENTRIES)
    got = _expert()._load_scoring_cache("_skill_cache", SHARD)
    assert len(got) == 2
    for k, v in ENTRIES.items():
        assert got[k] == v


def test_jsonl_preserves_none_and_int_types(cache_dir):
    """Same fidelity bar as the in-memory table -- the format must not launder types."""
    _write_jsonl(cache_dir, "congress_skill_scores__60_5_50_12.jsonl", ENTRIES)
    got = _expert()._load_scoring_cache("_skill_cache", SHARD)["Alice|10|2024-01-02"]
    assert got["hit_rate"] is None
    assert type(got["scored_trades"]) is int


def test_blank_lines_are_tolerated(cache_dir):
    p = cache_dir / "congress_skill_scores__60_5_50_12.jsonl"
    p.write_text('\n{"k":"a","v":{"skill_score":1.0}}\n\n')
    got = _expert()._load_scoring_cache("_skill_cache", SHARD)
    assert len(got) == 1 and got["a"]["skill_score"] == 1.0


# --------------------------------------------------------------------------- #
# coexistence with the legacy format
# --------------------------------------------------------------------------- #
def test_legacy_json_still_loads(cache_dir):
    """Unconverted shards must keep working -- they just don't get the peak win."""
    (cache_dir / SHARD).write_text(json.dumps(ENTRIES))
    got = _expert()._load_scoring_cache("_skill_cache", SHARD)
    assert len(got) == 2 and got["Bob|20|2024-01-02"] == ENTRIES["Bob|20|2024-01-02"]


def test_jsonl_wins_when_both_exist(cache_dir):
    """After a flush the .json is retired, but a leftover must never shadow the newer file."""
    (cache_dir / SHARD).write_text(json.dumps({"stale|1|2024-01-01": {"skill_score": -99.0}}))
    _write_jsonl(cache_dir, "congress_skill_scores__60_5_50_12.jsonl", ENTRIES)
    got = _expert()._load_scoring_cache("_skill_cache", SHARD)
    assert "stale|1|2024-01-01" not in got and len(got) == 2


def test_a_missing_shard_is_still_empty_not_fatal(cache_dir):
    assert len(_expert()._load_scoring_cache("_skill_cache", SHARD)) == 0


# --------------------------------------------------------------------------- #
# the write side
# --------------------------------------------------------------------------- #
def test_flush_writes_jsonl_and_retires_the_legacy_json(cache_dir):
    """Leaving the .json behind would let a stale base outlive the fresh one invisibly, since
    the loader prefers .jsonl."""
    (cache_dir / SHARD).write_text(json.dumps(ENTRIES))
    e = _expert()
    e._load_scoring_cache("_skill_cache", SHARD)["new|1|2024-02-01"] = {"skill_score": 9.0}
    flush_all_scoring_caches()

    assert (cache_dir / "congress_skill_scores__60_5_50_12.jsonl").exists()
    assert not (cache_dir / SHARD).exists(), "legacy base was not retired"


def test_flushed_jsonl_reloads_identically(cache_dir):
    """Round trip through disk in the new format."""
    _write_jsonl(cache_dir, "congress_skill_scores__60_5_50_12.jsonl", ENTRIES)
    e = _expert()
    e._load_scoring_cache("_skill_cache", SHARD)
    flush_all_scoring_caches()
    clear_worker_scoring_cache()

    got = _expert()._load_scoring_cache("_skill_cache", SHARD)
    assert {k: got[k] for k in got.keys()} == ENTRIES


def test_a_delta_still_layers_over_a_jsonl_base(cache_dir):
    """The delta mechanism is unchanged and must keep applying on top of the new base."""
    _write_jsonl(cache_dir, "congress_skill_scores__60_5_50_12.jsonl", ENTRIES)
    (cache_dir / (SHARD + ".delta.jsonl")).write_text(
        json.dumps({"k": "Alice|10|2024-01-02", "v": {"skill_score": 77.0}}) + "\n")
    got = _expert()._load_scoring_cache("_skill_cache", SHARD)
    assert got["Alice|10|2024-01-02"]["skill_score"] == 77.0


def test_plain_dict_mode_reads_a_converted_shard(cache_dir, monkeypatch):
    """REGRESSION. The conversion tool REMOVES the legacy .json, so if the .jsonl read were
    gated on the table being enabled, BA2_SCORING_TABLE=0 would load every shard as EMPTY --
    silently, because an empty scoring cache is valid (it just recomputes). The escape hatch
    must read whatever is on disk; only the in-memory representation differs. Found by the
    control arm of the memory A/B reporting 0 MB loaded."""
    monkeypatch.setattr(mod, "_USE_SCORING_TABLE", False)
    _write_jsonl(cache_dir, "congress_skill_scores__60_5_50_12.jsonl", ENTRIES)
    got = _expert()._load_scoring_cache("_skill_cache", SHARD)
    assert isinstance(got, dict)
    assert len(got) == 2, "converted shard loaded EMPTY with the table disabled"
    assert got["Alice|10|2024-01-02"] == ENTRIES["Alice|10|2024-01-02"]


def test_plain_dict_mode_still_writes_legacy_json(cache_dir, monkeypatch):
    """BA2_SCORING_TABLE=0 must stay round-trippable, or the escape hatch is one-way."""
    monkeypatch.setattr(mod, "_USE_SCORING_TABLE", False)
    (cache_dir / SHARD).write_text(json.dumps(ENTRIES))
    e = _expert()
    got = e._load_scoring_cache("_skill_cache", SHARD)
    assert isinstance(got, dict)
    flush_all_scoring_caches()
    assert (cache_dir / SHARD).exists()
    assert json.loads((cache_dir / SHARD).read_text()) == ENTRIES


# --------------------------------------------------------------------------- #
# the primitive
# --------------------------------------------------------------------------- #
def test_table_jsonl_round_trip(tmp_path):
    src = {"a": {"x": 1, "y": None}, "b": {"x": 2.5, "y": 0.0}}
    p = tmp_path / "t.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        ScoringTable.from_dict(src).dump_jsonl(f)
    with open(p, "r", encoding="utf-8") as f:
        assert ScoringTable.from_jsonl(f).to_dict() == src


def test_jsonl_has_one_line_per_entry(tmp_path):
    """The property that makes it streamable."""
    src = {f"k{i}": {"v": float(i)} for i in range(50)}
    p = tmp_path / "t.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        ScoringTable.from_dict(src).dump_jsonl(f)
    assert len(p.read_text().strip().splitlines()) == 50
