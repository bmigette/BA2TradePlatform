"""The two historically-inert RM toggles are MEASURED from the run, not hardcoded False.

``forced_expert_settings`` declared ``use_atr_stop`` and ``regime_overlay_enabled`` as
``lambda f: False``. That is true of every run on record -- ``INERT_RM_TOGGLES`` pins both off
in the trial config, and the GA's search space no longer offers them -- but it is an ASSERTION
the exporter never measured, and it becomes a lie the first time a run is scored with the ATR
stop genuinely on (the planned ATR-on baseline after goal2020).

The failure would be silent and in the wrong direction: the live interface defaults
``use_atr_stop`` to TRUE, so a deploy declaring False for a run that used it sizes its live
stops differently from the backtest it came from -- which is exactly what the row's own `why`
warns about.

Both sides now read what the run resolved: the handler from the expert's own settings (where
INERT_RM_TOGGLES is merged last), the exporter from the row's ``model:*`` genes. Absent means
False, which is what 244 of the 692 stored backtests carry and what they executed.
"""
import pytest

from ba2_common.core.deploy_parity import BacktestRunFacts, forced_expert_settings

TOGGLES = ("use_atr_stop", "regime_overlay_enabled")


def _facts(**kw):
    base = dict(enable_short=False, hold_assigned_stock=False, entry_action=None)
    base.update(kw)
    return BacktestRunFacts(**base)


class TestTodayIsUnchanged:
    """Every run on record has both off, so the export must not move at all."""

    def test_the_default_is_off_exactly_as_the_constant_was(self):
        out = forced_expert_settings(_facts())
        assert out["use_atr_stop"] is False
        assert out["regime_overlay_enabled"] is False

    def test_a_run_that_pinned_them_off_still_exports_off(self):
        out = forced_expert_settings(_facts(use_atr_stop=False, regime_overlay_enabled=False))
        assert (out["use_atr_stop"], out["regime_overlay_enabled"]) == (False, False)

    def test_the_other_forced_rows_are_untouched(self):
        """The gates are invariants of a backtest and must stay constants."""
        out = forced_expert_settings(_facts())
        assert out["allow_automated_trade_opening"] is True
        assert out["allow_automated_trade_modification"] is True
        assert out["enable_buy"] is True
        assert out["enable_sell"] is False
        assert forced_expert_settings(_facts(enable_short=True))["enable_sell"] is True


class TestAnAtrOnRunExportsTruthfully:
    """THE POINT: the day INERT_RM_TOGGLES is lifted, the export follows with no edit."""

    @pytest.mark.parametrize("name", TOGGLES)
    def test_on_travels_as_on(self, name):
        out = forced_expert_settings(_facts(**{name: True}))
        assert out[name] is True, f"{name} was scored ON and must deploy ON"

    def test_the_two_are_independent(self):
        out = forced_expert_settings(_facts(use_atr_stop=True, regime_overlay_enabled=False))
        assert out["use_atr_stop"] is True
        assert out["regime_overlay_enabled"] is False

    @pytest.mark.parametrize("name", TOGGLES)
    def test_the_value_is_a_real_bool_not_the_gene_integer(self, name):
        """The GA passes genes as integers; a 1 written through the settings writer as the JSON
        string "1" is the original defect these two were pinned for."""
        out = forced_expert_settings(_facts(**{name: 1}))
        assert out[name] is True and isinstance(out[name], bool)


def test_the_table_no_longer_hardcodes_them():
    """Guards the regression directly: a constant here cannot measure anything."""
    import inspect
    from ba2_common.core import deploy_parity

    src = inspect.getsource(deploy_parity)
    for name in TOGGLES:
        assert f'"{name}": lambda f: False' not in src, (
            f"{name} is hardcoded False again -- it must read the run's own value, or the "
            f"export will declare it off for a run that used it")
