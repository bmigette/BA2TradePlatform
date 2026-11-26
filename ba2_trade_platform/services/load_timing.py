"""Where one page load spent its time: ONE ``[PERF:page:load]`` INFO line per load, every step named.

The Account Growth tab and the Portfolio Allocation page each make several broker reads, a price read, some maths and
a draw. ``ui.utils.perf_logger`` writes one line per operation and drops the fast ones (below 100 ms it logs at DEBUG),
which cannot answer "which of the seven steps was the load waiting on": this keeps the steps of ONE load together, in
order, fast ones included, so a slow load reads off a single line::

    [PERF:page:load] AccountGrowth(accounts=[2], range=1y) - 9312ms (acct2 dividends 1203ms [233 rows] | ... | untimed 12ms)

``untimed`` is the part of the load no step covered. Times are wall clock on the server: the browser's own drawing of
the charts is not in them. Lives in ``services`` (not ``ui``) because the allocator's loaders do."""
import time
from contextlib import contextmanager
from typing import Callable, List, Optional

from ..logger import logger


class Step:
    """One timed step. ``note`` may be set inside the ``with`` (a row count is only known after the fetch)."""
    __slots__ = ("name", "seconds", "note", "failed")

    def __init__(self, name: str, note: Optional[str] = None):
        self.name = name
        self.seconds = 0.0
        self.note = note
        self.failed: Optional[str] = None

    def __str__(self) -> str:
        tags = [t for t in (self.note, f"FAILED: {self.failed}" if self.failed else None) if t]
        return f"{self.name} {self.seconds * 1000:.0f}ms" + (f" [{'; '.join(tags)}]" if tags else "")


class LoadBreakdown:
    """The steps of one load. Use from ONE task / thread at a time (a load is sequential)."""

    def __init__(self, component: str, detail: str = "", *, clock: Callable[[], float] = time.perf_counter):
        self.component = component
        self.detail = detail
        self._clock = clock
        self._start = clock()
        self._steps: List[Step] = []
        self._logged = False

    @contextmanager
    def step(self, name: str, note: Optional[str] = None):
        entry = Step(name, note)
        self._steps.append(entry)
        began = self._clock()
        try:
            yield entry
        except BaseException as e:
            entry.failed = type(e).__name__
            raise
        finally:
            entry.seconds = self._clock() - began

    def add(self, name: str, seconds: float, note: Optional[str] = None) -> Step:
        """A step timed ELSEWHERE (it ran in its own thread, in parallel with the steps around it): reported with its
        own wall time. ``untimed`` then never goes below zero, since parallel steps can add up to more than the load."""
        entry = Step(name, note)
        entry.seconds = seconds
        self._steps.append(entry)
        return entry

    def summary(self) -> str:
        total = self._clock() - self._start
        untimed = max(0.0, total - sum(s.seconds for s in self._steps))
        who = f"{self.component}({self.detail})" if self.detail else self.component
        parts = [str(s) for s in self._steps] + [f"untimed {untimed * 1000:.0f}ms"]
        return f"[PERF:page:load] {who} - {total * 1000:.0f}ms ({' | '.join(parts)})"

    def log(self) -> None:
        """Write the line, once. A load that timed nothing (overtaken before its first step) writes nothing."""
        if self._logged or not self._steps:
            return
        self._logged = True
        logger.info(self.summary())


def rows_note(rows) -> str:
    """``"233 rows"`` for a step's note. Never raises: a note must not be what fails the read it describes."""
    if rows is None:
        return "no answer"
    try:
        return f"{len(rows)} rows"
    except TypeError:
        return type(rows).__name__


def add(breakdown: Optional[LoadBreakdown], name: str, seconds: float, note: Optional[str] = None) -> None:
    """``breakdown.add(...)``, or nothing when the caller has no breakdown (``None``)."""
    if breakdown is not None:
        breakdown.add(name, seconds, note)


@contextmanager
def step(breakdown: Optional[LoadBreakdown], name: str, note: Optional[str] = None):
    """``breakdown.step(name)``, or an untimed pass-through when the caller has no breakdown (``None``)."""
    if breakdown is None:
        yield Step(name, note)
        return
    with breakdown.step(name, note) as entry:
        yield entry
