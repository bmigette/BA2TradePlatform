# SYMBOL360 — Design

Date: 2026-08-17
Status: Implemented (2026-08-19). See
`docs/superpowers/plans/2026-08-17-symbol360.md` for the accurate, executed
14-task build — a few details below drifted from what actually shipped
during implementation (discoveries that changed the plan, not bugs):
the `congress_trades.py` extraction described in "Files" never happened
(`FMPCongressTradingMixin` turned out to already be centralized — see the
plan's "Key facts" #5); the price chart ships two indicator overlays
(SMA200 + RSI), not four; the dated price-target table has no
implied-upside color tinting. Treat this document for the *why*; treat the
plan document for the *what actually shipped*.

## Problem

There is no single place to see everything the platform's experts know about
one symbol. Answering "what does the platform think about AAPL right now"
means opening several Tools tabs (Analyst Ratings, Penny Screener for RVOL)
plus mentally recombining Weinstein stage, insider activity, earnings drift,
and DeterministicScorer's section scores — each of which already exists
somewhere in the codebase, computed by a different expert or provider.

## Goal

A `SYMBOL360` tab in the Tools page (`ui/pages/tools.py`) that, given a
symbol, shows a research dashboard: price chart, Weinstein phase, RVOL,
recent earnings/PEAD, insider activity, analyst ratings (consensus + dated
price targets), Senate/House trade activity, DeterministicScorer's full
technical/fundamental/macro breakdown, and FactorRanker's composite factor
score. Every metric that has a directional meaning shows a buy/sell/neutral
tag.

**Reuse is the constraint that shapes this design**: every metric must come
from the same code the live/backtest experts already run — never a
reimplementation that can drift from what's actually trading.

## Architecture

### The reuse insight

Every candidate expert (`DeterministicScorer`, `FMPRating`, `FinnHubRating`,
`FMPInsiderClusterBuy`, `FMPEarningsDrift`, `FactorRanker`) already implements
the Phase-1 contract:

```
_gather(providers, as_of) -> data_bundle
_process(data_bundle, settings, as_of) -> Recommendation
analyze_as_of(as_of, context: BacktestContext) -> Recommendation
```

This is the exact path backtests and live analysis exercise. SYMBOL360 does
not re-fetch or re-score anything itself for these cards — it builds a
synthetic `BacktestContext` (live providers, default settings + UI overrides,
`extra={"symbol": symbol}`) and calls the expert's real `analyze_as_of`.

### The constructor problem

Most of these experts' `__init__` eagerly loads a real `ExpertInstance` DB
row via `_load_expert_instance(id)`, raising if it doesn't exist. SYMBOL360
must work with **no** ExpertInstance configured, using each class's default
settings (tweakable per-card in the UI) — never a picked live instance's
tuned config.

`_gather`/`_process`/`analyze_as_of` never touch `self.instance` in any of
the six reviewed experts (only `run_analysis` and the balance/instrument
helpers on the base class do). So `ExpertDataExportInterface` constructs the
object via `cls.__new__(cls)`, bypassing `__init__`/`_load_expert_instance`
entirely, and hand-sets only the safe base state:

```python
self = cls.__new__(cls)
self.id = -1                      # sentinel, never a real DB id
self._settings_cache = {}         # ExtendableSettingsInterface.settings returns {}
                                   # with ZERO DB calls (confirmed: settings property
                                   # short-circuits when _settings_cache is not None)
self.instance = None              # defensive; unused by the export path
cls._ensure_builtin_settings()    # classmethod, no DB
self.logger = get_expert_logger(cls.__name__, self.id)
```

`self.settings.get(x)` then always misses, so every `get_setting_with_interface_default`
call inside the expert falls back to its class default — confirmed safe by
reading `ExtendableSettingsInterface.get_setting_with_interface_default`.

**Contract for any future `ExpertDataExportInterface` implementer**: its
`_gather`/`_process`/`analyze_as_of` must not depend on DB-loaded
`self.instance` state. If one does, the bypass factory raises
`AttributeError`, caught by `export_symbol_data` and surfaced as that one
card's `error` field — never crashes the page.

### Where it lives

Per the Phase 6 convention (shared code lives in the packages, in-tree is a
re-export shim):

- `packages/common/ba2_common/core/interfaces/ExpertDataExportInterface.py`
  — source of truth
- `ba2_trade_platform/core/interfaces/ExpertDataExportInterface.py` — thin
  re-export shim

## The `ExpertDataExportInterface` contract

```python
@dataclass
class ExpertMetric:
    label: str                    # "Piotroski F-Score", "Technical section", "Insider buy value"
    value: Any                    # raw numeric/string value
    display: str                  # formatted for UI, e.g. "7 / 9", "+0.42", "$1.2M"
    signal: Optional[str] = None  # "buy" | "sell" | "neutral" | None (not applicable)
    detail: Optional[str] = None  # short explanation / tooltip

@dataclass
class ExpertDataExport:
    expert_name: str
    symbol: str
    overall_signal: Optional[str]     # rec.signal normalized to buy/sell/hold
    confidence: Optional[float]
    metrics: List[ExpertMetric]
    settings_used: Dict[str, Any]     # defaults + overrides actually applied
    raw: Dict[str, Any]               # untouched rec.raw_outputs (advanced/debug expander)
    error: Optional[str] = None       # populated instead of raising

class ExpertDataExportInterface:
    @classmethod
    def export_default_settings(cls) -> Dict[str, Any]:
        """Class-default settings dict, from get_settings_definitions()."""

    @classmethod
    def export_symbol_data(cls, symbol: str,
                            overrides: Optional[Dict[str, Any]] = None) -> ExpertDataExport:
        """Bypass-construct (see above), build settings = defaults | overrides,
        build a LiveProviderBundle + BacktestContext(extra={"symbol": symbol}),
        call self.analyze_as_of(as_of=None, context=context), adapt the
        Recommendation via _build_export_metrics. Catches all exceptions into
        ExpertDataExport.error so one card's failure never breaks the page."""

    @classmethod
    def _build_export_metrics(cls, rec: "Recommendation",
                               settings: Dict[str, Any]) -> List[ExpertMetric]:
        """Default: one row from rec.signal/confidence/expected_profit_percent.
        Subclasses override for richer per-section rows (DeterministicScorer,
        FMPInsiderClusterBuy's per-buyer rows, etc.)."""
```

`overrides` is exactly what the UI's per-card settings expander produces —
merged over `export_default_settings()` before being handed to
`analyze_as_of` as `context.settings`, so a changed weight/threshold
genuinely re-runs that expert's real math, not a cosmetic relabel.

## Cards and their sources

| Card | Source | Signal rule |
|---|---|---|
| Header (price/mcap/sector/exchange) | Direct: `fmpsdk.quote` + profile (same call `PennyScreenerTab._fetch_quotes_chunked` makes) | n/a, context only |
| Price chart | `ui/components/InstrumentGraph.py`, fed the same way `TradingAgentsUI.py` feeds it: OHLCV via the live `ohlcv()` provider (tz-normalized index) + an `indicators_data` dict (SMA200/RSI14/Donchian/ATR bands) via `providers.indicators()` | n/a, visual context |
| Weinstein Stage | Direct: `ba2_common.core.weinstein.classify_weinstein_stage(closes)` | Stage 2 → buy, Stage 4 → sell, Stage 1/3 → neutral |
| RVOL | Direct: same calc as `StockScreener._enrich_with_rvol` / `PennyScreenerTab`, factored into one shared helper both now call | RVOL ≥ 2.0 → "elevated" (attention flag, not directional) |
| Recent Earnings / PEAD | `FMPEarningsDrift.export_symbol_data` | rec.signal (BUY on fresh beat ≥ threshold) |
| Insider Activity | `FMPInsiderClusterBuy.export_symbol_data` | rec.signal (BUY on detected cluster); per-buyer rows from `raw["cluster"]["buyers"]` |
| Analyst Ratings | `FMPRating.export_symbol_data` + `FinnHubRating.export_symbol_data`, plus dated price-target rows reusing `DeterministicScorer.data.fetch_price_targets` (name/firm/date/target) | each source's rec.signal; individual targets tinted by implied-upside sign |
| Senate/House Activity | Direct: `fetch_senate_trades`/`fetch_house_trades`, extracted once into `ba2_experts.congress_trades` so `FMPSenateTraderCopy`, `FMPSenateTraderWeight`, the existing Tools Senate tab, and SYMBOL360 share one implementation instead of duplicating it | Purchase → buy-tinted row, Sale → sell-tinted row, per trade |
| DeterministicScorer breakdown | `DeterministicScorer.export_symbol_data`, custom `_build_export_metrics` unpacking `raw["technical"]`/`raw["fundamental"]`/`raw["regime"]`: momentum, SMA200 distance, RSI, Donchian, F-Score, Altman Z (+veto flag), quality, value, growth accel, macro regime | section score > +0.1 → buy, < −0.1 → sell, else neutral; Altman veto → hard sell flag |
| FactorRanker score | Special-cased `export_symbol_data` override: pins a synthetic single-symbol static universe (`enabled_instruments={symbol: {...}}`) before `_gather`/`_process`, reads that symbol's row from `raw["book"]` | composite z-score sign → buy/sell |

## UI/UX

**Layout** — single scrolling dashboard, symbol search at top:

```
[ Symbol search box ] [ Search button ]
[ Progress checklist — visible only while loading ]
[ Header card: price / mcap / sector / exchange ]
[ Price chart card ]
[ Weinstein ] [ RVOL ]
[ Recent Earnings/PEAD ] [ Insider Activity ]
[ Analyst Ratings ] [ Senate/House Activity ]
[ DeterministicScorer breakdown ]           <- full width
[ FactorRanker score ]
```

**Progress bar** — every card's fetch runs as its own `asyncio.to_thread`
task, fired concurrently via `asyncio.gather`, not sequentially. A checklist
row per card (`⏳ Weinstein Stage… → ✅ Weinstein Stage (Stage 2)` / `❌
Insider Activity (error)`) updates live as each task resolves, plus an
overall `N / 9 loaded` bar. Cards populate as soon as their own fetch
completes rather than waiting on the slowest one.

**Settings tweak panel** — each card has a collapsed "⚙ Settings" expander
pre-filled from `export_default_settings()`. Editing a value and clicking
"Re-run" re-calls only that card's `export_symbol_data(symbol,
overrides=...)`.

**Persistence** — reuses the existing `app.storage.user` pattern already
established in `ui/account_filter_context.py` (cookie-identified user id,
server-side store, `storage_secret` already wired in `ui/main.py` — survives
app restarts today for the account filter). Each card's overrides are stored
under a namespaced key, e.g.
`app.storage.user["symbol360_settings"]["DeterministicScorer"] = {...}`,
written on "Re-run" and read back to pre-fill the expander on the next
visit — per browser/user. Per the same file's documented caveat, this
read/write happens only on the UI (request) thread, never inside the
`asyncio.to_thread` fetch workers.

## Files

**New:**
- `packages/common/ba2_common/core/interfaces/ExpertDataExportInterface.py`
- `ba2_trade_platform/core/interfaces/ExpertDataExportInterface.py` (shim)
- `packages/experts/ba2_experts/congress_trades.py`
- `ba2_trade_platform/ui/pages/symbol360.py` (`Symbol360Tab`)

**Changed:**
- `DeterministicScorer/__init__.py`, `FMPRating.py`, `FinnHubRating.py`,
  `FMPInsiderClusterBuy.py`, `FMPEarningsDrift.py`, `FactorRanker/__init__.py`
  — add the mixin + optional `_build_export_metrics` override
- `FMPSenateTraderCopy.py`, `FMPSenateTraderWeight.py`, `tools.py` — switch
  to the shared `congress_trades` helper
- `tools.py` — `content()` gains the `SYMBOL360` tab

## Testing

- Unit test per expert's `export_symbol_data` (mocked providers): asserts
  metrics/signal shape, and that the bypass factory never issues a DB query.
- One test asserting `export_symbol_data` degrades to `error=...` on a
  provider exception rather than raising.
- One test for the FactorRanker single-symbol universe override.

## Rollout

Single PR, no DB migration (`app.storage.user` needs none). Ship all 9 cards
together — the interface is already proven per-expert by the existing
golden/backtest tests exercising the same `_gather`/`_process`/`analyze_as_of`
paths this design reuses.
