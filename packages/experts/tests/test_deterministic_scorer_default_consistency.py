"""Every DeterministicScorer setting has ONE default.

How the yc_scale bug hid (2026-08-07 .. 2026-11-09): ``macro.DEF_YC_SCALE`` was corrected from
0.005 to 0.5, but the SETTING DEFINITION kept a literal 0.005. Live resolves a setting through the
definition (``get_setting_with_interface_default``) and the backtest pre-fills every
``_SETTING_KEYS`` entry from it, so the corrected constant was only ever read by an inline
``settings.get("yc_scale", DEF_YC_SCALE)`` fallback that no real caller reaches.

This test compares each definition default with the two other places a default can live:

* a module-level ``DEF_*`` constant of the package that names the same setting;
* an inline ``<dict>.get("<setting>", <fallback>)`` in the package (or in
  ``ba2_experts.earnings_surprise``, which reads the DS settings dict for the PEAD section).

A mismatch that is known and deliberately NOT changed (changing it could move a result) sits in
``KNOWN_MISMATCHES`` with its reason; the test also fails when such an entry stops being a
mismatch, so the list cannot go stale.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

DS_PKG = importlib.import_module("ba2_experts.DeterministicScorer")
DeterministicScorer = DS_PKG.DeterministicScorer
DEFS: Dict[str, Dict[str, Any]] = DeterministicScorer.get_settings_definitions()

_PKG_DIR = Path(DS_PKG.__file__).parent
_MODULES = {p.stem: f"ba2_experts.DeterministicScorer.{p.stem}" for p in _PKG_DIR.glob("*.py")}
_MODULES["__init__"] = "ba2_experts.DeterministicScorer"
_MODULES["earnings_surprise"] = "ba2_experts.earnings_surprise"

# DEF_* constants whose name does not spell the setting key (module, constant) -> key.
# Constants named DEF_<KEY upper-cased> are matched automatically.
_CONSTANT_ALIASES = {
    ("analyst", "DEF_WINDOW_DAYS"): "analyst_window_days",
    ("analyst", "DEF_HALFLIFE_DAYS"): "analyst_recency_halflife_days",
    ("analyst", "DEF_TARGET_WINDOW_DAYS"): "analyst_target_window_days",
    ("analyst", "DEF_TARGET_SCALE"): "analyst_target_scale",
    ("analyst", "DEF_MIN_TARGETS"): "analyst_min_targets",
    ("combine", "DEF_W_MACRO_AS_INPUT"): "w_macro",
    ("combine", "DEF_SKIP_ON_MISSING"): "skip_on_missing_section",
    ("technical", "DEF_MOM_LOOKBACK"): "mom_lookback_days",
    ("technical", "DEF_MOM_SKIP"): "mom_skip_days",
    ("technical", "DEF_SMA_TREND"): "sma_trend_period",
    ("technical", "DEF_DONCHIAN"): "donchian_period",
    ("earnings_surprise", "DEF_DRIFT_WINDOW_DAYS"): "earnings_drift_window_days",
    ("earnings_surprise", "DEF_HALFLIFE_DAYS"): "earnings_halflife_days",
    ("earnings_surprise", "DEF_SCALE_PCT"): "earnings_scale_pct",
}

# DEF_* constants that are NOT a DeterministicScorer setting (fixed internals).
_NOT_A_SETTING = {
    ("combine", "DEF_EXIT_HYSTERESIS"),        # exit_hysteresis is deliberately not exposed
    ("fundamental", "DEF_Z_GREY"),
    ("fundamental", "DEF_QUALITY_NEUTRAL_ROE"),
    ("fundamental", "DEF_QUALITY_K"),
    ("fundamental", "DEF_VALUE_NEUTRAL_YIELD"),
    ("fundamental", "DEF_VALUE_K"),
    ("fundamental", "DEF_ACCEL_TRAILING_PERIODS"),
    ("macro", "DEF_MIN_INPUTS_FOR_RISKOFF"),
    ("earnings_surprise", "DEF_MIN_SUE_HISTORY"),
}

# Known, deliberately unchanged mismatches: (module, key, fallback) -> reason.
KNOWN_MISMATCHES = {
    ("__init__", "fundamentals_max_age_days", 0): (
        "_build_fundamental falls back to 0 (= age filter OFF) while the definition says 450. "
        "Unreachable for live and backtest (both hand _process a fully-resolved dict); only a "
        "caller passing a partial dict sees it. Reported 2026-11-09, not changed here."),
}


def _module_constants() -> List[Tuple[str, str, Any]]:
    out = []
    for stem, modname in sorted(_MODULES.items()):
        mod = importlib.import_module(modname)
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.startswith("DEF_"):
                        out.append((stem, t.id, getattr(mod, t.id)))
    return out


def _inline_fallbacks() -> List[Tuple[str, int, str, Any]]:
    """(module, line, key, evaluated fallback) for every ``X.get("<setting>", <fallback>)``."""
    out = []
    # A fallback can name a constant imported INSIDE a function (combine.final_score imports
    # DEF_M_FLOOR from .macro locally), so names resolve against every scanned module, the
    # module's own namespace winning.
    shared: Dict[str, Any] = {}
    for modname in _MODULES.values():
        shared.update(vars(importlib.import_module(modname)))
    for stem, modname in sorted(_MODULES.items()):
        mod = importlib.import_module(modname)
        namespace = {**shared, **vars(mod)}
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value in DEFS):
                continue
            expr = ast.Expression(body=node.args[1])
            value = eval(compile(expr, f"<{stem}:{node.lineno}>", "eval"), namespace)  # noqa: S307 -- our own source
            out.append((stem, node.lineno, node.args[0].value, value))
    return out


def test_every_def_constant_is_classified():
    """A new DEF_* constant must be mapped to its setting (or declared not-a-setting), so this
    test cannot silently stop checking it."""
    unclassified = []
    for stem, name, _value in _module_constants():
        if name == "DEF_MW" or (stem, name) in _CONSTANT_ALIASES or (stem, name) in _NOT_A_SETTING:
            continue
        if name[len("DEF_"):].lower() in DEFS:
            continue
        unclassified.append(f"{stem}.{name}")
    assert not unclassified, f"classify these constants in this test: {unclassified}"


def test_definition_defaults_equal_the_module_constants():
    mismatches = []
    for stem, name, value in _module_constants():
        if name == "DEF_MW":
            for sub, w in value.items():
                key = f"mw_{sub}"
                assert key in DEFS, f"DEF_MW[{sub!r}] has no {key} setting"
                if DEFS[key]["default"] != w:
                    mismatches.append(f"{key}: definition {DEFS[key]['default']!r} != DEF_MW[{sub!r}] {w!r}")
            continue
        if (stem, name) in _NOT_A_SETTING:
            continue
        key = _CONSTANT_ALIASES.get((stem, name), name[len("DEF_"):].lower())
        if DEFS[key]["default"] != value:
            mismatches.append(f"{key}: definition {DEFS[key]['default']!r} != {stem}.{name} {value!r}")
    assert not mismatches, "\n".join(mismatches)


def test_yc_scale_definition_is_the_module_constant():
    macro = importlib.import_module("ba2_experts.DeterministicScorer.macro")
    assert DEFS["yc_scale"]["default"] == macro.DEF_YC_SCALE == 0.5
    assert "percent" in DEFS["yc_scale"]["description"].lower()


def test_inline_fallbacks_equal_the_definition_defaults():
    found = set()
    mismatches = []
    for stem, line, key, fallback in _inline_fallbacks():
        if fallback == DEFS[key]["default"]:
            continue
        if (stem, key, fallback) in KNOWN_MISMATCHES:
            found.add((stem, key, fallback))
            continue
        mismatches.append(f"{stem}.py:{line} .get({key!r}, {fallback!r}) != definition "
                          f"{DEFS[key]['default']!r}")
    assert not mismatches, "\n".join(mismatches)
    stale = set(KNOWN_MISMATCHES) - found
    assert not stale, f"KNOWN_MISMATCHES entries that no longer mismatch (remove them): {stale}"


def test_the_scan_sees_the_known_call_sites():
    """Guard the scanner itself: it must find fallbacks in several modules, or a refactor of the
    call shape would make the equality test pass vacuously."""
    stems = {stem for stem, *_ in _inline_fallbacks()}
    assert {"__init__", "combine", "technical", "fundamental", "analyst",
            "earnings_surprise"} <= stems


def test_build_regime_reads_the_resolved_setting():
    """_build_regime reads yc_scale from the resolved settings, with no inline fallback: a
    settings dict without it is a caller bug, not a reason to pick a scale."""
    import math
    import pandas as pd
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1
    bundle = {"macro_inputs": {"spread_10y3m_series": pd.Series([0.25] * 70)},
              "index_closes": None}
    for scale in (0.005, 0.5):
        regime = e._build_regime(bundle, {"macro_mode": "multiply", "yc_scale": scale})
        assert regime["components"]["yield_curve"]["value"] == pytest.approx(math.tanh(0.25 / scale))
    with pytest.raises(KeyError):
        e._build_regime(bundle, {"macro_mode": "multiply"})
