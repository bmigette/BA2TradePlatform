"""Single seam-wiring entry point for the Phase 6 package migration (Task 7).

The live BA2TradePlatform now *consumes* the extracted ``ba2_common`` /
``ba2_providers`` / ``ba2_experts`` packages (the in-tree ``core`` /
``modules/dataproviders`` / ``modules/experts`` modules are re-export shims). The
packages are kept free of live-platform runtime dependencies (the DB engine, the
expert/account registries + instance caches, the LLM stack / ModelFactory, the
concrete data providers, the InstrumentAutoAdder, the indicator provider). Each of
those is exposed by the package as a *seam* (an injection point with a default that
either raises ``*NotConfigured`` or is a no-op); the live host supplies the
concrete implementation and installs it here, once, at startup.

``wire_all_seams()`` installs every seam. It is:

* **Called FIRST** in ``main.initialize_system()`` -- before the very first
  ``from ba2_trade_platform.core.db import ...`` and before the worker-queue /
  smart-risk-manager-queue / job-manager module imports, any of which may touch a
  seam at *import* time. (See ``main.py``: the wiring is inserted right after the
  ``logger`` / ``config`` imports and before the line-52 db import.)
* **Idempotent** -- guarded by a module-level flag under a lock, so tests / repeated
  startup paths can call it freely and only the first call does work.

Call order (locked by the Phase 6 re-plan):

1. ``ba2_common.core.db.configure_db(config.DB_FILE)`` -- point the package's lazy
   DB engine at the live sqlite path. The engine is built lazily inside
   ``get_engine()``; configuring here, before ``init_db()``, guarantees the live
   path is used.
2. ``ba2_common.core.instance_resolver.set_instance_resolver(LiveInstanceResolver())``
   -- so package interface code can turn an expert/account id (or a transaction)
   into a live instance via the retained live factory funcs + instance caches.
3. ``ba2_common.core.interfaces.set_llm_service(ModelFactoryLLMService())``
   (PACKAGE level -- ``set_llm_service`` lives on the ``ba2_common.core.interfaces``
   package ``__init__``, not the ``LLMServiceInterface`` submodule) -- so package
   expert code gets an LLM through the abstract interface without importing the live
   ModelFactory / langchain.
4. ``ba2_common.core.TradeConditions.set_provider_resolver(get_provider)`` -- route
   the package ``TradeConditions`` provider lookups through the live merge-shim
   ``modules.dataproviders.get_provider`` (live AI providers overlaid on the package
   registry). The live ``get_provider(category, provider_name, **kwargs)`` matches
   the resolver contract ``fn(category, name, **kw)`` exactly.
5. ``ba2_experts.set_instrument_auto_adder_hook(auto_add_instruments_hook)`` -- so
   the package Penny screening can queue screened symbols into the live
   ``InstrumentAutoAdder`` service without importing the live infra.
6. **ATR provider injection (pattern (a) -- RM constructor attribute).** The classic
   risk manager ``ba2_common.core.TradeRiskManagement`` threads its
   ``indicator_provider`` through its constructor and exposes a *no-setter* lazy
   singleton ``get_risk_management()`` (``_risk_management = TradeRiskManagement()``).
   We seed that singleton BEFORE first use with a provider-backed RM:
   ``RM._risk_management = RM.TradeRiskManagement(indicator_provider=
   get_default_indicator_provider())`` so ``position_sizing.get_latest_atr`` can
   resolve ATR through ``ba2_providers`` (which ``ba2_common`` never imports). Seeding
   only when the singleton has not already been built avoids clobbering a host that
   built it earlier.

After ``wire_all_seams()`` returns, ``init_db()`` runs and hits the engine the
DB seam configured.
"""
from __future__ import annotations

import threading

from ..logger import logger

_wired = False
_lock = threading.Lock()


def wire_all_seams() -> None:
    """Inject every live implementation into the package seams. Idempotent.

    Safe to call more than once (and from tests): the first call wires, later calls
    are no-ops. Must be called before any DB / provider / expert / LLM / instance
    resolution.
    """
    global _wired
    with _lock:
        if _wired:
            return

        import ba2_trade_platform.config as config

        # 1) DB seam: point the package engine at the live sqlite path. The engine
        #    is lazy, so this only records the path; get_engine() builds it later.
        from ba2_common.core import db

        db.configure_db(config.DB_FILE)

        # 2) Instance resolver: expert/account id (or transaction) -> live instance.
        from ba2_common.core.instance_resolver import set_instance_resolver

        from .instance_registry import LiveInstanceResolver

        set_instance_resolver(LiveInstanceResolver())

        # 3) LLM service (PACKAGE-level set_llm_service on ba2_common.core.interfaces).
        from ba2_common.core.interfaces import set_llm_service

        from .llm_service import ModelFactoryLLMService

        set_llm_service(ModelFactoryLLMService())

        # 4) TradeConditions provider resolver -> live merge-shim get_provider
        #    (package registry overlaid with the 3 live AI providers).
        from ba2_common.core import TradeConditions

        from ..modules.dataproviders import get_provider

        TradeConditions.set_provider_resolver(get_provider)

        # 5) Instrument auto-adder hook (Penny screening queues symbols via ba2_experts).
        import ba2_experts

        from .seam_helpers import auto_add_instruments_hook

        ba2_experts.set_instrument_auto_adder_hook(auto_add_instruments_hook)

        # 6) ATR provider injection (pattern (a)): seed the classic-RM no-setter
        #    singleton with a provider-backed RM before first use. Only seed if the
        #    singleton has not already been built, to avoid clobbering an existing one.
        try:
            import ba2_common.core.TradeRiskManagement as RM

            if getattr(RM, "_risk_management", None) is None:
                from .seam_helpers import get_default_indicator_provider

                RM._risk_management = RM.TradeRiskManagement(
                    indicator_provider=get_default_indicator_provider()
                )
        except Exception as e:  # pragma: no cover - defensive; ATR sizing degrades gracefully
            logger.warning(f"ATR indicator-provider injection skipped: {e}")

        _wired = True
        logger.info(
            "All ba2_common/providers/experts seams wired to live implementations"
        )
