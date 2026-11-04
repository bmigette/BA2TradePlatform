"""Golden parity: the Portfolio Allocation page loaders and ``services.portfolio`` return what the ORIGINAL page
loaders returned. ``tests/golden/portfolio_loaders.json`` was captured from ``ui/pages/portfolio_allocation.py``
BEFORE the loaders moved (regenerate only deliberately: ``BA2_REGEN_GOLDEN=1``)."""
import json
import os
from pathlib import Path

import pytest

from ba2_trade_platform.core.portfolio_allocation import VALUATION_MODE_COST, VALUATION_MODE_MARKET
from tests.portfolio_golden import build_scenario, normalise

GOLDEN = Path(__file__).parent / "golden" / "portfolio_loaders.json"


def _use(monkeypatch):
    import ba2_trade_platform.core.utils as core_utils

    def install(account):
        monkeypatch.setattr(core_utils, "get_account_instance_from_id", lambda account_id: account)
    return install


def _capture(loaders, acct_id):
    gate = loaders.gate(acct_id)
    out = {
        "gate": normalise(gate),
        "valuation_mode": loaders.valuation_mode(acct_id),
        "view_market": normalise(loaders.view(acct_id, VALUATION_MODE_MARKET)),
        "view_cost": normalise(loaders.view(acct_id, VALUATION_MODE_COST)),
        "view_simulated": normalise(loaders.view(acct_id, VALUATION_MODE_MARKET, 30_000.0)),
    }
    events, open_total, note = loaders.income(acct_id)
    out["income"] = normalise({"events": events, "open_total": open_total, "note": note})
    return out


class _PageLoaders:
    def __init__(self):
        from ba2_trade_platform.ui.pages import portfolio_allocation as page
        self.gate, self.valuation_mode = page._load_gate, page._load_valuation_mode
        self.view, self.income = page._load_view_payload, page._load_income_panel


class _ServiceLoaders:
    def __init__(self):
        from ba2_trade_platform.services import portfolio as svc
        self.gate, self.valuation_mode = svc.load_gate, svc.load_valuation_mode
        self.view, self.income = svc.load_view_payload, svc.load_income_panel


def _subset(expected, actual, path=""):
    """Every key/value of ``expected`` is in ``actual`` (the service may ADD keys, never change one)."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        for k, v in expected.items():
            assert k in actual, f"{path}/{k} missing"
            _subset(v, actual[k], f"{path}/{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(expected) == len(actual), path
        for i, (e, a) in enumerate(zip(expected, actual)):
            _subset(e, a, f"{path}[{i}]")
    else:
        assert expected == actual, f"{path}: {expected!r} != {actual!r}"


def test_regenerate_golden_from_the_page(monkeypatch):
    if not os.environ.get("BA2_REGEN_GOLDEN"):
        pytest.skip("golden is committed; set BA2_REGEN_GOLDEN=1 to recapture from the page")
    acct_id, _ = build_scenario(_use(monkeypatch))
    GOLDEN.write_text(json.dumps(_capture(_PageLoaders(), acct_id), indent=1, sort_keys=True), encoding="utf-8")


@pytest.mark.parametrize("which", [_PageLoaders, _ServiceLoaders], ids=["page", "service"])
def test_loaders_equal_the_golden_capture(monkeypatch, which):
    acct_id, _ = build_scenario(_use(monkeypatch))
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    _subset(expected, json.loads(json.dumps(_capture(which(), acct_id), sort_keys=True)))
