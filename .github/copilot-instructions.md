# BA2 Trade Platform: AI assistant instructions

The conventions for this repository live in [CLAUDE.md](../CLAUDE.md) and
[AGENTS.md](../AGENTS.md). Read them before changing code; they take precedence over this file.
The most important rules:

1. **Shared code lives in the packages.** Most of `ba2_trade_platform/core`, the non-AI data
   providers and the non-LLM experts are thin re-export shims over `packages/common`
   (`ba2_common`), `packages/providers` (`ba2_providers`) and `packages/experts` (`ba2_experts`).
   Change shared code in the package, never in the shim. Live-only code (brokers, Smart Risk
   Manager, TradingAgents, the LLM stack, `JobManager` / `WorkerQueue` / `TradeManager`, the UI)
   stays in-tree.
2. **No config defaults.** Read configuration with explicit access (`config["quick_think_llm"]`),
   never `.get()` with a default: a missing key must fail loudly.
3. **No fallbacks for live data.** Never substitute a default for a price, balance or quantity
   (`price or 1.0`). Raise an error when the value is missing.
4. **Bump the version before every push.** Changes under `ba2_trade_platform/` bump `APP_VERSION`
   in `ba2_trade_platform/version.py`; changes under `testplatform/` or `packages/` bump
   `TEST_APP_VERSION` in `testplatform/version.py`.
5. **Reuse before writing.** Check `core/utils.py` (shared helpers in `ba2_common.core.utils`) for
   an existing helper first. Log through `ba2_trade_platform.logger`, with `exc_info=True` only
   inside `except` blocks.
