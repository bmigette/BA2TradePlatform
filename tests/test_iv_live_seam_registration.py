"""Live host wiring of the iv-v1 Alpha Vantage chain reader (2026-11-15): registered, SILENT, inert.

Prod (8081) pulls this code with no iv gate anywhere. What changes for such an instance must be
nothing: the registration reads no setting, opens no file, makes no call and logs nothing at INFO
or above; an expert without a chain profile never builds (or even asks for) a chain reader.
"""
from __future__ import annotations

import logging

from ba2_common.core.market_condition_iv_live import clear_live_chain_readers, live_chain_reader_factory


def test_the_registration_is_silent_and_registers_the_alphavantage_reader(caplog, monkeypatch):
    import requests

    from ba2_trade_platform.core import seam_wiring
    from ba2_providers.market_conditions.av_live_chain import make_reader

    def no_network(*a, **k):
        raise AssertionError("the registration must not make a request")
    monkeypatch.setattr(requests, "get", no_network)
    clear_live_chain_readers()
    try:
        with caplog.at_level(logging.DEBUG):
            seam_wiring._register_live_chain_readers()
        assert [r for r in caplog.records if r.levelno >= logging.INFO] == []
        assert live_chain_reader_factory("alphavantage") is make_reader
        assert live_chain_reader_factory("thetadata") is None
    finally:
        clear_live_chain_readers()


def test_an_expert_without_a_chain_profile_never_reaches_the_chain_seam(monkeypatch):
    from ba2_common.core import instance_resolver
    from ba2_common.core import market_condition_iv_live as ivl
    from ba2_common.core import market_condition_live as live

    class _E:
        settings = {}

    monkeypatch.setattr(instance_resolver, "get_instance_resolver",
                        lambda: type("R", (), {"get_expert_instance": lambda self, i: _E()})())
    asked = []
    monkeypatch.setattr(ivl, "live_chain_reader_factory", lambda src: asked.append(src))
    pir = live.PerInstanceMarketConditionResolver()
    assert pir.resolver_for(7) is None
    assert asked == [] and pir._resolvers == {}
