"""Final whole-branch review of the ML exploration v2, part B (grid): the store guards (holdout, code version,
membership), unavailable regimes, the common-period column, the confounding note and the CPU hard deadline."""
import json
import os

import pytest

from app.services import ml_features as F
from app.services import ml_grid as G
from app.services import ml_holdout as HO
from tests.test_ml_grid import (SYMS, build_store, cell, payload, signal_world)  # noqa: F401

LOCKED = "AAA"
assert HO.is_locked_largecap(LOCKED)


@pytest.fixture
def world(tmp_path):
    """A private dev store (the module-scoped worlds are shared: a test here edits the meta)."""
    root = tmp_path / "w"
    d = build_store(root / "stores", "stocks", SYMS, root / "cache", 0.0, 3)
    return root / "stores", d, root / "cache"


def _edit_meta(d, **change):
    path = d / "features.parquet.meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    for k, v in change.items():
        if k == "code":
            meta["ensure"]["inputs"]["code"] = v
        else:
            meta[k] = v
    before = path.stat().st_mtime_ns
    path.write_text(json.dumps(meta), encoding="utf-8")
    os.utime(path, ns=(before + 10**9, before + 10**9))          # G._meta_of caches on the mtime: make the edit visible


def test_the_synthetic_store_is_a_valid_dev_store(world):
    assert len(G.store_signature(world[0], SYMS)) == 16
    assert G.load_panel(world[0], tuple(SYMS), "selection", world[2]).symbols == tuple(SYMS)


@pytest.mark.parametrize("field", ["universe", "symbols"])
def test_a_store_listing_a_locked_symbol_is_refused_by_the_signature_and_the_panel(world, field):
    store, d, cache = world
    _edit_meta(d, **{field: list(SYMS) + [LOCKED]})
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        G.store_signature(store, SYMS)
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        G.load_panel(store, tuple(SYMS), "selection", cache)


def test_a_holdout_store_is_refused_for_dev_grids(world):
    store, d, cache = world
    _edit_meta(d, purpose="holdout")
    for call in (lambda: G.store_signature(store, SYMS), lambda: G.load_panel(store, tuple(SYMS), "selection", cache)):
        with pytest.raises(ValueError, match="holdout"):
            call()


def test_a_dev_purpose_store_is_accepted(world):
    store, d, cache = world
    _edit_meta(d, purpose="dev")
    assert G.store_signature(store, SYMS)


def test_a_membership_store_is_refused(world):
    store, d, cache = world
    _edit_meta(d, membership={"top_n": 100, "purpose": "dev", "symbols": 10, "digest": "x"})
    msg = "point-in-time membership stores are not supported by the stage-1 grid yet"
    with pytest.raises(ValueError, match=msg):
        G.store_signature(store, SYMS)
    with pytest.raises(ValueError, match=msg):
        G.load_panel(store, tuple(SYMS), "selection", cache)


def test_a_store_built_by_other_feature_code_is_refused_with_the_remedy(world):
    store, d, cache = world
    _edit_meta(d, code="0123456789abcdef")
    for call in (lambda: G.store_signature(store, SYMS), lambda: G.load_panel(store, tuple(SYMS), "selection", cache)):
        with pytest.raises(ValueError, match="rebuild the store with `ba2-test ml-features`") as ei:
            call()
        assert "0123456789abcdef" in str(ei.value) and F.code_version() in str(ei.value)


def test_precheck_grid_refuses_a_stale_or_locked_store_as_the_api_does(world):
    store, d, cache = world
    body = payload((store, d, cache), cells=[cell(store)])
    assert G.precheck_grid(body) == 1
    _edit_meta(d, code="0123456789abcdef")
    with pytest.raises(ValueError, match="rebuild the store"):
        G.precheck_grid(body)
    _edit_meta(d, code=F.code_version(), universe=list(SYMS) + [LOCKED])
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        G.precheck_grid(body)
