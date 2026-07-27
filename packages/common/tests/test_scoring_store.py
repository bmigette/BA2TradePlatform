"""ScoringStore: columnar, mmapped replacement for the plain-dict scoring caches.

WHY THIS EXISTS (2026-07-27). ``FMPSenateTraderWeight._load_scoring_cache`` ``json.load``s each
scoring cache into ``_WORKER_SCORING_CACHE``, a MODULE-level dict — so every pool child holds
its own full copy. Measured on remote150 during Senate S4:

    skill       3,406,775 entries   452MB on disk -> 2.22GB in RAM
    confidence  1,778,046 entries   657MB on disk -> 2.95GB in RAM
                                     1.1GB        -> 5.17GB per process

x4 children = ~20.7GB of the box's 55.7GB spent on four identical copies of the same read-only
table, which drove remote150 to 99.2% memory and made trials swap past the master's 1800s
budget (six timeouts, all in S4).

The values have a FIXED schema (always the same 4 or 12 numeric fields), which is what makes a
columnar layout viable. The base is immutable and mmapped, so all children share one copy via
the OS page cache; new entries go to a small private overlay dict.
"""
import numpy as np
import pytest

from ba2_common.core.scoring_store import ScoringStore

# The real skill-cache schema (see congress_skill_scores.json).
FIELDS = ("skill_score", "scored_trades", "hit_rate", "avg_fwd_return_pct")
NULLABLE = ("hit_rate",)


def _v(n, skill=0.0, hit=None):
    return {"skill_score": skill, "scored_trades": n,
            "hit_rate": hit, "avg_fwd_return_pct": 0.0}


# --------------------------------------------------------------------------- #
# read path
# --------------------------------------------------------------------------- #
def test_roundtrip_get(tmp_path):
    src = {
        "Sheldon Whitehouse|854|2022-10-03|60|5|50|12":
            {"skill_score": 0.0, "scored_trades": 0, "hit_rate": None,
             "avg_fwd_return_pct": 0.0},
        "John Boozman|50|2022-10-03|60|5|50|12":
            {"skill_score": -0.30434782608695654, "scored_trades": 23,
             "hit_rate": 0.34782608695652173, "avg_fwd_return_pct": -1.301924681212208},
    }
    p = tmp_path / "skill.store"
    ScoringStore.build(p, src, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)

    got = s.get("John Boozman|50|2022-10-03|60|5|50|12")
    assert got["scored_trades"] == 23
    # exact float equality: a scoring cache that shifts values is a correctness bug, not a
    # rounding nuisance -- the GA would score a different strategy than the one it reports.
    assert got["skill_score"] == -0.30434782608695654
    assert got["hit_rate"] == 0.34782608695652173
    assert got["avg_fwd_return_pct"] == -1.301924681212208
    assert s.get("nope") is None


def test_none_roundtrips_as_none_not_nan(tmp_path):
    """hit_rate is legitimately None for an unscored trader. nan is NOT a safe stand-in: a real
    score could itself be nan, and callers branch on ``is None``."""
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"k|1": _v(0)}, FIELDS, nullable=NULLABLE)
    got = ScoringStore.open(p).get("k|1")
    assert got["hit_rate"] is None
    assert not isinstance(got["hit_rate"], float)


def test_int_fields_come_back_as_int(tmp_path):
    """scored_trades is used in integer contexts; float64 round-tripping would silently make
    it 23.0 and change formatting/comparisons downstream."""
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"k|1": _v(23)}, FIELDS, nullable=NULLABLE)
    assert isinstance(ScoringStore.open(p).get("k|1")["scored_trades"], int)


def test_hash_collision_cannot_return_the_wrong_entry(tmp_path, monkeypatch):
    """Force every key onto ONE hash. Exact key comparison must still resolve each correctly.

    Hash-only lookup would be ~3e-7 likely to collide across 3.4M keys -- small, but a wrong
    skill score is a silent correctness bug, so the key bytes are stored and compared."""
    import ba2_common.core.scoring_store as m
    monkeypatch.setattr(m, "_hash_key", lambda k: np.uint64(42))
    src = {f"key{i}|x": _v(i) for i in range(50)}
    p = tmp_path / "c.store"
    ScoringStore.build(p, src, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    for i in range(50):
        assert s.get(f"key{i}|x")["scored_trades"] == i
    assert s.get("absent|x") is None


def test_empty_store(tmp_path):
    p = tmp_path / "e.store"
    ScoringStore.build(p, {}, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    assert s.get("anything") is None
    assert len(s) == 0


def test_unicode_and_long_keys(tmp_path):
    """Trader names are free text from FMP; the key blob is UTF-8 and offsets are byte-based,
    so a multibyte name must not corrupt neighbouring keys."""
    src = {"René Müller|12|2024-01-02|60|5|50|12": _v(7),
           "A" * 400 + "|1": _v(8)}
    p = tmp_path / "u.store"
    ScoringStore.build(p, src, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    assert s.get("René Müller|12|2024-01-02|60|5|50|12")["scored_trades"] == 7
    assert s.get("A" * 400 + "|1")["scored_trades"] == 8


# --------------------------------------------------------------------------- #
# write overlay -- the drop-in half
# --------------------------------------------------------------------------- #
def test_writes_go_to_overlay_and_are_readable(tmp_path):
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    s["b|2"] = _v(2)
    assert s.get("b|2")["scored_trades"] == 2
    assert s.get("a|1")["scored_trades"] == 1


def test_overlay_shadows_base(tmp_path):
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    s["a|1"] = _v(99)
    assert s.get("a|1")["scored_trades"] == 99


def test_base_is_never_mutated_on_disk(tmp_path):
    """The base is shared -- another process has it mapped. Writing must never touch it."""
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS, nullable=NULLABLE)
    before = (p / "col_scored_trades.npy").read_bytes()
    s = ScoringStore.open(p)
    s["a|1"] = _v(99)
    s["z|9"] = _v(9)
    assert (p / "col_scored_trades.npy").read_bytes() == before


def test_mapping_protocol_for_compaction(tmp_path):
    """_save_scoring_cache does json.dump(cache, f), so items()/len()/__iter__ must merge base
    and overlay -- otherwise a compacting flush would silently DROP every base entry."""
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1), "c|3": _v(3)}, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    s["b|2"] = _v(2)
    assert set(s.keys()) == {"a|1", "b|2", "c|3"}
    assert len(s) == 3
    assert dict(s.items())["c|3"]["scored_trades"] == 3
    assert "a|1" in s and "b|2" in s and "zz" not in s


def test_json_dump_roundtrip_matches_a_plain_dict(tmp_path):
    """The compacting save path must produce byte-identical JSON to the dict it replaces."""
    import json
    src = {"a|1": _v(1, skill=0.5, hit=0.25), "c|3": _v(3)}
    p = tmp_path / "s.store"
    ScoringStore.build(p, src, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    s["b|2"] = _v(2)
    expected = dict(src); expected["b|2"] = _v(2)
    assert json.loads(json.dumps(dict(s.items()))) == expected


def test_len_counts_overlay_shadow_once(tmp_path):
    p = tmp_path / "s.store"
    ScoringStore.build(p, {"a|1": _v(1)}, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    s["a|1"] = _v(2)
    assert len(s) == 1


# --------------------------------------------------------------------------- #
# the sharing property this whole change exists for
# --------------------------------------------------------------------------- #
def test_open_is_mmapped_not_copied(tmp_path):
    """The base columns must be numpy memmaps. If open() ever silently falls back to a plain
    in-RAM array, the per-child copy returns and the fix is void -- with no visible symptom
    until a box OOMs again."""
    p = tmp_path / "s.store"
    ScoringStore.build(p, {f"k{i}|x": _v(i) for i in range(1000)}, FIELDS, nullable=NULLABLE)
    s = ScoringStore.open(p)
    assert isinstance(s._cols["scored_trades"], np.memmap)
    assert isinstance(s._hashes, np.memmap)


def test_two_opens_share_backing_pages(tmp_path):
    """Two ScoringStore.open() calls on the same path (the 4-children case) must map the same
    file rather than each materialising a copy."""
    p = tmp_path / "s.store"
    ScoringStore.build(p, {f"k{i}|x": _v(i) for i in range(1000)}, FIELDS, nullable=NULLABLE)
    a, b = ScoringStore.open(p), ScoringStore.open(p)
    assert a.get("k500|x")["scored_trades"] == 500
    assert b.get("k500|x")["scored_trades"] == 500
    assert a._cols["scored_trades"].filename == b._cols["scored_trades"].filename
