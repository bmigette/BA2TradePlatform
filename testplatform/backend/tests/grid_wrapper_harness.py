"""Run ``tools/grid_goal2027.sh``'s ``run_step`` under bash with a stub interpreter that prints the
driver argv, one token per line: what the wrapper would launch for a step, without launching it."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SH = os.path.join(_REPO, "tools", "grid_goal2027.sh")

#: The wrapper's own environment variables, cleared so the host's shell cannot leak into a test.
WRAPPER_VARS = ("PHASE", "ESTIMATES_SOURCE", "MC_OHLCV", "MC_TA", "MC_SEN_OHLCV", "MC_SEN_TA", "WORKERS",
                "PARALLEL", "STEPS", "STORE", "PLAN_FILE", "EXCLUDE_FILE", "SENATE_UNIVERSE_FILE",
                "WF_REPORT_DIR", "POPULATION", "WF_FIRST_GENERATIONS", "WF_NEXT_GENERATIONS", "WF_TOP_N",
                "EARLY_STOP", "EARLY_STOP_MIN_REL", "SUFFIX_BASE", "TRAIN_START", "FITNESS", "INTERVAL",
                "DS_WEIGHTS", "G27_MARKET_EXIT", "G27_REGIME_SPLIT", "PASS_DD_MULT", "PASS_MAX_OOS_DD",
                "WF_TEST_YEARS", "PASS_MIN_FOLDS", "PASS_REQUIRE_FOLD", "WF_RESTARTS", "WF_CHAIN_SEED",
                "WF_VERDICT", "PASS_MIN_OOS_SCORE", "WF_MIN_TRAIN_YEARS", "PY", "VENV")

BASE_ENV = {"PHASE": "A", "ESTIMATES_SOURCE": "alphavantage", "MC_OHLCV": "mco", "MC_TA": "mct",
            "STORE": "/store", "WF_REPORT_DIR": "/reports", "PLAN_FILE": "/plan.json",
            "EXCLUDE_FILE": "/excl.txt", "SENATE_UNIVERSE_FILE": "/senate.txt"}


def bash() -> Optional[str]:
    return shutil.which("bash")


def _script(tmp_dir: str, source: str) -> str:
    """The wrapper up to (not including) its command dispatch, preceded by the stub interpreter."""
    body = source[:source.index('\ncase "$CMD" in')]
    path = os.path.join(tmp_dir, "grid_step.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("stubpy() { printf '%s\\n' \"$@\"; }\nPY=stubpy\n" + body
                 + '\nrun_step "$GRID_TEST_STEP" "$GRID_TEST_DRY"\n')
    return path


def run_step(tmp_dir: str, step: str, env: Dict[str, str], dry: str = "dry",
             source: Optional[str] = None) -> Tuple[int, List[str], str]:
    """(exit code, driver argv after the driver path, stderr) of ``run_step <step> <dry>``."""
    if source is None:
        with open(SH, encoding="utf-8") as fh:        # universal newlines: a CRLF checkout works too
            source = fh.read()
    path = _script(tmp_dir, source)
    base = {k: v for k, v in os.environ.items() if k not in WRAPPER_VARS}
    r = subprocess.run([bash(), path], capture_output=True, text=True,
                       env={**base, **BASE_ENV, **env, "GRID_TEST_STEP": step, "GRID_TEST_DRY": dry})
    lines = r.stdout.splitlines()
    try:
        i = lines.index("tools/run_screener_capband_matrix.py")
    except ValueError:
        return r.returncode, [], r.stderr
    end = next(k for k in range(i, len(lines)) if lines[k].startswith("=== step "))
    return r.returncode, lines[i + 1:end], r.stderr
