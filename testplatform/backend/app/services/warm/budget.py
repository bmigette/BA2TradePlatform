"""The warm bandwidth budget (spec section 6, "Operational policy").

Background warmup gets a stated daily allowance, shared across its workers, and it
gives way to live traffic. Three mechanics implement that:

* **Reserve before dispatch.** A worker reserves the plan's measured estimate for
  an item before fetching it, so N workers cannot each independently discover the
  allowance was already gone. An unknown-size response reserves an explicit
  conservative amount (:func:`unknown_reserve_for` derives it from what the plan
  actually measured -- there is no invented constant).
* **Settle against measurement, not the estimate.** ``fmp_common`` counts the real
  bytes by purpose; the budget charges those, so an over- or under-estimate corrects
  itself instead of stranding the allowance. Exhaustion is therefore bounded by the
  responses in flight, and is reported, not presented as a wire-level cap.
* **Yield to the rate limiter.** A 429/5xx armed by ANY caller arms the shared FMP
  gate; :meth:`WarmBudget.wait_for_gate` waits it out before the warm fires again.

Running out PAUSES with a :class:`RemainingGap` naming what was not dispatched.
A silent stop would leave a half-warmed root that the next plan reports as merely
"missing some things", with nothing to say the budget was the reason.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

from ba2_providers.fmp_common import (
    PURPOSE_WARM,
    gate_remaining_seconds,
    get_purpose_stats,
)

#: Longest single sleep while waiting out the FMP gate, so a shorter gate armed
#: meanwhile is re-read promptly (the same slice ``fmp_common._gate_wait`` uses).
_GATE_SLICE_SECONDS = 2.0
#: Refuse to wait forever on a gate that keeps being re-armed: a warm that has been
#: rate-limited for this long is reported as paused and retried on the next cycle,
#: rather than holding a worker thread indefinitely.
_GATE_MAX_WAIT_SECONDS = 300.0


class WarmBudgetError(RuntimeError):
    """The budget was used incorrectly (a double reservation, an unsizable plan)."""


@dataclass(frozen=True)
class RemainingGap:
    """What a paused warm still owes, and why it stopped."""

    reason: str
    allowance_bytes: int
    spent_bytes: int
    reserved_bytes: int
    remaining_bytes: int
    requested_bytes: int
    pending: Tuple[str, ...]

    @property
    def shortfall_bytes(self) -> int:
        """How much more allowance the blocked item needed."""
        return max(0, self.requested_bytes - self.remaining_bytes)

    def to_mapping(self) -> Dict[str, object]:
        return {
            "reason": self.reason,
            "allowance_bytes": self.allowance_bytes,
            "spent_bytes": self.spent_bytes,
            "reserved_bytes": self.reserved_bytes,
            "remaining_bytes": self.remaining_bytes,
            "requested_bytes": self.requested_bytes,
            "shortfall_bytes": self.shortfall_bytes,
            "pending": list(self.pending),
        }

    def to_markdown(self) -> str:
        return (
            f"Warm paused: {self.reason}. "
            f"allowance {self.allowance_bytes / 1048576.0:.1f} MiB, "
            f"spent {self.spent_bytes / 1048576.0:.1f} MiB, "
            f"reserved {self.reserved_bytes / 1048576.0:.1f} MiB, "
            f"remaining {self.remaining_bytes / 1048576.0:.1f} MiB; "
            f"the next item needed {self.requested_bytes} bytes "
            f"(short by {self.shortfall_bytes}). "
            f"Not dispatched ({len(self.pending)}): {', '.join(self.pending) or 'none'}"
        )


class BudgetExhausted(RuntimeError):
    """The daily warm allowance cannot cover the next item. Carries the gap report."""

    def __init__(self, gap: RemainingGap) -> None:
        super().__init__(gap.to_markdown())
        self.gap = gap


class WarmBudget:
    """The shared daily byte allowance for background warming.

    Thread-safe: the reservations are the point of contention between workers.

    ``allowance_bytes`` and ``unknown_reserve_bytes`` are both REQUIRED. The
    allowance is an operator's stated policy (``warm_daily_allowance_mib``), and the
    unknown reserve is derived from measurement (:func:`unknown_reserve_for`); a
    default for either would be this module inventing the thing it exists to
    enforce.
    """

    def __init__(self, *, allowance_bytes: int, unknown_reserve_bytes: int) -> None:
        if allowance_bytes <= 0:
            raise WarmBudgetError(f"allowance_bytes must be positive, got {allowance_bytes}")
        if unknown_reserve_bytes <= 0:
            raise WarmBudgetError(
                f"unknown_reserve_bytes must be positive, got {unknown_reserve_bytes}")
        self.allowance_bytes = int(allowance_bytes)
        self.unknown_reserve_bytes = int(unknown_reserve_bytes)
        self._lock = threading.Lock()
        self._reserved: Dict[str, int] = {}
        # Measure only what THIS budget's run spends: another warm earlier today has
        # already been charged to its own budget, and charging it twice would make
        # the second run's allowance disappear before it started.
        self._baseline_bytes = self._measured_warm_bytes()

    # -- measurement ------------------------------------------------------- #
    @staticmethod
    def _measured_warm_bytes() -> int:
        return int(get_purpose_stats().get(PURPOSE_WARM, {}).get("bytes", 0))

    def spent_bytes(self) -> int:
        """Warm bytes measured on the wire since this budget was created.

        The counters reset at the UTC day change, which shows up here as the
        measured total falling below the baseline. That is a NEW allowance, not a
        negative balance: the baseline is re-anchored and the run continues.
        """
        measured = self._measured_warm_bytes()
        if measured < self._baseline_bytes:
            self._baseline_bytes = 0
            measured = self._measured_warm_bytes()
        return max(0, measured - self._baseline_bytes)

    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(self._reserved.values())

    def remaining_bytes(self) -> int:
        """Allowance left after what was spent and what is currently in flight."""
        return max(0, self.allowance_bytes - self.spent_bytes() - self.reserved_bytes())

    # -- reservations ------------------------------------------------------ #
    def reserve(self, key: str, nbytes: Optional[int], *,
                pending: Sequence[str] = ()) -> int:
        """Hold ``nbytes`` (or the unknown reserve) for ``key``; return what was held.

        Raises :class:`BudgetExhausted` -- carrying a :class:`RemainingGap` that names
        ``pending``, the items the caller has not dispatched -- when the allowance
        cannot cover it.
        """
        want = self.unknown_reserve_bytes if nbytes is None else max(0, int(nbytes))
        with self._lock:
            if key in self._reserved:
                raise WarmBudgetError(
                    f"{key} is already reserved; a second reservation would charge it twice")
            spent = self.spent_bytes()
            reserved = sum(self._reserved.values())
            remaining = max(0, self.allowance_bytes - spent - reserved)
            if want > remaining:
                raise BudgetExhausted(RemainingGap(
                    reason="daily warm download allowance exhausted",
                    allowance_bytes=self.allowance_bytes,
                    spent_bytes=spent,
                    reserved_bytes=reserved,
                    remaining_bytes=remaining,
                    requested_bytes=want,
                    pending=tuple(pending),
                ))
            self._reserved[key] = want
        return want

    def settle(self, key: str, actual_bytes: Optional[int] = None) -> None:
        """Release ``key``'s reservation after its fetch finished.

        ``actual_bytes`` is accepted for symmetry and logging; the CHARGE comes from
        the measured wire counters, not from what a caller reports, so a fetch that
        under-reports cannot spend allowance invisibly.
        """
        with self._lock:
            self._reserved.pop(key, None)

    def release(self, key: str) -> None:
        """Release a reservation for work that never ran (a refusal, a shutdown)."""
        with self._lock:
            self._reserved.pop(key, None)

    def gap(self, reason: str, pending: Sequence[str]) -> RemainingGap:
        """A remaining-gap report for a pause this budget did not itself raise."""
        spent = self.spent_bytes()
        reserved = self.reserved_bytes()
        return RemainingGap(
            reason=reason,
            allowance_bytes=self.allowance_bytes,
            spent_bytes=spent,
            reserved_bytes=reserved,
            remaining_bytes=max(0, self.allowance_bytes - spent - reserved),
            requested_bytes=0,
            pending=tuple(pending),
        )

    # -- rate limiting ----------------------------------------------------- #
    def wait_for_gate(self, sleep: Callable[[float], None] = time.sleep) -> float:
        """Block while the SHARED FMP cooldown is armed; return the seconds waited.

        The gate is armed by whichever caller met the 429/5xx -- usually a live
        request. Waiting it out here is what "live requests retain priority" means
        in practice: the warm does not add load to a provider that is already
        pushing back.
        """
        waited = 0.0
        while True:
            remaining = gate_remaining_seconds()
            if remaining <= 0:
                return waited
            if waited >= _GATE_MAX_WAIT_SECONDS:
                return waited
            slice_seconds = min(remaining, _GATE_SLICE_SECONDS)
            sleep(slice_seconds)
            waited += slice_seconds


def unknown_reserve_for(plan) -> int:
    """What to reserve for an item whose size the plan could not estimate.

    The median of everything the plan DID measure on the roots. Raises when the
    roots held nothing measurable at all: a first warm into an empty root has no
    basis for any reservation, and the honest answer is to say so rather than to
    pick a number that makes the budget look enforced.
    """
    median = plan.measured_median_bytes()
    if median is None:
        raise WarmBudgetError(
            "this plan measured no artifact on any root, so there is no basis for an "
            "unknown-size reservation; seed the root (ba2-test prewarm) or state the "
            "reserve explicitly before warming")
    return int(median)


__all__ = [
    "BudgetExhausted",
    "RemainingGap",
    "WarmBudget",
    "WarmBudgetError",
    "unknown_reserve_for",
]
