"""Per-run backtest *trading* DB lifecycle.

This DB is distinct from BOTH:
  (a) BA2TestPlatform's own results DB (the ``Backtest`` SQLAlchemy row lives there,
      via app.models.database.SessionLocal), and
  (b) the live BA2TradePlatform DB (~/Documents/ba2_trade_platform/db.sqlite).

The inherited ba2_common AccountInterface / refresh_transactions / submit_order /
TradeActionEvaluator / TradeRiskManagement logic reads & writes TradingOrder /
Transaction / AccountDefinition / ExpertInstance / ExpertRecommendation rows via
``ba2_common.core.db``. We point THAT db layer at a throwaway sqlite file (one file
per run) so a run is hermetic, reproducible, and never touches the live DB. The
schema is created by ba2_common's own ``init_db`` (SQLModel.metadata.create_all), so
it is byte-identical to the live trading schema and the inherited DB logic works
unchanged.

Confirmed against the installed ba2_common.core.db:
  * configure_db(db_file) -> points the (lazy) engine at db_file and resets it.
  * init_db() -> imports models + SQLModel.metadata.create_all(get_engine()).
  * get_engine() / get_db() / add_instance / get_instance.

AccountDefinition's real columns are {name, provider, description} (NOT the plan
draft's account_type/enabled, which do not exist on the model).
"""
from __future__ import annotations

import pathlib
import tempfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from ba2_common.core import db as common_db


def backtest_db_root() -> pathlib.Path:
    """Directory holding per-run backtest sqlite files (created on demand)."""
    root = pathlib.Path(tempfile.gettempdir()) / "ba2_backtest_dbs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def backtest_db_path(run_id: int | str) -> pathlib.Path:
    """Deterministic sqlite path for a run id (so re-runs reuse the same file name)."""
    return backtest_db_root() / f"run_{run_id}.sqlite"


# Credential app-settings carried from the live app DB into each throwaway run DB so that
# live-flavoured providers/experts constructed inside the run (e.g. FMPOHLCVProvider, which reads
# get_app_setting("FMP_API_KEY"); FinnHubRating, which reads get_setting("finnhub_api_key"))
# resolve their keys. Only credentials needed by the data providers/experts — never trading
# config. Absent keys are skipped (hermetic runs carry nothing).
_CARRIED_APP_SETTINGS = ("FMP_API_KEY", "finnhub_api_key")


def _read_carry_settings(keys: tuple[str, ...]) -> Dict[str, str]:
    """Read the given AppSetting keys from the CURRENTLY-active ba2_common DB (the live DB,
    before the engine is switched to the run sqlite). Missing keys are omitted. Any error
    (DB not ready, no AppSetting table) yields an empty dict — a real run then fails loudly at
    provider construction, and a hermetic run carries nothing, both of which are correct."""
    out: Dict[str, str] = {}
    try:
        from ba2_common.config import get_app_setting

        for k in keys:
            v = get_app_setting(k)
            if v:
                out[k] = v
    except Exception:  # noqa: BLE001 — best-effort carry; absence is handled downstream
        return {}
    return out


def _seed_carry_settings(settings: Dict[str, str]) -> None:
    """Insert the carried credential settings into the now-active run DB's AppSetting table so
    in-run providers resolve them via get_app_setting. No-op when nothing was carried."""
    if not settings:
        return
    try:
        from ba2_common.core.models import AppSetting
        from ba2_common.core.db import add_instance

        for key, value in settings.items():
            add_instance(AppSetting(key=key, value_str=value))
    except Exception:  # noqa: BLE001 — if seeding fails, provider construction reports it clearly
        pass


@contextmanager
def backtest_trading_db(run_id: int | str, in_memory: bool = True) -> Iterator[str]:
    """Configure ba2_common.core.db at a fresh sqlite for this run, create the schema,
    and yield the target.

    ``in_memory`` (default True) backs the per-run trading DB with a RAM-only SQLite
    (``:memory:`` + StaticPool). Those order/transaction/recommendation rows are ephemeral —
    ``build_results`` extracts the trades/equity/metrics we keep BEFORE the context exits — so
    holding them in RAM removes the per-write disk fsync that dominates a many-thousand-order
    backtest (the GA fitness path). Pass ``in_memory=False`` to use a throwaway sqlite FILE
    instead (kept on disk after exit for post-mortem inspection) — used for the persisted
    top-N re-runs so their full instance/analysis rows survive the run.

    Either way the override is THREAD-LOCAL (parallel trials never clobber each other's DB)
    and is cleared on exit so the backtest DB never leaks into subsequent (live) code paths.
    """
    # THREAD-LOCAL DB override (not the global): each run/trial points ba2_common's engine at
    # its OWN sqlite ONLY on THIS thread, so parallel optimization trials (ThreadPoolExecutor)
    # never clobber each other's per-run DB. Single-threaded runs work identically (the main
    # thread just gets its own override, cleared on exit).
    #
    # Read credential app-settings (e.g. FMP_API_KEY) from the LIVE app DB BEFORE we switch the
    # engine target. A REAL-data run constructs FMPOHLCVProvider INSIDE this context, and the
    # provider resolves its key via get_app_setting("FMP_API_KEY") -> ba2_common.core.db. Once the
    # engine is pointed at the throwaway run sqlite that key would be invisible (the run DB has no
    # AppSetting rows), so the provider would raise "FMP API key not configured". We capture the
    # key here (from whatever DB is currently active — the global/live one, since no override is
    # set yet on this thread) and re-seed it into the run DB after init.
    carried_settings = _read_carry_settings(_CARRIED_APP_SETTINGS)
    if in_memory:
        target = ":memory:"                         # RAM-only, StaticPool (no disk fsync)
    else:
        path = backtest_db_path(run_id)
        if path.exists():
            path.unlink()
        target = str(path)
    common_db.configure_db_threadlocal(target)      # per-thread engine -> this run's DB
    common_db.init_db()                             # SQLModel.metadata.create_all(get_engine())
    _seed_carry_settings(carried_settings)
    try:
        yield target
    finally:
        # Drop THIS thread's override so the backtest DB never leaks into subsequent (live) code
        # paths or other trials on this thread. (A file DB is left on disk for post-mortem; the
        # in-memory DB is freed when its engine is disposed by clear_threadlocal_db.)
        common_db.clear_threadlocal_db()


def seed_account_definition(
    account_id: int,
    settings: Optional[Dict[str, Any]] = None,
    *,
    name: Optional[str] = None,
    provider: str = "backtest",
    description: Optional[str] = None,
) -> int:
    """Insert an ``AccountDefinition`` row for the BacktestAccount into the backtest DB.

    The inherited ba2_common code loads ``AccountDefinition`` by id (e.g. when
    constructing/validating the account), so the row must exist before the engine
    drives the loop. Returns the row id (== ``account_id`` since we set the PK).

    ``settings`` is accepted for caller symmetry (the BacktestAccount's resolved
    config dict) but is NOT persisted onto AccountDefinition: AccountDefinition has no
    settings column; account settings live in the separate AccountSetting table and
    the BacktestAccount carries its config dict in-process. Passing it here is a no-op
    beyond documenting intent.
    """
    from ba2_common.core.models import AccountDefinition
    from ba2_common.core.db import add_instance

    row = AccountDefinition(
        id=int(account_id),
        name=name or f"backtest-{account_id}",
        provider=provider,
        description=description or f"Backtest simulated broker (run account {account_id})",
    )
    return add_instance(row)


def seed_expert_instance(
    *,
    account_id: int,
    expert_class_name: str,
    enter_market_ruleset_id: int,
    open_positions_ruleset_id: Optional[int] = None,
    virtual_equity_pct: float = 100.0,
    instance_id: Optional[int] = None,
) -> int:
    """Insert an ``ExpertInstance`` row for a backtest expert and return its id.

    The packaged decision path resolves the expert by id in several places — the inherited
    ``_create_transaction_for_order`` reads the recommendation's ``instance_id`` to set
    ``Transaction.expert_id``; ``TradeRiskManagement.review_and_prioritize_pending_orders``
    loads the ``ExpertInstance`` (for the account_id + ruleset ids) and the resolver-provided
    expert object (for settings/balance). So the row MUST exist (with the enter_market ruleset
    linked) before the engine drives the loop.

    Args:
        account_id: the BacktestAccount's AccountDefinition id (FK).
        expert_class_name: the ba2_experts class name (stored in ``ExpertInstance.expert``),
            e.g. ``"FMPEarningsDrift"``.
        enter_market_ruleset_id: the seeded enter ruleset id (see ``default_rulesets``).
        open_positions_ruleset_id: optional open-positions ruleset id (v1: usually None).
        virtual_equity_pct: the expert's share of the account equity (default 100%).
        instance_id: optional explicit PK (so callers can pin the id); auto-assigned if None.

    Returns:
        the ExpertInstance row id.
    """
    from ba2_common.core.models import ExpertInstance
    from ba2_common.core.db import add_instance

    row = ExpertInstance(
        id=instance_id,
        account_id=int(account_id),
        expert=expert_class_name,
        enabled=True,
        virtual_equity_pct=float(virtual_equity_pct),
        enter_market_ruleset_id=int(enter_market_ruleset_id),
        open_positions_ruleset_id=(
            int(open_positions_ruleset_id) if open_positions_ruleset_id is not None else None
        ),
    )
    return add_instance(row)
