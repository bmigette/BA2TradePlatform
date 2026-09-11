"""Offline replay of recorded live analyses (spec steps 3 and 5).

`docs/plans/2026-09-10-live-capture-prewarm-backtest-replay-spec.md` sections 2
and 8. Step 2 recorded, for every live expert analysis, the normalized `_gather`
bundle, the provider returns behind it, the evaluation-clock reads and the
outcome. This package reads one exported session bundle back and answers three of
the spec's comparison capabilities, offline:

* ``recorded_expert`` (:mod:`expert_replay`) -- re-run the shared ``_process``
  on the recorded bundle and settings under the recorded clock, and compare the
  produced ``Recommendation`` to the recorded one by exact serialized equality.
* ``gather_tape`` (:mod:`gather_tape`) -- re-run the live ``_gather`` against a
  tape of the recorded provider returns, matched by exact request identity, and
  compare the produced bundle to the recorded one. This is what separates a
  mapping/shortcut defect from a calculation defect.
* ``historical`` (:mod:`historical`) -- re-run the normal ``analyze_as_of`` path
  against a PINNED cache root, in a child process whose ``CACHE_FOLDER`` was set
  before import, and diff both the rebuilt inputs and the recommendation. This is
  the only capability that measures reconstruction, coverage and revision drift,
  and the only one for which zero difference is not always attainable.

``decision`` is a LATER delivery and is reported as ``not_run`` -- never as
absent rows, and never rolled into a percentage.

Everything here runs under :func:`isolation.replay_isolation`: no network at the
transport, no instance/provider resolution, no trading database. Every
unexpected dependency surfaces as a typed
:class:`~ba2_common.core.replay.ReplayMiss` naming the analysis and the request
identity that was not on the tape.
"""
from app.services.replay.report import (
    AnalysisResult,
    ReplayReport,
    STAGE_ROWS,
)

__all__ = ["AnalysisResult", "ReplayReport", "STAGE_ROWS"]
