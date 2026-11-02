"""The API's screener-optimization weave (``strategies._merge_screener_opt``).

History: after the screener-simulation merge (2026-10-08) the API REFUSED every screener optimization that arrived without the
launcher's stamp (the web UI never sends one), with a message naming ``ba2-test optimize --screener``.  Since the
"UI screener launch" change the SERVER builds the universe and the stamps itself, with the functions the CLI calls
(``screener_launch`` / ``screener_job_request``): a UI-shaped request is no longer refused for lacking stamps, it is built, or
refused with the CLI's own reason.  The end-to-end equality with the CLI, the refusals table, FactorRanker and the stamped-replay
verification are pinned in ``test_screener_launch_shared.py``; this file pins the entry point's contract.
"""
import pytest
from fastapi import HTTPException

from app.api.strategies import _merge_screener_opt, _screener_job_name

UI_BODY = {"param_ranges": {"market_cap_min": {"optimize": True, "min": 1e9, "max": 5e9, "step": 1e9, "type": "float"}},
           "cadence_days": 7, "base_settings": {"market_cap_min": 2e9}}
BT = {"expert": {"class": "FMPRating", "settings": {}}, "start_date": "2024-01-02", "end_date": "2024-03-01",
      "execution_interval": "1d", "initial_capital": 10000.0, "commission": 0.1, "slippage": 0.0,
      "fill_model": "next_bar_open", "universe": {"mode": "screener"},
      "ohlcv_history_source": "fmp"}   # the API stamps it before the weave; the tests call the weave directly


def _cfg():
    return {"seed": 1, "backtest": dict(BT)}


FULL = {"market_cap_min": 2e9, "relative_volume_min": 0, "price_drop_pct": 0, "price_drop_days": 5, "max_stocks": 20,
        "weinstein_stage2_only": 0}


def test_selection_keys_the_request_leaves_unstated_are_refused_by_name_never_defaulted(tmp_path, monkeypatch):
    """The UI states its base settings; a key it does not state would run on StockScreener's defaults live, so the launch
    refuses naming the keys (409) and the remedy is the request field, not the CLI flag."""
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    from ba2_providers.screener import metric_store as ms
    import pandas as pd
    monkeypatch.setattr(ms, "load_store", lambda p: pd.DataFrame({"symbol": ["A"]}))
    with pytest.raises(HTTPException) as e:
        _merge_screener_opt(_cfg(), {**UI_BODY, "store": str(tmp_path / "store")})
    assert e.value.status_code == 409 and "missing selection key(s)" in e.value.detail
    assert "screener_opt.base_settings" in e.value.detail and "--screener-base-json" not in e.value.detail


def test_the_web_ui_request_shape_is_no_longer_refused_for_lacking_stamps(tmp_path, monkeypatch):
    """With no panel on the machine the UI gets the simulation's own refusal (409, the CLI's text), never the old
    'launch it with ba2-test optimize --screener' redirect and never a silent fall back to the weekly gate."""
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    from ba2_providers.screener import metric_store as ms
    import pandas as pd
    monkeypatch.setattr(ms, "load_store", lambda p: pd.DataFrame({"symbol": ["A"]}))
    c = _cfg()
    with pytest.raises(HTTPException) as e:
        _merge_screener_opt(c, {**UI_BODY, "base_settings": FULL, "store": str(tmp_path / "store")})
    assert e.value.status_code == 409
    assert e.value.detail.startswith("screener optimization REFUSED: the daily screener-criteria panel cannot serve this job:")
    assert "no daily criteria panel under" in e.value.detail and "ba2-test prewarm --screener-panel" in e.value.detail
    assert "cannot be launched from the API" not in e.value.detail
    assert "screener_opt" not in c["backtest"] and "enabled_instruments" not in c["backtest"]   # cfg untouched on refusal


@pytest.mark.parametrize("bad", [{"cadence_days": None}, {"base_settings": None}, {"param_ranges": {}},
                                 {"param_ranges": {"market_cap_min": {"optimize": False, "min": 1, "max": 2}}},
                                 {"cap_band": "huge"}, {"gate_only": True}])
def test_malformed_or_unsupported_requests_are_400(bad):
    with pytest.raises(HTTPException) as e:
        _merge_screener_opt(_cfg(), {**UI_BODY, **bad})
    assert e.value.status_code == 400


def test_a_request_without_a_backtest_window_is_400_not_a_default():
    for drop in ("start_date", "end_date", "execution_interval"):
        c = _cfg()
        del c["backtest"][drop]
        with pytest.raises(HTTPException) as e:
            _merge_screener_opt(c, dict(UI_BODY))
        assert e.value.status_code == 400 and drop in e.value.detail


def test_job_names_carry_the_identity_tokens():
    assert _screener_job_name("job", bypass=False) == "job-sup1-lds2"
    assert _screener_job_name("job", bypass=True) == "job-sup1"
    assert _screener_job_name("job-sup1-lds2", bypass=False) == "job-sup1-lds2"      # idempotent
    assert _screener_job_name(None, bypass=False) is None
