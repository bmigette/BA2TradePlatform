"""One-shot: reset the dev/test trade-platform DB (~/Documents/ba2/trade/db.sqlite) to a clean
slate for a fresh round of paper-account testing.

Keeps: appsetting (API keys / app config) untouched.
Wipes: all trade/analysis/activity data, all rulesets, ALL expert instances except
       PennyMomentumTrader (instance id=4, alias 'TestPenny') whose CONFIG (expertsetting) is
       kept but whose own trade/analysis data is wiped along with everyone else's.
Replaces: the 3 existing accounts with 3 new ones (BA2-Test1/2/3, given Alpaca PAPER
           key/secret), and reassigns Penny onto BA2-Test3 at virtual_equity_pct=10.0.

Not a general-purpose tool — one-shot script, run once, keep for the record."""
import sqlite3

DB = r"C:\Users\basti\Documents\ba2\trade\db.sqlite"
PENNY_INSTANCE_ID = 4

NEW_ACCOUNTS = [
    # (name, api_key, api_secret)
    ("BA2-Test1", "PKZGDFJFTHBEZDUPYIG4QAOLNT", "BSx4ka1jdR1oxi6HYt5yywmB312e8SB9r5rsirNipCgA"),
    ("BA2-Test2", "PK7653XZDWWQEOWZA7CHLWXLP7", "3oN5teBpDii79CfcBrhiN9E1RLACQ381NZHsEPEjmLFV"),
    ("BA2-Test3", "PKMD3AZNHSBJSVR3I5FFJFO7HI", "HT6mBqoSWn3FvWi3sSupyJUVXidftj6g1Ly9jP74UoJX"),
]
PENNY_TARGET_ACCOUNT_NAME = "BA2-Test3"
PENNY_VIRTUAL_EQUITY_PCT = 10.0


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=OFF")  # we order deletes ourselves; avoid FK surprises
    cur = conn.cursor()

    # Verify Penny is who we think before touching anything.
    row = cur.execute(
        "SELECT id, expert, alias, account_id FROM expertinstance WHERE id=?", (PENNY_INSTANCE_ID,)
    ).fetchone()
    if not row or row[1] != "PennyMomentumTrader":
        raise SystemExit(f"expected PennyMomentumTrader at id={PENNY_INSTANCE_ID}, got {row}")
    print(f"Penny confirmed: {row}")

    old_account_ids = [r[0] for r in cur.execute("SELECT id FROM accountdefinition").fetchall()]
    print(f"existing accounts to replace: {old_account_ids}")

    # --- Phase A: operational/trade/analysis data (children first) -------------------------
    for stmt in [
        "DELETE FROM trade_action_result",
        "DELETE FROM llmusagelog",
        "DELETE FROM persistedqueuetask",
        "DELETE FROM tradingorder",
        'DELETE FROM "transaction"',
        "DELETE FROM analysisoutput",
        "DELETE FROM expertrecommendation",
        "DELETE FROM marketanalysis",
        "DELETE FROM activitylog",
        "DELETE FROM option_activity",
        "DELETE FROM option_iv_snapshot",
        "DELETE FROM smartriskmanagerjob",
        "DELETE FROM position",
    ]:
        n = conn.execute(stmt).rowcount
        print(f"{stmt} -> {n} rows")

    # --- Phase B: expert instances (keep Penny) ---------------------------------------------
    n = conn.execute(
        "DELETE FROM expertsetting WHERE instance_id != ?", (PENNY_INSTANCE_ID,)
    ).rowcount
    print(f"expertsetting (non-penny) -> {n} rows")
    n = conn.execute(
        "DELETE FROM expertinstance WHERE id != ?", (PENNY_INSTANCE_ID,)
    ).rowcount
    print(f"expertinstance (non-penny) -> {n} rows")

    # --- Phase C: rulesets (now unreferenced; Penny has NULL ruleset ids already) -----------
    for stmt in [
        "DELETE FROM ruleset_eventaction_link",
        "DELETE FROM eventaction",
        "DELETE FROM ruleset",
    ]:
        n = conn.execute(stmt).rowcount
        print(f"{stmt} -> {n} rows")

    # --- Phase D: swap accounts ---------------------------------------------------------------
    if old_account_ids:
        qmarks = ",".join("?" * len(old_account_ids))
        n = conn.execute(
            f"DELETE FROM accountsetting WHERE account_id IN ({qmarks})", old_account_ids
        ).rowcount
        print(f"accountsetting (old) -> {n} rows")
        n = conn.execute(
            f"DELETE FROM accountdefinition WHERE id IN ({qmarks})", old_account_ids
        ).rowcount
        print(f"accountdefinition (old) -> {n} rows")

    new_account_ids = {}
    for name, api_key, api_secret in NEW_ACCOUNTS:
        cur.execute(
            "INSERT INTO accountdefinition (name, provider, description) VALUES (?,?,?)",
            (name, "Alpaca", ""),
        )
        acc_id = cur.lastrowid
        new_account_ids[name] = acc_id
        settings = [
            ("api_key", api_key, "{}", None),
            ("api_secret", api_secret, "{}", None),
            ("paper_account", None, "true", None),
            ("data_feed", "delayed_sip", "{}", None),
            ("drip_enabled", None, "false", None),
            ("minimum_equity_threshold_percent", None, "{}", 5.0),
        ]
        for key, vstr, vjson, vfloat in settings:
            cur.execute(
                "INSERT INTO accountsetting (account_id, key, value_str, value_json, value_float) "
                "VALUES (?,?,?,?,?)",
                (acc_id, key, vstr, vjson, vfloat),
            )
        print(f"created account {name} -> id={acc_id}")

    # --- Phase E: reassign Penny onto BA2-Test3 at 10% equity --------------------------------
    target_id = new_account_ids[PENNY_TARGET_ACCOUNT_NAME]
    conn.execute(
        "UPDATE expertinstance SET account_id=?, virtual_equity_pct=?, "
        "enter_market_ruleset_id=NULL, open_positions_ruleset_id=NULL WHERE id=?",
        (target_id, PENNY_VIRTUAL_EQUITY_PCT, PENNY_INSTANCE_ID),
    )
    print(f"Penny (id={PENNY_INSTANCE_ID}) -> account {PENNY_TARGET_ACCOUNT_NAME} "
          f"(id={target_id}), virtual_equity_pct={PENNY_VIRTUAL_EQUITY_PCT}")

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print("done.")


if __name__ == "__main__":
    main()
