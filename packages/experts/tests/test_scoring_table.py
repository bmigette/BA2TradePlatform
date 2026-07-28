"""ScoringTable: column storage for the scoring caches must be byte-for-byte transparent.

This structure only earns its place if it is INDISTINGUISHABLE from the dict it replaces. It
stores 400-600k numeric entries column-wise (measured 173MB -> 67MB on the real confidence
shard) and the callers index the result directly (``trader_stats['confidence_modifier']``), so
any drift in value, TYPE, key set, or key order changes scoring silently -- a cache is a pure
memo, so a wrong one produces no error, just wrong trades.
"""
import json

import pytest

from ba2_experts.scoring_table import ScoringTable

CONF = {"confidence_modifier": 0.0, "symbol_focus_pct": 12.5,
        "recent_buy_amount": 199002.0, "recent_buy_count": 4,
        "yearly_symbol_buy_amount": 1.0, "yearly_symbol_sell_amount": 2.0}
CONF_NARROW = {"confidence_modifier": 0.5, "symbol_focus_pct": 0.0,
               "recent_buy_amount": 1.0, "recent_buy_count": 1}
SKILL = {"skill_score": 0.0, "scored_trades": 0, "hit_rate": None, "avg_fwd_return_pct": 0.0}


# --------------------------------------------------------------------------- #
# transparency
# --------------------------------------------------------------------------- #
def test_round_trips_a_value_exactly():
    t = ScoringTable()
    t["k"] = CONF
    assert t["k"] == CONF


def test_preserves_int_vs_float_types():
    """``scored_trades`` is an int and ``skill_score`` a float. Returning 4.0 where the dict had
    4 would change formatting and any equality/identity check downstream."""
    t = ScoringTable()
    t["k"] = SKILL
    got = t["k"]
    assert type(got["scored_trades"]) is int
    assert type(got["skill_score"]) is float


def test_preserves_none_distinctly_from_zero():
    """``hit_rate`` is None when no trades were scored -- NOT 0.0, which would read as a 0% hit
    rate. This is why the null mask exists instead of a NaN/zero sentinel."""
    t = ScoringTable()
    t["k"] = SKILL
    assert t["k"]["hit_rate"] is None
    t["z"] = dict(SKILL, hit_rate=0.0)
    assert t["z"]["hit_rate"] == 0.0 and t["z"]["hit_rate"] is not None


def test_preserves_key_order():
    """Callers iterate/serialize these; order churn would produce spurious cache-file diffs."""
    t = ScoringTable()
    t["k"] = CONF
    assert list(t["k"].keys()) == list(CONF.keys())


def test_returns_a_copy_not_shared_state():
    """Materialising per read is deliberate: a live view would let one caller's mutation leak
    into every later reader of the same cache."""
    t = ScoringTable()
    t["k"] = CONF
    a, b = t["k"], t["k"]
    assert a == b and a is not b
    a["confidence_modifier"] = 99.0
    assert t["k"]["confidence_modifier"] == 0.0


# --------------------------------------------------------------------------- #
# multi-shape -- the real shards carry two key sets
# --------------------------------------------------------------------------- #
def test_two_shapes_coexist_with_exact_key_sets():
    """A confidence entry has the two yearly_symbol_* subtotals ONLY when the trader traded that
    symbol. The real shard splits 234,867 x 12-field / 164,133 x 10-field; a single-schema table
    sent 41% to overflow."""
    t = ScoringTable()
    t["wide"] = CONF
    t["narrow"] = CONF_NARROW
    assert t["wide"] == CONF
    assert t["narrow"] == CONF_NARROW
    assert t.overflow_count == 0


def test_a_field_absent_from_a_shape_is_absent_not_none():
    """``'yearly_symbol_buy_amount' in stats`` must still tell the truth. Filling the union with
    None would invent a value the entry never had."""
    t = ScoringTable()
    t["wide"] = CONF
    t["narrow"] = CONF_NARROW
    assert "yearly_symbol_buy_amount" not in t["narrow"]


def test_a_shape_seen_first_does_not_poison_later_rows():
    """Back-fill order: narrow first, then wide, so the union grows AFTER rows already exist."""
    t = ScoringTable()
    t["narrow"] = CONF_NARROW
    t["wide"] = CONF
    assert t["narrow"] == CONF_NARROW
    assert t["wide"] == CONF


# --------------------------------------------------------------------------- #
# mutation
# --------------------------------------------------------------------------- #
def test_overwrite_replaces_in_place():
    t = ScoringTable()
    t["k"] = CONF
    updated = dict(CONF, confidence_modifier=7.5)
    t["k"] = updated
    assert t["k"] == updated and len(t) == 1


def test_overwrite_can_change_shape():
    t = ScoringTable()
    t["k"] = CONF
    t["k"] = CONF_NARROW
    assert t["k"] == CONF_NARROW


def test_an_int_field_that_later_sees_a_float_is_promoted():
    """Without promotion an int64 column would truncate 0.7 to 0."""
    t = ScoringTable()
    t["a"] = {"v": 1}
    t["b"] = {"v": 0.7}
    assert t["a"]["v"] == 1 and t["b"]["v"] == 0.7


# --------------------------------------------------------------------------- #
# mapping protocol + overflow
# --------------------------------------------------------------------------- #
def test_mapping_basics():
    t = ScoringTable.from_dict({"a": SKILL, "b": SKILL})
    assert len(t) == 2 and "a" in t and "zz" not in t
    assert sorted(t.keys()) == ["a", "b"]
    assert t.get("zz") is None and t.get("zz", "dflt") == "dflt"
    assert dict(t.items())["a"] == SKILL


def test_from_dict_to_dict_is_identity():
    src = {"a": CONF, "b": CONF_NARROW, "c": SKILL}
    assert ScoringTable.from_dict(src).to_dict() == src


def test_json_round_trip_matches_a_plain_dict():
    """The persist path serialises these; the bytes must not change."""
    src = {"a": CONF, "b": CONF_NARROW}
    assert json.loads(json.dumps(ScoringTable.from_dict(src).to_dict())) == src


def test_non_numeric_values_go_to_overflow_not_lost():
    """Degrade to the old memory profile rather than corrupt or drop."""
    t = ScoringTable()
    t["ok"] = SKILL
    t["weird"] = {"note": "a string"}
    assert t["weird"] == {"note": "a string"}
    assert t.overflow_count == 1 and len(t) == 2


def test_bools_are_not_stored_as_ints():
    """bool is an int subclass; round-tripping True as 1 would break an ``is True`` check."""
    t = ScoringTable()
    t["k"] = {"flag": True}
    assert t["k"]["flag"] is True


def test_empty_table():
    t = ScoringTable()
    assert len(t) == 0 and list(t.keys()) == [] and t.to_dict() == {}
