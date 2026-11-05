"""``GeneticOptimizer.encode_params_strict``: a seed is encoded exactly or refused by name.

The lenient ``encode_params`` fills an absent gene with ``min`` and maps an unknown choice to
index 0, so a seed missing a member's ``entry:<m>-entry:enabled`` gene would silently seed that
member OFF. Seeds (stage 2) go through the strict path; warm start keeps the lenient one.

Run:
    python -m pytest tests/test_genetic_strict_encode.py -q
"""
import pytest

from app.services.genetic import GeneticOptimizer

SPACE = {"a": {"type": "float", "min": 0.0, "max": 1.0, "step": 0.1},
         "n": {"type": "int", "min": 410, "max": 500, "step": 15},
         "c": {"type": "choice", "choices": ["off", "below", "above"], "min": 0, "max": 2, "step": 1}}


def _opt(anchor="zero"):
    return GeneticOptimizer(param_ranges=SPACE, population_size=4, n_generations=1,
                            lattice_anchor=anchor)


def test_round_trips_a_complete_genome():
    o = _opt()
    d = o.decode_individual(o.encode_params_strict({"a": 0.3, "n": 420, "c": "above"}))
    assert d["a"] == pytest.approx(0.3) and d["n"] == 420 and d["c"] == "above"


def test_a_value_off_the_decode_lattice_is_refused():
    """425 is inside [410, 500] but decodes to 420: the GA could never reproduce it."""
    with pytest.raises(ValueError, match="'n'"):
        _opt().encode_params_strict({"a": 0.3, "n": 425, "c": "above"})


def test_a_missing_gene_is_refused_not_filled_with_min():
    with pytest.raises(ValueError, match=r"missing \['n'\]"):
        _opt().encode_params_strict({"a": 0.3, "c": "above"})


def test_an_unknown_gene_is_refused():
    with pytest.raises(ValueError, match=r"unknown \['zz'\]"):
        _opt().encode_params_strict({"a": 0.3, "n": 420, "c": "above", "zz": 1})


def test_an_unknown_choice_is_refused_not_mapped_to_index_0():
    with pytest.raises(ValueError, match="'sideways'"):
        _opt().encode_params_strict({"a": 0.3, "n": 420, "c": "sideways"})


def test_a_value_outside_the_range_is_refused():
    with pytest.raises(ValueError, match="'a'"):
        _opt().encode_params_strict({"a": 1.7, "n": 420, "c": "above"})


@pytest.mark.parametrize("bad", ["0.3", None, True, float("nan")])
def test_a_non_number_is_refused(bad):
    with pytest.raises(ValueError, match="'a'"):
        _opt().encode_params_strict({"a": bad, "n": 420, "c": "above"})


def test_a_decode_image_value_just_outside_min_is_accepted_on_the_zero_lattice():
    """The legacy lattice counts from 0: 410 on step 15 decodes to 405, so a winner can carry 405."""
    o = _opt()
    assert o.decode_individual(o.encode_params_strict({"a": 0.3, "n": 405, "c": "above"}))["n"] == 405


def test_the_min_lattice_refuses_what_it_can_never_decode():
    o = _opt("min")
    with pytest.raises(ValueError, match="'n'"):
        o.encode_params_strict({"a": 0.3, "n": 405, "c": "above"})
    assert o.decode_individual(o.encode_params_strict({"a": 0.3, "n": 425, "c": "above"}))["n"] == 425


def test_the_lenient_encode_is_unchanged():
    assert list(_opt().encode_params({"a": 0.3})) == [0.3, 410, 0]
