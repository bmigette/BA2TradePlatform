"""GA-NEUTRALITY of the structure_share feature (owner 2026-11-06).

A style WITHOUT the opt-in ``structure_share`` field must build rules, genes and id maps that are
byte-identical to what the builder produced BEFORE the feature existed. The goldens in
``data/option_style_goldens.json`` were generated from the unmodified builder (plan
2026-11-06-structure-share-condition-plan.md, Task 0). ``long`` is golden WITHOUT the field: the
shipped ``long`` style opts in later (Task 10), so its golden is compared after removing the field.

Regenerate ONLY deliberately (a builder change that is meant to move these rules):
    python testplatform/backend/tests/test_option_style_ga_neutral.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

from tests.test_option_grid_foundations import _launcher  # noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
_STAGE2 = str(_ROOT / "tools/option_styles.stage2.json")
_GOLDEN = Path(__file__).resolve().parent / "data" / "option_style_goldens.json"
_NAMES = ["mid", "premium", "long"]


def snapshot(m, name: str, *, drop_structure_share: bool = False) -> dict:
    """The built style, as plain JSON: entry rules, exit rules, gene space, id maps."""
    from app.services.strategy_param_space import collect_param_space

    style = m._load_style_spec(_STAGE2, name)
    if drop_structure_share:
        style.pop("structure_share", None)
    m._STYLE_ACTIVE = style
    s = m._build_strategy_style("O_STYLE")
    return json.loads(json.dumps(
        {"entry": s.entry_rules, "exit": s.exit_rules, "genes": collect_param_space(s),
         "id_maps": s.style_id_maps}, sort_keys=True, default=str))


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    m = _launcher()
    monkeypatch.setattr(m, "_STYLE_ACTIVE", None)
    yield


@pytest.mark.parametrize("name", _NAMES)
def test_a_style_without_structure_share_builds_byte_identical_rules_and_genes(name):
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    got = snapshot(_launcher(), name, drop_structure_share=True)
    assert got["genes"] == golden[name]["genes"]
    assert got["id_maps"] == golden[name]["id_maps"]
    assert got["entry"] == golden[name]["entry"]
    assert got["exit"] == golden[name]["exit"]
    assert "structure_share" not in json.dumps(got)


def _regen() -> None:
    m = _launcher()
    out = {n: snapshot(m, n, drop_structure_share=True) for n in _NAMES}
    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN.write_text(json.dumps(out, sort_keys=True, indent=1), encoding="utf-8")
    print("wrote", _GOLDEN, {n: len(json.dumps(v)) for n, v in out.items()})


if __name__ == "__main__":
    _regen()
