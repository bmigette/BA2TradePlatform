#!/usr/bin/env python
"""BA2 trade platform daily report generator (reusable ops tooling).

Reads a BA2 trade platform sqlite DB (read-only) and prints a plain-text
summary to stdout. Designed to be run ad-hoc or from Hermes cron (no_agent).

Timestamps in the DB are stored naive **UTC** (verified: recommendation at
13:30:06 UTC == 09:30:06 ET market open). "Today" is defined on the
America/New_York trading day and converted back to UTC for queries.

Usage:
  python tools/trade_report.py --mode morning            # prod: open trades, today's closes + realized P&L, top5 analyses
  python tools/trade_report.py --mode evening            # dev: daily delta per expert
  python tools/trade_report.py --mode morning --date 2026-08-27 --top 5
  python tools/trade_report.py --auto                    # cron entrypoint: picks mode by current ET time

Exit code 0 with empty output when there is nothing to report (cron-safe).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DEFAULT_DBS = {
    "prod": r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite",
    "dev": r"C:\Users\basti\Documents\ba2\trade\db.sqlite",
}

MORNING_WINDOW = (10, 30)  # 1h after open -> prod report
EVENING_WINDOW = (15, 30)  # 30min before close -> dev report
WINDOW_MINUTES = 14


def connect(db_path: str) -> sqlite3.Connection:
    # read-only URI so we never block/lock the live platform DB (WAL mode)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def et_day_bounds_utc(day: str | None) -> tuple[str, str, str]:
    """Return (day_label, start_utc, end_utc) for one ET trading day."""
    d = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now(ET).date()
    start_et = datetime(d.year, d.month, d.day, 0, 0, tzinfo=ET)
    end_et = start_et + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return d.isoformat(), start_et.astimezone(timezone.utc).strftime(fmt), end_et.astimezone(timezone.utc).strftime(fmt)


def experts_map(con) -> dict[int, str]:
    return {r["id"]: (r["alias"] or r["expert"]) for r in con.execute("select id, expert, alias from expertinstance")}


def realized_pl(row) -> float:
    qty = row["quantity"] or 0.0
    o, c = row["open_price"] or 0.0, row["close_price"] or 0.0
    mult = row["multiplier"] or 1
    sign = 1.0 if (row["side"] or "BUY").upper() == "BUY" else -1.0
    return sign * (c - o) * qty * mult


def fmt_pl(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:,.2f}$"


def morning_report(db: str, day: str | None, top_n: int) -> str:
    con = connect(db)
    experts = experts_map(con)
    label, start_utc, end_utc = et_day_bounds_utc(day)

    out = [f"🌅 BA2 PROD — morning report {label} (ET day, 10:30 ET)"]

    # 1) open trades
    open_rows = list(con.execute(
        'select * from "transaction" where status=\'OPENED\' order by open_date'))
    if open_rows:
        out.append(f"\n📂 Open trades ({len(open_rows)}):")
        for r in open_rows:
            out.append(f"  • {r['symbol']} {r['side']} x{r['quantity']:g} @ {r['open_price']:.2f} — {experts.get(r['expert_id'], r['expert_id'])}")
    else:
        out.append("\n📂 Open trades: none")

    # 2) closed today + realized P&L
    closed = list(con.execute(
        'select * from "transaction" where status=\'CLOSED\' and close_date >= ? and close_date < ? order by close_date',
        (start_utc, end_utc)))
    total = 0.0
    if closed:
        out.append(f"\n✅ Closed today ({len(closed)}):")
        for r in closed:
            pl = realized_pl(r)
            total += pl
            out.append(f"  • {r['symbol']} {r['side']} {r['open_price']:.2f}→{r['close_price']:.2f} = {fmt_pl(pl)} ({r['close_reason']}) — {experts.get(r['expert_id'], r['expert_id'])}")
    else:
        out.append("\n✅ Closed today: none")
    out.append(f"\n💰 Realized P&L today: {fmt_pl(total)}")

    # 3) top analyses by expected profit (today's recommendations)
    recs = list(con.execute(
        """select er.*, ei.alias, ei.expert from expertrecommendation er
           left join expertinstance ei on ei.id = er.instance_id
           where er.created_at >= ? and er.created_at < ?
             and er.recommended_action in ('BUY','OVERWEIGHT')
           order by er.expected_profit_percent desc""", (start_utc, end_utc)))
    out.append(f"\n🔭 Top {top_n} analyses by expected profit (today):")
    if recs:
        for r in recs[:top_n]:
            who = r["alias"] or r["expert"] or "?"
            out.append(f"  • {r['symbol']} {r['recommended_action']} exp +{r['expected_profit_percent']:.1f}% "
                       f"(conf {r['confidence']:.0f}%, {r['time_horizon']}) — {who}")
    else:
        out.append("  (no BUY/OVERWEIGHT recommendations today)")
    con.close()
    return "\n".join(out)


def evening_report(db: str, day: str | None) -> str:
    con = connect(db)
    experts = experts_map(con)
    label, start_utc, end_utc = et_day_bounds_utc(day)

    out = [f"🌆 BA2 DEV — evening report {label} (daily delta per expert, 15:30 ET)"]

    per: dict[str, dict] = {}

    def slot(eid) -> dict:
        name = experts.get(eid, f"expert#{eid}")
        return per.setdefault(name, {"recs": {}, "opened": 0, "closed": 0, "pl": 0.0})

    for r in con.execute(
            "select instance_id, recommended_action, count(*) n from expertrecommendation "
            "where created_at >= ? and created_at < ? group by instance_id, recommended_action",
            (start_utc, end_utc)):
        s = slot(r["instance_id"])
        s["recs"][r["recommended_action"]] = s["recs"].get(r["recommended_action"], 0) + r["n"]

    for r in con.execute(
            'select expert_id, count(*) n from "transaction" where status=\'OPENED\' '
            "and open_date >= ? and open_date < ? group by expert_id", (start_utc, end_utc)):
        slot(r["expert_id"])["opened"] += r["n"]

    for r in con.execute(
            'select * from "transaction" where status=\'CLOSED\' and close_date >= ? and close_date < ?',
            (start_utc, end_utc)):
        s = slot(r["expert_id"])
        s["closed"] += 1
        s["pl"] += realized_pl(r)

    if not per:
        out.append("\n(no activity today on dev fleet)")
    else:
        for name in sorted(per):
            s = per[name]
            recs = ", ".join(f"{k}:{v}" for k, v in sorted(s["recs"].items())) or "no recs"
            bits = [recs]
            if s["opened"]:
                bits.append(f"opened {s['opened']}")
            if s["closed"]:
                bits.append(f"closed {s['closed']} ({fmt_pl(s['pl'])})")
            out.append(f"  • {name}: " + " | ".join(bits))

        grand = sum(s["pl"] for s in per.values())
        n_closed = sum(s["closed"] for s in per.values())
        n_opened = sum(s["opened"] for s in per.values())
        out.append(f"\nΣ {len(per)} experts active | opened {n_opened} | closed {n_closed} | realized {fmt_pl(grand)}")
    con.close()
    return "\n".join(out)


def auto() -> str:
    """Cron entrypoint: report only when inside an ET reporting window."""
    now = datetime.now(ET)
    if now.weekday() >= 5:  # Sat/Sun: markets closed
        return ""
    minutes = now.hour * 60 + now.minute
    day = now.date().isoformat()
    for (h, m), mode, dbkey in ((MORNING_WINDOW, "morning", "prod"), (EVENING_WINDOW, "evening", "dev")):
        if 0 <= minutes - (h * 60 + m) < WINDOW_MINUTES:
            return morning_report(DEFAULT_DBS[dbkey], day, 5) if mode == "morning" else evening_report(DEFAULT_DBS[dbkey], day)
    return ""


def main() -> int:
    p = argparse.ArgumentParser(description="BA2 daily trade report")
    p.add_argument("--mode", choices=["morning", "evening", "auto"])
    p.add_argument("--db", help="sqlite path (defaults: prod for morning, dev for evening)")
    p.add_argument("--date", help="ET day YYYY-MM-DD (default: today)")
    p.add_argument("--top", type=int, default=5)
    a = p.parse_args()

    if a.mode == "auto" or not a.mode:
        text = auto()
    else:
        db = a.db or DEFAULT_DBS["prod" if a.mode == "morning" else "dev"]
        text = morning_report(db, a.date, a.top) if a.mode == "morning" else evening_report(db, a.date)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
