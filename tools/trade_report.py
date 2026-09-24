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
  python tools/trade_report.py --mode close              # prod: market-close summary (open book + today's closes + realized P&L)
  python tools/trade_report.py --mode morning --date 2026-08-27 --top 5
  python tools/trade_report.py --auto                    # cron entrypoint: picks mode by current ET time

Exit code 0 with empty output when there is nothing to report (cron-safe).
"""
from __future__ import annotations

import argparse
import json
import os
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
CLOSE_WINDOW = (16, 5)     # just after the 16:00 ET close -> prod close summary
# NOTE: the cron tick grid is */10, so the close report effectively fires on the first
# tick at/after 16:05 ET (16:10 ET). That lag is deliberate: it lets the engine write its
# end-of-day closes before we read the book.
WINDOW_MINUTES = 30        # wide window: survives a missed tick; dedupe keeps output once/day
STATE_FILE = os.path.join(os.environ.get("LOCALAPPDATA", "."), "hermes", "state", "ba2_trade_report_last.json")


def _claim(mode: str, day: str) -> bool:
    """Return True only for the first call per (mode, day) — dedupes cron ticks.

    Keeps a per-mode map so the morning/evening/close reports cannot clobber each
    other's claim; the legacy single "last" key is still written for compatibility.
    """
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    days = state.get("days") if isinstance(state.get("days"), dict) else {}
    if days.get(mode) == day:
        return False
    days[mode] = day
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last": f"{mode}:{day}", "days": days}, f)
    return True


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
    return {r["id"]: (r["alias"] or r["expert"] or f"expert#{r['id']}") for r in con.execute("select id, expert, alias from expertinstance")}


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

    # 1) open book — AGGREGATE ONLY. User, 2026-09-21: "remove open trades from report? Too many..."
    #    The per-trade listing reached 80 lines and flooded the channel; the count, the exposure and
    #    the per-expert split carry the same signal in two lines.
    def notional_of(r) -> float:
        return abs((r["open_price"] or 0.0) * (r["quantity"] or 0.0) * (r["multiplier"] or 1))

    def who_of(r) -> str:
        return experts.get(r["expert_id"]) or ("unassigned" if r["expert_id"] is None else f"expert#{r['expert_id']}")

    open_rows = list(con.execute(
        'select * from "transaction" where status=\'OPENED\' order by open_date'))
    if open_rows:
        notional = sum(notional_of(r) for r in open_rows)
        longs = sum(1 for r in open_rows if (r["side"] or "BUY").upper() == "BUY")
        out.append(f"\n📂 Open book: {len(open_rows)} position(s) "
                   f"({longs} long / {len(open_rows) - longs} short, exposure ≈ {notional:,.0f}$)")
        per_expert: dict[str, int] = {}
        for r in open_rows:
            per_expert[who_of(r)] = per_expert.get(who_of(r), 0) + 1
        top = sorted(per_expert.items(), key=lambda kv: -kv[1])[:3]
        out.append("   by expert: " + ", ".join(f"{k} {n}" for k, n in top)
                   + (f" (+{len(per_expert) - len(top)} more)" if len(per_expert) > len(top) else ""))
    else:
        out.append("\n📂 Open book: none")

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


def _book_snapshot(con) -> dict[str, dict]:
    """Latest broker position snapshot keyed by symbol (may be empty between syncs)."""
    try:
        rows = list(con.execute("select * from position"))
    except sqlite3.Error:
        return {}
    return {r["symbol"]: dict(r) for r in rows}


def close_report(db: str, day: str | None) -> str:
    """Market-close summary for the PROD book: the open book (aggregated), the day's
    closes with realized P&L, and unrealized P&L when the broker snapshot is populated.

    Deliberately excludes the recommendations / market-analysis section and the dev
    instance: this is the end-of-day book summary, not an ideas report.
    """
    con = connect(db)
    experts = experts_map(con)
    label, start_utc, end_utc = et_day_bounds_utc(day)
    book = _book_snapshot(con)

    out = [f"🔔 BA2 PROD — close report {label} (ET trading day, 16:00 ET close)"]

    def notional_of(r) -> float:
        return abs((r["open_price"] or 0.0) * (r["quantity"] or 0.0) * (r["multiplier"] or 1))

    def who_of(r) -> str:
        return experts.get(r["expert_id"]) or ("unassigned" if r["expert_id"] is None else f"expert#{r['expert_id']}")

    # 1) the open book carried into the close — aggregated: 80+ positions would flood the channel
    open_rows = list(con.execute(
        'select * from "transaction" where status=\'OPENED\' and open_date < ? order by open_date',
        (end_utc,)))
    longs = sum(1 for r in open_rows if (r["side"] or "BUY").upper() == "BUY")
    notional = sum(notional_of(r) for r in open_rows)
    unreal_total = 0.0
    unreal_seen = False
    if open_rows:
        out.append(f"\n📂 OPEN AT CLOSE — {len(open_rows)} positions "
                   f"({longs} long / {len(open_rows) - longs} short, exposure ≈ {notional:,.0f}$)")
        per_expert: dict[str, list] = {}
        for r in open_rows:
            slot = per_expert.setdefault(who_of(r), [0, 0.0])
            slot[0] += 1
            slot[1] += notional_of(r)
        out.append("   by expert: " + " | ".join(
            f"{who} {n} ({v:,.0f}$)" for who, (n, v) in sorted(per_expert.items(), key=lambda kv: -kv[1][1])))
        # NO per-position listing. User, 2026-09-21: "remove open trades from report? Too many..."
        # The unrealized P&L still lands in the day's numbers below, now summed over EVERY open
        # position instead of only the handful that used to be listed.
        for r in open_rows:
            pos = book.get(r["symbol"])
            if pos and pos.get("unrealized_pl") is not None:
                unreal_seen = True
                unreal_total += pos["unrealized_pl"]
    else:
        out.append("\n📂 OPEN AT CLOSE — none")

    # 2) everything closed during the ET day + realized P&L
    closed = list(con.execute(
        'select * from "transaction" where status=\'CLOSED\' and close_date >= ? and close_date < ? order by close_date',
        (start_utc, end_utc)))
    total = 0.0
    wins = losses = 0
    best = worst = None
    per_expert_pl: dict[str, list] = {}
    for r in closed:
        pl = realized_pl(r)
        total += pl
        if pl > 0:
            wins += 1
        elif pl < 0:
            losses += 1
        if best is None or pl > best[1]:
            best = (r, pl)
        if worst is None or pl < worst[1]:
            worst = (r, pl)
        slot = per_expert_pl.setdefault(who_of(r), [0, 0.0])
        slot[0] += 1
        slot[1] += pl
    if closed:
        out.append(f"\n✅ CLOSED TODAY — {len(closed)} ({wins} win / {losses} loss / "
                   f"{len(closed) - wins - losses} flat)")
        for r in closed[:15]:
            pl = realized_pl(r)
            reason = f" ({r['close_reason']})" if r["close_reason"] else ""
            out.append(f"  • {r['symbol']} {r['side']} {(r['open_price'] or 0):.2f}→{(r['close_price'] or 0):.2f} "
                       f"= {fmt_pl(pl)}{reason} — {who_of(r)}")
        if len(closed) > 15:
            out.append(f"   … +{len(closed) - 15} more")
        if len(per_expert_pl) > 1:
            out.append("   by expert: " + " | ".join(
                f"{who} {n} {fmt_pl(v)}" for who, (n, v) in sorted(per_expert_pl.items(), key=lambda kv: -kv[1][1])))
    else:
        out.append("\n✅ CLOSED TODAY — none")

    # 3) the day's numbers
    out.append(f"\n💰 Realized P&L today: {fmt_pl(total)}")
    if closed:
        out.append(f"   best: {best[0]['symbol']} {fmt_pl(best[1])} | worst: {worst[0]['symbol']} {fmt_pl(worst[1])}")
    if unreal_seen:
        out.append(f"📈 Unrealized P&L (broker snapshot): {fmt_pl(unreal_total)}")
    out.append(f"📌 Book at close: {len(open_rows)} open / {len(closed)} closed today | exposure ≈ {notional:,.0f}$")
    con.close()
    return "\n".join(out)


def auto(now: datetime | None = None) -> str:
    """Cron entrypoint: report only when inside an ET reporting window.

    `now` may be injected for tests; production callers omit it.
    """
    now = now or datetime.now(ET)
    if now.weekday() >= 5:  # Sat/Sun: markets closed
        return ""
    minutes = now.hour * 60 + now.minute
    day = now.date().isoformat()
    for (h, m), mode, dbkey in ((MORNING_WINDOW, "morning", "prod"),
                                (EVENING_WINDOW, "evening", "dev"),
                                (CLOSE_WINDOW, "close", "prod")):
        if 0 <= minutes - (h * 60 + m) < WINDOW_MINUTES and _claim(mode, day):
            if mode == "morning":
                return morning_report(DEFAULT_DBS[dbkey], day, 5)
            if mode == "evening":
                return evening_report(DEFAULT_DBS[dbkey], day)
            return close_report(DEFAULT_DBS[dbkey], day)
    return ""


def main() -> int:
    p = argparse.ArgumentParser(description="BA2 daily trade report")
    p.add_argument("--mode", choices=["morning", "evening", "close", "auto"])
    p.add_argument("--db", help="sqlite path (defaults: prod for morning/close, dev for evening)")
    p.add_argument("--date", help="ET day YYYY-MM-DD (default: today)")
    p.add_argument("--top", type=int, default=5)
    a = p.parse_args()

    if a.mode == "auto" or not a.mode:
        text = auto()
    else:
        db = a.db or DEFAULT_DBS["prod" if a.mode in ("morning", "close") else "dev"]
        if a.mode == "morning":
            text = morning_report(db, a.date, a.top)
        elif a.mode == "evening":
            text = evening_report(db, a.date)
        else:
            text = close_report(db, a.date)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
