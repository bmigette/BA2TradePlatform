"""Live callers of the screener receiving ScreenerDataError: loud, with the cause, no silent empty."""
import logging
from types import SimpleNamespace

import pytest
import requests

import ba2_providers
import ba2_providers.fmp_common as fmp_common
from ba2_providers.StockScreener import ScreenerDataError
from ba2_providers.screener.FMPScreenerProvider import FMPScreenerProvider


def _outage_provider(monkeypatch):
    def down(url, params=None, **kw):
        raise requests.ConnectionError("vendor down")

    monkeypatch.setattr(fmp_common, "fmp_http_get", down)
    p = FMPScreenerProvider.__new__(FMPScreenerProvider)
    p.api_key = "k"
    monkeypatch.setattr(ba2_providers, "get_provider", lambda cat, name, **kw: p)


def test_job_manager_logs_the_cause_and_returns_so_other_instances_continue(monkeypatch):
    import ba2_trade_platform.core.JobManager as jm
    import ba2_trade_platform.core.utils as utils
    import ba2_trade_platform.core.StockScreener as shim

    errors = []
    monkeypatch.setattr(jm, "logger", SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda msg, *a, **k: errors.append(msg)))
    monkeypatch.setattr(utils, "get_expert_instance_from_id",
                        lambda i: SimpleNamespace(settings={}, shortname="X"))

    class _Boom:
        def __init__(self, settings):
            pass

        def screen(self):
            raise ScreenerDataError("FMP screener request failed (ConnectionError: vendor down)")

    monkeypatch.setattr(shim, "StockScreener", _Boom)
    # must NOT raise: the scheduler loop moves on to the next instance
    jm.JobManager._execute_screener_analysis(SimpleNamespace(), 7, "ENTER_MARKET")
    assert len(errors) == 1
    assert "expert 7" in errors[0] and "vendor down" in errors[0]


def test_penny_phase_1_propagates_a_stage_1_outage(monkeypatch):
    from ba2_experts.PennyMomentumTrader.screening import ScreeningPhasesMixin

    _outage_provider(monkeypatch)
    vals = {"screener_provider": "fmp", "scan_sector_exclude": "", "max_scan_candidates": 10,
            "min_relative_volume": 1.0, "include_gainers": False, "scan_price_min": 0.1,
            "scan_price_max": 5.0, "scan_market_cap_min": 1e7, "scan_market_cap_max": 5e8,
            "scan_float_max": 0, "scan_volume_min": 500000}
    stub = SimpleNamespace(logger=logging.getLogger("penny-test"),
                           get_setting_with_interface_default=lambda k, log_warning=False: vals[k])
    with pytest.raises(ScreenerDataError, match="ConnectionError"):
        ScreeningPhasesMixin._phase_1_screen(stub, SimpleNamespace(id=1))
