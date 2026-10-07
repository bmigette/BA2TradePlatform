"""STRUCTURAL guard: decision code never takes "the price" from a bar.

THE RULE (CLAUDE.md, "Prices in decision code"): the CURRENT price is the account
(``account.get_instrument_current_price``: live quote, or in a backtest the decision price of the
engine's price source); HISTORY is the clamped OHLCV provider; nobody reads the last row of a bar
frame as "now". The 2026-10 look-ahead (a 09:30 decision reading the decision day's own finished
daily bar) came from exactly that, in many places at once, so this test makes the next one fail at
review time instead of costing a re-optimisation.

It walks the DECISION code with ``ast`` (``packages/common/ba2_common/core``,
``packages/experts/ba2_experts``, ``ba2_trade_platform/modules/experts``,
``testplatform/backend/app/services/backtest``) and fails on a site of any of these kinds that is not
ALLOWLISTED with a one-line reason (history / indicator / display / the price source's own
implementation / live-only):

  * ``last-row``      ``<expr mentioning Close/Open/High/Low/close>.iloc[-1]``
  * ``last-element``  ``<such a list>[-1]``
  * ``tail(1)``       a one-row tail
  * ``price_at_date`` a call to a bundle's ``price_at_date`` (a bar-based read that is NOT "now")
  * ``source-import`` a direct import of the backtest price source from decision code
  * ``ohlcv-read``    a ``get_ohlcv_data`` call (counted per file: every read must be history)

The allowlist keys are the stripped SOURCE TEXT of the site (not line numbers), so an unrelated edit
above a site does not break it; moving or adding a site, or leaving a stale entry, does. Be pragmatic:
a false positive is fine, add it with its reason.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DIRS = ["packages/common/ba2_common/core", "packages/experts/ba2_experts",
        "ba2_trade_platform/modules/experts", "testplatform/backend/app/services/backtest"]
OHLC = re.compile(r"""["'](Close|Open|High|Low|close|open|high|low)["']|\.(close|open|high|low)\b"""
                  r"""|\b(close|closes|last_close|prev_close|opens|highs|lows)\b""")


def _neg_one(node) -> bool:
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and node.operand.value == 1)


def scan():
    """[(rel, kind, normalised source text)] for every site, sorted."""
    out = []
    for d in DIRS:
        for p in sorted((REPO / d).rglob("*.py")):
            if "tests" in p.parts or "__pycache__" in p.parts:
                continue
            src = p.read_text(encoding="utf-8-sig", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            lines = src.splitlines()
            rel = p.relative_to(REPO).as_posix()
            for n in ast.walk(tree):
                kind = None
                if isinstance(n, ast.Subscript) and _neg_one(n.slice):
                    seg = ast.get_source_segment(src, n) or ""
                    if OHLC.search(seg):
                        kind = "last-row" if (isinstance(n.value, ast.Attribute) and n.value.attr == "iloc") \
                            else "last-element"
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "tail"
                      and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == 1):
                    kind = "tail(1)"
                elif isinstance(n, ast.Call) and ((isinstance(n.func, ast.Attribute) and n.func.attr == "price_at_date")
                                                  or (isinstance(n.func, ast.Name) and n.func.id == "price_at_date")):
                    kind = "price_at_date"
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr in ("get_ohlcv_data", "get_ohlcv_data_unsliced")):
                    kind = "ohlcv-read"
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    mod = getattr(n, "module", "") or ""
                    names = " ".join(a.name for a in n.names)
                    if "price_source" in mod or "AsOfPriceSource" in names or "MemoizedOHLCVProvider" in names:
                        kind = "source-import"
                if kind:
                    text = re.sub(r"\s+", " ", lines[n.lineno - 1].strip())
                    out.append((rel, kind, text))
    return sorted(out)


ENG = "testplatform/backend/app/services/backtest"
# (file, kind, source text) -> reason.  Sites that are NOT a "current price" read.
SITES = {
    ("packages/common/ba2_common/core/backtest_context.py", "last-row", 'return float(df["Close"].iloc[-1])'):
        "LiveProviderBundle.price_at_date: the HISTORICAL close <= as_of (daily clock / historical replay, no account); "
        "refused on an intraday backtest clock by the bundle override (_BacktestProviderBundle) -- (b) historical lookup",
    ("packages/common/ba2_common/core/interfaces/ExpertDataExportInterface.py", "price_at_date",
     "return providers.price_at_date(sym, datetime.now(timezone.utc))"):
        "SYMBOL360 export with no account (display card, live wall clock); not a trading decision -- (b)/display",
    ("packages/common/ba2_common/core/interfaces/MarketExpertInterface.py", "price_at_date",
     "return providers.price_at_date(symbol, as_of)"):
        "the ONE expert seam _decision_price: fallback ONLY for a bundle without an account (historical replay tool); "
        "every backtest/live run resolves the account first",
    ("packages/common/ba2_common/core/interfaces/MarketExpertInterface.py", "price_at_date",
     "daily_price = providers.price_at_date(symbol, as_of)"):
        "the same seam on a DAILY-clock backtest (and the replay tool): the pre-existing bundle read, kept "
        "bit-identical; the daily clock has no decision-time look-ahead (CLAUDE.md: intraday clock only)",
    ("packages/common/ba2_common/core/ohlcv_topup_guard.py", "tail(1)", "series = pd.concat([c.tail(1), new]).sort_index()"):
        "cache top-up guard (data hygiene, not a decision)",
    ("packages/common/ba2_common/core/weinstein.py", "last-element", "price = closes[-1]"):
        "Weinstein stage classifier over a history window (indicator input); callers feed finished history",
    ("packages/experts/ba2_experts/FactorRanker/data.py", "last-row", "return float(closes.iloc[-1])"):
        "FactorRanker E/P + market-cap input: last FINISHED daily close (clamped); a one-night-stale ratio input, "
        "not an order anchor -- OWNER QUESTION Q-FR (use the account price?)",
    ("packages/experts/ba2_experts/PennyMomentumTrader/conditions.py", "last-row",
     'ema = df["close"].ewm(span=period, adjust=False).mean().iloc[-1]'): "LIVE-ONLY expert (no analyze_as_of): indicator",
    ("packages/experts/ba2_experts/PennyMomentumTrader/conditions.py", "last-row",
     'sma = df["close"].rolling(window=period).mean().iloc[-1]'): "LIVE-ONLY expert: indicator",
    ("packages/experts/ba2_experts/PennyMomentumTrader/conditions.py", "last-row", 'price = float(df["close"].iloc[-1])'):
        "LIVE-ONLY expert: its own live 1m-bar price (never runs in a backtest; not in the backtest expert list)",
    ("packages/experts/ba2_experts/PullbackReversion.py", "last-row",
     'close = float(bars["Close"].iloc[-1]) if len(bars) else None'):
        "last COMPLETED daily close of the decision session = the signal input by design (RSI/SMA gates) and the stored "
        "rec price; the order anchor comes from the account -- OWNER QUESTION Q-PB (stored price_at_date is prior close)",
    ("packages/experts/ba2_experts/ETFTrend.py", "last-row", "above_trend = bool(close.iloc[-1] > close.iloc[-trend:].mean())"):
        "ETFTrend signal: last COMPLETED close vs its SMA (trend gate, history by design; frame cut to rows < today)",
    ("packages/experts/ba2_experts/ETFTrend.py", "last-row", "momentum = float(close.iloc[-1] / close.iloc[-lookback - 1] - 1)"):
        "ETFTrend signal: momentum over completed closes (history by design)",
    ("packages/experts/ba2_experts/ETFTrend.py", "last-row", "prices[symbol] = float(close.iloc[-1])"):
        "ETFTrend STORED rec price (current_price / price_at_date, expected_profit 0.0): the prior completed close -- "
        "OWNER QUESTION Q-ETF: store the account price instead (an anchor only; no level is built from it today)",
    ("packages/experts/ba2_experts/PullbackReversion.py", "last-element", "last = float(close[-1])"):
        "PullbackReversion signal input (last completed close vs SMA200/SMA5), history by design",
    (f"{ENG}/backtest_account.py", "source-import", "from .price_source import AsOfPriceSource"):
        "the engine's own account type-annotates its price source",
    (f"{ENG}/daily_backtest_handler.py", "source-import", "from app.services.backtest.price_source import MemoizedOHLCVProvider"):
        "engine wiring (builds the clamped provider)",
    (f"{ENG}/daily_backtest_handler.py", "source-import", "from app.services.backtest.price_source import ("):
        "engine wiring (builds the price source and the clamped provider)",
    (f"{ENG}/daily_backtest_handler.py", "source-import",
     "from app.services.backtest.price_source import evict_memo_if_working_set_changed"): "engine wiring (memo eviction)",
    (f"{ENG}/daily_engine.py", "source-import", "from app.services.backtest.price_source import BacktestCacheMiss"):
        "engine imports an exception type (5 sites)",
    (f"{ENG}/daily_engine.py", "price_at_date", "return super().price_at_date(symbol, as_of)"):
        "_BacktestProviderBundle: the daily-clock historical close (identical to the live bundle); intraday goes to the account",
    (f"{ENG}/market_condition_bt.py", "source-import", "from app.services.backtest.price_source import _is_intraday"):
        "imports the interval classifier (is the run intraday?), not a price",
    (f"{ENG}/seam_wiring.py", "source-import", "from app.services.backtest.price_source import _is_intraday"):
        "imports the interval classifier (is the run intraday?), not a price",
    (f"{ENG}/parity_harness.py", "source-import", "from app.services.backtest.price_source import AsOfPriceSource"):
        "the parity harness builds a price source fixture",
}
SITE_COUNTS = {("testplatform/backend/app/services/backtest/daily_engine.py", "source-import",
                "from app.services.backtest.price_source import BacktestCacheMiss"): 5,
               ("packages/experts/ba2_experts/FMPSenateTraderWeight.py", "source-import",
                "from app.services.backtest.price_source import BacktestCacheMiss"): 1}
SITES[("packages/experts/ba2_experts/FMPSenateTraderWeight.py", "source-import",
       "from app.services.backtest.price_source import BacktestCacheMiss")] = \
    "imports the cache-miss EXCEPTION TYPE only (hermetic refusal), not a price"

# every get_ohlcv_data read is HISTORY (indicators, ATR, regimes, factor inputs, display); on an intraday backtest
# clock the provider clamps it to finished sessions by default. Counted per file so a NEW read is a review event.
OHLCV_READS = {
    "ba2_trade_platform/modules/experts/TradingAgentsUI.py": (1, "UI chart history (live)"),
    "packages/common/ba2_common/core/TradeConditions.py": (4, "recent high/low, relative volume, realised vol: history of FINISHED sessions (clamped)"),
    "packages/common/ba2_common/core/backtest_context.py": (1, "the bundle's historical close (see above)"),
    "packages/common/ba2_common/core/interfaces/MarketDataProviderInterface.py": (1, "the provider's own implementation"),
    "packages/experts/ba2_experts/ETFTrend.py": (1, "signal history (completed closes)"),
    "packages/experts/ba2_experts/FactorRanker/data.py": (2, "factor input history (clamped)"),
    "packages/experts/ba2_experts/PennyMomentumTrader/conditions.py": (1, "LIVE-ONLY expert"),
    "packages/experts/ba2_experts/PennyMomentumTrader/screening.py": (1, "LIVE-ONLY expert"),
    "packages/experts/ba2_experts/PullbackReversion.py": (1, "signal history (completed closes)"),
    "packages/experts/ba2_experts/warm_fetchers.py": (1, "cache pre-warm (not a decision)"),
    f"{ENG}/daily_backtest_handler.py": (1, "regime benchmark history (own bounded reader)"),
    f"{ENG}/fetch_options.py": (1, "option cache build"),
    f"{ENG}/price_source.py": (4, "the price source's own implementation (clamp + explicit unsliced read)"),
    f"{ENG}/results.py": (1, "results/intraday drawdown refinement, not a decision"),
}


def test_every_bar_price_site_is_allowlisted_with_a_reason():
    found = scan()
    counts = Counter(found)
    problems = []
    for (rel, kind, text), n in sorted(counts.items()):
        if kind == "ohlcv-read":
            continue
        want = SITE_COUNTS.get((rel, kind, text), 1)
        if (rel, kind, text) not in SITES:
            problems.append(f"NEW {kind} site in {rel}: `{text}`  -- take the price from the account "
                            f"(account.get_instrument_current_price / MarketExpertInterface._decision_price) or add it "
                            f"to SITES with a reason")
        elif n != want:
            problems.append(f"{kind} site in {rel} appears {n}x, allowlist says {want}x: `{text}`")
    for key in SITES:
        if key not in counts:
            problems.append(f"STALE allowlist entry (site gone or edited): {key}")
    assert not problems, "\n".join(problems)


def test_ohlcv_reads_per_file_match_the_audited_history_reads():
    per_file = Counter(rel for rel, kind, _ in scan() if kind == "ohlcv-read")
    problems = []
    for rel, n in sorted(per_file.items()):
        if rel not in OHLCV_READS:
            problems.append(f"NEW get_ohlcv_data reader file {rel} ({n}): history only; add it to OHLCV_READS with a reason")
        elif OHLCV_READS[rel][0] != n:
            problems.append(f"{rel}: {n} get_ohlcv_data calls, audited {OHLCV_READS[rel][0]}: review the new read "
                            f"(it must be history, never 'the price now')")
    for rel in OHLCV_READS:
        if rel not in per_file:
            problems.append(f"STALE OHLCV_READS entry {rel}")
    assert not problems, "\n".join(problems)


def test_the_scanner_catches_the_defect_it_exists_for(tmp_path, monkeypatch):
    """A decision-code file that reads the last row of a daily frame as 'now' is reported."""
    import sys
    mod = sys.modules[__name__]
    pkg = tmp_path / "packages" / "experts" / "ba2_experts"
    pkg.mkdir(parents=True)
    (pkg / "Bad.py").write_text(
        "def f(df, providers):\n"
        "    px = float(df['Close'].iloc[-1])\n"
        "    y = providers.price_at_date('A', None)\n"
        "    from app.services.backtest.price_source import AsOfPriceSource\n"
        "    return px, y\n")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "DIRS", ["packages/experts/ba2_experts"])
    kinds = {k for _, k, _ in mod.scan()}
    assert {"last-row", "price_at_date", "source-import"} <= kinds
