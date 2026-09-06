"""Reset the DEV/test trade instance (port 8080) to a clean slate, keeping app settings.

What survives: the ``appsetting`` table (every global API key and app-level knob) and
the schema itself. Everything else -- experts, recommendations, analyses, orders,
transactions, rulesets, activity, instruments, accounts -- is deleted, then the three
test broker accounts are recreated from an accounts file.

WHY AN IN-PLACE ROW WIPE AND NOT A FRESH DB FILE
------------------------------------------------
The obvious reading of "wipe the db and reimport the settings" is: delete the file,
let ``init_db()`` re-create it, replay the settings. That works, but the re-created
schema comes from ``SQLModel.metadata.create_all()`` while the live schema came from
the alembic chain -- and the two are only guaranteed to agree if every migration in
the chain is a pure reflection of the models. Any index or constraint a migration
added by hand would silently vanish, and ``alembic_version`` would have to be stamped
back to head by hand to keep future migrations working.

Deleting the ROWS instead leaves the schema and ``alembic_version`` byte-identical to
what alembic actually built, which is the thing we want to be sure about. The end
state the operator sees is the same. The settings export is still written to disk
(and can be re-imported with --settings-from) so the operation is reversible and the
artifact exists.

ORDER IDS AND client_order_id REUSE
-----------------------------------
Wiping ``tradingorder`` restarts its ids at 1, and the live code derives Alpaca
client_order_ids from that id. Alpaca rejects a client_order_id an account has
already seen (error 40010001), so this reset is only safe onto broker accounts with
NO prior order history -- which is exactly the case when the accounts file carries
freshly created paper accounts. Resetting while KEEPING broker accounts that have
already traded would re-mint ids those accounts have seen. The script refuses to run
unless every account in the accounts file is new to this database (--allow-reused-
accounts overrides, and prints what it is risking).

Usage:
    python tools/reset_test_instance.py --accounts accounts.json            # dry run
    python tools/reset_test_instance.py --accounts accounts.json --apply
    python tools/reset_test_instance.py --export-only                       # settings only

accounts.json:
    [{"name": "BA2 Test1", "provider": "Alpaca",
      "api_key": "PK...", "api_secret": "...", "paper_account": true}]
"""
import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys

REPO = os.environ.get("BA2_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (REPO, os.path.join(REPO, "packages", "experts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_DB = os.path.expanduser(r"~\Documents\ba2\trade\db.sqlite")

# Tables whose ROWS survive the wipe. Everything else in sqlite_master is emptied.
# Kept as a name list (not "everything except these models") so a table added by a
# future migration is wiped by default: forgetting to wipe leaks stale state into a
# "clean" instance, which is the failure that actually costs debugging time.
PRESERVE = {"appsetting", "alembic_version"}

# SQLite's own bookkeeping -- never touched directly.
INTERNAL_PREFIX = "sqlite_"

# Non-credential settings written for a recreated broker account.
#
# Every key here MUST be declared in the provider's get_merged_settings_definitions().
# An undeclared key cannot be typed on read: the loader falls back to inferring a type
# from the stored columns, and a bool lands in value_json, so it infers "json" and hands
# back the raw string 'false' -- which is truthy. The older ad-hoc reset script
# (test_files/reset_test_trade_db.py) seeds a `drip_enabled` row this way; no account
# class declares it and no code reads it, so it is deliberately NOT reproduced here.
ACCOUNT_DEFAULTS = {
    "data_feed": "delayed_sip",                    # AlpacaAccount.get_settings_definitions
    "minimum_equity_threshold_percent": 5.0,       # ReadOnlyAccountInterface
}


def _fail(msg):
    print(f"FATAL: {msg}")
    raise SystemExit(1)


def _timestamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _guard_target(db_path):
    """Refuse to touch anything that is not the dev instance's database."""
    real = os.path.abspath(db_path)
    if not os.path.exists(real):
        _fail(f"database does not exist: {real}")
    low = real.lower()
    if "prod" in low:
        _fail(f"refusing to reset a path containing 'prod': {real}")
    # A live instance holding the DB would be wiped out from under itself, and its
    # in-memory caches would then write stale ids back into the fresh tables.
    try:
        con = sqlite3.connect(real, timeout=1.0)
        con.execute("BEGIN EXCLUSIVE")
        con.rollback()
        con.close()
    except sqlite3.OperationalError as e:
        _fail(f"database is locked by another process ({e}); stop the instance first")
    return real


def _tables(con):
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [n for (n,) in rows if not n.startswith(INTERNAL_PREFIX)]


def _export_settings(con, out_path):
    cols = [r[1] for r in con.execute("PRAGMA table_info(appsetting)")]
    rows = [dict(zip(cols, r)) for r in con.execute("SELECT * FROM appsetting")]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"table": "appsetting", "columns": cols, "rows": rows}, f, indent=2)
    return rows


def _backup(db_path, out_path):
    """WAL-safe backup. A plain file copy would miss everything still in -wal."""
    src = sqlite3.connect(db_path)
    src.execute("VACUUM INTO ?", (out_path,))
    src.close()
    return os.path.getsize(out_path)


def _load_accounts(path, db_path):
    with open(path, encoding="utf-8") as f:
        accounts = json.load(f)
    if not isinstance(accounts, list) or not accounts:
        _fail(f"{path} must be a non-empty JSON list")
    for a in accounts:
        for field in ("name", "provider", "api_key", "api_secret"):
            if not a.get(field):
                _fail(f"account entry missing {field!r}: {a.get('name', a)}")
    _guard_fresh_accounts(accounts, db_path)
    return accounts


def _guard_fresh_accounts(accounts, db_path):
    """See the client_order_id note in the module docstring."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    known = {r[0] for r in con.execute(
        "SELECT value_str FROM accountsetting WHERE key='api_key'") if r[0]}
    con.close()
    reused = [a["name"] for a in accounts if a["api_key"] in known]
    if reused:
        print("WARNING: these accounts already exist in this database, so wiping "
              "tradingorder will re-mint client_order_ids the broker has already "
              f"seen: {', '.join(reused)}")
        if not os.environ.get("BA2_ALLOW_REUSED_ACCOUNTS"):
            _fail("refusing; pass --allow-reused-accounts if the broker accounts are "
                  "genuinely order-free")


def _wipe(db_path, preserve):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=OFF")
    deleted = {}
    try:
        for name in _tables(con):
            if name in preserve:
                continue
            before = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            con.execute(f'DELETE FROM "{name}"')
            deleted[name] = before
        con.commit()
    finally:
        con.close()
    # VACUUM has to run outside the transaction, on its own connection.
    con = sqlite3.connect(db_path)
    con.execute("VACUUM")
    con.close()
    return deleted


def _create_accounts(db_path, accounts):
    """Create AccountDefinition rows and write their settings.

    This mirrors the account-creation path in ui/pages/settings.py:897-923 rather than
    inventing one. The ``__new__`` dance is not a hack around the constructor: an
    account class validates its credentials in ``__init__``, so a brand-new account
    cannot be instantiated until its settings row exists. The UI solves it the same
    way -- build a settings-only shell, write the keys through ``save_setting`` (which
    types each value from get_merged_settings_definitions), then construct the real
    instance, whose success IS the credential check.
    """
    os.environ["DB_FILE"] = db_path
    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(db_path)

    from ba2_common.core.db import add_instance
    from ba2_common.core.models import AccountDefinition
    from ba2_trade_platform.modules.accounts import get_account_class

    created = []
    for a in accounts:
        provider_cls = get_account_class(a["provider"])
        if provider_cls is None:
            _fail(f"unknown provider {a['provider']!r} for account {a['name']!r}")

        definition = AccountDefinition(
            name=a["name"], provider=a["provider"], description=a.get("description", ""))
        account_id = add_instance(definition)

        settings = dict(ACCOUNT_DEFAULTS)
        settings["api_key"] = a["api_key"]
        settings["api_secret"] = a["api_secret"]
        settings["paper_account"] = bool(a.get("paper_account", True))
        settings.update(a.get("extra_settings") or {})

        # Writing a key the class does not declare produces a row nothing can type on
        # read (see the ACCOUNT_DEFAULTS comment). Refuse instead of writing it.
        declared = provider_cls.get_merged_settings_definitions()
        undeclared = sorted(set(settings) - set(declared))
        if undeclared:
            _fail(f"{a['provider']} does not declare these settings: "
                  f"{', '.join(undeclared)}")

        shell = provider_cls.__new__(provider_cls)   # settings-only; see docstring
        shell.id = account_id
        for key, value in settings.items():
            shell.save_setting(key, value)

        created.append((account_id, a["name"], provider_cls))
    return created


def _verify(created):
    """Prove the credentials reach the broker rather than assuming a written row is a
    working account. Constructing the class is itself the authentication check; the
    balance call confirms the session actually answers."""
    ok = True
    for account_id, name, provider_cls in created:
        try:
            instance = provider_cls(account_id)
            balance = instance.get_balance()
            positions = instance.get_positions()
            held = "fetch failed" if positions is None else f"{len(positions)} positions"
            print(f"  [OK]   id={account_id} {name}: balance={balance}, {held}")
        except Exception as e:  # noqa: BLE001 -- report every account, fail at the end
            ok = False
            print(f"  [FAIL] id={account_id} {name}: {type(e).__name__}: {e}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"target database (default {DEFAULT_DB})")
    ap.add_argument("--accounts", help="JSON file of broker accounts to recreate")
    ap.add_argument("--apply", action="store_true", help="actually do it (default: dry run)")
    ap.add_argument("--export-only", action="store_true", help="dump app settings and stop")
    ap.add_argument("--keep", action="append", default=[],
                    help="extra table whose rows survive (repeatable, e.g. --keep instrument)")
    ap.add_argument("--allow-reused-accounts", action="store_true",
                    help="proceed even if an api_key already exists in this database")
    args = ap.parse_args()

    if args.allow_reused_accounts:
        os.environ["BA2_ALLOW_REUSED_ACCOUNTS"] = "1"

    db_path = _guard_target(args.db)
    preserve = PRESERVE | set(args.keep)
    stamp = _timestamp()
    export_dir = os.path.join(os.path.dirname(db_path), "exports")
    os.makedirs(export_dir, exist_ok=True)
    settings_path = os.path.join(export_dir, f"appsetting-{stamp}.json")

    print(f"target      : {db_path}")
    print(f"preserving  : {', '.join(sorted(preserve))}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    counts = {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
              for n in _tables(con)}
    settings_rows = _export_settings(con, settings_path)
    con.close()
    print(f"exported    : {len(settings_rows)} app settings -> {settings_path}")

    doomed = {n: c for n, c in counts.items() if n not in preserve and c}
    print(f"would delete: {sum(doomed.values())} rows across {len(doomed)} tables")
    for name, count in sorted(doomed.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<38} {count}")

    if args.export_only:
        return 0

    accounts = _load_accounts(args.accounts, db_path) if args.accounts else []
    print(f"accounts    : {len(accounts)} to recreate "
          f"({', '.join(a['name'] for a in accounts) or 'none'})")

    if not args.apply:
        print("\nDRY RUN -- nothing changed. Re-run with --apply.")
        return 0

    backup_path = f"{db_path}.bak-pre-testreset-{stamp}"
    size = _backup(db_path, backup_path)
    print(f"\nbacked up   : {backup_path} ({size / 1e6:.1f} MB)")

    deleted = _wipe(db_path, preserve)
    print(f"wiped       : {sum(deleted.values())} rows across "
          f"{len([k for k, v in deleted.items() if v])} tables, vacuumed")

    if accounts:
        created = _create_accounts(db_path, accounts)
        print(f"created     : {len(created)} accounts")
        print("verifying against the broker:")
        if not _verify(created):
            print("\nSome accounts failed to authenticate -- fix the credentials and "
                  "re-save them in the UI. The reset itself succeeded.")
            return 2

    print(f"\nDone. Settings export kept at {settings_path}; "
          f"previous database at {backup_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
