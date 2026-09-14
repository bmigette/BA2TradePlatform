"""ThetaData as a first-class option store, and ``parquet`` renamed to ``tastytrade``.

WHY THETADATA IS A STORE AND NOT A FLAG. The option grids need to start at 2020. Measured
vendor floors:

    alpaca      2024-01-18
    tastytrade  2022-10-01
    thetadata   2018-09-14      <- the only one that reaches 2020

The bars were already on disk (131,678 parquet files under ThetaDataOptionsProvider/, against
75,910 for TastyTrade) and the parquet READER already understands their shape -- it has a
real-quotes branch specifically for them. What was missing is the store->vendor tie. Reading
ThetaData through the ``parquet`` store would have reported TastyTrade as the serving vendor, so
``validate_options_window`` would police a 2020 run against a 2022-10-01 floor: a floor naming a
vendor the store does not hold, which is precisely the lie STORE_VENDOR exists to prevent.

WHY ``parquet`` BECAME ``tastytrade``. ``parquet`` names the FILE FORMAT. With two parquet-backed
stores it identifies nothing -- "the parquet store" stopped being a referring expression the
moment ThetaData became the second one. Stores are named for their VENDOR, because the vendor is
what the name has to answer (which history, whose floor). ``sqlite`` keeps its name: it is the
only store in that format, so it is still unambiguous.

The old name stays accepted as an ALIAS, and that is not politeness -- every optimization_config
and Backtest on record carries ``options_store: "parquet"``, and a rejected value RAISES here by
design, so dropping it would break re-runs and deploys of the entire existing options archive.
"""
import pytest

from app.services.backtest import options_store as S


# ---------------------------------------------------------------------------------------------
# The new store
# ---------------------------------------------------------------------------------------------

def test_thetadata_is_a_selectable_store():
    assert S.THETADATA in S.OPTIONS_STORES
    assert S.resolve_options_store({"options_store": "thetadata"}) == S.THETADATA


def test_thetadata_serves_thetadata_history_not_someone_elses():
    """The whole point: the floor a 2020 run is policed against must be ThetaData's."""
    assert S.STORE_VENDOR[S.THETADATA] == "thetadata"


def test_every_store_names_a_real_vendor():
    """STORE_VENDOR values must be keys of OPTIONS_HISTORY_PROVIDERS or the floor lookup raises."""
    from ba2_providers.options import OPTIONS_HISTORY_PROVIDERS
    for store in S.OPTIONS_STORES:
        assert S.STORE_VENDOR[store] in OPTIONS_HISTORY_PROVIDERS, store


def test_thetadata_floor_actually_reaches_2020():
    """Pins the reason this store exists. If ThetaData's floor ever moves past 2020-01-01 the
    option grids cannot start there and this test is the place that says so."""
    from datetime import date
    from ba2_providers.options import options_history_floor
    assert options_history_floor(S.STORE_VENDOR[S.THETADATA]) <= date(2020, 1, 1)
    # ...and the store it replaces genuinely cannot.
    assert options_history_floor(S.STORE_VENDOR[S.TASTYTRADE]) > date(2020, 1, 1)


# ---------------------------------------------------------------------------------------------
# The rename, and its alias
# ---------------------------------------------------------------------------------------------

def test_tastytrade_is_the_new_name():
    assert S.TASTYTRADE == "tastytrade"
    assert S.TASTYTRADE in S.OPTIONS_STORES
    assert S.resolve_options_store({"options_store": "tastytrade"}) == S.TASTYTRADE


def test_parquet_still_resolves_for_every_row_already_on_disk():
    """Existing optimization_configs and Backtests carry options_store="parquet"."""
    assert S.resolve_options_store({"options_store": "parquet"}) == S.TASTYTRADE


def test_the_alias_works_through_the_env_var_too(monkeypatch):
    monkeypatch.setenv("BACKTEST_OPTIONS_STORE", "parquet")
    assert S.resolve_options_store(None) == S.TASTYTRADE


def test_parquet_is_an_alias_not_a_store():
    """It resolves, but it is not offered as a choice -- new runs should name the vendor."""
    assert "parquet" not in S.OPTIONS_STORES


# ---------------------------------------------------------------------------------------------
# Roots: each vendor reads its OWN tree
# ---------------------------------------------------------------------------------------------

def test_each_store_reads_its_own_provider_directory(monkeypatch):
    monkeypatch.delenv("BACKTEST_OPTIONS_PARQUET_ROOT", raising=False)
    tt = S.default_options_parquet_root(S.TASTYTRADE)
    td = S.default_options_parquet_root(S.THETADATA)
    assert tt.endswith("TastyTradeOptionsProvider")
    assert td.endswith("ThetaDataOptionsProvider")
    assert tt != td, "the two vendors must never resolve to one tree"


def test_the_tastytrade_root_is_taken_from_the_writer_not_retyped():
    """A hand-copied directory name is how a reader drifts from its writer."""
    from ba2_providers.options.parquet_store import PROVIDER_DIR
    assert S.default_options_parquet_root(S.TASTYTRADE).endswith(PROVIDER_DIR)


def test_an_explicit_root_still_overrides(monkeypatch):
    monkeypatch.setenv("BACKTEST_OPTIONS_PARQUET_ROOT", r"X:\pinned")
    assert S.default_options_parquet_root(S.THETADATA) == r"X:\pinned"
    assert S.default_options_parquet_root(S.TASTYTRADE) == r"X:\pinned"


def test_the_root_defaults_to_the_selected_store_when_asked_for_nothing(monkeypatch):
    """Back-compat: the old signature took no argument and meant "the parquet tree"."""
    monkeypatch.delenv("BACKTEST_OPTIONS_PARQUET_ROOT", raising=False)
    assert S.default_options_parquet_root().endswith("TastyTradeOptionsProvider")


# ---------------------------------------------------------------------------------------------
# Still refuses nonsense
# ---------------------------------------------------------------------------------------------

def test_an_unknown_store_still_raises_rather_than_falling_back():
    with pytest.raises(ValueError) as e:
        S.resolve_options_store({"options_store": "theta"})     # near-miss typo
    assert "theta" in str(e.value)


def test_the_error_lists_what_is_actually_choosable():
    with pytest.raises(ValueError) as e:
        S.resolve_options_store({"options_store": "nope"})
    msg = str(e.value)
    for store in S.OPTIONS_STORES:
        assert store in msg
