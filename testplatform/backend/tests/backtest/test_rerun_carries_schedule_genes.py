"""A re-run must honour the genome's OPTIMIZED ENTRY WEEKDAYS, not the run-level cadence.

THE DEFECT (found 2026-09-07). The GA searches which weekday(s) an expert scans for new
positions -- one ``schedule:<day>`` gene per day (``_SCHEDULE_DAY_OPT`` in the launcher) --
and ``_build_daily_trial_config`` lets the decoded ``schedule_days`` REPLACE the run-level
``run_schedule_override`` for that individual. So a winning genome may say "enter on Tue,
Thu, Fri" while the grid was launched Monday-only.

``rerun_handler._gene_params`` filters stored strategy_params down to the namespaces
``decode_params`` accepts, and its whitelist listed model/screener/cond/exit/entry --
never ``schedule``. Every schedule gene was dropped on the way in, ``schedule_days`` came
back None, and the re-run silently fell back to the run-level Monday-only cadence. Nothing
raised; the run completed and reported economics for a cadence the GA never selected.

Blast radius was every consumer of the shared rebuild path: the ``/rerun`` button, the
robustness schedule-variant launcher, and tools/run_ok1000.py (which is how 21 static-balance
runs came to be measured on the wrong days). The CLI's own ``_persist_top_backtests`` was
never affected -- it calls ``decode_params`` on the raw genome, no filter in between.

The filter is the only thing between a stored genome and decode_params, so these tests pin
BOTH that schedule genes now survive it AND that the display keys it exists to remove still
get removed.
"""
import pytest

from app.services.backtest import rerun_handler as RH


DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _stored_params(**overrides):
    """A stored strategy_params as the optimizer writes it: raw genes + camelCase display keys."""
    params = {
        "model:risk_per_trade_pct": 2.5,
        "screener:screener_max_stocks": 20,
        "cond:u4:enabled": 1,
        "entry:buy-1:enabled": 1,
        "exit:s1_exit_signal:enabled": 1,
        "tp": 12.0,
        "sl": 6.0,
        # The optimized cadence: NOT Monday. This is the shape that was being lost.
        "schedule:monday": 0,
        "schedule:tuesday": 1,
        "schedule:wednesday": 0,
        "schedule:thursday": 1,
        "schedule:friday": 1,
        "schedule:saturday": 0,
        "schedule:sunday": 1,
        # Display/bookkeeping keys decode_params raises on -- the reason the filter exists.
        "buyEntryConditions": {"any": []},
        "exitConditions": [],
        "expertFixedSettings": {"sizing_mode": "risk_atr"},
        "entryRules": [],
        "runScheduleOverride": None,
        "_atr_swap_migration": True,
    }
    params.update(overrides)
    return params


class TestScheduleGenesSurviveTheFilter:
    def test_every_schedule_day_gene_is_kept(self):
        """THE DEFECT: all seven were dropped, so decode_params never saw a cadence."""
        kept = RH._gene_params(_stored_params())
        for day in DAYS:
            assert f"schedule:{day}" in kept, f"schedule:{day} must reach decode_params"

    def test_the_gene_VALUES_are_carried_through_unchanged(self):
        """Dropping is one failure; coercing is another. Tue/Thu/Fri/Sun ON, Mon/Wed/Sat OFF."""
        kept = RH._gene_params(_stored_params())
        assert kept["schedule:monday"] == 0
        assert kept["schedule:tuesday"] == 1
        assert kept["schedule:thursday"] == 1
        assert kept["schedule:friday"] == 1
        assert kept["schedule:sunday"] == 1

    def test_schedule_is_declared_in_the_prefix_whitelist(self):
        """The filter is prefix-driven; pin the list itself so a refactor cannot quietly
        shorten it back."""
        assert "schedule" in RH._GENE_PREFIXES

    def test_a_monday_only_genome_is_also_carried(self):
        """The inverse: a genome that genuinely chose Monday must be carried as Monday, not
        left to coincide with the run-level default."""
        kept = RH._gene_params(_stored_params(**{
            "schedule:monday": 1, "schedule:tuesday": 0, "schedule:thursday": 0,
            "schedule:friday": 0, "schedule:sunday": 0,
        }))
        assert kept["schedule:monday"] == 1
        assert kept["schedule:tuesday"] == 0


class TestTheFilterStillDoesItsOriginalJob:
    """It exists because decode_params RAISES on unknown keys -- widening it must not
    let the display keys back through."""

    @pytest.mark.parametrize("display_key", [
        "buyEntryConditions", "exitConditions", "expertFixedSettings", "entryRules",
        "runScheduleOverride", "_atr_swap_migration",
    ])
    def test_display_keys_are_still_dropped(self, display_key):
        assert display_key not in RH._gene_params(_stored_params())

    def test_the_other_gene_namespaces_are_untouched(self):
        kept = RH._gene_params(_stored_params())
        for key in ("model:risk_per_trade_pct", "screener:screener_max_stocks",
                    "cond:u4:enabled", "entry:buy-1:enabled", "exit:s1_exit_signal:enabled"):
            assert key in kept

    def test_the_bare_tp_and_sl_genes_still_pass(self):
        """They carry no namespace prefix and are whitelisted by name."""
        kept = RH._gene_params(_stored_params())
        assert kept["tp"] == 12.0 and kept["sl"] == 6.0

    def test_a_key_whose_prefix_merely_STARTS_with_a_gene_name_is_dropped(self):
        """The match is on the ``:``-split head, not a startswith -- 'scheduled_at' is not
        a schedule gene."""
        kept = RH._gene_params(_stored_params(scheduled_at="2026-09-07", modelVersion=3))
        assert "scheduled_at" not in kept
        assert "modelVersion" not in kept

    def test_empty_and_none_params_are_handled(self):
        assert RH._gene_params({}) == {}
        assert RH._gene_params(None) == {}


class TestDecodedCadenceReachesTheTrialConfig:
    """End of the chain: filtered genes -> decode_params -> schedule_days. Without the fix
    this comes back None and the trial config keeps the run-level Monday-only override."""

    def test_decode_params_turns_the_kept_genes_into_schedule_days(self):
        from app.services.strategy_param_space import decode_params

        kept = RH._gene_params(_stored_params())
        decoded = decode_params(None, {k: v for k, v in kept.items()
                                       if k.startswith("schedule:")})
        days = decoded["schedule_days"]
        assert days is not None, "the whole point: a cadence must be decoded"
        assert days["tuesday"] and days["thursday"] and days["friday"]
        assert not days["monday"] and not days["wednesday"]
