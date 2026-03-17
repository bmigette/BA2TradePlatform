"""
Structured Condition Schema and Evaluator for PennyMomentumTrader.

Defines condition types for entry/exit signals and provides a deterministic
evaluator that checks conditions against live market data without LLM calls.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pytz

from ba2_trade_platform.logger import logger


# ---------------------------------------------------------------------------
# Condition Types Registry
# ---------------------------------------------------------------------------

CONDITION_TYPES: Dict[str, Dict[str, Any]] = {
    # Price-based
    "price_above": {
        "params": {"value": {"type": "float", "required": True, "description": "Price threshold"}},
        "description": "Current price is above the given value",
    },
    "price_below": {
        "params": {"value": {"type": "float", "required": True, "description": "Price threshold"}},
        "description": "Current price is below the given value",
    },
    "price_above_ema": {
        "params": {
            "period": {"type": "int", "required": True, "description": "EMA period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe (e.g. '1m', '5m', '15m', '1d')"},
        },
        "description": "Current price is above the EMA of the given period and timeframe",
    },
    "price_below_ema": {
        "params": {
            "period": {"type": "int", "required": True, "description": "EMA period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "Current price is below the EMA of the given period and timeframe",
    },
    "price_above_sma": {
        "params": {
            "period": {"type": "int", "required": True, "description": "SMA period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "Current price is above the SMA of the given period and timeframe",
    },
    "price_below_sma": {
        "params": {
            "period": {"type": "int", "required": True, "description": "SMA period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "Current price is below the SMA of the given period and timeframe",
    },
    "price_above_vwap": {
        "params": {
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "Current price is above the VWAP",
    },
    "price_below_vwap": {
        "params": {
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "Current price is below the VWAP",
    },
    "opening_range_breakout": {
        "params": {
            "minutes": {"type": "int", "required": True, "description": "Number of minutes for the opening range"},
        },
        "description": "Current price is above the high of the first N minutes of trading",
    },
    # Volume-based
    "volume_above_avg": {
        "params": {
            "multiplier": {"type": "float", "required": True, "description": "Multiplier above average volume"},
            "window": {"type": "int", "required": True, "description": "Lookback window in bars for average"},
        },
        "description": "Current volume is above the average volume by the given multiplier",
    },
    "volume_spike": {
        "params": {
            "multiplier": {"type": "float", "required": True, "description": "Multiplier above recent average volume"},
            "minutes": {"type": "int", "required": True, "description": "Number of recent minutes to compare"},
        },
        "description": "Recent volume spike compared to average volume in the given minutes window",
    },
    # Indicator-based
    "rsi_above": {
        "params": {
            "threshold": {"type": "float", "required": True, "description": "RSI threshold"},
            "period": {"type": "int", "required": True, "description": "RSI period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "RSI is above the given threshold",
    },
    "rsi_below": {
        "params": {
            "threshold": {"type": "float", "required": True, "description": "RSI threshold"},
            "period": {"type": "int", "required": True, "description": "RSI period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "RSI is below the given threshold",
    },
    "rsi_between": {
        "params": {
            "min": {"type": "float", "required": True, "description": "Minimum RSI value"},
            "max": {"type": "float", "required": True, "description": "Maximum RSI value"},
            "period": {"type": "int", "required": True, "description": "RSI period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "RSI is between the given min and max values",
    },
    "macd_bullish_cross": {
        "params": {
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "MACD line has crossed above the signal line (bullish crossover)",
    },
    "macd_bearish_cross": {
        "params": {
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "MACD line has crossed below the signal line (bearish crossover)",
    },
    "ema_cross_above": {
        "params": {
            "fast_period": {"type": "int", "required": True, "description": "Fast EMA period"},
            "slow_period": {"type": "int", "required": True, "description": "Slow EMA period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "Fast EMA has crossed above the slow EMA",
    },
    "ema_cross_below": {
        "params": {
            "fast_period": {"type": "int", "required": True, "description": "Fast EMA period"},
            "slow_period": {"type": "int", "required": True, "description": "Slow EMA period"},
            "timeframe": {"type": "str", "required": True, "description": "Candle timeframe"},
        },
        "description": "Fast EMA has crossed below the slow EMA",
    },
    # Percentage-based
    "percent_above_entry": {
        "params": {
            "percent": {"type": "float", "required": True, "description": "Percentage above entry price"},
        },
        "description": "Current price is the given percentage above the entry price",
    },
    "percent_below_entry": {
        "params": {
            "percent": {"type": "float", "required": True, "description": "Percentage below entry price"},
        },
        "description": "Current price is the given percentage below the entry price",
    },
    # Time-based
    "time_after": {
        "params": {
            "time": {"type": "str", "required": True, "description": "Time in HH:MM format (market timezone)"},
        },
        "description": "Current time is after the given time in market timezone",
    },
    "time_before": {
        "params": {
            "time": {"type": "str", "required": True, "description": "Time in HH:MM format (market timezone)"},
        },
        "description": "Current time is before the given time in market timezone",
    },
}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def get_condition_types_for_llm() -> str:
    """Return a formatted description of all condition types for LLM prompts."""
    lines = ["Available condition types:\n"]
    for name, info in CONDITION_TYPES.items():
        params_str = ", ".join(
            f"{p}({details['type']})" for p, details in info["params"].items()
        )
        lines.append(f"- {name}({params_str}): {info['description']}")
    lines.append("")
    lines.append("Conditions can be composed with 'all' (AND) and 'any' (OR):")
    lines.append('  {"all": [<condition>, ...]}  -- all must be true')
    lines.append('  {"any": [<condition>, ...]}  -- at least one must be true')
    lines.append("")
    lines.append("Each condition is a dict with 'type' key plus required params.")
    lines.append('Example: {"type": "price_above_ema", "period": 9, "timeframe": "15m"}')
    return "\n".join(lines)


def validate_condition(condition: dict) -> Tuple[bool, str]:
    """
    Validate a single condition dict has a valid type and all required params.

    Returns (is_valid, error_message). error_message is empty string when valid.
    """
    if not isinstance(condition, dict):
        return False, "Condition must be a dict"

    ctype = condition.get("type")
    if not ctype:
        return False, "Condition missing 'type' field"

    if ctype not in CONDITION_TYPES:
        return False, f"Unknown condition type: {ctype}"

    type_def = CONDITION_TYPES[ctype]
    for param_name, param_def in type_def["params"].items():
        if param_def.get("required", False) and param_name not in condition:
            return False, f"Condition '{ctype}' missing required param: {param_name}"

    return True, ""


def _validate_composite(composite: Any, path: str, errors: List[str]) -> None:
    """Internal helper for recursive composite condition validation."""
    if not isinstance(composite, dict):
        errors.append(f"{path}: composite must be a dict, got {type(composite).__name__}")
        return

    if "all" in composite:
        items = composite["all"]
        if not isinstance(items, list):
            errors.append(f"{path}.all: must be a list")
            return
        for i, item in enumerate(items):
            if "all" in item or "any" in item:
                _validate_composite(item, f"{path}.all[{i}]", errors)
            else:
                valid, msg = validate_condition(item)
                if not valid:
                    errors.append(f"{path}.all[{i}]: {msg}")
    elif "any" in composite:
        items = composite["any"]
        if not isinstance(items, list):
            errors.append(f"{path}.any: must be a list")
            return
        for i, item in enumerate(items):
            if "all" in item or "any" in item:
                _validate_composite(item, f"{path}.any[{i}]", errors)
            else:
                valid, msg = validate_condition(item)
                if not valid:
                    errors.append(f"{path}.any[{i}]: {msg}")
    else:
        # Single condition at composite level
        valid, msg = validate_condition(composite)
        if not valid:
            errors.append(f"{path}: {msg}")


def validate_condition_set(conditions: dict) -> Tuple[bool, List[str]]:
    """
    Validate a full condition set with entry/stop_loss/take_profit sections.

    Returns (is_valid, list_of_errors).
    """
    errors: List[str] = []

    if not isinstance(conditions, dict):
        return False, ["Condition set must be a dict"]

    # Validate entry
    if "entry" in conditions:
        _validate_composite(conditions["entry"], "entry", errors)

    # Validate stop_loss
    if "stop_loss" in conditions:
        _validate_composite(conditions["stop_loss"], "stop_loss", errors)

    # Validate take_profit (list of {condition, exit_pct})
    if "take_profit" in conditions:
        tp = conditions["take_profit"]
        if not isinstance(tp, list):
            errors.append("take_profit: must be a list")
        else:
            for i, item in enumerate(tp):
                if not isinstance(item, dict):
                    errors.append(f"take_profit[{i}]: must be a dict")
                    continue
                if "condition" not in item:
                    errors.append(f"take_profit[{i}]: missing 'condition' key")
                else:
                    cond = item["condition"]
                    if "all" in cond or "any" in cond:
                        _validate_composite(cond, f"take_profit[{i}].condition", errors)
                    else:
                        valid, msg = validate_condition(cond)
                        if not valid:
                            errors.append(f"take_profit[{i}].condition: {msg}")
                if "exit_pct" not in item:
                    errors.append(f"take_profit[{i}]: missing 'exit_pct' key")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Condition Evaluator
# ---------------------------------------------------------------------------

# Mapping from timeframe string to approximate lookback days for data fetching
_TIMEFRAME_LOOKBACK: Dict[str, int] = {
    "1m": 5,
    "5m": 10,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 120,
    "1d": 365,
}


class ConditionEvaluator:
    """
    Evaluates structured conditions against live market data.

    Uses an OHLCV provider for data fetching and caches indicator
    computations within each evaluation cycle.
    """

    def __init__(self, ohlcv_provider, market_timezone: str = "US/Eastern"):
        """
        Args:
            ohlcv_provider: Object with get_ohlcv_data(symbol, interval, lookback_days)
                returning a pandas DataFrame with columns: open, high, low, close, volume.
            market_timezone: Timezone string for time-based conditions.
        """
        self.ohlcv_provider = ohlcv_provider
        self.market_timezone = market_timezone
        self._indicator_cache: Dict[str, Any] = {}

    def clear_cache(self) -> None:
        """Clear the indicator cache between evaluation cycles."""
        self._indicator_cache.clear()

    # -------------------------------------------------------------------
    # Public evaluation methods
    # -------------------------------------------------------------------

    def evaluate(self, conditions: dict, symbol: str, entry_price: Optional[float] = None) -> bool:
        """
        Evaluate a composite condition (with 'all'/'any' logic).

        Args:
            conditions: Composite condition dict with 'all' or 'any' key,
                or a single condition dict with 'type' key.
            symbol: Ticker symbol.
            entry_price: Entry price for percent-based conditions.

        Returns:
            True if conditions are met.
        """
        if "all" in conditions:
            return all(
                self.evaluate(c, symbol, entry_price) for c in conditions["all"]
            )
        if "any" in conditions:
            return any(
                self.evaluate(c, symbol, entry_price) for c in conditions["any"]
            )
        # Single condition
        return self.evaluate_single(conditions, symbol, entry_price)

    def evaluate_single(self, condition: dict, symbol: str, entry_price: Optional[float] = None) -> bool:
        """
        Evaluate a single condition dict.

        Returns False (and logs warning) if the condition type is unknown
        or data is unavailable.
        """
        ctype = condition.get("type")
        handler = self._handlers.get(ctype)
        if handler is None:
            logger.warning(f"Unknown condition type: {ctype}")
            return False
        try:
            return bool(handler(self, condition, symbol, entry_price))
        except Exception as e:
            logger.warning(f"Error evaluating condition {ctype} for {symbol}: {e}")
            return False

    def get_condition_status(
        self, conditions: dict, symbol: str, entry_price: Optional[float] = None
    ) -> Dict[str, bool]:
        """
        Return per-condition evaluation status for UI display.

        For composite conditions, returns a flat dict keyed by condition description.
        """
        result: Dict[str, bool] = {}
        self._collect_status(conditions, symbol, entry_price, result)
        return result

    def _collect_status(
        self, conditions: dict, symbol: str, entry_price: Optional[float], result: Dict[str, bool]
    ) -> None:
        if "all" in conditions:
            for c in conditions["all"]:
                self._collect_status(c, symbol, entry_price, result)
        elif "any" in conditions:
            for c in conditions["any"]:
                self._collect_status(c, symbol, entry_price, result)
        else:
            # Single condition
            ctype = conditions.get("type", "unknown")
            params = {k: v for k, v in conditions.items() if k != "type"}
            key = f"{ctype}({params})" if params else ctype
            result[key] = self.evaluate_single(conditions, symbol, entry_price)

    def get_condition_details(
        self, conditions: dict, symbol: str, entry_price: Optional[float] = None
    ) -> Dict[str, str]:
        """
        Return per-condition evaluation detail strings for debug logging.

        Must be called after get_condition_status() so the indicator cache
        is already populated. Returns a flat dict: condition_key -> detail_str.
        E.g. "volume_above_avg(...)" -> "unmet (vol=123k < avg=456k × 1.5 = 684k)"
        """
        result: Dict[str, str] = {}
        self._collect_details(conditions, symbol, entry_price, result)
        return result

    def _collect_details(
        self,
        conditions: dict,
        symbol: str,
        entry_price: Optional[float],
        result: Dict[str, str],
    ) -> None:
        if "all" in conditions:
            for c in conditions["all"]:
                self._collect_details(c, symbol, entry_price, result)
        elif "any" in conditions:
            for c in conditions["any"]:
                self._collect_details(c, symbol, entry_price, result)
        else:
            ctype = conditions.get("type", "unknown")
            params = {k: v for k, v in conditions.items() if k != "type"}
            key = f"{ctype}({params})" if params else ctype
            met = self.evaluate_single(conditions, symbol, entry_price)
            detail = self._explain_single(conditions, symbol, entry_price, met)
            result[key] = detail

    def _fmt(self, v: Optional[float]) -> str:
        """Format a float for display (4 sig figs, no trailing zeros)."""
        if v is None:
            return "N/A"
        if abs(v) >= 1_000_000:
            return f"{v/1_000_000:.2f}M"
        if abs(v) >= 1_000:
            return f"{v/1_000:.1f}k"
        return f"{v:.4g}"

    def _explain_single(
        self,
        cond: dict,
        symbol: str,
        entry_price: Optional[float],
        met: bool,
    ) -> str:
        """Return a human-readable detail string for a single condition."""
        label = "met" if met else "unmet"
        ctype = cond.get("type", "")
        try:
            if ctype in ("price_above", "price_below"):
                price = self._get_current_price(symbol)
                op = ">" if ctype == "price_above" else "<"
                return f"{label} (price={self._fmt(price)} {op} {self._fmt(cond['value'])})"

            if ctype in ("price_above_ema", "price_below_ema"):
                price = self._get_current_price(symbol)
                ema = self._get_ema(symbol, cond["period"], cond["timeframe"])
                op = ">" if ctype == "price_above_ema" else "<"
                return f"{label} (price={self._fmt(price)} {op} ema{cond['period']}={self._fmt(ema)} [{cond['timeframe']}])"

            if ctype in ("price_above_sma", "price_below_sma"):
                price = self._get_current_price(symbol)
                sma = self._get_sma(symbol, cond["period"], cond["timeframe"])
                op = ">" if ctype == "price_above_sma" else "<"
                return f"{label} (price={self._fmt(price)} {op} sma{cond['period']}={self._fmt(sma)} [{cond['timeframe']}])"

            if ctype in ("price_above_vwap", "price_below_vwap"):
                price = self._get_current_price(symbol)
                vwap = self._get_vwap(symbol, cond["timeframe"])
                op = ">" if ctype == "price_above_vwap" else "<"
                return f"{label} (price={self._fmt(price)} {op} vwap={self._fmt(vwap)} [{cond['timeframe']}])"

            if ctype == "volume_above_avg":
                lookback = _TIMEFRAME_LOOKBACK.get("1d", 365)
                df = self._get_ohlcv(symbol, "1d", lookback)
                if df is not None and len(df) >= cond["window"]:
                    avg = df["volume"].iloc[-cond["window"]:].mean()
                    cur = df["volume"].iloc[-1]
                    required = avg * cond["multiplier"]
                    return (
                        f"{label} (vol={self._fmt(cur)}, "
                        f"avg={self._fmt(avg)} × {cond['multiplier']} = {self._fmt(required)})"
                    )

            if ctype in ("rsi_above", "rsi_below", "rsi_between"):
                rsi = self._get_rsi(symbol, cond["period"], cond["timeframe"])
                if ctype == "rsi_between":
                    return f"{label} (rsi={self._fmt(rsi)} in [{cond['min']}, {cond['max']}] [{cond['timeframe']}])"
                op = ">" if ctype == "rsi_above" else "<"
                return f"{label} (rsi={self._fmt(rsi)} {op} {cond['threshold']} [{cond['timeframe']}])"

            if ctype in ("percent_above_entry", "percent_below_entry"):
                price = self._get_current_price(symbol)
                if entry_price:
                    pct = cond["percent"]
                    target = entry_price * (1.0 + pct / 100.0 if "above" in ctype else 1.0 - pct / 100.0)
                    return f"{label} (price={self._fmt(price)}, target={self._fmt(target)} [{pct}% from entry={self._fmt(entry_price)}])"

            if ctype in ("time_after", "time_before"):
                import pytz as _pytz
                tz = _pytz.timezone(self.market_timezone)
                now = datetime.now(tz)
                return f"{label} (now={now.strftime('%H:%M')}, threshold={cond['time']})"

        except Exception:
            pass
        return label

    # -------------------------------------------------------------------
    # Data fetching helpers (cached per evaluation cycle)
    # -------------------------------------------------------------------

    def _get_current_price(self, symbol: str) -> Optional[float]:
        cache_key = f"price:{symbol}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        # Try 1m data first, fallback to 1d
        for tf in ("1m", "1d"):
            try:
                df = self._get_ohlcv(symbol, tf, _TIMEFRAME_LOOKBACK.get(tf, 5))
                if df is not None and not df.empty:
                    price = float(df["close"].iloc[-1])
                    self._indicator_cache[cache_key] = price
                    return price
            except Exception:
                continue
        return None

    def _get_ohlcv(self, symbol: str, timeframe: str, lookback_days: int) -> Optional[pd.DataFrame]:
        cache_key = f"ohlcv:{symbol}:{timeframe}:{lookback_days}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        try:
            df = self.ohlcv_provider.get_ohlcv_data(
                symbol, interval=timeframe, lookback_days=lookback_days
            )
            if df is not None and not df.empty:
                df.columns = [c.lower() for c in df.columns]
            self._indicator_cache[cache_key] = df
            return df
        except Exception as e:
            logger.warning(f"Failed to get OHLCV for {symbol} ({timeframe}): {e}")
            return None

    def _get_ema(self, symbol: str, period: int, timeframe: str) -> Optional[float]:
        cache_key = f"ema:{symbol}:{period}:{timeframe}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        lookback = _TIMEFRAME_LOOKBACK.get(timeframe, 30)
        df = self._get_ohlcv(symbol, timeframe, lookback)
        if df is None or len(df) < period:
            return None

        ema = df["close"].ewm(span=period, adjust=False).mean().iloc[-1]
        self._indicator_cache[cache_key] = float(ema)
        return float(ema)

    def _get_sma(self, symbol: str, period: int, timeframe: str) -> Optional[float]:
        cache_key = f"sma:{symbol}:{period}:{timeframe}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        lookback = _TIMEFRAME_LOOKBACK.get(timeframe, 30)
        df = self._get_ohlcv(symbol, timeframe, lookback)
        if df is None or len(df) < period:
            return None

        sma = df["close"].rolling(window=period).mean().iloc[-1]
        self._indicator_cache[cache_key] = float(sma)
        return float(sma)

    def _get_rsi(self, symbol: str, period: int, timeframe: str) -> Optional[float]:
        cache_key = f"rsi:{symbol}:{period}:{timeframe}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        lookback = _TIMEFRAME_LOOKBACK.get(timeframe, 30)
        df = self._get_ohlcv(symbol, timeframe, lookback)
        if df is None or len(df) < period + 1:
            return None

        # Wilder's RSI
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

        value = float(rsi.iloc[-1])
        self._indicator_cache[cache_key] = value
        return value

    def _check_macd_cross(self, symbol: str, timeframe: str, bullish: bool) -> bool:
        cache_key = f"macd_cross:{symbol}:{timeframe}:{bullish}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        lookback = _TIMEFRAME_LOOKBACK.get(timeframe, 30)
        df = self._get_ohlcv(symbol, timeframe, lookback)
        if df is None or len(df) < 35:  # Need enough data for MACD(12,26,9)
            self._indicator_cache[cache_key] = False
            return False

        close = df["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()

        # Check crossover on the last two bars
        if len(macd_line) < 2:
            self._indicator_cache[cache_key] = False
            return False

        prev_diff = macd_line.iloc[-2] - signal_line.iloc[-2]
        curr_diff = macd_line.iloc[-1] - signal_line.iloc[-1]

        if bullish:
            result = prev_diff <= 0 and curr_diff > 0
        else:
            result = prev_diff >= 0 and curr_diff < 0

        self._indicator_cache[cache_key] = result
        return result

    def _check_ema_cross(
        self, symbol: str, fast: int, slow: int, timeframe: str, above: bool
    ) -> bool:
        cache_key = f"ema_cross:{symbol}:{fast}:{slow}:{timeframe}:{above}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        lookback = _TIMEFRAME_LOOKBACK.get(timeframe, 30)
        df = self._get_ohlcv(symbol, timeframe, lookback)
        if df is None or len(df) < slow + 1:
            self._indicator_cache[cache_key] = False
            return False

        fast_ema = df["close"].ewm(span=fast, adjust=False).mean()
        slow_ema = df["close"].ewm(span=slow, adjust=False).mean()

        if len(fast_ema) < 2:
            self._indicator_cache[cache_key] = False
            return False

        prev_diff = fast_ema.iloc[-2] - slow_ema.iloc[-2]
        curr_diff = fast_ema.iloc[-1] - slow_ema.iloc[-1]

        if above:
            result = prev_diff <= 0 and curr_diff > 0
        else:
            result = prev_diff >= 0 and curr_diff < 0

        self._indicator_cache[cache_key] = result
        return result

    def _filter_today_session(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter DataFrame to today's regular market session (9:30-16:00)."""
        if df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return df

        tz = pytz.timezone(self.market_timezone)
        now = datetime.now(tz)
        today = now.date()

        # Convert index to market timezone
        if df.index.tz is not None:
            idx = df.index.tz_convert(tz)
        else:
            try:
                idx = df.index.tz_localize(tz)
            except Exception:
                return df

        market_open = tz.localize(datetime(today.year, today.month, today.day, 9, 30))
        market_close = tz.localize(datetime(today.year, today.month, today.day, 16, 0))

        mask = (idx >= market_open) & (idx <= market_close)
        filtered = df.loc[mask]
        return filtered if not filtered.empty else df

    def _get_vwap(self, symbol: str, timeframe: str) -> Optional[float]:
        cache_key = f"vwap:{symbol}:{timeframe}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        lookback = _TIMEFRAME_LOOKBACK.get(timeframe, 5)
        df = self._get_ohlcv(symbol, timeframe, lookback)
        if df is None or df.empty:
            return None

        # Filter to today's session for proper intraday VWAP
        df = self._filter_today_session(df)
        if df.empty:
            return None

        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_tp_vol = (typical_price * df["volume"]).cumsum()
        cum_vol = df["volume"].cumsum()

        # Avoid division by zero
        if cum_vol.iloc[-1] == 0:
            return None

        vwap = cum_tp_vol.iloc[-1] / cum_vol.iloc[-1]
        self._indicator_cache[cache_key] = float(vwap)
        return float(vwap)

    def _check_opening_range_breakout(self, symbol: str, minutes: int) -> bool:
        cache_key = f"orb:{symbol}:{minutes}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        df = self._get_ohlcv(symbol, "1m", 1)
        if df is None or len(df) < minutes:
            self._indicator_cache[cache_key] = False
            return False

        # Filter to today's regular session to get the actual opening range
        df = self._filter_today_session(df)
        if len(df) < minutes:
            self._indicator_cache[cache_key] = False
            return False

        # Take first N minutes of today's session
        opening_high = df["high"].iloc[:minutes].max()
        current_price = self._get_current_price(symbol)

        if current_price is None:
            self._indicator_cache[cache_key] = False
            return False

        result = current_price > opening_high
        self._indicator_cache[cache_key] = result
        return result

    def _check_volume_spike(self, symbol: str, multiplier: float, minutes: int) -> bool:
        cache_key = f"vol_spike:{symbol}:{multiplier}:{minutes}"
        if cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key]

        df = self._get_ohlcv(symbol, "1m", 5)
        if df is None or len(df) < minutes + 20:
            self._indicator_cache[cache_key] = False
            return False

        recent_vol = df["volume"].iloc[-minutes:].mean()
        avg_vol = df["volume"].iloc[:-minutes].mean()

        if avg_vol == 0:
            self._indicator_cache[cache_key] = False
            return False

        result = recent_vol >= avg_vol * multiplier
        self._indicator_cache[cache_key] = result
        return result

    # -------------------------------------------------------------------
    # Condition handler methods
    # -------------------------------------------------------------------

    def _handle_price_above(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        if price is None:
            return False
        return price > cond["value"]

    def _handle_price_below(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        if price is None:
            return False
        return price < cond["value"]

    def _handle_price_above_ema(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        ema = self._get_ema(symbol, cond["period"], cond["timeframe"])
        if price is None or ema is None:
            return False
        return price > ema

    def _handle_price_below_ema(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        ema = self._get_ema(symbol, cond["period"], cond["timeframe"])
        if price is None or ema is None:
            return False
        return price < ema

    def _handle_price_above_sma(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        sma = self._get_sma(symbol, cond["period"], cond["timeframe"])
        if price is None or sma is None:
            return False
        return price > sma

    def _handle_price_below_sma(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        sma = self._get_sma(symbol, cond["period"], cond["timeframe"])
        if price is None or sma is None:
            return False
        return price < sma

    def _handle_price_above_vwap(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        vwap = self._get_vwap(symbol, cond["timeframe"])
        if price is None or vwap is None:
            return False
        return price > vwap

    def _handle_price_below_vwap(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        price = self._get_current_price(symbol)
        vwap = self._get_vwap(symbol, cond["timeframe"])
        if price is None or vwap is None:
            return False
        return price < vwap

    def _handle_opening_range_breakout(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        return self._check_opening_range_breakout(symbol, cond["minutes"])

    def _handle_volume_above_avg(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        lookback = _TIMEFRAME_LOOKBACK.get("1d", 365)
        df = self._get_ohlcv(symbol, "1d", lookback)
        if df is None or len(df) < cond["window"]:
            return False
        avg_vol = df["volume"].iloc[-cond["window"]:].mean()
        current_vol = df["volume"].iloc[-1]
        if avg_vol == 0:
            return False
        return current_vol >= avg_vol * cond["multiplier"]

    def _handle_volume_spike(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        return self._check_volume_spike(symbol, cond["multiplier"], cond["minutes"])

    def _handle_rsi_above(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        rsi = self._get_rsi(symbol, cond["period"], cond["timeframe"])
        if rsi is None:
            return False
        return rsi > cond["threshold"]

    def _handle_rsi_below(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        rsi = self._get_rsi(symbol, cond["period"], cond["timeframe"])
        if rsi is None:
            return False
        return rsi < cond["threshold"]

    def _handle_rsi_between(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        rsi = self._get_rsi(symbol, cond["period"], cond["timeframe"])
        if rsi is None:
            return False
        return cond["min"] <= rsi <= cond["max"]

    def _handle_macd_bullish_cross(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        return self._check_macd_cross(symbol, cond["timeframe"], bullish=True)

    def _handle_macd_bearish_cross(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        return self._check_macd_cross(symbol, cond["timeframe"], bullish=False)

    def _handle_ema_cross_above(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        return self._check_ema_cross(
            symbol, cond["fast_period"], cond["slow_period"], cond["timeframe"], above=True
        )

    def _handle_ema_cross_below(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        return self._check_ema_cross(
            symbol, cond["fast_period"], cond["slow_period"], cond["timeframe"], above=False
        )

    def _handle_percent_above_entry(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        if entry_price is None:
            return False
        price = self._get_current_price(symbol)
        if price is None:
            return False
        target = entry_price * (1.0 + cond["percent"] / 100.0)
        return price >= target

    def _handle_percent_below_entry(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        if entry_price is None:
            return False
        price = self._get_current_price(symbol)
        if price is None:
            return False
        target = entry_price * (1.0 - cond["percent"] / 100.0)
        return price <= target

    def _handle_time_after(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        tz = pytz.timezone(self.market_timezone)
        now = datetime.now(tz)
        parts = cond["time"].split(":")
        target_hour, target_minute = int(parts[0]), int(parts[1])
        return (now.hour, now.minute) >= (target_hour, target_minute)

    def _handle_time_before(self, cond: dict, symbol: str, entry_price: Optional[float]) -> bool:
        tz = pytz.timezone(self.market_timezone)
        now = datetime.now(tz)
        parts = cond["time"].split(":")
        target_hour, target_minute = int(parts[0]), int(parts[1])
        return (now.hour, now.minute) < (target_hour, target_minute)

    # Handler dispatch table
    _handlers: Dict[str, Any] = {
        "price_above": _handle_price_above,
        "price_below": _handle_price_below,
        "price_above_ema": _handle_price_above_ema,
        "price_below_ema": _handle_price_below_ema,
        "price_above_sma": _handle_price_above_sma,
        "price_below_sma": _handle_price_below_sma,
        "price_above_vwap": _handle_price_above_vwap,
        "price_below_vwap": _handle_price_below_vwap,
        "opening_range_breakout": _handle_opening_range_breakout,
        "volume_above_avg": _handle_volume_above_avg,
        "volume_spike": _handle_volume_spike,
        "rsi_above": _handle_rsi_above,
        "rsi_below": _handle_rsi_below,
        "rsi_between": _handle_rsi_between,
        "macd_bullish_cross": _handle_macd_bullish_cross,
        "macd_bearish_cross": _handle_macd_bearish_cross,
        "ema_cross_above": _handle_ema_cross_above,
        "ema_cross_below": _handle_ema_cross_below,
        "percent_above_entry": _handle_percent_above_entry,
        "percent_below_entry": _handle_percent_below_entry,
        "time_after": _handle_time_after,
        "time_before": _handle_time_before,
    }
