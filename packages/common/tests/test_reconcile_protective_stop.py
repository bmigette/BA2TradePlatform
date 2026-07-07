"""Shared tighter-wins protective-stop reconciliation (P1b of the live<->backtest unification):
the single definition of how a strategy's ruleset entry-bracket SL and the RM safeguard SL combine
at submit. Used by the backtest submit tails; keeps the tighter-wins policy defined once."""
from ba2_common.core.position_sizing import reconcile_protective_stop


def test_both_present_long_takes_higher():
    # long: tighter stop = the HIGHER price (closer to entry) -> smaller loss
    assert reconcile_protective_stop(ruleset_sl=95.0, safeguard_sl=90.0, is_long=True) == 95.0


def test_both_present_short_takes_lower():
    # short: tighter stop = the LOWER price (closer to entry)
    assert reconcile_protective_stop(ruleset_sl=105.0, safeguard_sl=110.0, is_long=False) == 105.0


def test_only_safeguard_returns_safeguard():
    assert reconcile_protective_stop(ruleset_sl=None, safeguard_sl=92.0, is_long=True) == 92.0


def test_only_ruleset_returns_none():
    # ruleset SL already attached as its own leg -> nothing tighter to add
    assert reconcile_protective_stop(ruleset_sl=95.0, safeguard_sl=None, is_long=True) is None


def test_neither_returns_none():
    assert reconcile_protective_stop(ruleset_sl=None, safeguard_sl=None, is_long=True) is None
    assert reconcile_protective_stop(ruleset_sl=0.0, safeguard_sl=0.0, is_long=False) is None
