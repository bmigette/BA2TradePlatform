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


def test_assert_unlocked_accepts_all_ndq30_dev_symbols():
    H.assert_unlocked(H.load()["ndq30"]["dev"], purpose="training")  # must not raise


def test_no_ndq30_dev_symbol_is_largecap_locked():
    assert not [s for s in H.load()["ndq30"]["dev"] if H.is_locked_largecap(s)]


def test_every_ndq30_locked_symbol_is_largecap_locked():
    locked = H.load()["ndq30"]["locked"]
    assert all(H.is_locked_largecap(s) for s in locked)
    with pytest.raises(H.LockedSymbolError):
        H.assert_unlocked(locked, purpose="scale-up training")


def test_is_locked_largecap_is_deterministic_and_about_a_third():
    rng = random.Random(1)
    syms = ["".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(rng.randint(1, 5))) for _ in range(3000)]
    ndq = set(H.NDQ30)
    syms = sorted(set(syms) - ndq)
    first = [H.is_locked_largecap(s) for s in syms]
    assert first == [H.is_locked_largecap(s) for s in syms]
    frac = sum(first) / len(first)
    assert 0.30 < frac < 0.37, frac


def test_hard_coded_anchors_catch_a_change_of_seed_modulus_or_hash():
    import hashlib
    # first sha256 byte of f"20261028:{symbol}" (computed once, hard-coded)
    first_byte = {"XOM": 37, "BRK-B": 224, "JPM": 240, "KO": 156}
    for sym, byte in first_byte.items():
        assert hashlib.sha256(f"20261028:{sym}".encode()).digest()[0] == byte
    assert H.load()["largecap"]["seed"] == 20261028
    assert H.is_locked_largecap("AAPL") is False   # NDQ30 dev
    assert H.is_locked_largecap("MSFT") is True    # NDQ30 locked (the hash alone says False)
    assert H.is_locked_largecap("XOM") is False    # 37 % 3 == 1
    assert H.is_locked_largecap("BRK-B") is False  # 224 % 3 == 2
    assert H.is_locked_largecap("JPM") is True     # 240 % 3 == 0
    assert H.is_locked_largecap("KO") is True      # 156 % 3 == 0


@pytest.mark.parametrize("bad", ["aapl", "msft", " MSFT", "MSFT ", "", " ", "MSFT" + chr(10), None, 7, b"MSFT"])
def test_non_canonical_symbol_is_refused(bad):
    with pytest.raises(ValueError):
        H.is_locked_largecap(bad)
    with pytest.raises(ValueError):
        H.assert_unlocked([bad], purpose="training")


def test_a_bare_string_is_refused_not_iterated_by_character():
    with pytest.raises(TypeError):
        H.assert_unlocked("MSFT", purpose="training")
    with pytest.raises(TypeError):
        H.assert_unlocked("AAPL", purpose="training")


def test_write_once_refuses_to_overwrite_unless_told_to(tmp_path, monkeypatch):
    target = tmp_path / "ml_holdout_symbols.json"
    monkeypatch.setattr(H, "HOLDOUT_FILE", target)
    H._state.cache_clear()
    try:
        H._write_once()                       # no file yet: writes
        written = target.read_text(encoding="utf-8")
        assert H.load()["ndq30"]["locked"] == H.make_lists()["ndq30"]["locked"]
        target.write_text("sentinel", encoding="utf-8")
        with pytest.raises(SystemExit):
            H._write_once()
        assert target.read_text(encoding="utf-8") == "sentinel"
        H._write_once(overwrite=True)
        assert target.read_text(encoding="utf-8") == written
    finally:
        H._state.cache_clear()


def test_largecap_rule_matches_documented_formula():
    import hashlib
    spec = H.load()["largecap"]
    for s in ("XOM", "BRK-B", "ZZZ", "JPM", "KO"):
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
