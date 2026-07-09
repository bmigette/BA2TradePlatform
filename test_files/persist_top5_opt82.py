"""One-off: persist the TOP-5 backtests for opt 82 (scr-large-FMPRating-S1-goal5), whose
persist phase never ran because the job's CLI process hung at pool shutdown after the
remote-worker BrokenProcessPool cascade (2026-07-09). Real script file (not stdin) because
Windows spawn children must be able to re-import __main__.

Run (test venv, from testplatform/backend):
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe ../../test_files/persist_top5_opt82.py
"""
import importlib.util
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_REPO, "testplatform", "backend")
sys.path.insert(0, _BACKEND)
os.chdir(_BACKEND)


def main() -> int:
    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db")
    if not os.getenv("FMP_API_KEY"):
        from ba2_common.config import get_app_setting
        k = get_app_setting("FMP_API_KEY")
        if k:
            os.environ["FMP_API_KEY"] = k

    spec = importlib.util.spec_from_file_location(
        "ba2test_launcher", os.path.join(_REPO, "testplatform", "ba2test_launcher.py"))
    launcher = importlib.util.module_from_spec(spec)
    sys.modules["ba2test_launcher"] = launcher
    spec.loader.exec_module(launcher)

    n = launcher._persist_top_backtests(82, "FMPRating", n=5, parallel=2)
    print(f"PERSISTED {n} of 5 for opt 82")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
