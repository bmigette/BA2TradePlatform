"""The warm bandwidth budget (spec section 6, "Operational policy").

Background warmup gets a stated daily allowance, shared across its workers, and it
gives way to live traffic. Three mechanics implement that:

* **Reserve before dispatch.** A worker reserves the plan's measured estimate for
  an item before fetching it, so N workers cannot each independently discover the
  allowance was already gone. An item whose size the plan could not estimate
  reserves an explicit amount (:func:`unknown_reserve_for`, derived from what the
  plan actually measured -- there is no invented constant).
* **Settle against measurement, not the estimate.** The METER counts real bytes;
  the budget charges those, so an over- or under-estimate corrects itself instead
  of stranding the allowance. Exhaustion is therefore bounded by the responses in
  flight, and is reported, not presented as a wire-level cap.
* **Yield to the rate limiter.** The GATE reports how long a provider-wide cooldown
  still has to run; :meth:`WarmBudget.wait_for_gate` waits it out before the warm
  fires again.

The meter and the gate are INJECTED callables. ``ba2_common`` may not import
``ba2_providers``, and the honest consequence is that this class is told how to
measure rather than assuming FMP: a caller warming a different provider supplies
that provider's counters (``ba2_providers.warm.seams`` supplies FMP's).

Running out PAUSES with a :class:`RemainingGap` naming what was not dispatched.
A silent stop would leave a half-warmed root that the next plan reports as merely
"missing some things", with nothing to say the budget was the reason.
"""
from __future__ import annotations


import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

#: Longest single sleep while waiting out the rate-limit gate, so a shorter gate
#: armed meanwhile is re-read promptly.
_GATE_SLICE_SECONDS = 2.0
#: Refuse to wait forever on a gate that keeps being re-armed: a warm that has been
#: rate-limited for this long is reported as paused and retried on the next cycle,
#: rather than holding a worker thread indefinitely.
_GATE_MAX_WAIT_SECONDS = 300.0

#: Quantile of the measured sizes used to size an unknown-size reservation. High
#: rather than central: the reservation exists so an unmeasured item cannot
#: overshoot the allowance unnoticed, and a median would under-reserve for exactly
#: the long-tailed payloads (a full statement history) that matter.
_UNKNOWN_RESERVE_QUANTILE = 0.90


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

    Every input is REQUIRED. The allowance is an operator's stated policy
    (``warm_daily_allowance_mib``), the unknown reserve comes from measurement
    (:func:`unknown_reserve_for`), and the meter and gate come from whichever
    provider stack is being warmed. A default for any of them would be this class
    inventing the thing it exists to enforce.

    ``meter()`` returns the provider's cumulative WARM bytes for the current day; it
    is expected to reset at the UTC day boundary, which shows up here as the total
    falling below the baseline -- a new allowance, not a negative balance.
    ``gate()`` returns the seconds a provider-wide cooldown still has to run.
    """

    def __init__(self, *, allowance_bytes: int, unknown_reserve_bytes: int,
                 meter: Callable[[], int], gate: Callable[[], float]) -> None:
        if allowance_bytes <= 0:
            raise WarmBudgetError(f"allowance_bytes must be positive, got {allowance_bytes}")
        if unknown_reserve_bytes <= 0:
            raise WarmBudgetError(
                f"unknown_reserve_bytes must be positive, got {unknown_reserve_bytes}")
        self.allowance_bytes = int(allowance_bytes)
        self.unknown_reserve_bytes = int(unknown_reserve_bytes)
        self._meter = meter
        self._gate = gate
        self._lock = threading.Lock()
        self._reserved: Dict[str, int] = {}
        # Measure only what THIS budget's run spends: another warm earlier today has
        # already been charged to its own budget, and charging it twice would make
        # the second run's allowance disappear before it started.
        self._baseline_bytes = int(meter())

    # -- measurement ------------------------------------------------------- #
    def spent_bytes(self) -> int:
        """Warm bytes measured since this budget was created.

        The provider's counters reset at the UTC day change, which shows up here as
        the measured total falling below the baseline. That is a NEW allowance, not a
        negative balance: the baseline is re-anchored (under the lock -- two workers
        crossing midnight together must not each re-anchor against the other's
        partial read) and the run continues.
        """
        measured = int(self._meter())
        if measured < self._baseline_bytes:
            with self._lock:
                if measured < self._baseline_bytes:
                    self._baseline_bytes = 0
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
        spent = self.spent_bytes()          # outside the lock: it may re-anchor
        with self._lock:
            if key in self._reserved:
                raise WarmBudgetError(
                    f"{key} is already reserved; a second reservation would charge it twice")
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
        the meter, not from what a caller reports, so a fetch that under-reports
        cannot spend allowance invisibly.
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
        """Block while the provider-wide cooldown is armed; return the seconds waited.

        The gate is armed by whichever caller met the 429/5xx -- usually a live
        request. Waiting it out here is what "live requests retain priority" means in
        practice: the warm does not add load to a provider that is already pushing
        back.
        """
        waited = 0.0
        while True:
            remaining = float(self._gate())
            if remaining <= 0:
                return waited
            if waited >= _GATE_MAX_WAIT_SECONDS:
                return waited
            slice_seconds = min(remaining, _GATE_SLICE_SECONDS)
            sleep(slice_seconds)
            waited += slice_seconds


def unknown_reserve_for(plan) -> int:
    """What to reserve for an item whose size the plan could not estimate.

    A HIGH QUANTILE (p90) of everything the plan measured on the roots, not the
    median: the reservation exists to stop an unmeasured item overshooting the
    allowance unnoticed, and the payloads whose size is unknown are exactly the ones
    that can be large. It is still a measured number -- every input came off a real
    file on a real root.

    Raises when the roots held nothing measurable at all: a first warm into an empty
    root has no basis for any reservation, and the honest answer is to say so rather
    than to pick a number that makes the budget look enforced.
    """
    return unknown_reserve_from_sizes(plan.measured_sizes())


def unknown_reserve_from_sizes(sizes: Sequence[int]) -> int:
    """The same reservation, from sizes measured any other way.

    A caller that has no requirements yet -- the live host sizing this at startup --
    cannot get them from a plan (a plan over no requirements measures nothing, whatever
    the root holds) and measures the root directly instead
    (``ba2_providers.warm.planner.measured_root_sizes``). The QUANTILE must still be the
    one ``unknown_reserve_for`` takes, so both go through here rather than through two
    copies of the same number.
    """
    if not sizes:
        raise WarmBudgetError(
            "nothing measured an artifact on any root, so there is no basis for an "
            "unknown-size reservation; seed the root (ba2-test prewarm) or state the "
            "reserve explicitly before warming")
    return quantile(sizes, _UNKNOWN_RESERVE_QUANTILE)


def quantile(values: Sequence[int], q: float) -> int:
    """The ``q`` quantile of ``values`` (nearest-rank), as an int.

    ``statistics.quantiles`` needs at least two data points and interpolates; a warm
    plan legitimately measures ONE file, and a reservation must still come out of it.
    """
    ordered = sorted(int(v) for v in values)
    if not ordered:
        raise WarmBudgetError("cannot take a quantile of nothing")
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


__all__ = [
    "BudgetExhausted",
    "RemainingGap",
    "WarmBudget",
    "WarmBudgetError",
    "quantile",
    "unknown_reserve_for",
    "unknown_reserve_from_sizes",
]
