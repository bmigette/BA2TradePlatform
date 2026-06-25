#!/usr/bin/env python
"""E2E comparison harness for the PROD TradingAgents experts 5 vs 6.

Expert 5 = OpenAI family (gpt5.4_mini / gpt5.4 / gpt5.4_nano)
Expert 6 = Anthropic Claude family (claude_sonnet_4_6 / claude_opus_4_8 / claude_haiku_4_5)

Both experts are configured identically except for the LLM models, so this runs the
real TradingAgents graph for each on the given symbols and compares the decisions.
It primarily validates that the Claude expert works end-to-end (Anthropic web search
+ reasoning_effort) and shows whether the two model families reach similar conclusions.

Safety: reads the experts' real settings from the PROD DB, but neutralizes the only
two prod write-paths so nothing is persisted to prod:
  - LLM usage logging is replaced with a no-op callback
  - persistent memory is disabled (config use_memory=False)
The current price is fetched from yfinance (no prod broker connection).

Usage:
    .venv/Scripts/python.exe test_files/compare_experts_5_6.py
    .venv/Scripts/python.exe test_files/compare_experts_5_6.py AAPL PANW
"""
import sys
import os

# Force UTF-8 stdout/stderr — model output contains unicode (arrows, box chars) that
# crashes the default Windows cp1252 console/file encoding with UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- 1. Point the ORM at the PROD database BEFORE importing anything DB-bound ---
import ba2_trade_platform.config as config
config.DB_FILE = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"

# --- 2. Neutralize prod writes AND capture token usage in-memory ---
# The platform stores tokens but not cost, and provider billing APIs are daily-bucketed
# with reporting lag (useless for a single run). So we capture exact token counts per
# model in-memory via a callback that writes nothing to the DB.
from langchain_core.callbacks import BaseCallbackHandler
import ba2_trade_platform.core.LLMUsageTracker as _ut

# accumulator: model_selection -> {calls, input, output, cache_read, cache_creation}
TOKENS = {}


def _reset_tokens():
    TOKENS.clear()


def _snapshot_tokens():
    import copy
    return copy.deepcopy(TOKENS)


class _TokenCapture(BaseCallbackHandler):
    def __init__(self, model_selection):
        self.model_selection = model_selection or "unknown"

    def on_llm_end(self, response, **kwargs):
        try:
            from ba2_trade_platform.core.prompt_caching import extract_cache_usage
            slot = TOKENS.setdefault(self.model_selection,
                                     {"calls": 0, "input": 0, "output": 0,
                                      "cache_read": 0, "cache_creation": 0})
            slot["calls"] += 1
            gens = getattr(response, "generations", None) or []
            for gl in gens:
                for g in gl:
                    msg = getattr(g, "message", None)
                    um = getattr(msg, "usage_metadata", None) if msg else None
                    if not um:
                        continue
                    slot["input"] += um.get("input_tokens", 0) or 0
                    slot["output"] += um.get("output_tokens", 0) or 0
                    try:
                        c = extract_cache_usage(um)
                        slot["cache_read"] += c.get("cache_read", 0) or 0
                        slot["cache_creation"] += c.get("cache_creation", 0) or 0
                    except Exception:
                        pass
                    break
        except Exception:
            pass


_ut.create_usage_callback = lambda model_selection=None, **k: _TokenCapture(model_selection)

from datetime import datetime
from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
from ba2_trade_platform.core.models_registry import get_model_context_size, parse_model_selection
from ba2_trade_platform.core.types import AnalysisUseCase


_RATINGS = ["STRONG BUY", "STRONG SELL", "OVERWEIGHT", "UNDERWEIGHT", "BUY", "SELL", "HOLD", "NEUTRAL"]


def _short_signal(signal):
    """Extract the rating keyword from the (verbose) processed signal text."""
    if not signal:
        return "?"
    first = str(signal).strip().splitlines()[0].upper()
    for r in _RATINGS:
        if r in first:
            return r
    up = str(signal).upper()
    for r in _RATINGS:
        if r in up:
            return r
    return first[:40]


def _current_price(symbol):
    try:
        import yfinance as yf
        fi = yf.Ticker(symbol).fast_info
        return float(fi["lastPrice"])
    except Exception as e:
        print(f"  [warn] could not fetch yfinance price for {symbol}: {e}")
        return None


def build_provider_args(expert):
    settings_def = expert.get_settings_definitions()
    websearch_model = (
        expert.settings.get("dataprovider_websearch_model")
        or expert.settings.get("openai_provider_model")
        or settings_def["dataprovider_websearch_model"]["default"]
    )
    alpha_vantage_source = expert.settings.get("alpha_vantage_source") or settings_def["alpha_vantage_source"]["default"]
    deep_think_llm = expert.settings.get("deep_think_llm") or settings_def["deep_think_llm"]["default"]
    _, analyst_model_name, _ = parse_model_selection(deep_think_llm)
    return {
        "websearch_model": websearch_model,
        "alpha_vantage_source": alpha_vantage_source,
        "economic_data_days": int(expert.settings.get("economic_data_days") or settings_def["economic_data_days"]["default"]),
        "news_lookback_days": int(expert.settings.get("news_lookback_days") or settings_def["news_lookback_days"]["default"]),
        "social_sentiment_days": int(expert.settings.get("social_sentiment_days") or settings_def["social_sentiment_days"]["default"]),
        "analyst_context_size": get_model_context_size(analyst_model_name),
    }


def run_one(expert, label, symbol, date):
    """Run the real TradingAgents graph for one expert+symbol. Returns a result dict."""
    print(f"\n>>> [{label}] running {symbol} ...")
    cfg = expert._create_tradingagents_config(AnalysisUseCase.ENTER_MARKET)
    cfg["use_memory"] = False  # never write memory to prod
    provider_map = expert._build_provider_map()
    provider_args = build_provider_args(expert)
    selected = expert._build_selected_analysts()

    graph = TradingAgentsGraph(
        selected_analysts=selected,
        debug=False,
        config=cfg,
        provider_map=provider_map,
        provider_args=provider_args,
    )
    price = _current_price(symbol)
    if price:
        print(f"  current price: ${price:.2f}")
    _reset_tokens()
    started = datetime.now()
    final_state, signal = graph.propagate(symbol, date, current_price=price)
    elapsed = (datetime.now() - started).total_seconds()
    tokens = _snapshot_tokens()

    rec = final_state.get("expert_recommendation") or {}
    result = {
        "label": label,
        "symbol": symbol,
        "signal": signal,
        "rating": _short_signal(signal),
        "elapsed_s": round(elapsed, 1),
        "deep": cfg.get("deep_think_llm"),
        "quick": cfg.get("quick_think_llm"),
        "trade_rec": cfg.get("trade_recommendation_llm"),
        "websearch": provider_args["websearch_model"],
        "recommendation": rec,
        "reports": {k: (final_state.get(k) or "") for k in (
            "market_report", "sentiment_report", "news_report",
            "fundamentals_report", "macro_report")},
        "final_decision": final_state.get("final_trade_decision") or "",
        "tokens": tokens,
    }
    # Persist reports + final decision to a markdown file for side-by-side comparison
    try:
        outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmp_reports")
        os.makedirs(outdir, exist_ok=True)
        safe_label = label.replace(" ", "_")
        fpath = os.path.join(outdir, f"{symbol}_{safe_label}.md")
        with open(fpath, "w", encoding="utf-8") as fh:
            fh.write(f"# {symbol} — {label}  (rating: {_short_signal(signal)})\n\n")
            fh.write(f"models: deep={cfg.get('deep_think_llm')} | trade_rec={cfg.get('trade_recommendation_llm')} "
                     f"| websearch={provider_args['websearch_model']}\n\n")
            for key in ("market_report", "sentiment_report", "news_report",
                        "fundamentals_report", "macro_report"):
                fh.write(f"\n\n## {key}\n\n{final_state.get(key) or '(none)'}\n")
            fh.write(f"\n\n## final_trade_decision\n\n{final_state.get('final_trade_decision') or '(none)'}\n")
            fh.write(f"\n\n## processed_signal\n\n{signal}\n")
    except Exception as e:
        print(f"  [warn] could not write reports for {symbol}/{label}: {e}")

    tot_in = sum(v["input"] for v in tokens.values())
    tot_out = sum(v["output"] for v in tokens.values())
    tot_cr = sum(v["cache_read"] for v in tokens.values())
    calls = sum(v["calls"] for v in tokens.values())
    print(f"  -> rating={_short_signal(signal)}  ({elapsed:.0f}s)  |  {calls} LLM calls, "
          f"in={tot_in:,} out={tot_out:,} cache_read={tot_cr:,}")
    return result


def _fmt_rec(rec):
    if not isinstance(rec, dict):
        return str(rec)[:200]
    parts = []
    for k in ("signal", "action", "confidence", "expected_profit_percent", "price_target", "risk_level"):
        if k in rec and rec[k] is not None:
            parts.append(f"{k}={rec[k]}")
    return ", ".join(parts) if parts else str(rec)[:200]


# Pricing per 1M tokens (USD): {provider_model_name: (input, output, cache_read)}
# Sources (fetched 2026-06): OpenAI developers.openai.com/api/docs/pricing,
# Anthropic docs.anthropic.com pricing. Anthropic cache_read = 0.1x input (standard).
PRICES = {
    # OpenAI GPT-5.4 family (input, output, cached-input)
    "gpt-5.4": (2.50, 15.00, 0.25),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4-nano": (0.20, 1.25, 0.02),
    # Anthropic Claude family (input, output, cache-read=0.1x input)
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-haiku-4-5-20251001": (1.00, 5.00, 0.10),
}


def _model_name(model_selection):
    from ba2_trade_platform.core.models_registry import parse_model_selection, get_model_for_provider
    provider, friendly, _ = parse_model_selection(model_selection)
    return get_model_for_provider(friendly, provider) or friendly


def _cost(model_selection, t):
    price = PRICES.get(_model_name(model_selection))
    if not price or price[0] is None:
        return None
    pin, pout, pcr = price
    cr = t.get("cache_read", 0)
    fresh_in = max(t["input"] - cr, 0)
    return (fresh_in * pin + cr * (pcr if pcr is not None else pin) + t["output"] * pout) / 1_000_000


def _print_cost_summary(results, symbols, experts):
    print("\n\n" + "=" * 90)
    print("TOKEN USAGE & COST PER RUN")
    print("=" * 90)
    prov_tot = {}
    for symbol in symbols:
        for label, _ in experts:
            r = results.get((symbol, label), {})
            toks = r.get("tokens") or {}
            if not toks:
                continue
            print(f"\n{label} — {symbol}:")
            run_cost = 0.0
            run_known = True
            for ms, t in sorted(toks.items()):
                c = _cost(ms, t)
                cstr = f"${c:.4f}" if c is not None else "(no price)"
                if c is None:
                    run_known = False
                else:
                    run_cost += c
                print(f"   {ms:<48} calls={t['calls']:>3} in={t['input']:>8,} "
                      f"out={t['output']:>7,} cache_read={t['cache_read']:>8,}  {cstr}")
            ti = sum(v['input'] for v in toks.values())
            to = sum(v['output'] for v in toks.values())
            cstr = f"${run_cost:.4f}" if run_known else f"~${run_cost:.4f}+ (partial pricing)"
            print(f"   {'TOTAL':<48} calls={sum(v['calls'] for v in toks.values()):>3} "
                  f"in={ti:>8,} out={to:>7,}  cost={cstr}")
            p = prov_tot.setdefault(label, {"in": 0, "out": 0, "cache_read": 0, "cost": 0.0, "known": True})
            p["in"] += ti; p["out"] += to
            p["cache_read"] += sum(v['cache_read'] for v in toks.values())
            if run_known:
                p["cost"] += run_cost
            else:
                p["known"] = False

    print("\n" + "-" * 90)
    print("PROVIDER TOTALS (all symbols):")
    for label, _ in experts:
        p = prov_tot.get(label)
        if not p:
            continue
        cstr = f"${p['cost']:.4f}" if p["known"] else f"~${p['cost']:.4f}+ (set PRICES)"
        print(f"  {label}: in={p['in']:,} out={p['out']:,} cache_read={p['cache_read']:,}  cost={cstr}")


def main():
    # Args: bare tokens are symbols; --experts=5,6 selects which experts to run.
    args = sys.argv[1:]
    expert_ids = [5, 6]
    symbols = []
    for a in args:
        if a.startswith("--experts="):
            expert_ids = [int(x) for x in a.split("=", 1)[1].split(",") if x.strip()]
        else:
            symbols.append(a)
    symbols = symbols or ["AAPL", "PANW"]
    date = datetime.now().strftime("%Y-%m-%d")
    print("=" * 90)
    print(f"PROD experts {expert_ids} (5=OpenAI, 6=Claude) — symbols={symbols} date={date}")
    print("=" * 90)

    expert_map = {5: ("E5 OpenAI", TradingAgents(5)), 6: ("E6 Claude", TradingAgents(6))}
    experts = [expert_map[i] for i in expert_ids]

    results = {}
    for symbol in symbols:
        for label, expert in experts:
            try:
                results[(symbol, label)] = run_one(expert, label, symbol, date)
            except Exception as e:
                import traceback
                traceback.print_exc()
                results[(symbol, label)] = {"label": label, "symbol": symbol, "error": str(e)}

    # ---- Comparison summary ----
    print("\n\n" + "=" * 90)
    print("COMPARISON SUMMARY")
    print("=" * 90)
    for symbol in symbols:
        print(f"\n----- {symbol} -----")
        for label, _ in experts:
            r = results.get((symbol, label), {})
            if "error" in r:
                print(f"  {label}: ERROR - {r['error'][:160]}")
                continue
            print(f"  {label}: rating={r.get('rating','?')}  ({r['elapsed_s']}s)")
            print(f"      models: deep={r['deep']} | trade_rec={r['trade_rec']} | websearch={r['websearch']}")
            print(f"      recommendation: {_fmt_rec(r['recommendation'])}")
        # agreement line
        r5 = results.get((symbol, "E5 OpenAI"), {})
        r6 = results.get((symbol, "E6 Claude"), {})
        s5, s6 = r5.get("rating"), r6.get("rating")
        if s5 is not None and s6 is not None:
            verdict = "MATCH" if str(s5).upper() == str(s6).upper() else "DIFFER"
            print(f"  => {verdict}: OpenAI={s5}  vs  Claude={s6}")

    _print_cost_summary(results, symbols, experts)
    print("\nDone.")


if __name__ == "__main__":
    main()
