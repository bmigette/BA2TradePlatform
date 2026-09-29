"""Capture the live app's expert_batch export (every instance) and the Performance page's
per-expert and monthly metrics from a COPY of a live trade DB. Run before and after a refactor
of those code paths, then diff. The tool writes to its copy (WAL); never point it at a real DB
(it refuses <BA2_HOME>/trade/db.sqlite, and ~/Documents/ba2/trade/db.sqlite).

Usage:
    cp /tmp/ba2-backups/dev_2026-09-28.sqlite /tmp/ba2-live-copy.sqlite
    BA2_HOME=/tmp/ba2-backups/home DB_FILE=/tmp/ba2-live-copy.sqlite \
      ~/ba2-venvs/trade/bin/python tools/live_export_capture.py /tmp/out.json
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# THIS checkout's code first: the venv's editable installs may point at another checkout.
for p in (*(os.path.join(REPO, "packages", n) for n in ("experts", "providers", "common")), REPO):
    if p not in sys.path:
        sys.path.insert(0, p)


def _refuse_real_db(path: str, rel: str) -> None:
    """Exit if ``path`` is the REAL default DB ``<root>/<rel>``, for root = ~/Documents/ba2 and,
    when set, $BA2_HOME. Compared after resolving symlinks (and by inode when both exist)."""
    roots = [os.path.join(os.path.expanduser("~"), "Documents", "ba2")]
    if os.environ.get("BA2_HOME"):
        roots.append(os.environ["BA2_HOME"])
    target = os.path.realpath(os.path.expanduser(path))
    for root in roots:
        real = os.path.realpath(os.path.join(os.path.expanduser(root), rel))
        if target == real or (os.path.exists(target) and os.path.exists(real)
                              and os.path.samefile(target, real)):
            sys.exit(f"Refusing to run against the real DB {real}: point the tool at a COPY")


DB = os.environ.get("DB_FILE")
if not DB:
    sys.exit("Set DB_FILE to a COPY of a live trade DB")
_refuse_real_db(DB, os.path.join("trade", "db.sqlite"))

from ba2_trade_platform.core.seam_wiring import wire_all_seams  # noqa: E402
wire_all_seams()
from ba2_common.core import db  # noqa: E402
db.configure_db(DB)

import ba2_common  # noqa: E402
from sqlmodel import select  # noqa: E402
from ba2_trade_platform.core.expert_batch_export_import import build_batch_export  # noqa: E402
from ba2_trade_platform.core.models import ExpertInstance, Transaction  # noqa: E402
from ba2_trade_platform.core.types import TransactionStatus  # noqa: E402
from ba2_trade_platform.ui.pages.performance import PerformanceTab  # noqa: E402

_NORMALIZED = "<normalized>"


def main() -> int:
    print(f"ba2_common from {ba2_common.__file__}")
    with db.get_db() as s:
        ids = [e.id for e in s.exec(select(ExpertInstance).order_by(ExpertInstance.id)).all()]
        closed = s.exec(select(Transaction).where(Transaction.status == TransactionStatus.CLOSED)
                        .order_by(Transaction.id)).all()
    batch = build_batch_export(ids)
    batch["export_timestamp"] = _NORMALIZED
    for entry in batch["experts"]:
        if entry.get("rulesets"):
            entry["rulesets"]["export_timestamp"] = _NORMALIZED
    tab = PerformanceTab(None)
    metrics = tab._calculate_transaction_metrics(closed)
    for m in metrics.values():
        m.pop("transactions", None)
    monthly = {month: {name: dict(v) for name, v in per.items()}
               for month, per in tab._calculate_monthly_metrics(closed).items()}
    with open(sys.argv[1], "w") as f:
        json.dump({"batch": batch, "metrics": metrics, "monthly": monthly}, f, indent=1,
                  default=str)
    print(f"captured {len(ids)} experts, {len(closed)} closed transactions -> {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
