# Scheduler Monthly (Nth-weekday) Extension — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let expert analysis schedules fire **monthly** on the "Nth weekday" (e.g. 1st Monday, 3rd Tuesday), in addition to the current weekly schedules, and let an expert hide the open-positions schedule.

**Architecture:** The schedule is a JSON dict stored per expert in the `execution_schedule_enter_market` / `execution_schedule_open_positions` settings and parsed by `JobManager._parse_schedule` into an APScheduler `CronTrigger`. Today it only builds weekly crons from `{days, times}`. Add an optional `frequency: "monthly"` shape that builds `CronTrigger(day="1st mon", hour, minute)` (APScheduler natively supports the `"<ordinal> <weekday>"` day expression). Add a `schedules_open_positions` expert property so experts that rebalance in one batch render only the enter-market schedule.

**Tech Stack:** Python, APScheduler (`apscheduler.triggers.cron.CronTrigger`), pytest, NiceGUI (schedule UI).

---

## Background the implementer needs

- `JobManager._parse_schedule(self, schedule_setting)` — `ba2_trade_platform/core/JobManager.py:783`. Current input shape (weekly):
  ```json
  {"days": {"monday": true, "tuesday": false, ...}, "times": ["09:30"]}
  ```
  It maps enabled day names → `day_of_week` numbers and returns `CronTrigger(hour, minute, day_of_week)`.
- APScheduler `CronTrigger` `day` field accepts `"1st mon"`, `"2nd tue"`, `"3rd fri"`, `"last fri"` (the x-th weekday of the month). We will use ordinals 1-3.
- `should_expand_instrument_jobs` is an expert **setting/property** (see `FMPSenateTraderCopy.get_settings_definitions` `:60`). `schedules_open_positions` will be the same kind of property, read by JobManager when scheduling.
- Schedule UI lives in the expert settings/schedule editor (search `execution_schedule_enter_market` in `ba2_trade_platform/ui/`). Find the existing weekly day/time picker before editing.

---

## Task 1: Pure helper — build a monthly CronTrigger from ordinal+weekday

**Files:**
- Modify: `ba2_trade_platform/core/JobManager.py` (add module-level helper `build_monthly_cron`)
- Test: `tests/test_job_scheduling.py` (new)

**Step 1 — failing test:**
```python
# tests/test_job_scheduling.py
from apscheduler.triggers.cron import CronTrigger
from ba2_trade_platform.core.JobManager import build_monthly_cron

def test_build_monthly_cron_first_monday():
    trig = build_monthly_cron(ordinal=1, weekday="monday", hour=9, minute=30)
    assert isinstance(trig, CronTrigger)
    # str(CronTrigger) exposes the fields; day must be the "1st mon" expression
    s = str(trig)
    assert "day='1st mon'" in s
    assert "hour='9'" in s and "minute='30'" in s

def test_build_monthly_cron_third_tuesday():
    trig = build_monthly_cron(ordinal=3, weekday="tuesday", hour=16, minute=0)
    assert "day='3rd tue'" in str(trig)

def test_build_monthly_cron_rejects_bad_ordinal():
    import pytest
    with pytest.raises(ValueError):
        build_monthly_cron(ordinal=5, weekday="monday", hour=9, minute=30)
```

**Step 2 — run, expect ImportError/fail:** `\.venv\Scripts\python.exe -m pytest tests/test_job_scheduling.py -q`

**Step 3 — implement** (module-level in `JobManager.py`, near the imports):
```python
_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}
_WEEKDAY_ABBR = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}

def build_monthly_cron(ordinal: int, weekday: str, hour: int, minute: int):
    """Build a CronTrigger firing on the Nth weekday of each month (e.g. 1st Monday)."""
    if ordinal not in _ORDINALS:
        raise ValueError(f"ordinal must be 1, 2 or 3 (got {ordinal})")
    wd = _WEEKDAY_ABBR.get(weekday.lower())
    if not wd:
        raise ValueError(f"invalid weekday {weekday!r}")
    return CronTrigger(day=f"{_ORDINALS[ordinal]} {wd}", hour=hour, minute=minute)
```

**Step 4 — run, expect PASS.**

**Step 5 — commit:** `git commit -m "feat(scheduler): add build_monthly_cron Nth-weekday helper"`

---

## Task 2: `_parse_schedule` understands the monthly shape

**Files:**
- Modify: `ba2_trade_platform/core/JobManager.py:783` (`_parse_schedule`)
- Test: `tests/test_job_scheduling.py`

New monthly input shape (backward compatible — absence of `frequency` ⇒ weekly):
```json
{"frequency": "monthly", "ordinal": 1, "weekday": "monday", "times": ["09:30"]}
```

**Step 1 — failing test** (instantiate a JobManager-like caller; `_parse_schedule` is an instance method but uses no instance state — call it on a bare instance via `JobManager.__new__`):
```python
def test_parse_schedule_monthly():
    from ba2_trade_platform.core.JobManager import JobManager
    jm = JobManager.__new__(JobManager)
    trig = jm._parse_schedule({"frequency": "monthly", "ordinal": 2, "weekday": "friday", "times": ["10:00"]})
    assert "day='2nd fri'" in str(trig)

def test_parse_schedule_weekly_still_works():
    from ba2_trade_platform.core.JobManager import JobManager
    jm = JobManager.__new__(JobManager)
    trig = jm._parse_schedule({"days": {"monday": True, "wednesday": True}, "times": ["09:30"]})
    assert trig is not None  # weekly path unchanged
```

**Step 2 — run, expect the monthly test to fail.**

**Step 3 — implement:** at the top of `_parse_schedule`, before the existing `{days, times}` branch:
```python
if isinstance(schedule_setting, dict) and schedule_setting.get("frequency") == "monthly":
    times = schedule_setting.get("times", ["09:30"])
    hour, minute = map(int, times[0].split(":"))
    return build_monthly_cron(
        ordinal=int(schedule_setting["ordinal"]),
        weekday=schedule_setting["weekday"],
        hour=hour, minute=minute,
    )
```

**Step 4 — run, expect PASS (both).**

**Step 5 — commit:** `git commit -m "feat(scheduler): parse monthly Nth-weekday schedules"`

---

## Task 3: `schedules_open_positions` property suppresses the open-positions job

**Files:**
- Modify: `ba2_trade_platform/core/JobManager.py:588-599` (`_schedule_expert_jobs`, the OPEN_POSITIONS block)
- Test: `tests/test_job_scheduling.py`

**Step 1 — failing test:** verify the guard logic via a small extracted predicate. Add a helper `should_schedule_open_positions(expert_properties)` and test it:
```python
def test_open_positions_suppressed_when_flag_false():
    from ba2_trade_platform.core.JobManager import should_schedule_open_positions
    assert should_schedule_open_positions({"schedules_open_positions": False}) is False
    assert should_schedule_open_positions({}) is True  # default on
```

**Step 2 — run, expect fail.**

**Step 3 — implement** the helper and use it to gate the OPEN_POSITIONS scheduling block (read `expert_properties` the same way the existing code reads `should_expand_instrument_jobs`, see `:722-724`). Wrap the `execution_schedule_open_positions` block in `if should_schedule_open_positions(expert_properties):`.

**Step 4 — run, expect PASS.**

**Step 5 — commit:** `git commit -m "feat(scheduler): schedules_open_positions property gate"`

---

## Task 4: Schedule UI — weekly/monthly switch

**Files:**
- Modify: the schedule editor component (find it: `grep -rn "execution_schedule_enter_market" ba2_trade_platform/ui/`)
- Manual verification (NiceGUI UI; no unit test required, but keep the produced dict shape exactly as Tasks 1-2 expect)

**Steps:**
1. Add a frequency toggle (Weekly | Monthly) to the schedule editor, for **both** `enter_market` and `open_positions` editors.
2. Weekly → existing day checkboxes + time (unchanged output `{days, times}`).
3. Monthly → a weekday dropdown (Monday–Sunday) + an ordinal dropdown (1st/2nd/3rd) + time, producing `{frequency: "monthly", ordinal, weekday, times}`.
4. When the expert's `schedules_open_positions` property is False, **hide** the open-positions schedule editor entirely.
5. Manual verify with `@verify` / the `/run` skill: create a FactorRanker (or any expert), set a 1st-Monday monthly schedule, confirm the stored setting JSON matches the shape and the job is created (check logs for `Creating ... cron trigger` / next run time).

**Commit:** `git commit -m "feat(ui): weekly/monthly schedule switch + hide open-positions when unused"`

---

## Task 5: Docs + final check

- Update any schedule docs/tooltips to mention monthly Nth-weekday.
- Run full suite: `\.venv\Scripts\python.exe -m pytest -q` — expect no new failures.
- Commit: `git commit -m "docs(scheduler): monthly schedule notes"`

## Notes
- DRY: reuse `build_monthly_cron` from both `_parse_schedule` and any preview/next-run UI.
- YAGNI: only ordinals 1-3 (per product decision). Add `"4th"`/`"last"` later if needed.
- Backward compatibility: existing weekly configs have no `frequency` key and keep working untouched.
