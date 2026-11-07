"""The ``data_provider_estimates`` app setting: declared, shown on the Settings page, and read live."""
import pytest

from ba2_common.core import estimates_source as es
from ba2_trade_platform.core.app_settings import APP_SETTINGS_DEFINITIONS, app_setting_default


def test_declared_with_fmp_default_and_both_choices():
    meta = APP_SETTINGS_DEFINITIONS[es.ESTIMATES_SETTING_KEY]
    assert app_setting_default(es.ESTIMATES_SETTING_KEY) == "fmp"
    assert tuple(meta["choices"]) == es.ESTIMATES_SOURCES and meta["type"] == "str"


def test_the_settings_page_lists_it():
    import inspect
    from ba2_trade_platform.ui.pages import settings

    src = inspect.getsource(settings)
    assert "'data_provider_estimates': 'estimates_provider_input'" in src
    assert "estimates_provider_input = ui.select(" in src


def test_live_resolver_reads_the_stored_value_else_the_declared_default(monkeypatch):
    import ba2_common.config as cfg
    from ba2_trade_platform.core.seam_wiring import live_estimates_source_from_settings

    stored = {}
    monkeypatch.setattr(cfg, "get_app_setting", lambda key, d=None: stored.get(key, d))
    es.set_live_estimates_source_resolver(live_estimates_source_from_settings)
    try:
        assert es.live_estimates_source() == "fmp"                      # nothing stored: the declared default
        stored[es.ESTIMATES_SETTING_KEY] = "alphavantage"
        assert es.live_estimates_source() == "alphavantage"
        stored[es.ESTIMATES_SETTING_KEY] = "bogus"
        with pytest.raises(es.UnknownEstimatesSource):                  # a bad stored value is loud
            es.live_estimates_source()
    finally:
        es.set_live_estimates_source_resolver(None)


def test_seam_wiring_installs_the_resolver():
    import inspect
    from ba2_trade_platform.core import seam_wiring

    assert "set_live_estimates_source_resolver(live_estimates_source_from_settings)" in inspect.getsource(seam_wiring)
