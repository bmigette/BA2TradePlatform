#!/usr/bin/env python
"""Repair / audit the intraday price basis of the OHLCV cache when the vendor cannot re-serve it on the daily basis.

WHY. The vendor's 5-minute endpoint serves old dates AS-TRADED for many symbols while its daily endpoint serves
them split-adjusted, so ``force_full_refetch(<symbol>, '5min')`` can never make the two agree (it now REFUSES with
``IntradayVendorBasisConflict`` and leaves the existing file alone). The cross-interval check measures the
disagreement as clean piecewise-constant factors; ``ba2_providers.ohlcv.intraday_rebase`` turns them into a corrected
frame (prices / factor; volume * factor only for real share splits), and this tool applies it ONCE, with the origin of
the data recorded next to the file (``_split_basis/<stem>.intraday-rebase.json``).

SUBCOMMANDS (every one is a DRY RUN unless ``--apply`` is given; nothing here ever calls a vendor):

  plan     [--symbols A,B|@file | --from-csv basis.csv | --all-mismatched] [--report-dir DIR]
           per symbol: the check's segments, the factor and its SOURCE (split calendar / measured only), the sessions
           and bars affected, the post-rebase check; or the REFUSAL code (noisy / unclean / bad_prints / below_noise /
           implausible / insufficient / ok) so the symbol can be refetched or put on the reviewed exclusion list.
           ``--apply --backup-dir DIR`` (the directory is REQUIRED): backs the file up there, writes the rebased frame
           through the platform writer (``native_cache.write_timeseries``), verifies it with the shared check
           (restoring the backup if it does not come back ok) and writes the provenance sidecar.
  adopt    --notes-dir DIR   files rescaled earlier by an ad-hoc script (one ``<SYM>_5min.json`` note each): when the
           file as it is NOW passes the check, the note becomes the provenance sidecar (``--apply`` writes it).
  markers  --list | --clear A,B [--apply]    list every stale marker with its age and every rebase sidecar, or clear
           markers (e.g. whose file was deleted). NOTE: markers and sidecars live INSIDE the cache tree and are pushed to
           workers by ``cache_sync``; a marker cleared on the master lingers on a worker until the next prune/push, and
           a worker keeps refusing that file until then.

Usage (test venv):
    python tools/repair_intraday_basis.py plan --all-mismatched --report-dir <dir>
    python tools/repair_intraday_basis.py plan --symbols T,WDC --apply --backup-dir <dir>
    python tools/repair_intraday_basis.py adopt --notes-dir <rescale_notes>
    python tools/repair_intraday_basis.py markers --list
"""
import argparse
import csv
import glob
import json
import os
import shutil
import sys
import time
from typing import Dict, List, Optional

_BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testplatform", "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

PROVIDER = "FMPOHLCVProvider"


def _symbols_arg(raw: str) -> List[str]:
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as f:
            items = f.read().replace(",", "\n").splitlines()
    else:
        items = raw.split(",")
    return sorted({t.strip().strip('"').upper() for t in items if t.strip()})


def _select(args, cib) -> List[str]:
    if args.symbols:
        return _symbols_arg(args.symbols)
    if args.from_csv:
        with open(args.from_csv, encoding="utf-8") as f:
            return sorted(r["symbol"] for r in csv.DictReader(f) if r["class"] in cib.MISMATCH_KLASSES)
    if args.all_mismatched:
        folder = cib.ohlcv_cache_dir(args.provider)
        suffix = f"_{args.interval}.parquet"
        syms = sorted(n[:-len(suffix)] for n in os.listdir(folder) if n.endswith(suffix))
        res = cib.check_many(syms, args.interval, *cib.WHOLE_HISTORY, provider=args.provider,
                             workers=args.workers, memo_dir=args.memo_dir)
        return sorted(r.symbol for r in res if r.mismatched)
    raise SystemExit("plan: give --symbols, --from-csv or --all-mismatched")


def cmd_plan(args) -> int:
    import pandas as pd
    from ba2_common.core import native_cache, split_basis
    from ba2_providers.ohlcv import cross_interval_basis as cib
    from ba2_providers.ohlcv import intraday_rebase as rb

    if args.apply and not args.backup_dir:
        raise SystemExit("plan --apply: --backup-dir is REQUIRED (the original file is copied there first)")
    syms = _select(args, cib)
    store = cib.BasisStore(args.provider, memo=None if args.memo_dir == "" else cib.MemoDir(args.memo_dir))
    folder = cib.ohlcv_cache_dir(args.provider)
    rows: List[Dict] = []
    print(f"=== plan: {len(syms)} symbol(s), {args.interval}, {'APPLY' if args.apply else 'DRY RUN'} ===")
    for sym in syms:
        row = {"symbol": sym}
        try:
            res = store.check(sym, args.interval, *cib.WHOLE_HISTORY)
            row["class"] = res.klass
            splits = rb.load_split_calendar(sym)
            plan = rb.plan_rebase(res, splits)
            plan.interval = args.interval
            daily_p, intra_p = cib.symbol_files(sym, args.interval, folder)
            frame = pd.read_parquet(intra_p)
            daily = pd.read_parquet(daily_p)
            rebased = rb.apply_plan(frame, plan)
            post = rb.verify_rebased(sym, args.interval, rebased, daily)
            bars, sessions = rb.count_affected(frame, plan)
            row.update(status="rebasable" if post.klass == cib.KLASS_OK else "refused",
                       code="" if post.klass == cib.KLASS_OK else "post_check_failed",
                       sources="+".join(plan.sources), calendar_known=plan.calendar_known,
                       segments=" | ".join(f"{sg.first_day or '..'}..{sg.last_day or '..'} x{sg.factor:.6g} [{sg.source}"
                                           f"{', volume_unadjusted' if sg.volume_unadjusted else ''}]" for sg in plan.segments),
                       sessions_affected=sessions, bars_affected=bars, post_check=post.klass,
                       post_reason=post.reason, volume_unadjusted=any(sg.volume_unadjusted for sg in plan.segments))
            if args.apply and row["status"] == "rebasable":
                row.update(_apply_one(args, sym, plan, rebased, daily, intra_p, daily_p, post, native_cache, split_basis, rb, cib))
        except rb.IntradayRebaseRefused as e:
            row.update(status="refused", code=e.code, segments=str(e), sources="", post_check="")
        except (OSError, ValueError, KeyError) as e:
            row.update(status="refused", code="error", segments=f"{type(e).__name__}: {e}", sources="", post_check="")
        rows.append(row)
        print(f"  {sym:<8} {row['status']:<10} {row.get('code', ''):<14} {row.get('sources', ''):<26} "
              f"{row.get('sessions_affected', '')!s:>5} sess  post={row.get('post_check', '')}  {row.get('segments', '')[:150]}")
    ok = [r for r in rows if r["status"] == "rebasable"]
    cal = [r for r in ok if r["sources"] == "split_calendar"]
    meas = [r for r in ok if r["sources"] != "split_calendar"]
    refused = [r for r in rows if r["status"] == "refused"]
    codes: Dict[str, int] = {}
    for r in refused:
        codes[r["code"]] = codes.get(r["code"], 0) + 1
    print(f"\nSUMMARY: {len(rows)} symbols; rebasable cleanly {len(ok)} (split-calendar factors only: {len(cal)}, "
          f"measured-only or mixed: {len(meas)}; volume left unadjusted: {sum(1 for r in ok if r['volume_unadjusted'])}); "
          f"cannot be rebased {len(refused)}: {codes}")
    if args.report_dir:
        os.makedirs(args.report_dir, exist_ok=True)
        with open(os.path.join(args.report_dir, "repair_plan.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1, default=str)
        for name, sel in (("rebasable_split_calendar", cal), ("rebasable_measured_only", meas)):
            with open(os.path.join(args.report_dir, name + ".txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(r["symbol"] for r in sel))
        with open(os.path.join(args.report_dir, "cannot_rebase.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "class", "code", "detail"])
            for r in refused:
                w.writerow([r["symbol"], r.get("class", ""), r["code"], r.get("segments", "")])
        print(f"reports -> {args.report_dir}")
    if not args.apply:
        print("DRY RUN: nothing was written. Re-run with --apply --backup-dir <dir> to apply the rebasable ones.")
    return 0


def _apply_one(args, sym, plan, rebased, daily, intra_p, daily_p, post, native_cache, split_basis, rb, cib) -> Dict:
    stem = os.path.basename(intra_p)
    backup_dir = os.path.join(args.backup_dir, args.provider)
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, stem)
    shutil.copy2(intra_p, backup)
    daily_id = cib._identity(daily_p)
    try:
        native_cache.write_timeseries(args.provider, sym, args.interval, rebased, replace_stale=True)
        fresh = cib.BasisStore(args.provider, memo=None).check(sym, args.interval, *cib.WHOLE_HISTORY)
        if fresh.klass != cib.KLASS_OK:
            raise RuntimeError(f"verification after the write failed: {fresh.klass} {fresh.reason}")
    except (RuntimeError, OSError) as e:
        shutil.copy2(backup, intra_p)
        return {"status": "refused", "code": "apply_failed_restored", "post_reason": str(e)}
    prov = rb.provenance_for(plan, daily_identity=daily_id, backup=backup,
                             post_check={"klass": fresh.klass, "common_sessions": fresh.common_sessions})
    split_basis.write_intraday_rebase(intra_p, prov)
    return {"applied": True, "backup": backup}


def cmd_adopt(args) -> int:
    import pandas as pd
    from ba2_common.core import split_basis
    from ba2_providers.ohlcv import cross_interval_basis as cib
    from ba2_providers.ohlcv import intraday_rebase as rb

    notes = sorted(glob.glob(os.path.join(args.notes_dir, f"*_{args.interval}.json")))
    store = cib.BasisStore(args.provider, memo=None if args.memo_dir == "" else cib.MemoDir(args.memo_dir))
    folder = cib.ohlcv_cache_dir(args.provider)
    print(f"=== adopt: {len(notes)} note(s) in {args.notes_dir}, {'APPLY' if args.apply else 'DRY RUN'} ===")
    n_ok = n_no = 0
    for path in notes:
        with open(path, "r", encoding="utf-8") as f:
            note = json.load(f)
        sym = str(note["symbol"]).upper()
        daily_p, intra_p = cib.symbol_files(sym, args.interval, folder)
        if intra_p is None:
            print(f"  {sym:<8} NOT adoptable: no {args.interval} file"); n_no += 1; continue
        res = store.check(sym, args.interval, *cib.WHOLE_HISTORY)
        existing = split_basis.read_intraday_rebase(intra_p)
        if res.klass != cib.KLASS_OK:
            print(f"  {sym:<8} NOT adoptable: the file as it is now is {res.klass} ({res.reason or res.describe()[:100]})")
            n_no += 1
            continue
        prov = rb.provenance_from_note(note, path, post_check={"klass": res.klass, "common_sessions": res.common_sessions},
                                       daily_identity=cib._identity(daily_p))
        flag = " (sidecar already present: would be overwritten)" if existing else ""
        print(f"  {sym:<8} adoptable: {len(prov['segments'])} off-basis segment(s) "
              f"{[(s['first_day'] or '..', s['last_day'] or '..', s['factor'], s['source']) for s in prov['segments']]}{flag}")
        n_ok += 1
        if args.apply:
            split_basis.write_intraday_rebase(intra_p, prov)
    print(f"\nSUMMARY: {n_ok} adoptable, {n_no} not adoptable" + ("" if args.apply else "  (DRY RUN: nothing written; --apply writes the sidecars)"))
    return 0


def cmd_markers(args) -> int:
    from ba2_common.core import native_cache, split_basis
    from ba2_providers.ohlcv import cross_interval_basis as cib
    folder = cib.ohlcv_cache_dir(args.provider)
    if args.clear:
        for sym in _symbols_arg(args.clear):
            for p in native_cache.intraday_paths(args.provider, sym) or []:
                m = split_basis.read_intraday_stale(p)
                if m is None:
                    continue
                print(f"  {os.path.basename(p)}: stale since {m.get('marked_at_utc')} -- {str(m.get('reason'))[:120]}")
                if args.apply:
                    split_basis.clear_intraday_stale(p)
                    print("    CLEARED (on this machine only: a worker keeps its copy until its next cache push/prune)")
        if not args.apply:
            print("DRY RUN: pass --apply to clear.")
        return 0
    st = split_basis.list_intraday_states(folder)
    print(f"=== {folder} ===")
    print(f"stale markers: {len(st['stale'])}")
    for r in st["stale"]:
        print(f"  {r['file']:<22} age {r['age_days']} d   {str(r['reason'])[:140]}")
    print(f"rebased files (provenance sidecars): {len(st['rebased'])}")
    for r in st["rebased"]:
        print(f"  {r['file']:<22} {r['applied_utc']}  source={r['source']}  volume_unadjusted={r['volume_unadjusted']}")
    print("NOTE: these files live inside the cache tree and are pushed to workers by cache_sync; clearing one here does "
          "not clear it on a worker until that worker's next push/prune.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--provider", default=PROVIDER)
    common.add_argument("--interval", default="5min")
    common.add_argument("--memo-dir", default=None, help="scan memo dir ('' = none; default next to the cache)")
    common.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 1)))
    common.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p = sub.add_parser("plan", parents=[common])
    p.add_argument("--symbols"); p.add_argument("--from-csv"); p.add_argument("--all-mismatched", action="store_true")
    p.add_argument("--report-dir"); p.add_argument("--backup-dir")
    a = sub.add_parser("adopt", parents=[common])
    a.add_argument("--notes-dir", required=True)
    m = sub.add_parser("markers", parents=[common])
    m.add_argument("--list", action="store_true"); m.add_argument("--clear")
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "adopt": cmd_adopt, "markers": cmd_markers}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
