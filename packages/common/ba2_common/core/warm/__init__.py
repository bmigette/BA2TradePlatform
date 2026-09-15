"""Warm MECHANISM: a budgeted, low-priority work queue (spec step 4, section 6).

This package knows how to hold a daily byte allowance, how to run N background
threads that never touch a trading lock, and how to pause and report a gap. It
knows NOTHING about FMP, parquet layouts, cache roots or experts: the meter it
charges, the rate-limit gate it yields to and the context each fetch runs in are
all INJECTED.

That split is what keeps the layering honest (spec section 8). ``ba2_common`` may
not import ``ba2_providers``, and before this split the same code reached straight
into ``ba2_providers.fmp_common`` -- which forced the live host to bolt the test
platform's directory onto ``sys.path`` to reach it at all. Now:

* mechanism -> here;
* cache-layout knowledge (what is on disk, what a pin looks like) ->
  ``ba2_providers.warm``;
* the fetch that closes a gap -> ``ba2_experts.warm_fetchers``, beside the experts
  whose reads it mirrors;
* whether to warm at all, with what budget and when -> the hosts
  (``ba2_trade_platform.core.warm_service``, the ``ba2-test replay warm`` CLI).
"""
from ba2_common.core.warm.budget import (
    BudgetExhausted,
    RemainingGap,
    WarmBudget,
    WarmBudgetError,
    unknown_reserve_for,
)
from ba2_common.core.warm.worker import WarmQueue

__all__ = [
    "BudgetExhausted",
    "RemainingGap",
    "WarmBudget",
    "WarmBudgetError",
    "WarmQueue",
    "unknown_reserve_for",
]
