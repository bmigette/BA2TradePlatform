"""Default-assumption valuation snapshot provider backed by finance_calc.

Pulls FCF history, shares, cash/debt and beta through the composed fundamentals
providers (dict contract), then computes CAPM cost of equity (discount rate), a
Gordon-growth DCF over a projected FCF schedule, and a ±100bp rate / ±50bp
terminal-growth sensitivity grid. EVERY assumption is printed in the report.
Missing fundamentals -> "not computable: <reason>", never a fabricated number.
"""

from datetime import datetime
from typing import Any, Dict, Literal

from ba2_common.core.interfaces.ValuationSnapshotInterface import ValuationSnapshotInterface
from ba2_common.core.finance_calc.valuation import (
    CostOfCapitalRequest, compute_cost_of_capital,
    DCFRequest, compute_dcf,
    DCFSensitivityRequest, compute_sensitivity,
)
from ba2_common.core.finance_calc.format import money, num, pct


def _real(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


class FinanceCalcValuationProvider(ValuationSnapshotInterface):
    def __init__(self, fundamentals_overview_provider, fundamentals_details_provider,
                 ohlcv_provider=None,
                 risk_free_rate: float = 0.045,
                 equity_risk_premium: float = 0.05,
                 terminal_growth_rate: float = 0.025,
                 projection_years: int = 5):
        self._overview = fundamentals_overview_provider
        self._details = fundamentals_details_provider
        self._ohlcv = ohlcv_provider  # reserved (beta override); unused in v1
        self.risk_free_rate = risk_free_rate
        self.equity_risk_premium = equity_risk_premium
        self.terminal_growth_rate = terminal_growth_rate
        self.projection_years = projection_years

    def get_provider_name(self) -> str:
        return "finance_calc"

    def get_supported_features(self) -> list[str]:
        return ["valuation_snapshot"]

    def validate_config(self) -> bool:
        return True

    # ---- data assembly (documented dict contracts; see interface docstrings) ----

    def _compute(self, symbol: str, as_of: datetime) -> Dict[str, Any]:
        missing = []

        ov = self._overview.get_fundamentals_overview(symbol, as_of, format_type="dict")
        beta = (ov.get("metrics") or {}).get("beta")
        if not _real(beta):
            missing.append("beta")

        cf = self._details.get_cashflow_statement(symbol, "annual", as_of,
                                                  lookback_periods=4, format_type="dict")
        fcfs = [s.get("free_cash_flow") for s in cf.get("statements", [])]
        fcfs = [f for f in fcfs if _real(f)]  # most-recent first
        if len(fcfs) < 2:
            missing.append("free cash flow history (need >=2 annual statements)")

        inc = self._details.get_income_statement(symbol, "annual", as_of,
                                                 lookback_periods=1, format_type="dict")
        shares = None
        if inc.get("statements"):
            s0 = inc["statements"][0]
            shares = s0.get("weighted_average_shares_diluted") or \
                s0.get("weighted_average_shares_outstanding")
        if not _real(shares) or shares <= 0:
            missing.append("shares outstanding")

        bs = self._details.get_balance_sheet(symbol, "annual", as_of,
                                             lookback_periods=1, format_type="dict")
        net_debt = 0.0
        net_debt_assumed_zero = False
        if bs.get("statements"):
            b0 = bs["statements"][0]
            debt = sum(x for x in (b0.get("short_term_debt"), b0.get("long_term_debt"))
                       if _real(x))
            cash = b0.get("cash_and_cash_equivalents")
            if _real(cash):
                net_debt = debt - cash
            elif _real(debt):
                net_debt = debt
        else:
            net_debt_assumed_zero = True
        # missing balance-sheet detail is NOT fatal: net_debt defaults to 0 and the
        # report says so explicitly.

        if missing:
            return {"symbol": symbol, "computable": False,
                    "reason": "missing " + ", ".join(missing)}

        base_fcf = fcfs[0]
        if base_fcf <= 0:
            # A Gordon-growth DCF projected from a cash-burning base is
            # meaningless — say so, never fabricate a negative "intrinsic value".
            return {"symbol": symbol, "computable": False,
                    "reason": "negative free cash flow"}
        oldest = fcfs[-1]
        n_years = len(fcfs) - 1
        fcf_cagr = (base_fcf / oldest) ** (1 / n_years) - 1 if oldest > 0 else 0.0
        schedule = [base_fcf * (1 + fcf_cagr) ** t for t in range(1, self.projection_years + 1)]

        coc = compute_cost_of_capital(CostOfCapitalRequest(
            risk_free_rate=self.risk_free_rate,
            equity_risk_premium=self.equity_risk_premium, beta=float(beta)))
        wacc = coc["cost_of_equity"]

        dcf = compute_dcf(DCFRequest(
            fcf_schedule=schedule, discount_rate=wacc,
            terminal_method="gordon_growth",
            terminal_growth_rate=self.terminal_growth_rate,
            net_debt=net_debt, shares_outstanding=float(shares)))

        grid = compute_sensitivity(DCFSensitivityRequest(
            fcf_schedule=schedule,
            discount_rates=[wacc - 0.01, wacc, wacc + 0.01],
            terminal_method="gordon_growth",
            terminal_growth_rates=[self.terminal_growth_rate - 0.005,
                                   self.terminal_growth_rate,
                                   self.terminal_growth_rate + 0.005],
            net_debt=net_debt, shares_outstanding=float(shares)))

        return {
            "symbol": symbol,
            "computable": True,
            "assumptions": {
                "risk_free_rate": self.risk_free_rate,
                "equity_risk_premium": self.equity_risk_premium,
                "beta": float(beta),
                "terminal_growth_rate": self.terminal_growth_rate,
                "projection_years": self.projection_years,
                "fcf_growth_source": f"historical FCF CAGR over {n_years}y",
                "net_debt": net_debt,
                "net_debt_assumed_zero": net_debt_assumed_zero,
            },
            "fcf_cagr": fcf_cagr,
            "fcf_schedule": schedule,
            "wacc": wacc,
            "dcf": dcf,
            "sensitivity": grid,
        }

    def get_valuation_snapshot(self, symbol, as_of_date,
                               format_type: Literal["markdown", "dict", "both"] = "markdown"):
        data = self._compute(symbol, as_of_date)
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
            return (f"# Valuation snapshot — {data['symbol']}\n\n"
                    f"not computable: {data['reason']}")
        a = data["assumptions"]
        lines = [
            f"# Valuation snapshot — {data['symbol']} (DEFAULT assumptions — NOT the "
            f"analyst's own estimates)",
            "",
            "## Assumptions (all defaults, printed for audit)",
            f"- risk-free rate {pct(a['risk_free_rate'])} · equity risk premium "
            f"{pct(a['equity_risk_premium'])} · beta {num(a['beta'])} "
            f"-> discount rate (CAPM cost of equity) **{pct(data['wacc'])}**",
            f"- FCF growth: {a['fcf_growth_source']} = {pct(data['fcf_cagr'])} · "
            f"terminal growth {pct(a['terminal_growth_rate'])} · "
            f"{a['projection_years']}y explicit · net debt {money(a['net_debt'])}"
            + (" (net debt assumed 0 — balance sheet detail unavailable)"
               if a.get("net_debt_assumed_zero") else ""),
            "",
            "## Default DCF (Gordon growth)",
            f"- Enterprise value {money(data['dcf']['enterprise_value'])} · equity value "
            f"{money(data['dcf']['equity_value'])}",
            f"- **Intrinsic value/share: {money(data['dcf']['intrinsic_per_share'])}**",
            f"- Terminal value is {pct(data['dcf']['tv_share_of_ev'])} of EV.",
            "",
            f"## Sensitivity range (rate ±100bp x terminal g ±50bp)",
            f"- **{money(data['sensitivity']['low'])} – {money(data['sensitivity']['high'])}** "
            f"per share",
        ]
        return "\n".join(lines)
