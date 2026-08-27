"""ba2_experts.analyst_target_model -- the shared, opt-in fundamentals-only price-target
estimator for experts with no real analyst-target mechanism (FMPInsiderClusterBuy,
FMPEarningsDrift, DeterministicScorer)."""
import pytest

from ba2_experts.analyst_target_model import (
    DEFAULT_MAX_ANCHOR_PE, DEFAULT_MAX_EXPECTED_PROFIT_PERCENT,
    estimate_price_target, fetch_estimator_inputs,
)


def _earnings(*eps_values):
    return [{"reported_eps": v} for v in eps_values]


def _estimates(*eps_avgs):
    return [{"estimated_eps_avg": v} for v in eps_avgs]


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #
def test_forward_method_anchors_on_the_nearest_fy_estimate():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}  # FY0=5.0, FY1=6.0
    out = estimate_price_target(inputs, current_price=100.0, method="forward")
    assert out is not None
    assert out["method"] == "forward"
    assert out["anchor_pe"] == pytest.approx(20.0)      # 100 / 5.0
    assert out["target_price"] == pytest.approx(120.0)  # 20.0 * 6.0
    assert out["expected_profit_percent"] == pytest.approx(20.0)


def test_trailing_method_anchors_on_summed_ttm_reported_eps():
    inputs = {"earnings": _earnings(1.0, 1.0, 1.0, 1.0),  # TTM = 4.0
             "estimates": _estimates(5.0)}
    out = estimate_price_target(inputs, current_price=100.0, method="trailing")
    assert out is not None
    assert out["method"] == "trailing"
    assert out["anchor_pe"] == pytest.approx(25.0)       # 100 / 4.0
    assert out["target_price"] == pytest.approx(125.0)   # 25.0 * 5.0


def test_default_method_is_forward():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}
    out = estimate_price_target(inputs, current_price=100.0)
    assert out["method"] == "forward"


# --------------------------------------------------------------------------- #
# insufficient-data -> None, never an exception
# --------------------------------------------------------------------------- #
def test_forward_needs_two_estimate_periods():
    inputs = {"earnings": [], "estimates": _estimates(5.0)}  # only FY0
    assert estimate_price_target(inputs, 100.0, method="forward") is None


def test_trailing_needs_four_quarters():
    inputs = {"earnings": _earnings(1.0, 1.0, 1.0), "estimates": _estimates(5.0)}
    assert estimate_price_target(inputs, 100.0, method="trailing") is None


def test_trailing_needs_at_least_one_forward_estimate():
    inputs = {"earnings": _earnings(1.0, 1.0, 1.0, 1.0), "estimates": []}
    assert estimate_price_target(inputs, 100.0, method="trailing") is None


def test_no_current_price_is_none():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}
    assert estimate_price_target(inputs, None, method="forward") is None
    assert estimate_price_target(inputs, 0.0, method="forward") is None
    assert estimate_price_target(inputs, -5.0, method="forward") is None


def test_non_positive_anchor_eps_is_none():
    inputs = {"earnings": [], "estimates": _estimates(0.0, 6.0)}
    assert estimate_price_target(inputs, 100.0, method="forward") is None
    inputs2 = {"earnings": [], "estimates": _estimates(-1.0, 6.0)}
    assert estimate_price_target(inputs2, 100.0, method="forward") is None


def test_non_positive_following_eps_is_none():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 0.0)}
    assert estimate_price_target(inputs, 100.0, method="forward") is None


def test_unknown_method_raises_not_silently_misbehaves():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}
    with pytest.raises(ValueError, match="method must be one of"):
        estimate_price_target(inputs, 100.0, method="bogus")


# --------------------------------------------------------------------------- #
# max_anchor_pe: reject the degenerate-anchor blowup (the VRTX 905x case)
# --------------------------------------------------------------------------- #
def test_anchor_pe_past_the_cap_is_rejected_outright():
    # price=100, anchor_eps=1.0 -> anchor P/E = 100x, past the 80x default cap
    inputs = {"earnings": [], "estimates": _estimates(1.0, 6.0)}
    assert estimate_price_target(inputs, 100.0, method="forward") is None


def test_anchor_pe_at_exactly_the_cap_is_accepted():
    inputs = {"earnings": [], "estimates": _estimates(1.25, 6.0)}  # 100/1.25 = 80.0 exactly
    out = estimate_price_target(inputs, 100.0, method="forward", max_anchor_pe=80.0)
    assert out is not None
    assert out["anchor_pe"] == pytest.approx(80.0)


def test_custom_max_anchor_pe_is_respected():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}  # anchor P/E = 20x
    assert estimate_price_target(inputs, 100.0, method="forward", max_anchor_pe=10.0) is None
    assert estimate_price_target(inputs, 100.0, method="forward", max_anchor_pe=25.0) is not None


def test_default_max_anchor_pe_constant_matches_module_default():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}
    out = estimate_price_target(inputs, 100.0, method="forward",
                                max_anchor_pe=DEFAULT_MAX_ANCHOR_PE)
    assert out is not None


# --------------------------------------------------------------------------- #
# max_expected_profit_percent: cap the OUTPUT, don't reject the computation
# (the AI-supercycle overshoot case: a sane anchor, an extreme following-period estimate)
# --------------------------------------------------------------------------- #
def test_expected_profit_is_capped_not_rejected():
    # anchor P/E = 100/5 = 20x (sane), following EPS implies +900% profit uncapped
    inputs = {"earnings": [], "estimates": _estimates(5.0, 50.0)}
    out = estimate_price_target(inputs, 100.0, method="forward")
    assert out is not None  # capped, not None -- a sane anchor still answers
    assert out["expected_profit_percent"] == pytest.approx(DEFAULT_MAX_EXPECTED_PROFIT_PERCENT)


def test_target_price_and_expected_profit_stay_mutually_consistent_when_capped():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 50.0)}
    out = estimate_price_target(inputs, 100.0, method="forward")
    implied_pct = (out["target_price"] / 100.0 - 1.0) * 100.0
    assert implied_pct == pytest.approx(out["expected_profit_percent"], abs=0.01)
    assert out["target_price"] == pytest.approx(200.0)  # default cap 100% -> max 2x price


def test_default_cap_is_100_percent_ie_max_2x():
    assert DEFAULT_MAX_EXPECTED_PROFIT_PERCENT == 100.0


def test_a_profit_under_the_cap_is_untouched():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}  # 20% uncapped
    out = estimate_price_target(inputs, 100.0, method="forward")
    assert out["expected_profit_percent"] == pytest.approx(20.0)
    assert out["target_price"] == pytest.approx(120.0)


def test_cap_can_be_disabled():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 50.0)}
    out = estimate_price_target(inputs, 100.0, method="forward",
                                max_expected_profit_percent=float("inf"))
    assert out["expected_profit_percent"] == pytest.approx(900.0)


def test_custom_cap_is_respected():
    inputs = {"earnings": [], "estimates": _estimates(5.0, 6.0)}  # 20% uncapped
    out = estimate_price_target(inputs, 100.0, method="forward",
                                max_expected_profit_percent=10.0)
    assert out["expected_profit_percent"] == pytest.approx(10.0)
    assert out["target_price"] == pytest.approx(110.0)


# --------------------------------------------------------------------------- #
# fetch_estimator_inputs (I/O half) -- a minimal fake ProviderBundle
# --------------------------------------------------------------------------- #
class _FakeDetailsProvider:
    def __init__(self, earnings=None, estimates=None, raise_on=None):
        self._earnings = earnings if earnings is not None else []
        self._estimates = estimates if estimates is not None else []
        self._raise_on = raise_on  # "past" | "estimates" | None

    def get_past_earnings(self, **kw):
        if self._raise_on == "past":
            raise ValueError("boom")
        return {"earnings": self._earnings}

    def get_earnings_estimates(self, **kw):
        if self._raise_on == "estimates":
            raise ValueError("boom")
        return {"estimates": self._estimates}


class _FakeBundle:
    def __init__(self, details):
        self._details = details

    def fundamentals_details(self):
        return self._details


def test_fetch_estimator_inputs_happy_path():
    earnings = _earnings(1.0, 1.0, 1.0, 1.0)
    estimates = _estimates(5.0, 6.0)
    bundle = _FakeBundle(_FakeDetailsProvider(earnings=earnings, estimates=estimates))
    out = fetch_estimator_inputs(bundle, "AAPL", as_of=None)
    assert out == {"earnings": earnings, "estimates": estimates}


def test_fetch_estimator_inputs_degrades_to_empty_on_a_benign_fetch_failure(monkeypatch):
    import ba2_common.core.failure_modes as fm
    monkeypatch.setenv("BA2_ERROR_MODE", "legacy")  # a ValueError here is not a real defect type
    fm._mode.cache_clear() if hasattr(fm._mode, "cache_clear") else None
    bundle = _FakeBundle(_FakeDetailsProvider(raise_on="past"))
    out = fetch_estimator_inputs(bundle, "AAPL", as_of=None)
    assert out["earnings"] == []


def test_pipeline_fetch_then_estimate():
    earnings = _earnings(1.0, 1.0, 1.0, 1.0)
    estimates = _estimates(5.0, 6.0)
    bundle = _FakeBundle(_FakeDetailsProvider(earnings=earnings, estimates=estimates))
    inputs = fetch_estimator_inputs(bundle, "AAPL", as_of=None)
    out = estimate_price_target(inputs, current_price=100.0, method="trailing")
    assert out is not None
    assert out["anchor_pe"] == pytest.approx(25.0)
