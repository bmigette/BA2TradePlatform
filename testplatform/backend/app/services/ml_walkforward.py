"""Walk-forward geometry for the ML exploration v2 grid: prediction blocks, training windows, the purge, the
early-stopping split and the market regimes the results are broken down by.

Everything is pure calendar / mask arithmetic on sessions (dates); no data is read. ``ml_targets.quarterly_folds`` is
the model for the block rule (a trailing stub shorter than half a period merges into the previous block) and
``ml_targets.fold_rows`` / ``split_validation`` for the purge, here made generic over the block length, the window
length and the horizon (the caller supplies the session on which each row's target ends).

Look-ahead rules, each enforced by a function below:

* a block is predicted by a model trained on sessions strictly BEFORE the block (``train_window`` is
  ``[start, block.start)``);
* a training row whose target ends on or after the block start is dropped (``purge``): its label would have been
  known only after the block began;
* the early-stopping block is the LAST share of the training sessions, and the rows that fit the model are purged
  against it the same way (``validation_split``), so the validation labels are never seen by the fit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

TRAIN_MONTHS: Tuple[int, ...] = (12, 24, 36)
FREQS: Tuple[str, ...] = ("Q", "H")
#: regime names in time order; ``pre2019`` is training history only
REGIMES: Tuple[str, ...] = ("pre2019", "2019", "2020crash", "2021", "2022bear", "2023-24", "2025-26")
#: first session of each regime after ``pre2019`` (half-open: a regime runs up to the next one's start). The 2019
#: regime therefore runs to the crash start, 2020-01-01..2020-02-18 included, so that no session is unassigned.
_REGIME_STARTS: Tuple[Tuple[date, str], ...] = (
    (date(2019, 1, 1), "2019"),
    (date(2020, 2, 19), "2020crash"),
    (date(2020, 7, 1), "2021"),
    (date(2022, 1, 1), "2022bear"),
    (date(2023, 1, 1), "2023-24"),
    (date(2025, 1, 1), "2025-26"),
)


def _d(x: Any) -> date:
    """A ``date`` from a date / Timestamp / ISO string."""
    return pd.Timestamp(x).date()


@dataclass(frozen=True)
class Block:
    """One prediction block: sessions ``start`` .. ``end`` inclusive."""
    name: str
    start: date
    end: date

    @property
    def end_exclusive(self) -> date:
        return (pd.Timestamp(self.end) + pd.Timedelta(days=1)).date()

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def _period_months(freq: str) -> int:
    if freq not in FREQS:
        raise ValueError(f"freq must be one of {FREQS}, got {freq!r}")
    return 3 if freq == "Q" else 6


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


def _period_start(d: date, months: int) -> date:
    """First day of the calendar quarter / half year holding ``d``."""
    return date(d.year, ((d.month - 1) // months) * months + 1, 1)


def blocks(start: Any, end: Any, freq: str) -> List[Block]:
    """Calendar quarter ('Q') or half-year ('H') prediction blocks covering ``start``..``end`` (inclusive).

    Blocks follow the calendar (Q1 = Jan-Mar, H1 = Jan-Jun); the first one starts at ``start`` (shortened when
    ``start`` falls inside a period), the last is truncated at ``end``. A truncated last block shorter than half of
    its calendar period merges into the previous one (2026-10-01..12 joins 2026Q3; 2026-07-01..10-12 is 104 of 184
    days and stays its own half-year block). 2019-01-01..2026-10-12 gives 31 Q or 16 H blocks."""
    months = _period_months(freq)
    s, e = _d(start), _d(end)
    if e < s:
        raise ValueError(f"end {e} is before start {s}")
    out: List[Block] = []
    p0 = _period_start(s, months)
    while p0 <= e:
        p1 = _add_months(p0, months)
        period_days = (p1 - p0).days
        b_start = max(p0, s)
        b_end = min(e, (pd.Timestamp(p1) - pd.Timedelta(days=1)).date())
        label = f"{p0.year}Q{(p0.month - 1) // 3 + 1}" if freq == "Q" else f"{p0.year}H{(p0.month - 1) // 6 + 1}"
        truncated = b_end < (pd.Timestamp(p1) - pd.Timedelta(days=1)).date()
        if out and truncated and (b_end - b_start).days + 1 < period_days / 2:
            prev = out.pop()
            out.append(Block(prev.name, prev.start, b_end))
            break
        out.append(Block(label, b_start, b_end))
        p0 = p1
    return out


class Window(NamedTuple):
    """A training window ``[start, end)``; ``clipped`` is True when a fixed window was shortened because ``data_start``
    came after where ``months`` would have begun it (the model trains on LESS than the nominal length)."""
    start: date
    end: date
    clipped: bool


def train_window(block: Block, months: Union[int, str], data_start: Optional[Any] = None) -> Window:
    """``Window(start, end, clipped)`` of the model that predicts ``block``: ``months`` (12 / 24 / 36) before the
    block start, or ``"expanding"`` from ``data_start``. The end is always ``block.start`` (exclusive). A fixed
    window that would begin before ``data_start`` (when given) is clipped to it and ``clipped`` says so, so a grid
    can report or skip the cells whose nominal length was not available."""
    ds = _d(data_start) if data_start is not None else None
    clipped = False
    if months == "expanding":
        if ds is None:
            raise ValueError("an expanding window needs data_start")
        start = ds
    elif months in TRAIN_MONTHS and not isinstance(months, bool):
        start = _add_months(block.start, -int(months))
        if ds is not None and start < ds:
            start, clipped = ds, True
    else:
        raise ValueError(f"months must be one of {TRAIN_MONTHS} or 'expanding', got {months!r}")
    if start >= block.start:
        raise ValueError(f"empty training window for {block.name}: {start} .. {block.start}")
    return Window(start, block.start, clipped)


def _day_values(x: Any) -> np.ndarray:
    return pd.to_datetime(pd.Series(np.asarray(x).ravel() if not isinstance(x, pd.Series) else x.values)).values


def purge(rows_session: Sequence, rows_target_end_session: Sequence, block_start: Any) -> np.ndarray:
    """Boolean KEEP mask over training rows: True where the row's target ends strictly before ``block_start``.

    ``rows_target_end_session[i]`` is the session of the last bar row i's label depends on (NaT / None where the
    label does not exist -- those rows are dropped too). A row whose target ends at or after the block start would be
    labelled with prices from inside the block being predicted, so it is dropped: the horizon gap. Rows whose target
    ends before their own session are a caller bug and are refused."""
    sess = _day_values(rows_session)
    tgt = _day_values(rows_target_end_session)
    if len(sess) != len(tgt):
        raise ValueError(f"{len(sess)} sessions but {len(tgt)} target-end sessions")
    bs = np.datetime64(pd.Timestamp(block_start))
    known = ~pd.isna(tgt)
    if (tgt[known] < sess[known]).any():
        raise ValueError("a row's target ends before its own session")
    keep = np.zeros(len(sess), dtype=bool)
    keep[known] = tgt[known] < bs
    return keep


def validation_split(train_mask: Sequence[bool], sessions: Sequence, target_end_sessions: Sequence,
                     share: float = 0.15) -> Tuple[np.ndarray, np.ndarray]:
    """``(fit_mask, val_mask)`` inside the (already purged) training rows ``train_mask``.

    The validation rows are those of the LAST ``share`` of the training sessions (time-ordered early stopping). The
    fit rows are the earlier ones whose target ends before the first validation session, so no fit label depends on
    prices inside the validation block. Refuses a split that leaves either side empty."""
    if not 0.0 < share < 1.0:
        raise ValueError("share must be in (0, 1)")
    tm = np.asarray(train_mask, dtype=bool)
    sess = _day_values(sessions)
    tgt = _day_values(target_end_sessions)
    if not (len(tm) == len(sess) == len(tgt)):
        raise ValueError("train_mask, sessions and target_end_sessions must have the same length")
    ts = np.unique(sess[tm])
    if len(ts) < 2:
        raise ValueError("need at least 2 training sessions to split off a validation block")
    vstart = ts[min(int(len(ts) * (1.0 - share)), len(ts) - 1)]
    known = ~pd.isna(tgt)
    val = tm & (sess >= vstart)
    fit = tm & (sess < vstart) & known
    fit[known] &= tgt[known] < vstart
    if not val.any() or not fit.any():
        raise ValueError("validation split left the fit or the validation side empty")
    return fit, val


def regime(session: Any) -> str:
    """Market regime of one session (half-open bounds): ``2019`` (also 2020-01-01..2020-02-18), ``2020crash``
    (2020-02-19..2020-06-30), ``2021`` (2020-07-01..2021-12-31), ``2022bear``, ``2023-24``, ``2025-26`` (to the
    end of data); anything before 2019-01-01 is ``pre2019`` (training history only)."""
    d = _d(session)
    name = "pre2019"
    for start, label in _REGIME_STARTS:
        if d >= start:
            name = label
        else:
            break
    return name


def regimes(sessions: Sequence) -> np.ndarray:
    """Vectorised ``regime`` over an array / Series of sessions (object array of names)."""
    ts = pd.to_datetime(pd.Series(np.asarray(sessions).ravel() if not isinstance(sessions, pd.Series)
                                  else sessions.values))
    if ts.isna().any():
        raise ValueError("a session is NaT")
    starts = np.array([np.datetime64(pd.Timestamp(s)) for s, _ in _REGIME_STARTS])
    idx = np.searchsorted(starts, ts.values, side="right")        # 0 = before 2019
    names = np.array(("pre2019",) + tuple(n for _, n in _REGIME_STARTS), dtype=object)
    return names[idx]
