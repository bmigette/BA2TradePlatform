"""Pinned comparison roots (spec section 6, "Historical artifacts are versioned/pinned").

A historical comparison has to read a FIXED set of files: if the cache underneath
it can change between two runs, a difference it reports is not evidence of
anything. :func:`materialize_pinned_root` builds that fixed set as a normal-looking
cache root plus a manifest of hashes and provenance.

Three properties are load-bearing:

* the SOURCE roots are never written to -- the pin is a copy, and a warm that
  mutated the evidence it was pinning would destroy the thing being measured;
* the manifest distinguishes a file THIS run fetched (``warmed``) from one that was
  already there when nobody recorded which revision it holds
  (``legacy_history_unknown_revision``) -- spec section 5 forbids presenting a
  late download as a historical vintage;
* pinning twice produces the same root, so a comparison can be repeated.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core.replay import dependencies as dep
from app.services.warm import planner, roots

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
WINDOW = dep.Window(start=NOW - timedelta(days=365), end=NOW)


def _history_req(namespace, symbol):
    return dep.Requirement(provider="fmp", namespace=namespace, symbol=symbol, window=WINDOW,
                           interval=None, kind=dep.KIND_HISTORY, optional=False, reason="t")


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "cache"
    history = root / "fmp_history"
    history.mkdir(parents=True)
    (history / "price_target__AAPL.json").write_text('[{"t": 1}]', encoding="utf-8")
    (history / "grades_historical__AAPL.json").write_text('[{"g": 2}]', encoding="utf-8")
    fred = root / "fred"
    fred.mkdir()
    (fred / "VIXCLS.json").write_text('{"observations": []}', encoding="utf-8")
    return root


def _plan(source_root, requirements):
    return planner.plan(requirements, [str(source_root)], as_of_now=NOW)


def test_the_pinned_root_has_the_same_layout_as_a_normal_cache_root(source, tmp_path):
    dest = tmp_path / "pinned"
    p = _plan(source, [_history_req("price_target", "AAPL")])

    roots.materialize_pinned_root(p, [str(source)], str(dest))

    assert (dest / "fmp_history" / "price_target__AAPL.json").exists(), (
        "an ordinary reader must be able to use the pinned root unchanged")


def test_the_manifest_records_a_hash_source_and_provenance_per_file(source, tmp_path):
    dest = tmp_path / "pinned"
    p = _plan(source, [_history_req("price_target", "AAPL")])

    manifest = roots.materialize_pinned_root(p, [str(source)], str(dest))

    entry = manifest["files"]["fmp_history/price_target__AAPL.json"]
    assert entry["sha256"] == roots.sha256_file(
        str(source / "fmp_history" / "price_target__AAPL.json"))
    assert entry["source"] == str(source)
    assert entry["provenance"] == roots.PROVENANCE_LEGACY
    assert entry["mtime"]
    assert json.loads((dest / roots.MANIFEST_NAME).read_text(encoding="utf-8")) == manifest


def test_a_file_this_run_fetched_is_marked_warmed(source, tmp_path):
    dest = tmp_path / "pinned"
    req = _history_req("price_target", "AAPL")
    p = _plan(source, [req])

    manifest = roots.materialize_pinned_root(p, [str(source)], str(dest),
                                             warmed=[req.key])

    entry = manifest["files"]["fmp_history/price_target__AAPL.json"]
    assert entry["provenance"] == roots.PROVENANCE_WARMED, (
        "a payload downloaded now is NOT evidence of the revision live consumed")


def test_the_source_root_is_never_written_to(source, tmp_path):
    before = {p.relative_to(source).as_posix(): p.stat().st_mtime
              for p in source.rglob("*") if p.is_file()}

    roots.materialize_pinned_root(
        _plan(source, [_history_req("price_target", "AAPL"),
                       _history_req("grades_historical", "AAPL")]),
        [str(source)], str(tmp_path / "pinned"))

    after = {p.relative_to(source).as_posix(): p.stat().st_mtime
             for p in source.rglob("*") if p.is_file()}
    assert after == before


def test_pinning_refuses_a_destination_that_is_a_source_root(source):
    with pytest.raises(roots.PinnedRootError):
        roots.materialize_pinned_root(
            _plan(source, [_history_req("price_target", "AAPL")]),
            [str(source)], str(source))


def test_pinning_refuses_a_destination_that_would_copy_an_artifact_onto_itself(source,
                                                                               tmp_path):
    """A destination whose layout overlaps the source's would destroy what it preserves."""
    nested = source / "fmp_history"
    with pytest.raises(roots.PinnedRootError):
        roots.materialize_pinned_root(
            _plan(source, [_history_req("price_target", "AAPL")]),
            [str(source)], str(nested.parent))


def test_the_live_hosts_pinned_subdirectory_of_the_cache_root_is_allowed(source):
    """The live host pins into ``<cache>/replay/pinned/<date>`` -- inside the cache root.

    No reader resolves anything under ``replay/`` (they look in ``fmp_history/``,
    ``fred/`` and the per-provider parquet directories), so the pin's own subtree
    cannot be mistaken for cache content or be overwritten by a later warm.
    """
    dest = source / "replay" / "pinned" / "2026-09-11"

    manifest = roots.materialize_pinned_root(
        _plan(source, [_history_req("price_target", "AAPL")]), [str(source)], str(dest))

    assert "fmp_history/price_target__AAPL.json" in manifest["files"]
    assert (dest / "fmp_history" / "price_target__AAPL.json").exists()


def test_pinning_is_idempotent(source, tmp_path):
    dest = tmp_path / "pinned"
    p = _plan(source, [_history_req("price_target", "AAPL")])

    first = roots.materialize_pinned_root(p, [str(source)], str(dest))
    second = roots.materialize_pinned_root(p, [str(source)], str(dest))

    assert first["files"] == second["files"]
    assert first["root"] == second["root"]


def test_a_requirement_with_no_artifact_is_recorded_as_an_unpinned_gap(source, tmp_path):
    dest = tmp_path / "pinned"
    p = _plan(source, [_history_req("price_target", "AAPL"),
                       _history_req("insider_v2", "NVDA")])

    manifest = roots.materialize_pinned_root(p, [str(source)], str(dest))

    assert len(manifest["files"]) == 1
    assert manifest["unpinned"] == [_history_req("insider_v2", "NVDA").key], (
        "a comparison must be told which requirement has no pinned artifact, not "
        "discover it as a cache miss mid-run")


def test_every_pinned_file_verifies_against_its_manifest_hash(source, tmp_path):
    dest = tmp_path / "pinned"
    manifest = roots.materialize_pinned_root(
        _plan(source, [_history_req("price_target", "AAPL"),
                       _history_req("grades_historical", "AAPL")]),
        [str(source)], str(dest))

    assert roots.verify_pinned_root(str(dest)) == []

    target = dest / "fmp_history" / "price_target__AAPL.json"
    os.chmod(target, 0o644)
    target.write_text('[{"t": 999}]', encoding="utf-8")

    assert roots.verify_pinned_root(str(dest)) == ["fmp_history/price_target__AAPL.json"]
    assert manifest["files"]["fmp_history/price_target__AAPL.json"]["sha256"]


def test_fred_and_parquet_artifacts_are_pinned_under_their_own_directories(source, tmp_path):
    dest = tmp_path / "pinned"
    series = dep.Requirement(provider="fred", namespace="VIXCLS", symbol=None, window=WINDOW,
                             interval=None, kind=dep.KIND_SERIES, optional=False, reason="t")

    manifest = roots.materialize_pinned_root(_plan(source, [series]), [str(source)], str(dest))

    assert "fred/VIXCLS.json" in manifest["files"]
    assert (dest / "fred" / "VIXCLS.json").exists()
