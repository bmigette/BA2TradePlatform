#!/usr/bin/env python
"""
Database Migration Script: Create Indicator Collections

Creates new indicator collections with ALL available technical indicators.
Creates both standard (conservative) and aggressive parameter versions.
Each collection is created for multiple timeframes (30m, 1h, 4h).

Run from backend directory:
    ./venv/bin/python scripts/create_indicator_collections.py
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from app.models.database import SessionLocal
from app.models.indicator_collection import IndicatorCollection
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Timeframes to create collections for
TIMEFRAMES = ['30m', '1h', '4h']


def add_timeframe_to_indicators(indicators: list, timeframe: str) -> list:
    """Add timeframe field to all indicators in the list."""
    return [{**ind, "timeframe": timeframe} for ind in indicators]


def get_standard_indicators() -> list:
    """Get comprehensive list of indicators with standard parameters."""
    indicators = []

    # SMA variations
    for period in [10, 20, 50, 100, 200]:
        indicators.append({
            "type": "sma",
            "name": f"SMA {period}",
            "period": period,
        })

    # EMA variations
    for period in [12, 26, 50, 100, 200]:
        indicators.append({
            "type": "ema",
            "name": f"EMA {period}",
            "period": period,
        })

    # RSI
    indicators.append({
        "type": "rsi",
        "name": "RSI 14",
        "period": 14,
    })

    # MACD
    indicators.append({
        "type": "macd",
        "name": "MACD (12,26,9)",
        "fast": 12,
        "slow": 26,
        "signal": 9,
    })

    # Bollinger Bands
    indicators.append({
        "type": "bbands",
        "name": "Bollinger Bands (20,2)",
        "period": 20,
        "std_dev": 2.0,
    })

    # ATR
    indicators.append({
        "type": "atr",
        "name": "ATR 14",
        "period": 14,
    })

    # Stochastic
    indicators.append({
        "type": "stochastic",
        "name": "Stochastic (14,3)",
        "k_period": 14,
        "d_period": 3,
    })

    # SAR (Parabolic SAR)
    indicators.append({
        "type": "sar",
        "name": "SAR (0.02,0.2)",
        "af_start": 0.02,
        "af_max": 0.2,
    })

    # ZigZag
    indicators.append({
        "type": "zigzag",
        "name": "ZigZag 5%",
        "deviation_pct": 5.0,
    })

    # Donchian Channel
    indicators.append({
        "type": "donchian",
        "name": "Donchian 20",
        "period": 20,
    })

    # ADX (Average Directional Index)
    indicators.append({
        "type": "adx",
        "name": "ADX 14",
        "period": 14,
    })

    # OBV (On-Balance Volume)
    indicators.append({
        "type": "obv",
        "name": "OBV",
    })

    # Pivot Points
    indicators.append({
        "type": "pivot_points",
        "name": "Pivot Points (Standard)",
        "method": "standard",
    })

    return indicators


def get_aggressive_indicators() -> list:
    """Get comprehensive list of indicators with aggressive parameters."""
    indicators = []

    # SMA - shorter periods
    for period in [5, 10, 20, 50]:
        indicators.append({
            "type": "sma",
            "name": f"SMA {period}",
            "period": period,
        })

    # EMA - shorter periods (Fibonacci-based)
    for period in [8, 13, 21, 34, 55]:
        indicators.append({
            "type": "ema",
            "name": f"EMA {period}",
            "period": period,
        })

    # RSI - shorter period for faster signals
    indicators.append({
        "type": "rsi",
        "name": "RSI 7",
        "period": 7,
    })

    # MACD - faster settings
    indicators.append({
        "type": "macd",
        "name": "MACD (8,17,9)",
        "fast": 8,
        "slow": 17,
        "signal": 9,
    })

    # Bollinger Bands - tighter bands
    indicators.append({
        "type": "bbands",
        "name": "Bollinger Bands (10,1.5)",
        "period": 10,
        "std_dev": 1.5,
    })

    # ATR - shorter period
    indicators.append({
        "type": "atr",
        "name": "ATR 7",
        "period": 7,
    })

    # Stochastic - faster settings
    indicators.append({
        "type": "stochastic",
        "name": "Stochastic (9,3)",
        "k_period": 9,
        "d_period": 3,
    })

    # SAR - more aggressive acceleration
    indicators.append({
        "type": "sar",
        "name": "SAR (0.04,0.4)",
        "af_start": 0.04,
        "af_max": 0.4,
    })

    # ZigZag - tighter deviation
    indicators.append({
        "type": "zigzag",
        "name": "ZigZag 3%",
        "deviation_pct": 3.0,
    })

    # Donchian Channel - shorter lookback
    indicators.append({
        "type": "donchian",
        "name": "Donchian 10",
        "period": 10,
    })

    # ADX - shorter period
    indicators.append({
        "type": "adx",
        "name": "ADX 7",
        "period": 7,
    })

    # OBV
    indicators.append({
        "type": "obv",
        "name": "OBV",
    })

    # Pivot Points
    indicators.append({
        "type": "pivot_points",
        "name": "Pivot Points (Fibonacci)",
        "method": "fibonacci",
    })

    return indicators


def get_momentum_indicators() -> list:
    """Get momentum-focused indicators."""
    return [
        {"type": "rsi", "name": "RSI 14", "period": 14},
        {"type": "rsi", "name": "RSI 7", "period": 7},
        {"type": "macd", "name": "MACD (12,26,9)", "fast": 12, "slow": 26, "signal": 9},
        {"type": "stochastic", "name": "Stochastic (14,3)", "k_period": 14, "d_period": 3},
        {"type": "stochastic", "name": "Stochastic (9,3)", "k_period": 9, "d_period": 3},
        {"type": "adx", "name": "ADX 14", "period": 14},
        {"type": "obv", "name": "OBV"},
    ]


def get_trend_indicators() -> list:
    """Get trend-following indicators."""
    return [
        {"type": "sma", "name": "SMA 20", "period": 20},
        {"type": "sma", "name": "SMA 50", "period": 50},
        {"type": "sma", "name": "SMA 200", "period": 200},
        {"type": "ema", "name": "EMA 12", "period": 12},
        {"type": "ema", "name": "EMA 26", "period": 26},
        {"type": "ema", "name": "EMA 50", "period": 50},
        {"type": "sar", "name": "SAR (0.02,0.2)", "af_start": 0.02, "af_max": 0.2},
        {"type": "zigzag", "name": "ZigZag 5%", "deviation_pct": 5.0},
        {"type": "donchian", "name": "Donchian 20", "period": 20},
        {"type": "adx", "name": "ADX 14", "period": 14},
    ]


def get_volatility_indicators() -> list:
    """Get volatility-focused indicators."""
    return [
        {"type": "atr", "name": "ATR 14", "period": 14},
        {"type": "atr", "name": "ATR 7", "period": 7},
        {"type": "bbands", "name": "Bollinger Bands (20,2)", "period": 20, "std_dev": 2.0},
        {"type": "bbands", "name": "Bollinger Bands (20,2.5)", "period": 20, "std_dev": 2.5},
        {"type": "donchian", "name": "Donchian 20", "period": 20},
        {"type": "donchian", "name": "Donchian 10", "period": 10},
    ]


def get_essential_indicators() -> list:
    """Get minimal essential indicators."""
    return [
        {"type": "sma", "name": "SMA 20", "period": 20},
        {"type": "ema", "name": "EMA 20", "period": 20},
        {"type": "rsi", "name": "RSI 14", "period": 14},
        {"type": "macd", "name": "MACD (12,26,9)", "fast": 12, "slow": 26, "signal": 9},
        {"type": "atr", "name": "ATR 14", "period": 14},
    ]


# Collection definitions: (name_template, description_template, indicator_getter, is_default)
COLLECTION_DEFINITIONS = [
    (
        "All Indicators - Standard - {tf}",
        "Comprehensive collection with all indicators using standard parameters at {tf} timeframe. "
        "Includes moving averages, momentum, volatility, trend, and volume indicators.",
        get_standard_indicators,
        True,
    ),
    (
        "All Indicators - Aggressive - {tf}",
        "Comprehensive collection with aggressive parameters at {tf} timeframe. "
        "Shorter periods, tighter thresholds. Good for scalping strategies.",
        get_aggressive_indicators,
        True,
    ),
    (
        "Momentum Indicators - {tf}",
        "Momentum-focused indicators at {tf}: RSI, MACD, Stochastic, ADX, OBV. "
        "Best for identifying trend strength and overbought/oversold conditions.",
        get_momentum_indicators,
        False,
    ),
    (
        "Trend-Following - {tf}",
        "Trend-following indicators at {tf}: MAs, SAR, ZigZag, Donchian, ADX. "
        "Best for identifying trend direction and reversals.",
        get_trend_indicators,
        False,
    ),
    (
        "Volatility Indicators - {tf}",
        "Volatility-focused indicators at {tf}: ATR, Bollinger Bands, Donchian. "
        "Best for measuring price volatility and breakout opportunities.",
        get_volatility_indicators,
        False,
    ),
    (
        "Essential Indicators - {tf}",
        "Minimal set of essential indicators at {tf}: SMA, EMA, RSI, MACD, ATR. "
        "Good for quick dataset generation and avoiding feature bloat.",
        get_essential_indicators,
        False,
    ),
]


def main():
    """Main migration function."""
    db = SessionLocal()

    try:
        # Check existing collections
        existing_names = [c.name for c in db.query(IndicatorCollection).all()]
        logger.info(f"Found {len(existing_names)} existing indicator collections")

        created_count = 0
        skipped_count = 0

        # Create collections for each timeframe
        for timeframe in TIMEFRAMES:
            tf_upper = timeframe.upper()

            for name_template, desc_template, indicator_getter, is_default in COLLECTION_DEFINITIONS:
                collection_name = name_template.format(tf=tf_upper)

                if collection_name in existing_names:
                    logger.info(f"Skipping '{collection_name}' - already exists")
                    skipped_count += 1
                    continue

                # Get base indicators and add timeframe
                base_indicators = indicator_getter()
                indicators_with_tf = add_timeframe_to_indicators(base_indicators, timeframe)

                collection = IndicatorCollection(
                    name=collection_name,
                    description=desc_template.format(tf=tf_upper),
                    indicators=indicators_with_tf,
                    is_default=is_default,
                )
                db.add(collection)
                logger.info(f"Created: '{collection_name}' with {len(indicators_with_tf)} indicators")
                created_count += 1

        db.commit()

        logger.info(f"\n=== Migration Complete ===")
        logger.info(f"Created: {created_count} indicator collections")
        logger.info(f"Skipped: {skipped_count} (already existed)")

        # List all collections
        all_collections = db.query(IndicatorCollection).order_by(IndicatorCollection.name).all()
        logger.info(f"\nAll indicator collections ({len(all_collections)}):")
        for coll in all_collections:
            default_marker = " [DEFAULT]" if coll.is_default else ""
            logger.info(f"  - {coll.name}: {len(coll.indicators)} indicators{default_marker}")

    except Exception as e:
        db.rollback()
        logger.error(f"Migration failed: {e}", exc_info=True)
        raise
    finally:
        db.close()


if __name__ == '__main__':
    main()
