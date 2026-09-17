"""Market-condition feature warmup: provider-fetch orchestration over the central store.

The calculators and the store live in ``ba2_common`` (``market_conditions``,
``market_condition_store``); this package plans, fetches through the existing OHLCV provider
cache, builds and publishes (design section 4.4).
"""
