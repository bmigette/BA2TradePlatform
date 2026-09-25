"""The GA's step lattice: anchored at ZERO (legacy, the default) or at the gene's MIN (opt-in).

``GeneticOptimizer.decode_individual`` snapped a numeric gene with ``round(v / step) * step`` --
a lattice counted from ZERO, not from the gene's ``min``. For a gene whose ``min`` is not a
multiple of its ``step`` that decodes BELOW ``min`` (and above ``max``) and off the intended
levels: the LEAPS entry-DTE gene (410..500 step 15) decodes 410 to 405, i.e. a window starting at
360 under a design floor of 365; O_ERN's (14..23 step 3) decodes 23 to 24, i.e. dte_max 31.

The fix is OPT-IN (``lattice_anchor="min"``; optimization_config ``latticeAnchor``) because the
measurement for this change found genes that decode differently in the running stage-1 option
grid (``cond:xlk:value`` 3..20 step 2) and in the equity grids (S1/S4-S7 condition and action
genes, FMPSenateTraderWeight ``min_trader_avg_hold_days``): switching the default would make a
resumed checkpoint -- and every persisted TOP-N re-run's GA twin -- decode a different genome.
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys

import pytest

from app.services.genetic import LATTICE_ANCHORS, GeneticOptimizer

sys.path.insert(0, os.path.dirname(__file__))

_LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_lattice", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_lattice"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def _gene(lo, hi, step, kind):
    return {"type": kind, "min": lo, "max": hi, "step": step}


def _decoder(space, anchor=None):
    kw = {} if anchor is None else {"lattice_anchor": anchor}
    opt = GeneticOptimizer(param_ranges=space, population_size=2, n_generations=1, **kw)
    names = list(space)
    base = [space[n]["min"] if space[n]["type"] != "choice" else 0 for n in names]

    def decode(name, raw):
        ind = list(base)
        ind[names.index(name)] = raw
        return opt.decode_individual(ind)[name]
    return decode


def _raw_sweep(g):
    lo, hi = g["min"], g["max"]
    if g["type"] == "int":
        return list(range(int(lo), int(hi) + 1))
    pts = [lo + (hi - lo) * i / 2000 for i in range(2001)]
    k = 0
    while lo + k * g["step"] <= hi + 1e-9:
        x = lo + k * g["step"]
        pts += [x, math.nextafter(x, -math.inf), math.nextafter(x, math.inf)]
        k += 1
    return [p for p in pts if lo <= p <= hi]


# Genes whose min (and/or max) is NOT a multiple of the step -- each taken from a real space.
_OFF_LATTICE = {
    "leaps_dte": _gene(410, 500, 15, "int"),          # O_LEAPC/O_LEAPP/O_PMCC entry DTE centre
    "ern_dte": _gene(14, 23, 3, "int"),               # O_ERN entry DTE centre
    "xlk": _gene(3.0, 20.0, 2.0, "float"),            # stage-1 cond:xlk:value (max off too)
    "hold_days": _gene(1.0, 15.0, 2.0, "float"),      # FMPSenateTraderWeight
    "tp_follow": _gene(-15.0, 5.0, 2.0, "float"),     # S4 exit:tp_follow
}
# Genes on the zero lattice at both ends -- the overwhelming majority of every grid.
_ON_LATTICE = {
    "entry_cross": _gene(0.75, 1.0, 0.05, "float"),
    "risk": _gene(0.5, 10.0, 0.5, "float"),
    "slope": _gene(-0.3, 0.3, 0.05, "float"),
    "rvol": _gene(0.0, 3.0, 0.1, "float"),
    "max_stocks": _gene(10, 50, 10, "int"),           # even step: midpoint ints exercise rounding
    "atr_period": _gene(7, 28, 7, "int"),
    "sl": _gene(-20.0, -4.0, 2.0, "float"),
}


# --------------------------------------------------------------------------- #
# 1. the knob
# --------------------------------------------------------------------------- #
def test_the_default_anchor_is_zero_so_nothing_running_moves():
    assert LATTICE_ANCHORS == ("zero", "min")
    assert GeneticOptimizer(param_ranges=dict(_ON_LATTICE), population_size=2,
                            n_generations=1).lattice_anchor == "zero"


def test_an_unknown_anchor_is_refused():
    with pytest.raises(ValueError, match="lattice_anchor"):
        GeneticOptimizer(param_ranges=dict(_ON_LATTICE), population_size=2, n_generations=1,
                         lattice_anchor="max")


# --------------------------------------------------------------------------- #
# 2. zero (legacy) is pinned exactly as it was -- the bug included
# --------------------------------------------------------------------------- #
def test_the_zero_anchor_still_decodes_exactly_the_legacy_formula():
    space = {**_OFF_LATTICE, **_ON_LATTICE}
    decode = _decoder(space)
    for name, g in space.items():
        for raw in _raw_sweep(g):
            want = (int(round(raw / g["step"]) * g["step"]) if g["type"] == "int"
                    else round(raw / g["step"]) * g["step"])
            got = decode(name, raw)
            assert got == want and type(got) is type(want), (name, raw, got, want)
    # ...which is the bug this module exists for.
    assert decode("leaps_dte", 410) == 405
    assert decode("ern_dte", 23) == 24


# --------------------------------------------------------------------------- #
# 3. min anchor: bounded and on the min-anchored lattice
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted({**_OFF_LATTICE, **_ON_LATTICE}))
def test_min_anchor_never_decodes_below_min_or_above_max(name):
    g = {**_OFF_LATTICE, **_ON_LATTICE}[name]
    decode = _decoder({name: g}, "min")
    for raw in _raw_sweep(g):
        v = decode(name, raw)
        assert g["min"] <= v <= g["max"], (name, raw, v)


@pytest.mark.parametrize("name,levels", [
    ("leaps_dte", [410, 425, 440, 455, 470, 485, 500]),
    ("ern_dte", [14, 17, 20, 23]),
    ("xlk", [3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0, 17.0, 19.0]),
    ("hold_days", [1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0]),
    ("tp_follow", [-15.0, -13.0, -11.0, -9.0, -7.0, -5.0, -3.0, -1.0, 1.0, 3.0, 5.0]),
])
def test_min_anchor_lattice_is_counted_from_min(name, levels):
    g = _OFF_LATTICE[name]
    decode = _decoder({name: g}, "min")
    got = sorted({decode(name, raw) for raw in _raw_sweep(g)})
    assert got == levels
    assert all(type(v) is (int if g["type"] == "int" else float) for v in got)


def test_min_anchor_is_bit_identical_to_zero_on_every_on_lattice_gene():
    """Turning the anchor on moves ONLY genes whose lattice actually differs, so a stage-2 job
    warm-started from stage-1 winners re-decodes every on-lattice gene to the very same float
    (including the 0.30000000000000004-style noise the zero formula produces) -- except where
    that noise put an END level an ulp outside the range, where min returns the exact bound."""
    zero, anchored = _decoder(dict(_ON_LATTICE)), _decoder(dict(_ON_LATTICE), "min")
    bounded = 0
    for name, g in _ON_LATTICE.items():
        for raw in _raw_sweep(g):
            a, b = zero(name, raw), anchored(name, raw)
            if a < g["min"] or a > g["max"]:
                assert b == (g["min"] if a < g["min"] else g["max"]), (name, raw, a, b)
                assert abs(a - b) < 1e-12, (name, raw, a, b)
                bounded += 1
                continue
            assert a == b and type(a) is type(b), (name, raw, a, b)
    assert bounded, "the slope gene's -6 * 0.05 noise case should have been exercised"


def test_the_entry_cross_band_still_decodes_to_exactly_six_levels_under_either_anchor():
    g = _ON_LATTICE["entry_cross"]
    for anchor in LATTICE_ANCHORS:
        decode = _decoder({"x": g}, anchor)
        got = sorted({round(decode("x", raw), 10) for raw in _raw_sweep(g)})
        assert got == [0.75, 0.8, 0.85, 0.9, 0.95, 1.0], anchor


def test_min_anchor_clamps_a_raw_value_from_outside_the_range():
    """A warm-start seed encoded from a differently-ranged source can carry a raw value outside
    this space; the min anchor still decodes it to a level of THIS gene."""
    g = _OFF_LATTICE["leaps_dte"]
    decode = _decoder({"d": g}, "min")
    assert decode("d", 300) == 410
    assert decode("d", 900) == 500
    f = _OFF_LATTICE["xlk"]
    decode = _decoder({"f": f}, "min")
    assert decode("f", -50.0) == 3.0
    assert decode("f", 50.0) == 19.0


def test_choice_genes_are_untouched_by_the_anchor():
    space = {"c": {"type": "choice", "choices": ["a", "b", "c"], "min": 0, "max": 2, "step": 1}}
    for anchor in LATTICE_ANCHORS:
        decode = _decoder(space, anchor)
        assert [decode("c", i) for i in range(3)] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# 4. the handler: config key -> optimizer, and the checkpoint identity
# --------------------------------------------------------------------------- #
def test_resolve_lattice_anchor_defaults_to_zero_and_refuses_unknown_values():
    from app.services import strategy_optimization_handler as H
    assert H._resolve_lattice_anchor({}) == "zero"
    assert H._resolve_lattice_anchor({"latticeAnchor": "zero"}) == "zero"
    assert H._resolve_lattice_anchor({"latticeAnchor": "min"}) == "min"
    with pytest.raises(ValueError, match="latticeAnchor"):
        H._resolve_lattice_anchor({"latticeAnchor": "MIN"})


def test_the_fingerprint_of_every_existing_config_is_unchanged():
    """A running job resumes only into a matching fingerprint: a config without the key (every
    job launched before this change) -- or with the default spelled out -- must keep its hash."""
    from app.services import strategy_optimization_handler as H
    space = {"x": {"type": "float", "min": 3.0, "max": 20.0, "step": 2.0}}
    ga = {"populationSize": 40, "generations": 8}
    legacy = H.checkpoint_fingerprint(space, ga)
    assert H.checkpoint_fingerprint(space, {**ga, "latticeAnchor": "zero"}) == legacy
    # Pinned literal: the legacy payload shape, hashed exactly as before this change.
    import hashlib
    import json
    payload = {"genes": [["x", sorted(space["x"].items())]], "population": 40, "generations": 8}
    assert legacy == hashlib.sha1(json.dumps(payload, sort_keys=False, default=str)
                                  .encode("utf-8")).hexdigest()[:16]


def test_a_min_anchored_search_never_resumes_a_zero_anchored_checkpoint():
    from app.services import strategy_optimization_handler as H
    space = {"x": {"type": "float", "min": 3.0, "max": 20.0, "step": 2.0}}
    ga = {"populationSize": 40, "generations": 8}
    assert (H.checkpoint_fingerprint(space, {**ga, "latticeAnchor": "min"})
            != H.checkpoint_fingerprint(space, ga))


@pytest.mark.parametrize("cfg_over,expected", [({}, "zero"), ({"latticeAnchor": "min"}, "min")])
def test_the_handler_builds_the_optimizer_with_the_configured_anchor(monkeypatch, cfg_over,
                                                                       expected):
    import test_strategy_optimization_handler as T
    from app.models.database import Base, engine
    from app.services import strategy_optimization_handler as H

    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(H, "_run_trial_backtest", T._deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    seen = {}
    Real = H.GeneticOptimizer

    class _Spy(Real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["anchor"] = self.lattice_anchor

    monkeypatch.setattr(H, "GeneticOptimizer", _Spy)
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(populationSize=4, generations=1, **cfg_over))
    out = H.handle_strategy_optimization(f"t-lattice-{expected}", {"optimization_id": opt_id})
    assert out["status"] == "completed", out
    assert seen["anchor"] == expected


def test_the_handler_fails_a_job_with_an_unknown_anchor(monkeypatch):
    import test_strategy_optimization_handler as T
    from app.models.database import Base, engine
    from app.services import strategy_optimization_handler as H

    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(H, "_run_trial_backtest", T._deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})
    sid = T._seed_strategy()
    opt_id = T._seed_opt(sid, config=T._ga_config(latticeAnchor="bogus"))
    out = H.handle_strategy_optimization("t-lattice-bogus", {"optimization_id": opt_id})
    assert out["status"] == "failed"
    assert "latticeAnchor" in out["error"]


# --------------------------------------------------------------------------- #
# 5. the launcher: which grids opt in
# --------------------------------------------------------------------------- #
_DISCOVERY = ["O_LC", "O_LP", "O_VERT", "O_BULLCS", "O_BULLPS", "O_BEARCS", "O_BF", "O_IC",
              "O_JL", "O_RS", "O_CSP", "O_STRD", "O_STRG", "O_CC", "O_PP", "O_WHEEL"]
_EQUITY = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]


@pytest.mark.parametrize("kind", _DISCOVERY + _EQUITY + ["O_STK", "O_CONVEX", "OS1", "OS2",
                                                         "OS3", "OS4"])
def test_stage1_equity_and_other_existing_grids_stay_zero_anchored(kind):
    """The running stage-1 grid and every equity grid must decode exactly as their checkpoints
    and persisted TOP-N rows were produced."""
    m = _launcher()
    assert m._lattice_anchor_for(kind, None) == "zero"


@pytest.mark.parametrize("kind", ["O_LEAP", "O_PMCC", "O_ERN", "O_CBS", "O_PBS"])
def test_the_leaps_grid_defaults_to_the_min_anchor(kind):
    m = _launcher()
    assert kind in m._GRID2_OPTION_STRATEGIES
    assert m._lattice_anchor_for(kind, None) == "min"


def test_an_explicit_flag_overrides_the_per_grid_default():
    m = _launcher()
    assert m._lattice_anchor_for("O_LC", "min") == "min"
    assert m._lattice_anchor_for("O_PMCC", "zero") == "zero"


def test_the_stage1_discovery_list_is_the_one_this_file_checks():
    spec = importlib.util.spec_from_file_location(
        "rom_lattice", os.path.join(os.path.dirname(_LAUNCHER_PATH), "..", "tools",
                                    "run_options_matrix.py"))
    rom = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rom)
    assert rom._DISCOVERY_STRATEGIES == _DISCOVERY


def test_a_zero_anchored_job_writes_no_new_key_into_its_config(monkeypatch):
    """Byte-identical optimization_config for every job that does not opt in."""
    from test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize
    cfg = _run_optimize(_parse(_BASE_ARGV), monkeypatch)
    assert "latticeAnchor" not in cfg


def test_the_flag_reaches_the_optimize_config(monkeypatch):
    from test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize
    cfg = _run_optimize(_parse(_BASE_ARGV + ["--lattice-anchor", "min"]), monkeypatch)
    assert cfg["latticeAnchor"] == "min"


def test_the_flag_reaches_the_batch_config(monkeypatch):
    from test_equity_cap_launcher import (_BATCH_ARGV, _parse, _run_optimize_batch)
    cfg = _run_optimize_batch(
        _parse(_BATCH_ARGV + ["--lattice-anchor", "min"], cmd_attr="_cmd_optimize_batch"),
        monkeypatch)
    assert cfg["latticeAnchor"] == "min"
    cfg = _run_optimize_batch(_parse(_BATCH_ARGV, cmd_attr="_cmd_optimize_batch"), monkeypatch)
    assert "latticeAnchor" not in cfg


@pytest.mark.parametrize("kind,expected", [("O_PMCC", "min"), ("O_LC", None)])
def test_optimize_without_the_flag_stores_the_per_grid_default(monkeypatch, kind, expected):
    """END TO END through the real CLI and ``_cmd_optimize``: a grid-2 key launched with NO
    ``--lattice-anchor`` persists ``latticeAnchor: "min"``; a non-grid-2 key persists no key at
    all (its config stays byte-identical to every job launched before the anchor existed)."""
    from test_equity_cap_launcher import _parse, _run_optimize
    monkeypatch.delenv("BACKTEST_OPTIONS_STORE", raising=False)
    argv = ["optimize", "--expert", "FMPRating", "--universe", "AAPL",
            "--start", "2024-03-01", "--end", "2024-04-01",
            "--population", "2", "--generations", "1",
            "--strategy", kind, "--options-store", "parquet"]
    args = _parse(argv)
    assert args.lattice_anchor is None
    cfg = _run_optimize(args, monkeypatch)
    if expected is None:
        assert "latticeAnchor" not in cfg
    else:
        assert cfg["latticeAnchor"] == expected


def test_snap_to_lattice_refuses_an_unknown_anchor():
    """Public helper: an unknown anchor must raise, never fall through to the min branch."""
    from app.services.genetic import snap_to_lattice
    g = _OFF_LATTICE["leaps_dte"]
    assert snap_to_lattice(410, g, "zero") == 405
    assert snap_to_lattice(410, g, "min") == 410
    for bad in ("max", "MIN", "", None):
        with pytest.raises(ValueError, match="anchor"):
            snap_to_lattice(410, g, bad)


@pytest.mark.parametrize("adapter", ["PyGADAdapter", "ShinkaEvolveAdapter"])
def test_the_placeholder_adapters_decode_through_snap_to_lattice(monkeypatch, adapter):
    """The PyGAD/Shinka placeholders carried their own copy of the zero-lattice formula; they
    now call ``snap_to_lattice`` (zero anchor, their historical behaviour) -- one formula."""
    import app.services.genetic as G
    import app.services.genetic_optimizer_base as B

    space = {**_OFF_LATTICE, **_ON_LATTICE}
    opt = getattr(B, adapter)(param_ranges=space)
    raw = [g["min"] for g in space.values()]
    want = {n: G.snap_to_lattice(v, space[n], "zero") for n, v in zip(space, raw)}
    assert opt.decode_individual(raw) == want
    assert opt.decode_individual(raw)["leaps_dte"] == 405

    calls = []
    real = G.snap_to_lattice
    monkeypatch.setattr(G, "snap_to_lattice",
                        lambda v, c, a="zero": (calls.append(a), real(v, c, a))[1])
    opt.decode_individual(raw)
    assert calls == ["zero"] * len(space)
