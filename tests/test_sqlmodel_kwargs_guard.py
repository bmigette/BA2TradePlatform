"""No SQLModel may be constructed with a field it does not have.

SQLModel/Pydantic DROPS unknown keyword arguments in silence. Nothing raises, so
the value simply vanishes and the row is written without it -- and if the field
was NOT NULL, the failure only surfaces much later at COMMIT, far from the typo.

This has already cost real defects across the codebase:
  * AnalysisOutput(output_type=..., content=...) -- neither field exists, so
    every live DeterministicScorer analysis ended FAILED with an empty payload;
  * Position(quantity=..., average_entry_price=...) in IBKRAccount -- the model
    uses qty/avg_entry_price, so IBKR built positions with NO quantity;
  * TradingOrder(time_in_force=...) in AlpacaAccount's SL replacement;
  * Instrument(category=..., enabled=..., description=...) -- three fields lost
    on every auto-added instrument;
  * ExpertInstance(virtual_equity=...) on settings import;
  * TradingOrder(linked_order_id=...) -- a TP/SL order's parent link.

Fixing those six one by one only buys time until the seventh, so this scans the
whole repo statically instead. It is a compile-time-shaped check: no DB, no
imports of the call sites, just the AST.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from sqlmodel import SQLModel

from ba2_common.core import models as models_module

REPO = pathlib.Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("packages", "ba2_trade_platform", "testplatform/backend/app")
SKIP_PARTS = {"__pycache__", "node_modules", ".venv", "venv", "thirdparties"}


def _model_fields() -> dict:
    out = {}
    for name, obj in vars(models_module).items():
        if isinstance(obj, type) and issubclass(obj, SQLModel):
            fields = getattr(obj, "model_fields", None)
            if fields:
                out[name] = set(fields)
    return out


def _python_files():
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            parts = set(path.parts)
            if parts & SKIP_PARTS or "tests" in parts or path.name.startswith("test_"):
                continue
            yield path


def _violations():
    known = _model_fields()
    bad = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue                      # not our problem here
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name not in known:
                continue
            # **kwargs forwarding cannot be checked statically -- skip those calls
            if any(k.arg is None for k in node.keywords):
                continue
            unknown = sorted(k.arg for k in node.keywords
                             if k.arg and k.arg not in known[name])
            if unknown:
                bad.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno} "
                           f"{name}(...) unknown field(s): {', '.join(unknown)}")
    return sorted(bad)


def test_no_sqlmodel_constructed_with_unknown_fields():
    violations = _violations()
    assert not violations, (
        "SQLModel drops unknown kwargs silently, so each of these writes a row "
        "missing data nobody will be told about:\n  " + "\n  ".join(violations))


def test_the_guard_actually_detects_a_planted_typo(tmp_path, monkeypatch):
    """A scanner that silently matches nothing would pass forever. Plant a known
    violation and require the guard to see it."""
    planted = tmp_path / "packages" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("from x import Position\n"
                       "p = Position(symbol='AAPL', quantity=1.0)\n", encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "resolve", lambda self, *a, **k: self)
    monkeypatch.setitem(globals(), "REPO", tmp_path)
    import tests.test_sqlmodel_kwargs_guard as mod

    monkeypatch.setattr(mod, "REPO", tmp_path)
    found = mod._violations()
    assert any("quantity" in v for v in found), f"guard is blind; saw {found}"
