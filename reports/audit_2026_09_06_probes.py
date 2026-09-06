"""Offline audit reproductions; execute extracted source without app startup or broker IO."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def extract(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    node.body = [n for n in node.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    module = ast.Module(body=[node], type_ignores=[])
    code = "from __future__ import annotations\n" + ast.unparse(module)
    exec(compile(code, str(ROOT / path), "exec"), namespace)
    return namespace[name]


ns = {"np": np}
sizing = "packages/common/ba2_common/core/position_sizing.py"
for name in ["compute_risk_based_quantity", "synthesize_safeguard_stop"]:
    extract(sizing, name, ns)
ns["OrderDirection"] = SimpleNamespace(BUY="buy")
rm_path = "packages/common/ba2_common/core/TradeRiskManagement.py"
stop = extract(rm_path, "_ensure_safeguard_stop", ns)
size = extract(rm_path, "_risk_atr_quantity", ns)
settings = dict(atr_risk_budget_pct=1.0, risk_per_trade_pct=8.0,
                atr_multiplier=2.0, atr_period=14, min_stop_loss_pct=7.0,
                use_atr_stop=False)
expert = SimpleNamespace(get_virtual_balance=lambda: 100000.0,
                         get_setting_with_interface_default=lambda key, **kw: settings[key])
rm = SimpleNamespace(_regime_scale=lambda *args: 1.0,
                     _commission_per_trade=lambda account: 0.0,
                     logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None))
rm._ensure_safeguard_stop = lambda *args: stop(rm, *args)
order = SimpleNamespace(stop_price=None, side="buy", data=None)
qty = size(rm, order, "TEST", 100.0, expert, 30000.0, 100000.0)
assert order.stop_price == 93.0 and qty == 300
actual_risk = qty * (100 - order.stop_price)
assert actual_risk > 100000 * settings["atr_risk_budget_pct"] / 100

df = pd.DataFrame({"Date": pd.date_range("2020-01-01", periods=12),
                   "Close": [100, 103, 101, 104, 102, 105, 103, 106, 104, 107, 105, 108],
                   "indicator": range(12)})
horizon = 2
df["direction_up_2bar"] = (df.Close.shift(-horizon) > df.Close).astype(int)
# Execute the actual feature-selection statements from the job handler.
tree = ast.parse((ROOT / "testplatform/backend/app/services/job_handler.py").read_text(encoding="utf-8"))
handler = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "handle_training_job") if any(isinstance(n, ast.FunctionDef) and n.name == "handle_training_job" for n in ast.walk(tree)) else tree
nodes = [n for n in ast.walk(handler) if isinstance(n, (ast.Assign, ast.Expr)) and 1622 <= n.lineno <= 1626]
nodes.sort(key=lambda n: n.lineno)
env = {"combined_df": df}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "feature-selection", "exec"), env)
assert "direction_up_2bar" in env["feature_columns"]
seq = extract("testplatform/backend/app/services/tsai_training.py", "_create_sequences", ns)
X, y = seq(None, df[env["feature_columns"]].to_numpy(), df.direction_up_2bar.to_numpy(), 3, 0)
label_index = env["feature_columns"].index("direction_up_2bar")
assert np.array_equal(X[:, label_index, -1], y)
assert df.direction_up_2bar.iloc[-2:].tolist() == [0, 0]
split = int(len(df) * 0.8)
assert split - 1 + horizon >= split

# Public pure helper's cap handling, without caller guards.
zero_cap = ns["compute_risk_based_quantity"](100000, 100, 1, stop_price=95,
                                              max_position_value=0, available_balance=-1)
assert zero_cap["quantity"] == 200
print(json.dumps({"risk_budget": {"budget_dollars": 1000, "stop": order.stop_price,
    "quantity": qty, "actual_stop_risk_dollars": actual_risk},
    "target_leak": {"features": env["feature_columns"], "label_is_last_input": True},
    "unobservable_tail_labels": df.direction_up_2bar.iloc[-2:].tolist(),
    "training_label_crosses_split": True,
    "zero_cap_negative_cash_helper_quantity": zero_cap["quantity"]}, indent=2))
