"""Final whole-branch review of the ML exploration v2, part C (wizard / training): leftovers that only a reader would
trip over. Lint-style checks on the sources (an unused import, a doubled import line, a line mangled by a bad wrap, a
docstring that points at a module that no longer owns the function) so they cannot come back."""
import ast
import re
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"
API = Path(__file__).resolve().parents[1] / "app" / "api"


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _imported(path):
    out = {}
    for node in _tree(path).body:
        if isinstance(node, ast.ImportFrom):
            out.setdefault(node.module, []).append(node)
    return out


def _used_names(path):
    return {n.id for n in ast.walk(_tree(path)) if isinstance(n, ast.Name)}


@pytest.mark.parametrize("module, names", [
    ("darts_training.py", ["has_masked_cells"]),
    ("tsai_training.py", ["three_way_bounds"]),
    ("job_handler.py", ["purged_train_row_count", "three_way_bounds"]),
])
def test_the_unused_imports_are_gone(module, names):
    path = SERVICES / module
    imported = {a.asname or a.name for nodes in _imported(path).values() for n in nodes for a in n.names}
    used = _used_names(path)
    for name in names:
        assert name not in imported, f"{module} imports {name}" + ("" if name in used else " and never uses it")


def test_job_handler_has_one_import_line_per_module():
    dup = {m: [n.lineno for n in nodes] for m, nodes in _imported(SERVICES / "job_handler.py").items() if len(nodes) > 1}
    assert not dup, dup


def test_the_tsai_docstring_points_at_the_module_that_owns_the_mask_fill():
    text = (SERVICES / "tsai_training.py").read_text(encoding="utf-8")
    assert "darts_training.fill_masked_covariates" not in text
    assert "masked_fill.fill_masked_covariates" in text


def test_no_code_line_of_the_datasets_api_is_mangled_by_a_join_of_two_lines():
    for n, line in enumerate((API / "datasets.py").read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#")[0] if line.lstrip().startswith("#") is False else ""
        assert not re.search(r"\S {8,}\S", code), f"datasets.py:{n} has a long run of spaces inside a statement: {line.strip()[:80]}"
