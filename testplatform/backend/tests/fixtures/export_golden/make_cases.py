"""Build tests/fixtures/export_golden/cases.json from a COPY of the test DB.

Cases: for every distinct expert_name, the lowest-id optimization-linked backtest (its
optimization's run-level `backtest` block is embedded), the lowest-id row whose optimization
carries a screener_opt block (if any), plus two synthetic rows derived from the first case:
`standalone_fallback` (no optimization link) and `unified_rules` (legacy trees converted to
entryRules/exitRules). Re-run only when intentionally re-baselining.

Usage (from testplatform/backend):
    ~/ba2-venvs/test/bin/python tests/fixtures/export_golden/make_cases.py /tmp/ba2-test-copy.db
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# repo root = export_golden -> fixtures -> tests -> backend -> testplatform -> repo
REPO = HERE
for _ in range(5):
    REPO = os.path.dirname(REPO)
# THIS checkout's packages first: the venv's editable installs may point at another checkout.
for p in (os.path.join(REPO, "packages", n) for n in ("experts", "providers", "common")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ba2_common.core.rule_models import trade_rules_from_legacy  # noqa: E402
FIELDS = ("id", "name", "expert_name", "engine_type", "strategy_params", "start_date",
          "end_date", "initial_capital", "optimization_id")


def _row_case(con, row, case_id):
    bt = dict(zip(FIELDS, row))
    bt["strategy_params"] = json.loads(bt["strategy_params"] or "{}")
    opt_block = None
    strategy_name = None
    if bt["optimization_id"] is not None:
        o = con.execute("select optimization_config, strategy_id from strategy_optimizations "
                        "where id=?", (bt["optimization_id"],)).fetchone()
        if o is not None:
            cfg = json.loads(o[0] or "{}")
            opt_block = cfg.get("backtest")
            s = con.execute("select name from strategies where id=?", (o[1],)).fetchone()
            strategy_name = s[0] if s else None
    return {"case_id": case_id, "backtest": bt, "opt_backtest_block": opt_block,
            "strategy_name": strategy_name}


def main() -> int:
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    cols = ", ".join(FIELDS)
    cases = []
    for (expert,) in con.execute("select distinct expert_name from backtests "
                                 "where expert_name is not null order by expert_name"):
        row = con.execute(f"select {cols} from backtests where expert_name=? and "
                          f"optimization_id is not null order by id limit 1", (expert,)).fetchone()
        if row:
            cases.append(_row_case(con, row, f"opt_{expert}"))
    for row in con.execute(f"select {cols} from backtests where optimization_id is not null "
                           f"order by id"):
        o = con.execute("select optimization_config from strategy_optimizations where id=?",
                        (row[FIELDS.index("optimization_id")],)).fetchone()
        if o and "screener_opt" in json.loads(o[0] or "{}").get("backtest", {}):
            cases.append(_row_case(con, row, "opt_screener"))
            break
    base = cases[0]
    standalone = json.loads(json.dumps(base))
    standalone["case_id"] = "standalone_fallback"
    standalone["backtest"]["optimization_id"] = None
    standalone["opt_backtest_block"] = None
    sp = standalone["backtest"]["strategy_params"]
    sp.update({"universe": {"mode": "static", "symbols": ["AAPL", "MSFT"]}, "seed": 7,
               "fillModel": "next_open", "warmupDays": 20, "commission": 1.0,
               "slippage": 5, "enableShort": False, "executionInterval": "1d"})
    cases.append(standalone)
    unified = json.loads(json.dumps(base))
    unified["case_id"] = "unified_rules"
    usp = unified["backtest"]["strategy_params"]
    conv = trade_rules_from_legacy(
        buy_tree=usp.pop("buyEntryConditions", None), sell_tree=usp.pop("sellEntryConditions", None),
        entry_actions=usp.pop("entryActions", None), exit_conditions=usp.pop("exitConditions", None))
    usp["entryRules"], usp["exitRules"] = conv["entry_rules"], conv["exit_rules"]
    cases.append(unified)
    with open(os.path.join(HERE, "cases.json"), "w") as f:
        json.dump(cases, f, indent=1, sort_keys=True, default=str)
    print(f"wrote {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
