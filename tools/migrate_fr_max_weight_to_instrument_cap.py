"""One-shot migration: FactorRanker's retired ``max_weight_per_name`` (fraction 0-1,
FactorRanker-only) -> the platform-wide ``max_virtual_equity_per_instrument_percent``
(percent, shared with every other expert) it was unified into on 2026-09-29. See
``ba2_experts.FactorRanker._refuse_legacy_max_weight_per_name`` (the live/backtest code now
refuses to run with a stored ``max_weight_per_name`` -- this script is how an existing
instance gets out of that state) and
``AccountInterface._validate_position_size_limits`` (the account-level cap check that now
honours the declared default instead of silently skipping an unstored instrument cap).

For every ``ExpertInstance`` with ``expert == 'FactorRanker'`` that has a stored
``max_weight_per_name`` row:

  - no stored ``max_virtual_equity_per_instrument_percent`` row -> writes
    ``round(max_weight_per_name * 100, 1)`` through the platform settings save path
    (``FactorRanker(...).save_settings`` -- the same mechanism
    ``tools/import_deploy_payload.py`` uses, never raw SQL) and deletes the old row.
  - a stored ``max_virtual_equity_per_instrument_percent`` row that AGREES (within
    rounding) -> just deletes the old row; nothing to write.
  - a stored ``max_virtual_equity_per_instrument_percent`` row that DISAGREES -> REFUSES
    that instance (named in the report, both values printed) and the script exits
    non-zero. Every OTHER instance is still migrated normally.

Defaults to a DRY RUN: prints the planned before/after table and writes nothing. Pass
``--apply`` to actually write. Never run against a live DB without first testing on a copy.

Usage:
  python tools/migrate_fr_max_weight_to_instrument_cap.py --db-file PATH [--apply]
"""
import argparse
import os
import sys

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
for _p in (REPO, os.path.join(REPO, "packages", "experts"), os.path.join(REPO, "packages", "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Stored values are round(x, 1); this tolerance absorbs that rounding when comparing a
#: stored max_virtual_equity_per_instrument_percent against max_weight_per_name * 100.
_TOLERANCE_PCT = 0.05


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-file", required=True, help="SQLite db file to migrate")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the planned migration only; write nothing (default)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write the migration (default is dry run)")
    return parser.parse_args()


def _delete_legacy_row(instance_id: int) -> bool:
    """Delete the stored ``max_weight_per_name`` ``ExpertSetting`` row for one instance.

    An ORM delete through a session -- the same session-scoped delete
    ``ExtendableSettingsInterface.reset_settings`` uses for a whole-instance reset -- never
    raw SQL. Returns True iff a row existed and was deleted.
    """
    from sqlmodel import select
    from ba2_common.core.db import get_db
    from ba2_common.core.models import ExpertSetting

    with get_db() as session:
        rows = session.exec(
            select(ExpertSetting).where(
                ExpertSetting.instance_id == instance_id,
                ExpertSetting.key == "max_weight_per_name",
            )
        ).all()
        for row in rows:
            session.delete(row)
        session.commit()
        return bool(rows)


def main() -> int:
    args = _parse_args()
    apply = bool(args.apply)

    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(args.db_file)

    from sqlmodel import select
    from ba2_common.core.db import get_db
    from ba2_common.core.models import ExpertInstance
    from ba2_experts.FactorRanker import FactorRanker

    with get_db() as session:
        instances = session.exec(
            select(ExpertInstance).where(ExpertInstance.expert == "FactorRanker")
        ).all()
        # (id, alias) only -- the row objects are detached the moment this session closes,
        # and every settings read/write below opens its own session anyway.
        rows = [(inst.id, inst.alias) for inst in instances]

    if not rows:
        print("No FactorRanker instances found.")
        return 0

    print(f"{'id':>4}  {'alias':<24}  {'legacy':>10}  {'stored cap%':>12}  "
          f"{'final cap%':>11}  action")
    print("-" * 100)

    conflicts = []
    migrated = 0
    for inst_id, alias in rows:
        expert = FactorRanker(inst_id)
        settings = expert.settings
        legacy = settings.get("max_weight_per_name")
        stored_cap = settings.get("max_virtual_equity_per_instrument_percent")
        alias_disp = alias or f"id={inst_id}"

        if legacy is None:
            continue  # this instance never had the retired setting -- nothing to do

        try:
            derived_cap = round(float(legacy) * 100.0, 1)
        except (TypeError, ValueError):
            # e.g. the historical str(None) == "None" row (ExtendableSettingsInterface). Refused
            # per instance, like a conflict, so one bad row cannot abort the whole run.
            conflicts.append((inst_id, alias_disp, legacy, stored_cap, float("nan")))
            print(f"{inst_id:>4}  {alias_disp:<24}  {legacy!s:>10}  {stored_cap!s:>12}  "
                  f"{'REFUSED':>11}  UNREADABLE legacy value {legacy!r}")
            continue

        if stored_cap is not None and abs(float(stored_cap) - derived_cap) > _TOLERANCE_PCT:
            conflicts.append((inst_id, alias_disp, legacy, stored_cap, derived_cap))
            print(f"{inst_id:>4}  {alias_disp:<24}  {legacy!s:>10}  {stored_cap!s:>12}  "
                  f"{'REFUSED':>11}  CONFLICT (legacy*100={derived_cap:g} != stored {stored_cap!s})")
            continue

        final_cap = stored_cap if stored_cap is not None else derived_cap
        if stored_cap is not None:
            action = "delete legacy row only (already agree)"
        else:
            action = "write cap, delete legacy row"
        if not apply:
            action += " [DRY RUN]"

        print(f"{inst_id:>4}  {alias_disp:<24}  {legacy!s:>10}  {stored_cap!s:>12}  "
              f"{final_cap!s:>11}  {action}")

        if not apply:
            continue

        if stored_cap is None:
            expert.save_settings(
                {"max_virtual_equity_per_instrument_percent": (final_cap, None)})
        _delete_legacy_row(inst_id)
        migrated += 1

    print("-" * 100)

    if conflicts:
        print(f"\nREFUSED {len(conflicts)} instance(s) with disagreeing values -- resolve by "
              f"hand (pick one value, delete the other row) before re-running:")
        for inst_id, alias_disp, legacy, stored_cap, derived_cap in conflicts:
            print(f"  id={inst_id} ({alias_disp}): max_weight_per_name={legacy} "
                  f"(-> {derived_cap:g}%) but max_virtual_equity_per_instrument_percent="
                  f"{stored_cap} is already stored and disagrees.")
        return 1

    if not apply:
        print(f"\nDRY RUN -- nothing written ({migrated if apply else 'N/A'} would change). "
              f"Re-run with --apply to write.")
    else:
        print(f"\nApplied: {migrated} instance(s) migrated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
