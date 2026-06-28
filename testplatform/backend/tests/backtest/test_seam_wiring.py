"""Phase 2 Task 1: seam wiring + per-run backtest-DB bootstrap.

Run from the backend dir so the ``app.*`` import root resolves:
    ./venv/bin/python -m pytest tests/backtest/test_seam_wiring.py -v
"""
from __future__ import annotations

import pytest


def test_wire_seams_idempotent_and_resolves():
    """wire_backtest_seams() is idempotent and installs the instance resolver."""
    from app.services.backtest.seam_wiring import (
        wire_backtest_seams,
        get_backtest_resolver,
    )

    r1 = wire_backtest_seams()
    r2 = wire_backtest_seams()
    assert r1 is r2  # idempotent: same resolver, seams not re-installed
    assert get_backtest_resolver() is r1

    # Registering an account makes it resolvable through the ba2_common seam.
    r1.register_account(99, "the-account")
    from ba2_common.core.instance_resolver import get_instance_resolver

    assert get_instance_resolver().get_account_instance(99) == "the-account"
    assert get_instance_resolver().get_account_instance("99") == "the-account"  # int-coerced


def test_resolver_unknown_id_raises_loud():
    """Unknown ids raise a clear KeyError (no silent None)."""
    from app.services.backtest.seam_wiring import wire_backtest_seams

    r = wire_backtest_seams()
    with pytest.raises(KeyError):
        r.get_expert_instance(123456789)


def test_expert_register_and_resolve_from_transaction():
    """register_expert + get_account_instance_from_transaction work end to end."""
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.instance_resolver import get_instance_resolver

    r = wire_backtest_seams()
    r.register_expert(7, "the-expert")
    r.register_account(42, "acct-42")
    assert get_instance_resolver().get_expert_instance(7) == "the-expert"

    class _Txn:
        account_id = 42

    assert get_instance_resolver().get_account_instance_from_transaction(_Txn()) == "acct-42"


def test_no_llm_service_fails_loud():
    """The clean experts must not call an LLM; the wired service raises loudly."""
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.interfaces.LLMServiceInterface import (
        get_llm_service,
        LLMServiceNotConfigured,
    )

    wire_backtest_seams()
    svc = get_llm_service()
    with pytest.raises(LLMServiceNotConfigured):
        svc.create_llm("gpt-x")
    with pytest.raises(LLMServiceNotConfigured):
        svc.do_llm_call_with_websearch("gpt-x", "prompt")


def test_trade_conditions_provider_resolver_wired():
    """The provider resolver delegates to ba2_providers.get_provider, and honours a per-run
    OHLCV override (the memoized in-memory provider) for ("ohlcv", *)."""
    from app.services.backtest.seam_wiring import wire_backtest_seams, set_backtest_ohlcv_override
    from ba2_common.core import TradeConditions
    from ba2_providers import get_provider

    wire_backtest_seams()
    resolver = TradeConditions.get_provider_resolver()

    # No override -> delegates to get_provider (same provider TYPE; get_provider builds fresh
    # instances per call, so compare by type rather than identity).
    set_backtest_ohlcv_override(None)
    assert type(resolver("ohlcv", "fmp")) is type(get_provider("ohlcv", "fmp"))

    # Override set -> ("ohlcv", *) returns it; other categories still delegate.
    sentinel = object()
    set_backtest_ohlcv_override(sentinel)
    try:
        assert resolver("ohlcv", "fmp") is sentinel
        assert resolver("fundamentals_details", "fmp") is not sentinel
    finally:
        set_backtest_ohlcv_override(None)


def test_make_indicator_provider_builds_pandas_calc():
    """make_indicator_provider() builds a PandasIndicatorCalc backed by an OHLCV provider.

    PandasIndicatorCalc REQUIRES an ohlcv_provider in its constructor, so the no-arg
    get_provider('indicators','pandas') path would be broken; this asserts we pass one.
    """
    from app.services.backtest.seam_wiring import make_indicator_provider

    ind = make_indicator_provider()
    assert type(ind).__name__ == "PandasIndicatorCalc"

    # And with an explicit ohlcv provider (the engine path).
    from ba2_providers import get_provider

    ohlcv = get_provider("ohlcv", "fmp")
    ind2 = make_indicator_provider(ohlcv)
    assert type(ind2).__name__ == "PandasIndicatorCalc"


def test_backtest_db_isolates():
    """backtest_trading_db points ba2_common.core.db at a per-run DB — RAM-only by default,
    a throwaway file when ``in_memory=False``."""
    from app.services.backtest.backtest_db import backtest_trading_db
    from ba2_common.core import db

    # Default: in-memory (the fast GA fitness path) -> the RAM-only "sqlite://" engine.
    with backtest_trading_db("seamtest") as path:
        assert path == ":memory:"
        assert str(db.get_engine().url) == "sqlite://"

    # Opt-in file DB (the persisted top-N path) still isolates to a per-run sqlite.
    with backtest_trading_db("seamtest", in_memory=False) as path:
        assert path.endswith("run_seamtest.sqlite")
        assert str(path) in str(db.get_engine().url)


def test_backtest_db_fresh_per_run_and_seed_account():
    """A fresh DB is created per run and an AccountDefinition row can be seeded.

    Also proves the schema (from ba2_common init_db) exists by round-tripping the row.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import AccountDefinition

    cfg = {"starting_cash": 100_000.0, "commission_per_trade": 1.0}

    with backtest_trading_db("seedtest"):
        acct_id = seed_account_definition(1, cfg)
        assert acct_id == 1
        row = get_instance(AccountDefinition, 1)
        assert row.name == "backtest-1"
        assert row.provider == "backtest"
        assert row.description  # nullable column but we populate it

    # Re-running the same run id starts from a FRESH DB (empty until re-seeded).
    with backtest_trading_db("seedtest"):
        from ba2_common.core.db import get_all_instances

        assert get_all_instances(AccountDefinition) == []


def test_resolver_registry_is_thread_local():
    """Concurrent backtests register the SAME ids (account_id=1 / expert_id=1) but must NOT see
    each other's instances. The serve runs re-runs in worker threads; a process-global registry let
    run B's BacktestAccount overwrite run A's under id=1 -> cross-run balance corruption (negative
    equity / divergent re-runs). Each thread must resolve only what it registered."""
    import threading
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.instance_resolver import get_instance_resolver

    wire_backtest_seams()
    start = threading.Barrier(3)
    seen = {}

    def worker(tag):
        r = get_instance_resolver()
        r.get_account_instance  # noqa: B018 — ensure attr access works per-thread
        # Each thread registers a DIFFERENT object under the SAME id=1.
        from app.services.backtest.seam_wiring import get_backtest_resolver
        get_backtest_resolver().register_account(1, f"account-{tag}")
        get_backtest_resolver().register_expert(1, f"expert-{tag}")
        start.wait()  # all threads have registered id=1 before anyone reads
        seen[tag] = (
            get_instance_resolver().get_account_instance(1),
            get_instance_resolver().get_expert_instance(1),
        )

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every thread saw ITS OWN registration, not a neighbour's (no cross-thread clobber).
    assert seen["a"] == ("account-a", "expert-a")
    assert seen["b"] == ("account-b", "expert-b")
    assert seen["c"] == ("account-c", "expert-c")


def test_ohlcv_override_is_thread_local():
    """The per-run OHLCV override must be thread-local: concurrent runs setting different overrides
    must not clobber each other (a global override made run A read run B's prices)."""
    import threading
    from app.services.backtest.seam_wiring import (
        set_backtest_ohlcv_override,
        _current_ohlcv_override,
    )

    start = threading.Barrier(3)
    seen = {}

    def worker(tag):
        set_backtest_ohlcv_override(f"provider-{tag}")
        start.wait()  # all threads set their override before anyone reads
        seen[tag] = _current_ohlcv_override()
        set_backtest_ohlcv_override(None)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen == {"a": "provider-a", "b": "provider-b", "c": "provider-c"}
