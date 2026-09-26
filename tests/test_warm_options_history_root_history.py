"""``tools/warm_options_history.py --provider thetadata`` re-fetches what ROOT_HISTORY makes stale.

The XYZ case of the stage-2 repair (2026-09-26): the tree held only post-rename partitions, all
marked complete, fetched before XYZ <- SQ was known. The default ThetaData store now carries the
root-history hooks (``thetadata.parquet_store_hooks``), so a plain re-run of the warm plans
every such partition again -- next to the ones simply missing -- and says why. No network.
"""
import argparse
import importlib
from datetime import date, datetime, timedelta, timezone

from ba2_common.core.interfaces import OptionContractMeta, OptionEodBar
from ba2_providers.options import thetadata as T
from ba2_providers.options.parquet_store import OptionHistoryParquetStore
from ba2_providers.options.tastytrade import occ_symbol

warm = importlib.import_module("tools.warm_options_history")


class _Provider:
    name = "thetadata"

    def __init__(self, contracts):
        self.contracts = contracts

    def history_floor(self):
        return date(2016, 1, 1)

    def discover_contracts(self, underlying, *, expiry_gte, expiry_lte, **kw):
        return [c for c in self.contracts if expiry_gte <= c.expiry <= expiry_lte]


def _contract(u, e):
    return OptionContractMeta(occ_symbol(u, e, "C", 50.0), u, "call", 50.0, e)


def test_the_default_thetadata_store_carries_the_root_history_hooks(tmp_path):
    ns = argparse.Namespace(provider="thetadata", out=str(tmp_path / "t"))
    store = warm.make_store(ns)
    assert store.manifest_extra("XYZ") == {"root_history": T.root_history_signature("XYZ")}
    tasty = warm.make_store(argparse.Namespace(provider="tastytrade", out=str(tmp_path / "x")))
    assert tasty.manifest_extra("XYZ") == {}


def test_a_legacy_xyz_tree_is_planned_again_with_its_missing_partitions(tmp_path):
    root = str(tmp_path / "theta")
    legacy = T.ROOT_HISTORY_LEGACY_FETCHED["XYZ"] - timedelta(days=20)
    plain = OptionHistoryParquetStore(root=root, clock=lambda: legacy)
    held = [date(2025, 1, 24), date(2025, 2, 21)]          # post-rename partitions, complete
    for e in held:
        occ = occ_symbol("XYZ", e, "C", 50.0)
        plain.write_partition("XYZ", e, [OptionEodBar(occ_symbol=occ, bar_date=e - timedelta(days=3),
                                                      open=1, high=1, low=1, close=1, volume=1,
                                                      open_interest=1, iv=0.3)],
                              date(2020, 1, 1), e)
    missing = [date(2023, 6, 16), date(2024, 1, 19)]
    ns = argparse.Namespace(provider="thetadata", out=root)
    store = warm.make_store(ns)
    provider = _Provider([_contract("XYZ", e) for e in held + missing])
    args = warm.parse_args(["--provider", "thetadata", "--symbols", "XYZ", "--start", "2020-01-01",
                            "--end", "2025-03-01", "--discovery", "rest"])
    lines = []
    plan = warm.build_plan(provider, store, ["XYZ"], date(2020, 1, 1), date(2025, 3, 1), args,
                           persist=False, log=lines.append)
    assert sorted(u.expiry for u in plan.units) == sorted(held + missing)
    assert plan.per_symbol["XYZ"]["root_history_stale"] == 2
    assert any("ROOT_HISTORY" in ln and "XYZ" in ln for ln in lines)
