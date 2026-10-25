"""ml_forecast must stay importable in the heavy-model venv, which has only numpy, pandas and pyarrow
(plus ba2_common installed with --no-deps). A new heavy import here would break the store builder there."""
import os
import subprocess
import sys
from pathlib import Path

_PROG = r"""
import json, sys
import numpy, pandas, pyarrow  # noqa
base = set(sys.modules)
base_top = sorted({m.split('.')[0] for m in base})
import ba2_common.core.ml_forecast.model_card, ba2_common.core.ml_forecast.forecast_row
import ba2_common.core.ml_forecast.store, ba2_common.core.ml_forecast.mapping, ba2_common.core.ml_forecast.context
import pyarrow.parquet  # a submodule of an allowed package is fine
new = sorted(set(sys.modules) - base)
print(json.dumps({'new': new, 'base_top': base_top}))
"""


def test_ml_forecast_imports_only_stdlib_numpy_pandas_pyarrow():
    # the package under test, not whichever checkout the venv's editable install points at
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    out = subprocess.run([sys.executable, "-c", _PROG], capture_output=True, text=True, check=True, env=env).stdout
    import json
    res = json.loads(out.strip().splitlines()[-1])
    new, base_top = res['new'], set(res['base_top'])
    stdlib = sys.stdlib_module_names
    offenders = sorted({m.split(".")[0] for m in new
                        if m.split(".")[0] not in stdlib | base_top and not m.startswith("ba2_common")})
    assert offenders == [], f"ml_forecast pulled in non-allowed packages: {offenders}"
    assert not [m for m in new if m.startswith("ba2_common") and not (
        m in ("ba2_common", "ba2_common.version", "ba2_common.core", "ba2_common.core.types")
        or m.startswith("ba2_common.core.ml_forecast"))], "ml_forecast imports other ba2_common modules"
