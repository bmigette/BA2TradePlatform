#!/usr/bin/env python
"""Prepare the data of an OPTION universe for a grid, end to end: ONE command, ONE report.

The stage-2 data repair (2026-09-25/26) as a committed pipeline. Every step is an existing
committed tool run as a subprocess with this interpreter, in this order:

  1 split-check  warm_market_conditions.py plan       split calendars fetched (disk-cached), the
                                                       split-basis check of every FMP daily file
  2 dividends    warm_dividend_calendars.py           the FMP dividend calendars the option basis
                                                       guard's carry separation reads
  3 fmp-repair   warm_market_conditions.py repair     forced full re-fetch of every refused name,
                                                       top-up of missing coverage, the re-check;
                                                       STOPS naming what still refuses
  4 thetadata    warm_options_history.py              ThetaData backfill (--provider thetadata
                 --provider thetadata --wide          --wide): missing and unfinished partitions,
                                                       and every partition a ROOT_HISTORY alias or
                                                       None root makes stale (a whole incomplete
                                                       tree -- the XYZ case -- in one pass)
  5 ds-prewarm   ba2-test prewarm --experts           DeterministicScorer's fmp_history inputs
                 DeterministicScorer
  6 gate-check   option_universe_preflight.py         the screener metric store covers the names
                 --sections gate
  7 manifests    warm_market_conditions.py plan +     both market-condition profiles, built
                 build --cache-only (per profile)     cache-only from the repaired files
  8 preflight    option_universe_preflight.py         coverage, split basis, the REAL guard
                                                       replay, DS inputs, gate -> preflight.json

    python tools/prepare_option_universe.py --universe-file U --start 2020-01-01 \
        --end 2025-12-31 --report-dir <dir> [--steps ...|--from-step thetadata] [--dry-run]

Everything lands in ``--report-dir``: each step's stdout/stderr (``NN-step.out/.err``), the
plans, the build reports, ``preflight.json`` and ``prepare_report.json`` (per step: argv, exit
code, elapsed, parsed result; the manifest digests; what needs a human decision).

WHAT IS AUTOMATIC. The split-basis overrides, ROOT_HISTORY and the option basis guard are code:
they apply whenever data is read or fetched, so nothing here re-implements them. A step that
fails STOPS the pipeline (later steps would work on data known to be wrong); fix and re-run
with ``--from-step <that step>`` -- every tool is resumable/idempotent.

WHAT NEEDS A HUMAN (see docs/RUNBOOK-option-universe-data.md):
  * fmp-repair names a symbol still refused after its re-fetch -> a split-basis override with
    anchors (``ba2_common.core.split_basis_overrides``), ``--keep-newer-cached-tail`` when the
    vendor's history just ends a few bars early (logged; the provider checks it), or an
    exclusion (drop it from the universe, or ``--allow-exclusions`` for the manifests);
  * the preflight's guard refuses a name, or coverage shows a chain under another ticker -> a
    ROOT_HISTORY alias/None root (``ba2_providers.options.thetadata``), then re-run from
    ``thetadata``; or an exclusion;
  * review findings (late first bar, a DS file FMP does not have, a name absent from the gate
    store) -> accept or exclude.

Exit codes: 0 every step passed; 1 a step stopped the pipeline (named in the report and on
stderr); 2 a configuration error. The fmp-repair step refuses while the US session is open
(the re-fetch would store the day's partial bar) unless ``--allow-open-session``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
LAUNCHER = os.path.join(REPO, "testplatform", "ba2test_launcher.py")
STEPS = ("split-check", "dividends", "fmp-repair", "thetadata", "ds-prewarm", "gate-check",
         "manifests", "preflight")
PROFILES = ("ohlcv-v1", "ta-structure-v1")
EXIT_OK, EXIT_STOPPED, EXIT_CONFIG = 0, 1, 2


def _date(text):
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} is not an ISO date: {e}")


def subprocess_runner(argv: List[str], out_path: str, err_path: str) -> int:
    """Run one step; its stdout/stderr go to files, stdin is closed."""
    with open(out_path, "w", encoding="utf-8") as out, open(err_path, "w", encoding="utf-8") as err:
        return subprocess.call(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, cwd=REPO)


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Pipeline:
    def __init__(self, a: argparse.Namespace, runner: Callable[[List[str], str, str], int],
                 log: Callable[[str], None]):
        self.a, self.runner, self.log = a, runner, log
        self.py = a.python or sys.executable
        self.dir = os.path.abspath(a.report_dir)
        self.u = os.path.abspath(a.universe_file)
        self.n = 0
        self.records: List[Dict] = []

    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _call(self, step: str, label: str, argv: List[str]) -> Dict:
        self.n += 1
        stem = self._p(f"{self.n:02d}-{label}")
        rec = {"step": step, "label": label, "argv": argv, "stdout": stem + ".out",
               "stderr": stem + ".err"}
        if self.a.dry_run:
            rec["rc"] = None
            self.log(f"[dry-run] {label}: {' '.join(argv)}")
        else:
            self.log(f"{label}: {' '.join(argv)}")
            t0 = time.time()
            rec["rc"] = self.runner(argv, rec["stdout"], rec["stderr"])
            rec["elapsed_s"] = round(time.time() - t0, 1)
            self.log(f"{label}: exit {rec['rc']} after {rec['elapsed_s']}s")
        self.records.append(rec)
        return rec

    def _tool(self, name: str, *args: str) -> List[str]:
        return [self.py, os.path.join(TOOLS, name), *args]

    def _window(self) -> List[str]:
        return ["--start", self.a.start.isoformat(), "--end", self.a.end.isoformat()]

    # -- steps: each returns None to continue, or the reason the pipeline stops -------------
    def split_check(self) -> Optional[str]:
        r = self._call("split-check", "split-check", self._tool(
            "warm_market_conditions.py", "plan", "--profile", PROFILES[0], "--universe-file", self.u,
            *self._window(), "--out", self._p("plan.split-check.json")))
        if r["rc"] not in (None, 0, 1):          # 1 = an actionable inventory: fmp-repair's job
            return f"split-check exited {r['rc']} (configuration error): see {r['stderr']}"
        out = _read_json(r["stdout"]) or {}
        r["result"] = {"refetch_required": (out.get("plan") or {}).get("refetch_required"),
                       "split_calendar_errors": (out.get("plan") or {}).get("split_calendar_errors")}
        return None

    def dividends(self) -> Optional[str]:
        r = self._call("dividends", "dividends", self._tool(
            "warm_dividend_calendars.py", "--universe-file", self.u))
        r["result"] = _read_json(r["stdout"])
        if r["rc"] not in (None, 0):
            return f"dividends exited {r['rc']}: failed symbols in {r['stdout']}"
        return None

    def fmp_repair(self) -> Optional[str]:
        argv = self._tool("warm_market_conditions.py", "repair", "--plan", self._p("plan.split-check.json"))
        if self.a.keep_newer_cached_tail:
            argv.append("--keep-newer-cached-tail")
        if self.a.allow_open_session:
            argv.append("--allow-open-session")
        r = self._call("fmp-repair", "fmp-repair", argv)
        rep = _read_json(r["stdout"]) or {}
        r["result"] = {k: rep.get(k) for k in ("refetched", "range_fetched", "fetch_errors",
                                               "still_refused", "split_calendar_errors",
                                               "provider_calls", "keep_newer_cached_tail")}
        if r["rc"] == 1:
            return (f"fmp-repair: still refused {sorted(rep.get('still_refused') or {})}, fetch "
                    f"errors {sorted(rep.get('fetch_errors') or {})}, no split calendar "
                    f"{sorted(rep.get('split_calendar_errors') or {})}. Decide per name: an "
                    f"override, --keep-newer-cached-tail, or an exclusion; then --from-step fmp-repair")
        if r["rc"] not in (None, 0):
            return f"fmp-repair exited {r['rc']}: see {r['stderr']}"
        return None

    def thetadata(self) -> Optional[str]:
        argv = self._tool("warm_options_history.py", "--provider", "thetadata", "--wide",
                          "--symbols-file", self.u, "--start", self.a.start.isoformat(),
                          "--rate-limit", "0", "--concurrency", str(self.a.theta_concurrency),
                          "--log-file", self._p("thetadata.log"))
        if self.a.theta_end:
            argv += ["--end", self.a.theta_end.isoformat()]
        if self.a.db:
            argv += ["--db", self.a.db]
        r = self._call("thetadata", "thetadata", argv)
        if r["rc"] not in (None, 0):
            return f"thetadata exited {r['rc']}: re-run --from-step thetadata (resumable); log {self._p('thetadata.log')}"
        return None

    def ds_prewarm(self) -> Optional[str]:
        r = self._call("ds-prewarm", "ds-prewarm", [self.py, LAUNCHER, "prewarm", "--experts",
                                                    "DeterministicScorer", "--symbols", "@" + self.u])
        if r["rc"] not in (None, 0):
            return f"ds-prewarm exited {r['rc']}: see {r['stderr']}"
        return None

    def gate_check(self) -> Optional[str]:
        out = self._p("gate.json")
        r = self._call("gate-check", "gate-check", self._tool(
            "option_universe_preflight.py", "--universe-file", self.u, *self._window(),
            "--sections", "gate", "--out", out))
        g = (_read_json(out) or {}).get("gate") or {}
        r["result"] = {"present": g.get("present"), "absent": g.get("absent")}
        if r["rc"] not in (None, 0):
            return f"gate-check exited {r['rc']}: the screener metric store is missing ({out})"
        return None

    def manifests(self) -> Optional[str]:
        digests = {}
        for prof in self.a.profiles:
            plan = self._p(f"plan.{prof}.json")
            p = self._call("manifests", f"plan-{prof}", self._tool(
                "warm_market_conditions.py", "plan", "--profile", prof, "--universe-file", self.u,
                *self._window(), "--out", plan))
            if p["rc"] not in (None, 0):
                return (f"manifests: the {prof} plan is not buildable cache-only (exit {p['rc']}): "
                        f"see {p['stdout']}. Run --from-step fmp-repair.")
            argv = self._tool("warm_market_conditions.py", "--quiet", "build", "--plan", plan, "--cache-only")
            if self.a.allow_exclusions:
                argv.append("--allow-exclusions")
            b = self._call("manifests", f"build-{prof}", argv)
            rep = _read_json(b["stdout"]) or {}
            b["result"] = {"manifest_digest": rep.get("manifest_digest"), "excluded": rep.get("excluded"),
                           "counters": rep.get("counters")}
            digests[prof] = rep.get("manifest_digest")
            if b["rc"] not in (None, 0):
                return f"manifests: the {prof} build failed (exit {b['rc']}): see {b['stdout']}"
        self.digests = digests
        return None

    def preflight(self) -> Optional[str]:
        argv = self._tool("option_universe_preflight.py", "--universe-file", self.u, *self._window(),
                          "--out", self._p("preflight.json"), "--procs", str(self.a.procs),
                          "--guard-phases", str(self.a.guard_phases))
        if self.a.guard_starts:
            argv += ["--guard-starts", self.a.guard_starts]
        r = self._call("preflight", "preflight", argv)
        rep = _read_json(self._p("preflight.json")) or {}
        r["result"] = rep.get("summary")
        if r["rc"] == 1:
            return (f"preflight: {r['result'] and r['result'].get('blocking')} blocking finding(s) in "
                    f"{self._p('preflight.json')} -- each needs a decision (override, ROOT_HISTORY "
                    f"entry, re-fetch or exclusion)")
        if r["rc"] not in (None, 0):
            return f"preflight exited {r['rc']}: see {r['stderr']}"
        return None

    def run(self, steps: List[str]) -> int:
        os.makedirs(self.dir, exist_ok=True)
        self.digests: Dict[str, Optional[str]] = {}
        stopped = None
        t0 = time.time()
        for step in steps:
            stopped = getattr(self, step.replace("-", "_"))()
            if stopped:
                self.log(f"STOPPED at {step}: {stopped}")
                break
        report = {"universe_file": self.u, "window": [self.a.start.isoformat(), self.a.end.isoformat()],
                  "steps_requested": steps, "dry_run": bool(self.a.dry_run), "steps": self.records,
                  "manifest_digests": self.digests, "stopped": stopped,
                  "elapsed_s": round(time.time() - t0, 1),
                  "finished_at": datetime.now().astimezone().isoformat()}
        with open(self._p("prepare_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, default=str)
        self.log(f"report: {self._p('prepare_report.json')}")
        return EXIT_STOPPED if stopped else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe-file", required=True, help="one symbol per line (# comments)")
    ap.add_argument("--start", required=True, type=_date, help="first decision session of the grid")
    ap.add_argument("--end", required=True, type=_date,
                    help="last decision session of the grid: a COMPLETED session (the manifests' end)")
    ap.add_argument("--theta-end", type=_date, default=None,
                    help="ThetaData window end (default: today, the warm tool's default)")
    ap.add_argument("--report-dir", required=True)
    ap.add_argument("--steps", default=None, help=f"comma subset of {', '.join(STEPS)}, run in pipeline order")
    ap.add_argument("--from-step", default=None, choices=STEPS, help="run this step and every later one")
    ap.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")
    ap.add_argument("--python", default=None, help="interpreter for the steps (default: this one)")
    ap.add_argument("--db", default=None, help="read-only platform DB holding the ThetaData key "
                                               "(else THETADATA_API_KEY)")
    ap.add_argument("--theta-concurrency", type=int, default=1)
    ap.add_argument("--keep-newer-cached-tail", action="store_true",
                    help="fmp-repair: keep the cache's newer bars when the vendor file ends early")
    ap.add_argument("--allow-open-session", action="store_true",
                    help="fmp-repair: re-fetch although the US session is open")
    ap.add_argument("--allow-exclusions", action="store_true",
                    help="manifests: publish with unwarmable symbols recorded as exclusions")
    ap.add_argument("--profiles", default=",".join(PROFILES))
    ap.add_argument("--procs", type=int, default=2, help="preflight worker processes")
    ap.add_argument("--guard-starts", default=None, help="preflight: comma list of guard start dates")
    ap.add_argument("--guard-phases", type=int, default=5)
    return ap


def main(argv=None, *, runner=subprocess_runner, log=None) -> int:
    ap = build_parser()
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_CONFIG if e.code else EXIT_OK
    log = log or (lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}", file=sys.stderr, flush=True))
    if not os.path.isfile(a.universe_file):
        print(f"universe file not found: {a.universe_file}", file=sys.stderr)
        return EXIT_CONFIG
    if a.end < a.start:
        print("--end is before --start", file=sys.stderr)
        return EXIT_CONFIG
    a.profiles = [p.strip() for p in a.profiles.split(",") if p.strip()]
    if a.steps and a.from_step:
        print("pass --steps or --from-step, not both", file=sys.stderr)
        return EXIT_CONFIG
    if a.steps:
        want = [s.strip() for s in a.steps.split(",") if s.strip()]
        bad = [s for s in want if s not in STEPS]
        if bad:
            print(f"unknown step(s) {bad}; steps: {', '.join(STEPS)}", file=sys.stderr)
            return EXIT_CONFIG
        steps = [s for s in STEPS if s in want]
    elif a.from_step:
        steps = list(STEPS[STEPS.index(a.from_step):])
    else:
        steps = list(STEPS)
    if "fmp-repair" in steps and "split-check" not in steps and \
            not os.path.isfile(os.path.join(a.report_dir, "plan.split-check.json")) and not a.dry_run:
        print("fmp-repair needs the split-check plan: run split-check first (or --from-step split-check)",
              file=sys.stderr)
        return EXIT_CONFIG
    return Pipeline(a, runner, log).run(steps)


if __name__ == "__main__":
    sys.exit(main())
