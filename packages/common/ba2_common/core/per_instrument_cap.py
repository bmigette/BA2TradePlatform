"""THE per-instrument cap: one number for the risk manager that sizes an order and the account that
validates it.

``max_virtual_equity_per_instrument_percent`` bounds how much of an expert's virtual equity one
instrument may hold. The regime overlay (``regime_overlay.regime_scale(..., "regime_risk_scale")``)
shrinks or grows that cap while the benchmark is stressed (``regime_overlay_enabled``) and/or in a
SPY down-trend (``downtrend_overlay_enabled`` -> ``downtrend_risk_scale``; the product of both) --
its documented intent, applied to the CAP so it bites in both sizing modes (see
``TradeRiskManagement._calculate_order_quantities``).

WHY ONE FUNCTION. Until 2026-11-17 the risk manager scaled the cap and the account's pre-submit
validation (``AccountInterface._validate_position_size_limits``) did not. With a scale above 1 the
order the RM had just funded was refused at submit -- 44-46 times in one 4-year backtest -- and what
happened next differed between live, the in-memory backtest store and the SQL one. Both now ask
:func:`regime_scaled_cap`, so they cannot enforce different caps.

THE STRESSED FLAG is the one the host PUBLISHED for the bar (``regime_overlay.get_stressed()``: the
backtest engine at the top of each bar, live ``TradeManager`` on each refresh, cached per calendar
day). Sizing and validation of one entry happen inside the same bar / pass, after that publish, so
they read the same value.
"""
from __future__ import annotations

from typing import Any, NamedTuple

#: The setting the cap is read from (percent of the expert's virtual equity).
CAP_SETTING = "max_virtual_equity_per_instrument_percent"


class PerInstrumentCap(NamedTuple):
    """``value`` is the cap to enforce; ``base`` and ``regime_scale`` say how it was reached."""

    base: float           # virtual equity x the cap percent (the unscaled ceiling)
    regime_scale: float   # regime_risk_scale active for this bar (1.0 = no change)
    value: float          # base x regime_scale: THE number both sizing and validation enforce


def regime_scaled_cap(expert: Any, base: float) -> PerInstrumentCap:
    """``base`` (virtual equity x cap percent) scaled by this bar's ``regime_risk_scale``.

    1.0 -- the overlay off, an unstressed or unclassified bar -- returns ``base`` untouched, so the
    arithmetic is bit-for-bit what it was before the overlay existed.
    """
    from ba2_common.core.regime_overlay import get_stressed, regime_scale

    scale = regime_scale(expert, "regime_risk_scale", get_stressed())
    return PerInstrumentCap(base=base, regime_scale=scale,
                            value=base * scale if scale != 1.0 else base)


def per_instrument_cap(expert: Any, virtual_equity: float, cap_percent: float) -> PerInstrumentCap:
    """The cap for ``expert`` at ``virtual_equity`` with ``cap_percent`` (the expert's
    ``max_virtual_equity_per_instrument_percent``), regime scale included.

    The base is ``virtual_equity * (cap_percent / 100.0)`` -- the same expression the risk manager
    computes its ceiling with (``balances.virtual * ratio``), so the two agree to the last bit.
    """
    return regime_scaled_cap(expert, virtual_equity * (cap_percent / 100.0))
