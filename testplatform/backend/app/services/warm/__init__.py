"""Thin backend host for the shared warm service (spec step 4, section 8).

**The implementation is not here.** It was, and the layering was wrong: the live
trading app has to run the same warm, and reaching it meant bolting this backend
directory onto ``sys.path`` from inside ``ba2_trade_platform`` -- an edge from the
application to the test platform that the spec's step-8 layering does not allow.

So the code moved to the packages both trees already install:

===========================  ==========================================
What                         Where
===========================  ==========================================
queue + budget (mechanism)   ``ba2_common.core.warm``
planner + pinned roots       ``ba2_providers.warm``
the FMP thread seams         ``ba2_providers.warm.seams``
the namespace fetch table    ``ba2_experts.warm_fetchers``
===========================  ==========================================

What a HOST still owns is the policy: which roots, which budget, when, and on
whose behalf. For this backend that is the ``ba2-test replay warm-plan`` /
``replay warm`` argument parsing in ``ba2test_launcher``; for the live application
it is ``ba2_trade_platform.core.warm_service``.

This module re-exports the names so existing imports (and the backend's own tests)
keep working, and so that one place documents where each piece lives.
"""
from ba2_common.core.warm import (  # noqa: F401
    BudgetExhausted,
    RemainingGap,
    WarmBudget,
    WarmBudgetError,
    WarmQueue,
    unknown_reserve_for,
)
from ba2_experts.warm_fetchers import (  # noqa: F401
    DefaultWarmFetcher,
    NamespaceFetchers,
    NamespaceRequest,
    WarmFetchError,
)
from ba2_providers.warm import planner, roots, seams  # noqa: F401
from ba2_providers.warm.planner import PlanEntry, WarmPlan, WarmPlanError, plan  # noqa: F401
from ba2_providers.warm.roots import (  # noqa: F401
    PinnedRootError,
    materialize_pinned_root,
    verify_pinned_root,
)
from ba2_providers.warm.seams import new_warm_budget, new_warm_queue  # noqa: F401

__all__ = [
    "BudgetExhausted",
    "DefaultWarmFetcher",
    "NamespaceFetchers",
    "NamespaceRequest",
    "PinnedRootError",
    "PlanEntry",
    "RemainingGap",
    "WarmBudget",
    "WarmBudgetError",
    "WarmFetchError",
    "WarmPlan",
    "WarmPlanError",
    "WarmQueue",
    "materialize_pinned_root",
    "new_warm_budget",
    "new_warm_queue",
    "plan",
    "planner",
    "roots",
    "seams",
    "unknown_reserve_for",
    "verify_pinned_root",
]
