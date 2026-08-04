"""Deterministic risk-statistics provider backed by the vendored finance_calc suite.

Computes (never fetches beyond the OHLCV composition): descriptive return stats,
annualized realized vol, max drawdown, VaR (95%, 1d), and benchmark-relative
beta/correlation — all on DAILY bars over the lookback window ending at end_date
(point-in-time: nothing after end_date is ever requested).
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Literal

from ba2_common.core.interfaces.RiskStatsInterface import RiskStatsInterface
from ba2_common.core.finance_calc.risk import (
    pct_returns, compute_beta, compute_correlation, compute_var,
)
from ba2_common.core.finance_calc.statistics import describe
from ba2_common.core.finance_calc.portfolio import performance
from ba2_common.core.finance_calc.format import num, pct

_PERIODS_PER_YEAR = 252  # daily bars


class FinanceCalcRiskStatsProvider(RiskStatsInterface):
    def __init__(self, ohlcv_provider, benchmark_symbol: str = "SPY"):
        self._ohlcv = ohlcv_provider
        self._benchmark = benchmark_symbol

    def get_provider_name(self) -> str:
        return "finance_calc"

    def get_supported_features(self) -> list[str]:
        return ["risk_stats"]

    def validate_config(self) -> bool:
        return True  # no API keys — pure compute over the OHLCV composition

    def _closes(self, symbol: str, start: datetime, end: datetime) -> list[float]:
        df = self._ohlcv.get_ohlcv_data(symbol, start_date=start, end_date=end, interval="1d")
        # OHLCV contract: CAPITALIZED columns (Date, Open, High, Low, Close, Volume).
        return [float(c) for c in df["Close"].tolist()]

    def _compute(self, symbol: str, end_date: datetime, lookback_days: int) -> Dict[str, Any]:
        start = end_date - timedelta(days=lookback_days)
        closes = self._closes(symbol, start, end_date)
        bench = self._closes(self._benchmark, start, end_date)
        if len(closes) < 5:
            return {"symbol": symbol, "computable": False,
                    "reason": f"need >=5 daily closes, got {len(closes)}"}
        rets = pct_returns(closes)
        bench_rets = pct_returns(bench)
        # performance() pairs returns/benchmark with zip(strict=True): unequal lengths
        # raise (IPOs, halt days). Align to the most-recent common window; if the
        # benchmark history is shorter, skip the benchmark block instead of raising.
        bench_aligned = bench_rets[-len(rets):] if len(bench_rets) >= len(rets) else None
        return {
            "symbol": symbol,
            "computable": True,
            "benchmark": self._benchmark,
            "window_days": lookback_days,
            "descriptive": describe(rets),
            "realized_vol_annual": describe(rets)["std_sample"] * (_PERIODS_PER_YEAR ** 0.5),
            "var_95_1d": compute_var(closes, 0.95, 1),
            "beta": compute_beta(closes, bench) if len(bench) >= 3 else None,
            "correlation": compute_correlation({"asset": closes, "benchmark": bench})
                           if len(bench) >= 3 else None,
            "performance": performance(rets, periods_per_year=_PERIODS_PER_YEAR,
                                       benchmark=bench_aligned),
        }

    def get_risk_stats(self, symbol, end_date, lookback_days: int = 365,
                       format_type: Literal["markdown", "dict", "both"] = "markdown"):
        data = self._compute(symbol, end_date, lookback_days)
        if format_type == "dict":
            return data
        text = self._format_as_markdown(data)
        if format_type == "both":
            return {"text": text, "data": data}
        return text

    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        return data

    def _format_as_markdown(self, data: Any) -> str:
        if not data.get("computable"):
            return (f"# Risk statistics — {data['symbol']}\n\n"
                    f"not computable: {data['reason']}")
        d, v, b, perf = (data["descriptive"], data["var_95_1d"],
                         data["beta"], data["performance"])
        lines = [
            f"# Risk statistics — {data['symbol']} (daily, {data['window_days']}d window, "
            f"benchmark {data['benchmark']})",
            "",
            f"- **Realized vol (annualized):** {pct(data['realized_vol_annual'])}",
            f"- **Daily returns:** mean {pct(d['mean'], 2)} · std {pct(d['std_sample'], 2)} · "
            f"skew {num(d['skewness'])} · excess kurtosis {num(d['excess_kurtosis'])}",
            f"- **VaR (95%, 1d):** historical {pct(v['historical_var_pct'])} · "
            f"parametric {pct(v['parametric_var_pct'])}",
            f"- **Max drawdown:** {pct(perf['max_drawdown'])} · "
            f"Sharpe {num(perf['sharpe'])} (t={num(perf['t_stat'])})",
        ]
        if b:
            lines.append(f"- **Beta vs {data['benchmark']}:** {num(b['beta'])} "
                         f"(correlation {num(b['correlation'])}, R² {num(b['r_squared'])})")
        return "\n".join(lines)
