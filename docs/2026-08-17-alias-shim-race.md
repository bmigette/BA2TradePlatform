# The alias-shim import race (2026-08-17)

## What happened

The Monday `ENTER_MARKET` run for expert 6 (FMPEarningsDrift) on the **live prod platform**
died before it produced a single recommendation:

```
2026-08-17 15:30:00,077 - JobManager - ERROR - Error in screener analysis for expert 6:
cannot import name 'StockScreener' from 'ba2_trade_platform.core.StockScreener'
  File ".../ba2_trade_platform/core/JobManager.py", line 1394, in _execute_screener_analysis
    from .StockScreener import StockScreener
ImportError: cannot import name 'StockScreener' from 'ba2_trade_platform.core.StockScreener'
```

Entry is scheduled Mondays only (`execution_schedule_enter_market`), so the cost of this one
failed import was a full week of no entries for that expert — the next opportunity was
2026-08-24. It failed silently as far as the trading UI was concerned: the batch never started,
so there was no `ANALYSIS_STARTED` row, no recommendation, and no order to notice the absence of.

## Root cause: two threads, one lazily-imported alias shim

The Phase 6 migration replaced 90 in-tree modules with **alias shims** that make the in-tree path
the *same module object* as the package implementation:

```python
_pkg = _importlib.import_module("ba2_providers.StockScreener")
_sys.modules[__name__] = _pkg          # <-- the swap
```

Aliasing (rather than `from pkg import *`) is deliberate and correct: it preserves private names
and `__all__`, and it keeps `unittest.mock.patch("ba2_trade_platform...X.name")` and
`inspect.getsource` operating on the real implementation. That part is not the bug.

The bug is what the shim leaves behind. Executing a module body works like this:

1. the import machinery creates an **empty** module object and puts it in
   `sys.modules['ba2_trade_platform.core.StockScreener']`;
2. the body runs — and this body only ever binds `_importlib`, `_sys` and `_pkg` on itself;
3. the last line swaps `sys.modules` to point at the package module instead.

So between (1) and (3) there is a window in which `sys.modules` holds an object that will
**never** have a `StockScreener` attribute — not just "not yet", but *ever*, because the body
never assigns one. A second thread that reaches `from .StockScreener import StockScreener`
inside that window finds the module already present in `sys.modules`, does
`getattr(module, 'StockScreener')`, and raises. Its error message cites the *in-tree* path
(`...\core\StockScreener.py`) rather than the package file — that is the fingerprint of this race.

The prod log shows exactly this, 4 ms apart:

```
15:30:00,063  Executing screener analysis for expert 1
15:30:00,067  Executing screener analysis for expert 6
15:30:00,077  ERROR expert 6: cannot import name 'StockScreener'   <- lost the race
15:30:00,079  StockScreener: stage 1 - screening via 'fmp'          <- expert 1 won
```

Three conditions had to coincide, which is why it fired once in the two months since Phase 6
landed (2026-06-14):

* the import is **lazy** — `_execute_screener_analysis` imports inside the function, so the shim
  body runs for the first time long after startup, on a worker thread rather than the main one;
* **two threads** hit that first import concurrently — both experts are scheduled at the same
  `09:30` market time and `JobManager` dispatches them in parallel;
* it is the **first** such import in the process — prod had restarted at 09:45 that morning.

An eagerly-imported shim is effectively immune (it is imported once, on the main thread, during
startup). Every *lazily*-imported shim is exposed.

## The fix

Populate the shim module with the package's names **before** swapping it out, so a thread that
grabs the original object still resolves the name:

```python
_pkg = _importlib.import_module("ba2_providers.StockScreener")
_modules, _me, _target = _sys.modules, __name__, _pkg
globals().update({k: v for k, v in vars(_pkg).items() if not k.startswith("__")})
_modules[_me] = _target
```

The three locals are captured *before* the `globals().update()` because the update copies the
package's namespace wholesale: a package that happens to bind `_sys` or `_pkg` (e.g. via
`import sys as _sys`) would otherwise rebind the very names the swap depends on.

Dunders are excluded so the shim keeps its own `__name__`, `__file__`, `__spec__` and
`__loader__`; overwriting those would confuse the import machinery mid-import, which is the
opposite of what we want. `__all__` is unaffected in practice because the winning path still
returns the package module object itself.

This does not change the aliasing contract at all. Once the swap completes, every subsequent
importer gets the package module exactly as before; the copied names only matter for a thread
that observed the pre-swap object. Both `tools/make_shims.py` (the generator) and all 90
existing shims carry the fix, so regenerating a shim cannot silently reintroduce the race.

## Why not one of the alternatives

* **Make the imports eager.** Would fix these two call sites and leave the pattern armed for the
  next lazy import anyone adds. The failure mode is a silent skipped trading run; it should not
  depend on import placement.
* **Hold a lock around the shim body.** Python already holds a per-module import lock; the
  problem is not that the body runs twice, it is that the object left in `sys.modules` during the
  body is unusable. Populating it is the direct fix.
* **Drop aliasing for `from pkg import *`.** Loses private names, `__all__` fidelity, and the
  `mock.patch` / `inspect.getsource` behaviour the test suite depends on.

## Detecting a recurrence

The signature is an `ImportError: cannot import name 'X' from '<in-tree path>'` where the cited
file is a shim under `ba2_trade_platform/` and the name exists in the corresponding package
module. If that ever appears again, the shim in question is missing the race guard.
