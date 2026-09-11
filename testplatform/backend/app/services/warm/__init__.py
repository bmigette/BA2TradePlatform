"""The shared warm service (spec step 4, section 6).

Four pieces, in the order the lifecycle uses them:

* :mod:`planner` -- inspect the configured cache roots READ-ONLY and say what is
  present, stale or missing. Zero network by construction.
* :mod:`budget` -- reserve bytes before dispatch, count what was actually spent by
  purpose, and pause with an explicit remaining-gap report when the daily
  allowance runs out or the shared FMP gate arms.
* :mod:`worker` -- a low-priority queue of :class:`~ba2_common.core.replay.
  dependencies.Requirement` items with its own threads, sharing the FMP gate and
  never touching a trading lock.
* :mod:`roots` -- copy the selected artifact versions into an isolated, pinned
  root with a hashed manifest, so a comparison run reads a fixed set of files.

The dependency RESOLVER is not here: it is
``ba2_common.core.replay.dependencies`` plus the per-expert adapters in
``ba2_experts.replay_dependencies``, because the declaration has to live where the
fetch it describes lives. This package only decides what of it is already on disk
and how to close the difference.
"""
