"""A re-run must carry EVERY gene the optimizer searched, for every strategy the launcher builds.

THE DEFECT (found 2026-10-13). ``rerun_handler._gene_params`` filters a stored ``strategy_params``
down to the namespaces ``decode_params`` accepts, from a hand-kept list that lacked ``optsel``. The
three option selection weights (``optsel:<half>:w_premium|w_iv|w_rvol``) were stripped, the re-run
decoded DEFAULT weights and picked different contracts: stage-1 row bt 7, stored +8,648.6% /
-33.4% / 291 trades, re-ran as +1,492.6% / -44.5% / 296. The grid's own persist path (raw genome,
no filter) reproduced the stored row exactly. Same trap as ``schedule`` (2026-09-07) and ``market``.

So the list is now ONE tuple next to ``decode_params`` (``DECODE_GENE_NAMESPACES``), and these
tests pin it from both sides: against ``decode_params``' own dispatch, and against the gene space
of every strategy kind the launcher can build (a new namespace fails here, not in a measurement).
"""
import importlib.util
import inspect
import os
import re
import sys

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LAUNCHER_PATH = os.path.join(os.path.dirname(_BACKEND), "ba2test_launcher.py")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_rerun_ns", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_rerun_ns"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


L = _launcher()

from app.services import strategy_param_space as SPS  # noqa: E402
from app.services.backtest import rerun_handler as RH  # noqa: E402

OPTION_KINDS = sorted(L._OPTION_STRATS) + sorted(L._OPTION_GROUPS) + ["O_WHEEL"]


def _option_strategy(kind):
    if kind in L._OPTION_GROUPS:
        return L._build_strategy_option_group(kind)
    if kind == "O_WHEEL":
        return L._build_strategy_wheel(kind)
    return L._build_strategy_option(kind)


def test_the_filter_uses_the_one_namespace_list():
    assert RH._GENE_PREFIXES is SPS.DECODE_GENE_NAMESPACES


def test_the_list_is_exactly_what_decode_params_dispatches_on():
    """Read decode_params' own ``key.startswith("<ns>:")`` branches: a namespace added to the
    dispatch and not to the list (or the reverse) fails here."""
    src = inspect.getsource(SPS.decode_params)
    dispatched = set(re.findall(r'key\.startswith\("([a-z_]+):"\)', src))
    assert dispatched, "decode_params' dispatch was not found: this test must be re-pointed"
    assert dispatched == set(SPS.DECODE_GENE_NAMESPACES)
    assert len(set(SPS.DECODE_GENE_NAMESPACES)) == len(SPS.DECODE_GENE_NAMESPACES)


def test_the_option_selection_weights_survive_the_filter():
    """THE DEFECT, on the values of bt 7."""
    stored = {"optsel:debit:w_premium": -2.0, "optsel:debit:w_iv": -1.0,
              "optsel:debit:w_rvol": 1.0, "entryRules": [], "expertFixedSettings": {}}
    assert RH._gene_params(stored) == {"optsel:debit:w_premium": -2.0,
                                       "optsel:debit:w_iv": -1.0, "optsel:debit:w_rvol": 1.0}


@pytest.mark.parametrize("kind", OPTION_KINDS)
def test_every_searched_gene_of_an_option_strategy_survives_the_filter(kind):
    space = SPS.collect_param_space(_option_strategy(kind))
    assert space, f"{kind} has an empty gene space"
    genome = {k: 0 for k in space}
    dropped = sorted(set(genome) - set(RH._gene_params(genome)))
    assert not dropped, f"{kind}: a re-run would silently drop {dropped}"


@pytest.mark.parametrize("kind", OPTION_KINDS)
def test_a_decoded_rerun_carries_the_weights_onto_the_actions(kind):
    """End of the chain: filter -> decode_params -> the entry action carries the weight. Without
    the fix the action has no ``option_w_*`` key at all and the default policy picks the contract."""
    strat = _option_strategy(kind)
    space = SPS.collect_param_space(strat)
    weights = {k: (-1.5 if k.endswith("w_premium") else 1.5)
               for k in space if k.startswith("optsel:")}
    if not weights:
        pytest.skip(f"{kind} searches no selection weights")
    decoded = SPS.decode_params(strat, RH._gene_params(dict(weights, entryRules=[])))
    carried = [a for r in decoded["entry_rules"] for a in r["actions"]
               if "option_w_premium" in a]
    assert carried, f"{kind}: no entry action carries the re-run's selection weights"
    assert all(a["option_w_premium"] == -1.5 for a in carried)
