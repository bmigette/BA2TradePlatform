"""Explicit option-spread settings for option-account test fixtures (plan Part F).

An OPTIONS ``BacktestAccount`` refuses a config that does not state its spread model
(``BacktestAccount._resolve_spread_model``: no silent zero spread). Fixtures written before
Part F relied on the absent knobs meaning "0.0 = no spread"; they now say so explicitly with
the legacy model at zero, which prices every fill exactly as before. Spliced FIRST into a
fixture's settings dict, so any ``option_spread_pct``/``_min_tick`` the fixture itself sets
still wins.
"""
LEGACY_ZERO_SPREAD = {
    "option_spread_model": "legacy-pct",
    "option_spread_pct": 0.0,
    "option_spread_min_tick": 0.0,
}
