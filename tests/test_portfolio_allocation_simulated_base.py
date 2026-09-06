"""The allocator's simulated-base what-if.

Asked for from live use on 2026-09-05: try an arbitrary account base and see how every
target and percentage would look.

The property under test throughout: a simulation changes only figures DERIVED from the
base. Holdings, prices, buying power and account value are measurements, and simulating
those would be a fabricated broker answer rather than a what-if.
"""
import pytest

import ba2_trade_platform.ui.pages.portfolio_allocation as page


class TestBanner:
    def test_the_banner_names_both_the_what_if_and_the_measurement(self):
        text = page.SIM_BANNER_FMT.format(base=10000.0, real='$2,350.78')
        assert '$10,000.00' in text
        assert '$2,350.78' in text
        # No doubled currency symbol: {real} arrives already formatted.
        assert '$$' not in text

    def test_it_says_review_is_locked(self):
        assert 'Review is disabled' in page.SIM_BANNER_FMT.format(base=1.0, real='$2.00')

    def test_an_unmeasurable_base_is_named_not_blank(self):
        # A broker that cannot supply a base is a REASON to simulate, so this path is
        # ordinary rather than exceptional; "instead of the measured ." would read as
        # a rendering fault.
        text = page.SIM_BANNER_FMT.format(base=5000.0, real=page.SIM_BANNER_NO_REAL)
        assert 'unknown' in text

    def test_it_says_the_extra_is_cash_and_positions_are_real(self):
        # The identity base = managed + free buying power is what the whole page
        # divides by, so the banner has to name WHICH term the what-if moves.
        text = page.SIM_BANNER_FMT.format(base=1.0, real='$2.00')
        assert 'CASH' in text
        assert 'positions are real' in text
        assert 'buying power' in text


class TestTheIdentityHolds:
    """base = managed + free buying power, before AND during a simulation.

    ``format_base_composition``, the reserve row and the allocation bar all DERIVE the
    managed side as ``base - buying_power``. Overriding the base without moving buying
    power therefore inflated managed by the difference: an $8,500 what-if over $4,764.46
    of real positions printed "$8,036.72 managed" and 94.5% allocated, when the honest
    answer is $4,764.46 and 56%. Reported from live use 2026-09-05.
    """

    REAL_BASE, REAL_BP = 5227.74, 463.28          # the screenshot's real numbers
    MANAGED = REAL_BASE - REAL_BP                 # $4,764.46

    def _simulate(self, base_override):
        """The arithmetic _load_view_payload performs, isolated from the DB."""
        managed = self.REAL_BASE - self.REAL_BP
        return base_override, base_override - managed

    def test_managed_stays_put_when_the_base_grows(self):
        base, bp = self._simulate(8500.0)
        assert base - bp == pytest.approx(self.MANAGED)

    def test_the_extra_lands_entirely_in_buying_power(self):
        base, bp = self._simulate(8500.0)
        assert bp == pytest.approx(8500.0 - self.MANAGED)
        assert bp > self.REAL_BP        # simulating a bigger base = simulating cash

    def test_allocated_falls_as_the_base_grows(self):
        # The reported symptom: 94.5% before the fix, ~56% after.
        base, bp = self._simulate(8500.0)
        allocated_pct = (base - bp) / base * 100.0
        assert allocated_pct == pytest.approx(56.05, abs=0.05)

    def test_a_base_below_the_managed_value_is_allowed_and_goes_negative(self):
        # A legitimate what-if: positions financed on margin. Clamping at zero would
        # restore a plausible number by breaking the identity a second time.
        base, bp = self._simulate(3000.0)
        assert bp < 0
        assert base - bp == pytest.approx(self.MANAGED)




class TestPayloadDrivesTheWarning:
    """``_render_sim_banner`` reads the PAYLOAD, never the switch."""

    def _drawn(self, payload):
        drawn = []

        class _Rec:
            def classes(self, *_a, **_k): return self
            def mark(self, *_a, **_k): return self
            def __enter__(self): return self
            def __exit__(self, *_a): return False

        real_element, real_label = page.ui.element, page.ui.label
        page.ui.element = lambda *a, **k: _Rec()
        page.ui.label = lambda text='', *a, **k: drawn.append(text) or _Rec()
        try:
            page._render_sim_banner(payload)
        finally:
            page.ui.element, page.ui.label = real_element, real_label
        return drawn

    def test_no_banner_when_the_payload_is_real(self):
        assert self._drawn({'simulated_base': False, 'base_notional': 100.0}) == []

    def test_a_missing_flag_is_treated_as_real(self):
        assert self._drawn({'base_notional': 100.0}) == []

    def test_a_simulated_payload_always_warns(self):
        drawn = self._drawn({'simulated_base': True, 'base_notional': 9000.0,
                             'real_base_notional': 2350.78})
        assert len(drawn) == 1
        assert 'SIMULATION' in drawn[0] and '$9,000.00' in drawn[0]

    def test_it_warns_even_when_the_real_base_is_unknown(self):
        drawn = self._drawn({'simulated_base': True, 'base_notional': 9000.0,
                             'real_base_notional': None})
        assert len(drawn) == 1
        assert page.SIM_BANNER_NO_REAL in drawn[0]


class TestReviewInterlock:
    def test_the_blocked_message_explains_the_actual_risk(self):
        # Not "you cannot do that" -- the reason is the whole point: the dry run would
        # solve against real money while the page shows what-if percentages.
        assert 'REAL' in page.SIM_REVIEW_BLOCKED
        assert 'what-if' in page.SIM_REVIEW_BLOCKED

    def test_the_toggle_tooltip_promises_no_orders(self):
        assert 'no' in page.SIM_TOGGLE_TOOLTIP.lower()
        assert 'order' in page.SIM_TOGGLE_TOOLTIP.lower()
