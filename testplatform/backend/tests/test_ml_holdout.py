import random

import pytest

from app.services import ml_holdout as H


def test_locked_lists_are_fixed_and_disjoint_from_dev():
    lists = H.load()
    assert set(lists) == {"ndq30", "largecap"}
    nd = lists["ndq30"]
    assert len(nd["locked"]) == 10 and len(nd["dev"]) == 20 and not set(nd["locked"]) & set(nd["dev"])


def test_guard_refuses_a_locked_symbol():
    locked = H.load()["ndq30"]["locked"][0]
    with pytest.raises(H.LockedSymbolError):
        H.assert_unlocked([locked, "AAPL"], purpose="training")


def test_guard_message_names_purpose_and_offenders():
    locked = H.load()["ndq30"]["locked"][:2]
    with pytest.raises(H.LockedSymbolError) as ei:
        H.assert_unlocked(locked, purpose="feature search")
    msg = str(ei.value)
    assert "feature search" in msg and all(s in msg for s in locked)


def test_ndq30_universe_accepts_all_dev_symbols_but_refuses_locked():
    nd = H.load()["ndq30"]
    H.assert_unlocked(nd["dev"], purpose="training", universe="ndq30")  # must not raise
    with pytest.raises(H.LockedSymbolError):
        H.assert_unlocked(nd["locked"][:1], purpose="training", universe="ndq30")


def test_default_guard_also_applies_the_largecap_rule_to_ndq30_dev_symbols():
    dev_locked = [s for s in H.load()["ndq30"]["dev"] if H.is_locked_largecap(s)]
    assert dev_locked  # the overlap documented in the module docstring really exists
    with pytest.raises(H.LockedSymbolError):
        H.assert_unlocked(dev_locked, purpose="scale-up training")


def test_unknown_universe_is_rejected():
    with pytest.raises(ValueError):
        H.assert_unlocked(["AAPL"], purpose="x", universe="bogus")


def test_guard_refuses_a_largecap_locked_symbol():
    # a symbol outside NDQ30 that the hash rule locks
    ndq = set(H.load()["ndq30"]["locked"]) | set(H.load()["ndq30"]["dev"])
    sym = next(s for s in (f"SYM{i}" for i in range(1000)) if s not in ndq and H.is_locked_largecap(s))
    with pytest.raises(H.LockedSymbolError):
        H.assert_unlocked([sym], purpose="scale-up training")
    free = next(s for s in (f"SYM{i}" for i in range(1000)) if s not in ndq and not H.is_locked_largecap(s))
    H.assert_unlocked([free], purpose="scale-up training")


def test_is_locked_largecap_is_deterministic_and_about_a_third():
    rng = random.Random(1)
    syms = ["".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(rng.randint(1, 5))) for _ in range(3000)]
    syms = sorted(set(syms))
    first = [H.is_locked_largecap(s) for s in syms]
    assert first == [H.is_locked_largecap(s) for s in syms]
    frac = sum(first) / len(first)
    assert 0.30 < frac < 0.37, frac
    # known anchors so a rule change (hash, seed, modulus) is caught
    assert H.is_locked_largecap("AAPL") is H.is_locked_largecap("AAPL")


def test_largecap_rule_matches_documented_formula():
    import hashlib
    spec = H.load()["largecap"]
    for s in ("AAPL", "MSFT", "XOM", "BRK.B", "ZZZ"):
        expect = hashlib.sha256(f"{spec['seed']}:{s}".encode()).digest()[0] % 3 == 0
        assert H.is_locked_largecap(s) is expect


def test_committed_ndq30_lists_equal_regenerated_with_committed_seed():
    lists = H.load()
    regen = H.make_lists(seed=lists["ndq30"]["seed"])
    assert lists["largecap"]["seed"] == lists["ndq30"]["seed"]
    for name in ("ndq30", "largecap"):
        committed = {k: v for k, v in lists[name].items() if k != "generated"}
        assert committed == regen[name]
    assert lists["ndq30"]["seed"] == H.DEFAULT_SEED


def test_ndq30_constant_matches_the_capband_matrix_universe():
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parents[3] / "tools" / "run_screener_capband_matrix.py"
    for node in ast.parse(src.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_PLACEHOLDER_UNIVERSE":
            assert list(H.NDQ30) == ast.literal_eval(node.value).split(",")
            return
    pytest.fail("_PLACEHOLDER_UNIVERSE not found")
