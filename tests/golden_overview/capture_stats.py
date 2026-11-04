"""ONE-OFF golden capture of what the INLINE Overview widget code computes (run on the pre-extraction commit):

    BA2_CAPTURE_GOLDEN=1 pytest tests/golden_overview/capture_stats.py -q -p no:cacheprovider

It runs the real widget methods on ``seed.seed_world()`` and records the local variables they draw from (read at
function return through ``sys.setprofile``), then writes ``stats_golden.json``. Deleted once the services exist
(the JSON stays; ``test_stats_golden.py`` holds the service to it)."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.golden_overview.seed import NOW, POSITIONS, seed_world

pytestmark = pytest.mark.skipif(not os.environ.get("BA2_CAPTURE_GOLDEN"), reason="one-off capture")
OUT = Path(__file__).with_name("stats_golden.json")


def _locals_at_return(func_name, call):
    """Run ``call()`` and return the locals of the (last) frame of ``func_name`` at its return."""
    grabbed = {}

    def prof(frame, event, arg):
        if event == "return" and frame.f_code.co_name == func_name:
            grabbed.clear()
            grabbed.update(frame.f_locals)
    sys.setprofile(prof)
    try:
        result = call()
    finally:
        sys.setprofile(None)
    return grabbed, result


def test_capture(monkeypatch):
    from ba2_trade_platform.ui.components import BalanceUsagePerExpertChart as _bu  # noqa: F401
    from ba2_trade_platform.ui.components.InstrumentDistributionChart import InstrumentDistributionChart
    from ba2_trade_platform.ui.components.ProfitPerExpertChart import ProfitPerExpertChart
    ppe_mod = sys.modules[ProfitPerExpertChart.__module__]
    import ba2_trade_platform.ui.pages.overview as overview

    ids = seed_world()

    class _Frozen(overview.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(overview, "datetime", _Frozen)
    monkeypatch.setattr(overview, "ui", MagicMock())

    scopes = {"all": None, "acct1": ids["acct1"], "acct2": ids["acct2"]}
    out = {"inline_code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
           "ids": ids, "trade_performance": {}, "order_counts": {}, "profit_per_expert": {}, "distribution": {}}
    keys = ("total_count", "open_count", "pnl_7d", "wins_7d", "losses_7d", "pnl_prev_7d", "pnl_30d",
            "wins_30d", "losses_30d", "pnl_prev_30d", "closed_count")
    tab = object.__new__(overview.OverviewTab)
    for name, acct in scopes.items():
        monkeypatch.setattr(overview, "get_selected_account_id", lambda a=acct: a)
        got, _ = _locals_at_return(
            "_load_trade_performance_data",
            lambda: asyncio.run(tab._load_trade_performance_data(MagicMock(), MagicMock())))
        out["trade_performance"][name] = {k: got[k] for k in keys}
        got, _ = _locals_at_return(
            "_load_order_statistics_data",
            lambda: asyncio.run(tab._load_order_statistics_data(MagicMock(), MagicMock())))
        out["order_counts"][name] = [list(t) for t in got["account_stats"]]

        monkeypatch.setattr(ppe_mod, "get_selected_account_id", lambda a=acct: a)
        from sqlmodel import select
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import ExpertInstance

        def expert_ids(a):
            if a is None:
                return None
            with get_db() as s:
                return list(s.exec(select(ExpertInstance.id).where(ExpertInstance.account_id == a)).all())
        monkeypatch.setattr(ppe_mod, "get_expert_ids_for_account", expert_ids)
        chart = object.__new__(ProfitPerExpertChart)
        out["profit_per_expert"][name] = list(chart.calculate_expert_profits().items())

    for grouping in ("labels", "categories"):
        d = object.__new__(InstrumentDistributionChart)
        d.positions, d.grouping_field = POSITIONS, grouping
        out["distribution"][grouping] = list(d.calculate_position_distribution().items())
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True))
