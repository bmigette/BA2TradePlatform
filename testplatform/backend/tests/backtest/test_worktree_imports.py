"""Every import this suite makes comes from THIS checkout.

The venv's editable installs (``ba2trade_app``, ``ba2test_app``) map
``ba2_trade_platform`` and ``ba2test_launcher`` to the MAIN checkout by absolute
path, so in a git worktree a bare import of either silently loads another
checkout's code. ``ba2test_launcher`` makes it contagious: it puts its OWN
``testplatform/backend`` on ``sys.path`` at import time, so every later ``app.*``
import follows it there.

That is not hypothetical. Running ``tests/replay tests/backtest/...`` in one
invocation from this worktree failed inside ``gather_tape`` with a message
describing a refusal that no longer exists here -- the main checkout's copy. The
conftest guards fix it; this test is what keeps them honest, because the symptom
of their removal is a green-looking suite measuring the wrong tree.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.backtest.conftest import checkout_root, module_is_under

#: The `app` package is the one that actually broke: nothing maps it, so it
#: follows whichever `testplatform/backend` reached `sys.path` first.
MODULES = (
    "ba2_trade_platform",
    "ba2test_launcher",
    "app.services.replay.gather_tape",
    "ba2_common.core.replay.observe",
    "ba2_providers.fmp_common",
    "ba2_experts.FMPRating",
)


@pytest.mark.parametrize("name", MODULES)
def test_the_import_resolves_inside_this_checkout(name):
    root = checkout_root()
    module = importlib.import_module(name)
    assert module_is_under(module, root), (
        f"{name} came from {module.__file__}, outside this checkout ({root}): this "
        f"suite is measuring another tree's code")


def test_the_checkout_root_is_the_one_holding_this_test():
    """The guard must anchor on THIS file, not on whatever imported first."""
    root = checkout_root()
    assert Path(__file__).resolve().is_relative_to(root)
    assert (root / "testplatform" / "backend" / "pytest.ini").is_file()
