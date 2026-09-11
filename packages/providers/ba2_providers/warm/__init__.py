"""Warm CACHE-LAYOUT knowledge (spec step 4, section 6).

What is on disk, whether it covers a requirement, and what a pinned copy of it
looks like. This is the layer that knows ``fmp_history/<ns>__<SYM>.json``,
``fred/<SERIES>.json`` and ``<ProviderClass>/<SYM>_<interval>.parquet`` -- which is
exactly why it lives beside the providers that write those files rather than in the
test platform, where the live host could not reach it.

* :mod:`planner` -- read-only inspection of the configured roots.
* :mod:`roots` -- materialize a pinned, hashed copy of selected artifacts.
* :mod:`seams` -- the FMP-backed meter, gate, per-thread setup and fetch context the
  pure-mechanism :class:`ba2_common.core.warm.WarmQueue` and
  :class:`~ba2_common.core.warm.WarmBudget` are injected with.
"""
from ba2_providers.warm.planner import (
    ACTION_FETCH,
    ACTION_NONE,
    ACTION_REFRESH,
    ACTION_REPORT,
    STATUS_CHECKED_EMPTY,
    STATUS_LOCAL_STATE,
    STATUS_MISSING,
    STATUS_PRESENT,
    STATUS_STALE,
    STATUS_UNSUPPORTED,
    PlanEntry,
    WarmPlan,
    WarmPlanError,
    plan,
)
from ba2_providers.warm.roots import (
    MANIFEST_NAME,
    PROVENANCE_LEGACY,
    PROVENANCE_WARMED,
    PinnedRootError,
    materialize_pinned_root,
    sha256_file,
    verify_pinned_root,
)
from ba2_providers.warm.seams import (
    fmp_fetch_context,
    fmp_gate,
    fmp_meter,
    fmp_worker_init,
    new_warm_budget,
    new_warm_queue,
)

__all__ = [
    "ACTION_FETCH",
    "ACTION_NONE",
    "ACTION_REFRESH",
    "ACTION_REPORT",
    "MANIFEST_NAME",
    "PROVENANCE_LEGACY",
    "PROVENANCE_WARMED",
    "PinnedRootError",
    "PlanEntry",
    "STATUS_CHECKED_EMPTY",
    "STATUS_LOCAL_STATE",
    "STATUS_MISSING",
    "STATUS_PRESENT",
    "STATUS_STALE",
    "STATUS_UNSUPPORTED",
    "WarmPlan",
    "WarmPlanError",
    "fmp_fetch_context",
    "fmp_gate",
    "fmp_meter",
    "fmp_worker_init",
    "materialize_pinned_root",
    "new_warm_budget",
    "new_warm_queue",
    "plan",
    "sha256_file",
    "verify_pinned_root",
]
