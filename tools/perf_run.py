"""End-to-end PERFORMANCE measurement of one stored-row re-run (in memory, no DB row).

    python tools/perf_run.py <row> --window START END --out RESULT.json [--decision-time HH:MM] [--profile TXT]

Works on the pre-fix checkout and on the fixed tip (copy this file into the checkout's ``tools/``): it only
wraps ``tools/rerun_stored_row.py``'s ``main`` and the engine's ``resolve_universe``. Records, for ONE run:
wall seconds, CPU seconds (user + sys of the process), peak RSS (MB), the number of STEPPED ticks (bars the
engine visited), ``tick_x_symbol`` (sum over ticks of the decidable universe size), the number of trades,
and the summary figures. ``--profile`` also writes the top 40 functions by cumulative time (cProfile; its
overhead makes the wall/CPU of THAT run unusable, the figures of the unprofiled runs are the ones to read).
"""
import argparse
import json
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

ap = argparse.ArgumentParser()
ap.add_argument("backtest_id", type=int)
ap.add_argument("--window", nargs=2, required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--decision-time")
ap.add_argument("--profile")
ns = ap.parse_args()

argv = ["rerun_stored_row.py", str(ns.backtest_id), "--window", *ns.window, "--out", ns.out + ".rerun.json"]
if ns.decision_time:
    argv += ["--decision-time", ns.decision_time]
sys.argv = argv
import rerun_stored_row as R  # noqa: E402  (enters the backend)
from app.services.backtest import daily_engine as DE  # noqa: E402

counts = {"ticks": 0, "tick_x_symbol": 0}
_orig_resolve = DE.resolve_universe


def _counting(as_of, config, price_source):
    out = _orig_resolve(as_of, config, price_source)
    counts["ticks"] += 1
    counts["tick_x_symbol"] += len(out)
    return out


DE.resolve_universe = _counting

prof = None
if ns.profile:
    import cProfile
    prof = cProfile.Profile()
    prof.enable()
t0, c0 = time.perf_counter(), time.process_time()
rc = R.main()
wall, cpu = time.perf_counter() - t0, time.process_time() - c0
if prof is not None:
    import io
    import pstats
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(40)
    Path(ns.profile).write_text(buf.getvalue())
ru = resource.getrusage(resource.RUSAGE_SELF)
rerun = {}
try:
    rerun = json.loads(Path(ns.out + ".rerun.json").read_text()).get("rerun", {})
except Exception:  # noqa: BLE001
    pass
res = {"row": ns.backtest_id, "window": ns.window, "decision_time": ns.decision_time, "rc": rc,
       "wall_s": round(wall, 2), "cpu_s": round(cpu, 2),
       "peak_rss_mb": round(ru.ru_maxrss / 1024.0, 1),      # Linux: KB; the server is Linux
       **counts, "trades": rerun.get("total_trades"), "total_return": rerun.get("total_return"),
       "cpu_us_per_tick_symbol": (round(cpu / counts["tick_x_symbol"] * 1e6, 3) if counts["tick_x_symbol"] else None),
       "cpu_ms_per_tick": (round(cpu / counts["ticks"] * 1e3, 3) if counts["ticks"] else None)}
Path(ns.out).write_text(json.dumps(res, indent=1))
print(json.dumps(res))
sys.exit(rc)
