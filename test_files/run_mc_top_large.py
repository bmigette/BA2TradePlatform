"""Ad-hoc: run Monte Carlo robustness over the two best -goal large-cap backtests
(scr-large-FMPRating-S2-goal TOP1, scr-large-FMPRating-S4-goal TOP1) directly via
robustness_handler, without needing the FastAPI server up. Prints the persisted
percentile bands + drop-K table for each."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "testplatform", "backend"))

from app.models.backtest import Backtest, RobustnessRun  # noqa: E402
from app.models.database import SessionLocal  # noqa: E402
from app.services import robustness_handler  # noqa: E402

BACKTEST_IDS = {
    "scr-large-FMPRating-S2-goal (TOP1)": 91,
    "scr-large-FMPRating-S4-goal (TOP1)": 99,
}

MC_CFG = {
    "n_paths": 2000,
    "seed": 42,
    "methods": ["bootstrap", "shuffle", "jitter"],
    "drop_k": [1, 2, 3],
    "jitter_bp": 5.0,
}


def main():
    db = SessionLocal()
    try:
        for label, bid in BACKTEST_IDS.items():
            bt = db.query(Backtest).filter(Backtest.id == bid).first()
            if bt is None:
                print(f"{label}: backtest {bid} NOT FOUND")
                continue
            run = RobustnessRun(backtest_id=bid, kind="monte_carlo", params=MC_CFG, status="pending")
            db.add(run)
            db.commit()
            db.refresh(run)
            robustness_handler.run_monte_carlo_for_backtest(run.id)
            db.refresh(run)
            print(f"\n=== {label} (backtest_id={bid}, run_id={run.id}) status={run.status} ===")
            if run.status != "completed":
                print("error:", run.error_message)
                continue
            print(json.dumps(run.results, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
