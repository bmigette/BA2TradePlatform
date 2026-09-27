"""SYMBOL360 as an at-a-glance verdict: pure view-model. No NiceGUI, no network.

WHAT CHANGED AND WHY
--------------------
The page used to render each expert's export as the expert saw it: normalised scores,
tanh scales, section weights and the derivation behind every number. Faithful, and not
readable by a person deciding whether a symbol is worth a look. The operator's ask:
"cards with a score / trend -- (strong) buy / (strong) sell / hold -- and overall ...
I don't need the functions or weights, I want an overview of a symbol to see if it's a
good opportunity at a glance."

So each card now carries three things:

* a VERDICT chip -- Strong Buy / Buy / Hold / Sell / Strong Sell;
* 2-5 plain-English LINES, each marked good / bad / neutral ("Price 12% above its
  200-day average", "RSI 78 -- overbought");
* the underlying FACTS for "Show details" -- analyst counts, price targets, the actual
  RSI and ROE figures. Never the scoring arithmetic.

HOW A VERDICT IS REACHED -- THE TALLY
-------------------------------------
Chosen by the operator over reusing any expert's internal weighting, because it is the
one rule a reader can check by eye:

* a CARD's verdict is the balance of its good and bad lines;
* the OVERALL verdict is the balance of the cards' verdicts, one vote per card.

A line whose input was not measured is OMITTED, never counted as neutral or zero. A card
with no good or bad line at all ABSTAINS (no chip, no vote) rather than voting Hold:
"no insider activity" is not evidence the stock is a Hold, and letting it vote would drag
every overall towards Hold for symbols that simply have less coverage.

THRESHOLDS are named constants below. They are judgement calls about what reads as good
to a human (ROE 15%, RSI 70/30, analyst buy share 60%), not optimised parameters, and
they are stated in each line's text so a reader can disagree with one on sight.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ...logger import logger

# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

STRONG_BUY = "strong_buy"
BUY = "buy"
HOLD = "hold"
SELL = "sell"
STRONG_SELL = "strong_sell"

VERDICT_LABEL = {
    STRONG_BUY: "Strong Buy", BUY: "Buy", HOLD: "Hold", SELL: "Sell",
    STRONG_SELL: "Strong Sell",
}
#: Quasar colour names for the chip. Strong variants get the saturated tone.
VERDICT_COLOR = {
    STRONG_BUY: "green-8", BUY: "positive", HOLD: "grey-6", SELL: "negative",
    STRONG_SELL: "red-10",
}
BULLISH = (STRONG_BUY, BUY)
BEARISH = (SELL, STRONG_SELL)

#: Net balance (good - bad) / lines at or above which a verdict turns strong / leans.
STRONG_THRESHOLD = 0.6
LEAN_THRESHOLD = 0.2

GOOD = "good"
BAD = "bad"
NEUTRAL = "neutral"


@dataclass
class Line:
    """One plain-English observation, marked good / bad / neutral."""
    status: str
    text: str


@dataclass
class DetailTable:
    """A small table for "Show details" -- dated price targets, congress trades."""
    title: str
    columns: List[str]
    rows: List[List[str]]


@dataclass
class SummaryCard:
    key: str
    title: str
    lines: List[Line] = field(default_factory=list)
    facts: List[Tuple[str, str]] = field(default_factory=list)
    tables: List[DetailTable] = field(default_factory=list)
    #: Context cards (market backdrop) describe the market, not this stock, and never vote.
    votes: bool = True
    #: Why the card has nothing to say -- a missing key, a failed fetch, no coverage.
    unavailable: Optional[str] = None

    @property
    def verdict(self) -> Optional[str]:
        """The tally of this card's lines, or ``None`` when it abstains."""
        if self.unavailable is not None:
            return None
        return verdict_from_balance(
            sum(1 for ln in self.lines if ln.status == GOOD),
            sum(1 for ln in self.lines if ln.status == BAD),
            len(self.lines))

    @property
    def tally_text(self) -> str:
        good = sum(1 for ln in self.lines if ln.status == GOOD)
        bad = sum(1 for ln in self.lines if ln.status == BAD)
        return f"{good} good · {bad} bad"


@dataclass
class Overall:
    verdict: Optional[str]
    bullish: int
    neutral: int
    bearish: int
    abstained: int

    @property
    def voting(self) -> int:
        return self.bullish + self.neutral + self.bearish

    @property
    def answer(self) -> Optional[str]:
        """The yes/no the operator asked for. Hold is a NO -- no clear edge is not a buy."""
        if self.verdict is None:
            return None
        return "YES" if self.verdict in BULLISH else "NO"

    @property
    def summary(self) -> str:
        if self.voting == 0:
            return "Not enough data to judge"
        parts = [f"{self.bullish} of {self.voting} areas bullish",
                 f"{self.neutral} neutral", f"{self.bearish} bearish"]
        if self.abstained:
            parts.append(f"{self.abstained} with no signal")
        return " · ".join(parts)


def verdict_from_balance(good: int, bad: int, total: int) -> Optional[str]:
    """Map a good/bad tally to a verdict. ``None`` (abstain) when nothing was good or bad.

    Neutral lines stay in the denominator on purpose: two good observations among five is
    a weaker case than two among two, and the verdict should say so.
    """
    if good + bad == 0 or total <= 0:
        return None
    net = (good - bad) / float(total)
    if net >= STRONG_THRESHOLD:
        return STRONG_BUY
    if net >= LEAN_THRESHOLD:
        return BUY
    if net <= -STRONG_THRESHOLD:
        return STRONG_SELL
    if net <= -LEAN_THRESHOLD:
        return SELL
    return HOLD


def overall_from_cards(cards: Iterable[SummaryCard]) -> Overall:
    """One vote per voting card. Context cards and abstaining cards do not vote."""
    bullish = neutral = bearish = abstained = 0
    for card in cards:
        if not card.votes:
            continue
        v = card.verdict
        if v is None:
            abstained += 1
        elif v in BULLISH:
            bullish += 1
        elif v in BEARISH:
            bearish += 1
        else:
            neutral += 1
    total = bullish + neutral + bearish
    return Overall(verdict_from_balance(bullish, bearish, total),
                   bullish, neutral, bearish, abstained)


# ---------------------------------------------------------------------------
# Small formatting helpers. A None input is never formatted as 0.
# ---------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v: Optional[float], nd: int = 1, signed: bool = True) -> str:
    return "n/a" if v is None else (f"{v:+.{nd}f}%" if signed else f"{v:.{nd}f}%")


def _money(v: Optional[float]) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _big_money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"${v / div:,.2f}{unit}"
    return f"${v:,.0f}"


def _unavailable(export) -> Optional[str]:
    """Why an expert export has nothing usable, or ``None`` when it does."""
    if export is None:
        return "No data (missing API key or fetch failed)"
    error = getattr(export, "error", None)
    if error:
        first = str(error).splitlines()[0]
        return f"Unavailable: {first[:140]}"
    if getattr(export, "skipped", False):
        return "Skipped by the expert (e.g. not enough history or coverage)"
    return None


def _raw(export) -> Dict[str, Any]:
    return dict(getattr(export, "raw", None) or {})


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def build_header(symbol: str, header: Optional[Dict[str, Any]]) -> Dict[str, str]:
    quote = (header or {}).get("quote") or {}
    profile = (header or {}).get("profile") or {}
    price = _num(quote.get("price"))
    change = _num(quote.get("changesPercentage"))
    cap = _num(quote.get("marketCap")) or _num(profile.get("mktCap"))
    return {
        "symbol": symbol,
        "name": profile.get("companyName") or quote.get("name") or "",
        "price": _money(price),
        "change": _pct(change, 2),
        "change_status": (GOOD if (change or 0) > 0 else BAD if (change or 0) < 0 else NEUTRAL)
        if change is not None else NEUTRAL,
        "sector": " · ".join(x for x in (profile.get("sector"), profile.get("industry")) if x),
        "market_cap": _big_money(cap),
    }


# ---------------------------------------------------------------------------
# Trend & Momentum
# ---------------------------------------------------------------------------

#: Price this far above/below its long-term average counts as a trend, either way.
TREND_BAND_PCT = 2.0
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
#: A normalised technical leg beyond this reads as a clear signal.
LEG_SIGNAL = 0.15
#: Position in the trailing high/low channel (-1 bottom .. +1 top).
BREAKOUT_BAND = 0.5


def build_trend_card(ds_export, weinstein: Optional[Dict[str, Any]],
                     quote: Optional[Dict[str, Any]]) -> SummaryCard:
    card = SummaryCard("trend", "Trend & Momentum")
    tech = (_raw(ds_export).get("technical") or {}) if _unavailable(ds_export) is None else {}
    comps = tech.get("components") or {}
    period = int((getattr(ds_export, "settings_used", None) or {}).get("sma_trend_period", 200))

    dist = _num((comps.get("dist_sma_trend") or {}).get("raw"))
    if dist is not None:
        pct = dist * 100.0
        if pct >= TREND_BAND_PCT:
            card.lines.append(Line(GOOD, f"Price {pct:.1f}% above its {period}-day average — uptrend"))
        elif pct <= -TREND_BAND_PCT:
            card.lines.append(Line(BAD, f"Price {abs(pct):.1f}% below its {period}-day average — downtrend"))
        else:
            card.lines.append(Line(NEUTRAL, f"Price near its {period}-day average ({pct:+.1f}%)"))
        card.facts.append((f"Price vs {period}-day average", _pct(pct)))

    mom = comps.get("momentum_vol_adj") or {}
    mom_norm = _num(mom.get("normalized"))
    if mom_norm is not None:
        if mom_norm >= LEG_SIGNAL:
            card.lines.append(Line(GOOD, "Strong momentum over the past year"))
        elif mom_norm <= -LEG_SIGNAL:
            card.lines.append(Line(BAD, "Weak momentum over the past year"))
        else:
            card.lines.append(Line(NEUTRAL, "Flat momentum over the past year"))

    rsi = _num((comps.get("rsi_meanrev") or {}).get("raw"))
    if rsi is not None:
        if rsi >= RSI_OVERBOUGHT:
            card.lines.append(Line(BAD, f"RSI {rsi:.0f} — overbought, stretched short term"))
        elif rsi <= RSI_OVERSOLD:
            card.lines.append(Line(GOOD, f"RSI {rsi:.0f} — oversold, potential rebound"))
        else:
            card.lines.append(Line(NEUTRAL, f"RSI {rsi:.0f} — neither overbought nor oversold"))
        card.facts.append(("RSI (14)", f"{rsi:.1f}"))

    brk = _num((comps.get("donchian_breakout") or {}).get("raw"))
    if brk is not None:
        if brk >= BREAKOUT_BAND:
            card.lines.append(Line(GOOD, "Trading near the top of its recent range"))
        elif brk <= -BREAKOUT_BAND:
            card.lines.append(Line(BAD, "Trading near the bottom of its recent range"))

    stage = (weinstein or {}).get("stage")
    stage_text = {
        1: (NEUTRAL, "Weinstein Stage 1 — basing"),
        2: (GOOD, "Weinstein Stage 2 — advancing"),
        3: (NEUTRAL, "Weinstein Stage 3 — topping"),
        4: (BAD, "Weinstein Stage 4 — declining"),
    }.get(stage)
    if stage_text:
        card.lines.append(Line(*stage_text))
        card.facts.append(("Weinstein stage", str(stage)))

    adx = _num(tech.get("adx"))
    if adx is not None:
        card.facts.append(("Trend strength (ADX)",
                           f"{adx:.1f} ({'trending' if tech.get('trending') else 'not trending'})"))
    q = quote or {}
    for label, key in (("50-day average", "priceAvg50"), ("200-day average", "priceAvg200"),
                       ("52-week high", "yearHigh"), ("52-week low", "yearLow")):
        v = _num(q.get(key))
        if v is not None:
            card.facts.append((label, _money(v)))
    atr = _num(tech.get("atr"))
    if atr is not None:
        card.facts.append(("Average daily range (ATR)", _money(atr)))

    if not card.lines:
        card.unavailable = _unavailable(ds_export) or "No price history to judge the trend"
    return card


# ---------------------------------------------------------------------------
# Fundamentals & financial health
# ---------------------------------------------------------------------------

ROE_GOOD = 0.15
ROE_POOR = 0.05
PIOTROSKI_GOOD = 7
PIOTROSKI_POOR = 3
#: Altman's own zones: above SAFE is safe, below DISTRESS is financial distress.
ALTMAN_SAFE = 2.99
ALTMAN_DISTRESS = 1.81


def build_fundamentals_card(ds_export) -> SummaryCard:
    card = SummaryCard("fundamentals", "Fundamentals & Health")
    if _unavailable(ds_export) is not None:
        card.unavailable = _unavailable(ds_export)
        return card
    fund = _raw(ds_export).get("fundamental") or {}
    snap = fund.get("snapshot") or {}
    ev = fund.get("evidence") or {}

    quality = ev.get("quality") or {}
    roe = _num(quality.get("roe"))
    if roe is not None:
        if roe >= ROE_GOOD:
            card.lines.append(Line(GOOD, f"Return on equity {roe * 100:.1f}% — highly profitable"))
        elif roe < ROE_POOR:
            card.lines.append(Line(BAD, f"Return on equity {roe * 100:.1f}% — weak profitability"))
        else:
            card.lines.append(Line(NEUTRAL, f"Return on equity {roe * 100:.1f}% — moderate"))
        card.facts.append(("Return on equity", _pct(roe * 100, signed=False)))
        card.facts.append(("Net income", _big_money(_num(quality.get("net_income")))))
        card.facts.append(("Shareholder equity", _big_money(_num(quality.get("equity")))))

    fscore = snap.get("fscore")
    if fscore is not None:
        if fscore >= PIOTROSKI_GOOD:
            card.lines.append(Line(GOOD, f"Piotroski {fscore}/9 — strong, improving financials"))
        elif fscore <= PIOTROSKI_POOR:
            card.lines.append(Line(BAD, f"Piotroski {fscore}/9 — weak financials"))
        else:
            card.lines.append(Line(NEUTRAL, f"Piotroski {fscore}/9 — average financials"))
        card.facts.append(("Piotroski F-Score", f"{fscore} / 9"))
        # components: [{"name", "rule", "passed" (True/False/None), ...}]. None means the
        # test could not be run (missing prior-year data) -- shown as such, never a fail.
        for comp in (ev.get("piotroski") or {}).get("components") or []:
            if not isinstance(comp, dict) or not comp.get("name"):
                continue
            passed = comp.get("passed")
            outcome = "pass" if passed is True else "fail" if passed is False else "n/a"
            card.facts.append((f"  {str(comp['name']).replace('_', ' ')}", outcome))

    z = _num(snap.get("z"))
    if z is not None:
        if z >= ALTMAN_SAFE:
            card.lines.append(Line(GOOD, f"Altman Z {z:.2f} — low bankruptcy risk"))
        elif z < ALTMAN_DISTRESS:
            card.lines.append(Line(BAD, f"Altman Z {z:.2f} — financial distress zone"))
        else:
            card.lines.append(Line(NEUTRAL, f"Altman Z {z:.2f} — grey zone"))
        card.facts.append(("Altman Z-Score", f"{z:.2f}"))

    if not card.lines:
        card.unavailable = "No financial statements available"
    return card


# ---------------------------------------------------------------------------
# Valuation & Growth
# ---------------------------------------------------------------------------

def _growth_line(label: str, detail: Dict[str, Any]) -> Optional[Line]:
    latest = _num(detail.get("latest_growth"))
    accel = _num(detail.get("acceleration"))
    if latest is None:
        return None
    pct = latest * 100.0
    if latest < 0:
        return Line(BAD, f"{label} shrinking ({pct:+.1f}% last year)")
    if accel is not None and accel > 0:
        return Line(GOOD, f"{label} growing {pct:.1f}% and accelerating")
    trail = _num(detail.get("trailing_mean"))
    slowing = f", slowing from {trail * 100:.1f}%" if trail is not None else ""
    return Line(NEUTRAL, f"{label} growing {pct:.1f}%{slowing}")


def build_valuation_card(ds_export, quote: Optional[Dict[str, Any]]) -> SummaryCard:
    card = SummaryCard("valuation", "Valuation & Growth")
    if _unavailable(ds_export) is not None:
        card.unavailable = _unavailable(ds_export)
        return card
    fund = _raw(ds_export).get("fundamental") or {}
    snap = fund.get("snapshot") or {}
    ev = fund.get("evidence") or {}

    value = ev.get("value") or {}
    ey = _num(value.get("earnings_yield"))
    value_norm = _num(snap.get("value_norm"))
    if ey is not None and value_norm is not None:
        text = f"Earnings yield {ey * 100:.1f}% on enterprise value"
        if value_norm >= LEG_SIGNAL:
            card.lines.append(Line(GOOD, text + " — attractively priced"))
        elif value_norm <= -LEG_SIGNAL:
            card.lines.append(Line(BAD, text + " — expensive"))
        else:
            card.lines.append(Line(NEUTRAL, text + " — fairly priced"))
        card.facts.append(("Earnings yield (EBIT / EV)", _pct(ey * 100, signed=False)))
        card.facts.append(("Enterprise value", _big_money(_num(value.get("enterprise_value")))))

    growth = ev.get("growth") or {}
    for label, key in (("Revenue", "revenue"), ("Earnings per share", "eps")):
        detail = growth.get(key) or {}
        line = _growth_line(label, detail)
        if line is not None:
            card.lines.append(line)
            card.facts.append((f"{label} growth (last year)",
                               _pct(_num(detail.get("latest_growth")) * 100)))

    pe = _num((quote or {}).get("pe"))
    if pe is not None:
        card.facts.append(("P/E ratio", f"{pe:.1f}"))

    if not card.lines:
        card.unavailable = "No valuation or growth data available"
    return card


# ---------------------------------------------------------------------------
# Analysts
# ---------------------------------------------------------------------------

ANALYST_BUY_SHARE_GOOD = 0.60
ANALYST_SELL_SHARE_BAD = 0.30
UPSIDE_GOOD_PCT = 10.0


def build_analyst_card(analyst: Optional[Dict[str, Any]],
                       current_price: Optional[float]) -> SummaryCard:
    card = SummaryCard("analysts", "Analysts")
    analyst = analyst or {}
    fmp, finnhub = analyst.get("fmp"), analyst.get("finnhub")
    calc = (_raw(fmp).get("calc") or {}) if _unavailable(fmp) is None else {}

    counts = None
    if calc.get("analyst_count"):
        counts = {"Strong Buy": calc.get("strong_buy", 0), "Buy": calc.get("buy", 0),
                  "Hold": calc.get("hold", 0), "Sell": calc.get("sell", 0),
                  "Strong Sell": calc.get("strong_sell", 0)}
        source = "FMP"
    elif _unavailable(finnhub) is None and (_raw(finnhub).get("counts") or {}):
        c = _raw(finnhub)["counts"]
        counts = {"Strong Buy": c.get("strongBuy", 0), "Buy": c.get("buy", 0),
                  "Hold": c.get("hold", 0), "Sell": c.get("sell", 0),
                  "Strong Sell": c.get("strongSell", 0)}
        source = "Finnhub"

    if counts:
        total = sum(int(v or 0) for v in counts.values())
        if total > 0:
            buys = int(counts["Strong Buy"] or 0) + int(counts["Buy"] or 0)
            sells = int(counts["Sell"] or 0) + int(counts["Strong Sell"] or 0)
            buy_share, sell_share = buys / total, sells / total
            text = f"{buys} of {total} analysts rate it Buy ({buy_share * 100:.0f}%)"
            if buy_share >= ANALYST_BUY_SHARE_GOOD:
                card.lines.append(Line(GOOD, text))
            elif sell_share >= ANALYST_SELL_SHARE_BAD:
                card.lines.append(Line(BAD, f"{sells} of {total} analysts rate it Sell "
                                             f"({sell_share * 100:.0f}%)"))
            else:
                card.lines.append(Line(NEUTRAL, text + " — mixed views"))
            card.facts.append(("Analysts covering", f"{total} ({source})"))
            for bucket, n in counts.items():
                card.facts.append((f"  {bucket}", str(int(n or 0))))

    price = _num(current_price)
    consensus = _num(calc.get("target_consensus"))
    if consensus is not None and price:
        upside = (consensus / price - 1.0) * 100.0
        if upside >= UPSIDE_GOOD_PCT:
            card.lines.append(Line(GOOD, f"{upside:+.0f}% upside to the consensus target {_money(consensus)}"))
        elif upside < 0:
            card.lines.append(Line(BAD, f"Trades above the consensus target {_money(consensus)} ({upside:+.0f}%)"))
        else:
            card.lines.append(Line(NEUTRAL, f"{upside:+.0f}% upside to the consensus target {_money(consensus)}"))
    for label, key in (("Consensus target", "target_consensus"), ("Median target", "target_median"),
                       ("High target", "target_high"), ("Low target", "target_low")):
        v = _num(calc.get(key))
        if v is not None:
            suffix = f"  ({(v / price - 1.0) * 100:+.1f}%)" if price else ""
            card.facts.append((label, _money(v) + suffix))

    targets = analyst.get("price_targets") or []
    if targets:
        rows = sorted(
            ([str(t.get("publishedDate") or "")[:10], t.get("analystCompany") or "",
              t.get("analystName") or "", _money(_num(t.get("priceTarget")))]
             for t in targets),
            key=lambda r: r[0], reverse=True)
        card.tables.append(DetailTable("Recent price targets",
                                       ["Date", "Firm", "Analyst", "Target"], rows[:15]))

    if not card.lines:
        card.unavailable = (_unavailable(fmp) if _unavailable(fmp) and _unavailable(finnhub)
                            else "No analyst coverage")
    return card


# ---------------------------------------------------------------------------
# Earnings
# ---------------------------------------------------------------------------

SURPRISE_BAND_PCT = 2.0


def build_earnings_card(earnings_export) -> SummaryCard:
    card = SummaryCard("earnings", "Earnings")
    if _unavailable(earnings_export) is not None:
        card.unavailable = _unavailable(earnings_export)
        return card
    ev = _raw(earnings_export).get("evaluation") or {}
    surprise = _num(ev.get("surprise_pct"))
    days = ev.get("days_since_report")
    when = f" ({days} days ago)" if isinstance(days, int) else ""
    if surprise is not None:
        if surprise >= SURPRISE_BAND_PCT:
            card.lines.append(Line(GOOD, f"Beat estimates by {surprise:.1f}% last quarter{when}"))
        elif surprise <= -SURPRISE_BAND_PCT:
            card.lines.append(Line(BAD, f"Missed estimates by {abs(surprise):.1f}% last quarter{when}"))
        else:
            card.lines.append(Line(NEUTRAL, f"Results in line with estimates{when}"))
    if ev.get("is_signal") is True:
        card.lines.append(Line(GOOD, "Still inside the post-earnings drift window"))

    for label, key, fmt in (("Report date", "report_date", str),
                            ("Reported EPS", "reported_eps", lambda v: f"{float(v):.2f}"),
                            ("Estimated EPS", "estimated_eps", lambda v: f"{float(v):.2f}"),
                            ("Surprise", "surprise_pct", lambda v: _pct(float(v)))):
        v = ev.get(key)
        if v is not None:
            try:
                card.facts.append((label, fmt(v)))
            except (TypeError, ValueError):
                pass
    if not card.lines:
        card.unavailable = "No recent earnings report"
    return card


# ---------------------------------------------------------------------------
# Insiders & Congress
# ---------------------------------------------------------------------------

#: Congressional disclosures are full history; only this recent window says anything.
CONGRESS_WINDOW_DAYS = 180


def _is_purchase(kind: str) -> bool:
    k = kind.lower()
    return "purchase" in k or k.startswith("buy")


def _is_sale(kind: str) -> bool:
    k = kind.lower()
    return "sale" in k or k.startswith("sell")


def build_insider_card(insider_export, congress: Optional[Dict[str, Any]],
                       today: date) -> SummaryCard:
    card = SummaryCard("insiders", "Insiders & Congress")

    if _unavailable(insider_export) is None:
        cluster = _raw(insider_export).get("cluster") or {}
        buyers = int(cluster.get("buyer_count") or 0)
        buy_value = _num(cluster.get("buy_value"))
        sell_value = _num(cluster.get("sell_value"))
        if cluster.get("is_cluster") is True:
            card.lines.append(Line(GOOD, f"Cluster of {buyers} insiders buying ({_big_money(buy_value)})"))
        elif buyers > 0:
            card.lines.append(Line(NEUTRAL, f"{buyers} insider purchase(s) ({_big_money(buy_value)}), not a cluster"))
        # Insider SELLING is deliberately not a bad line: compensation and
        # diversification sales are routine, and the expert itself treats them as
        # context, never as a signal.
        if buy_value is not None:
            card.facts.append(("Insider buying", _big_money(buy_value)))
        if sell_value is not None:
            card.facts.append(("Insider selling (routine, not scored)", _big_money(sell_value)))
        buyer_rows = [[name, _big_money(_num(v))] for name, v in
                      sorted((cluster.get("buyers") or {}).items(), key=lambda kv: -(kv[1] or 0))]
        if buyer_rows:
            card.tables.append(DetailTable("Insider buyers", ["Insider", "Amount"], buyer_rows))

    if congress is not None:
        cutoff = today - timedelta(days=CONGRESS_WINDOW_DAYS)
        rows, buys, sells = [], 0, 0
        for chamber in ("senate", "house"):
            for t in congress.get(chamber) or []:
                kind = str(t.get("type") or t.get("transactionType") or "")
                when = str(t.get("transactionDate") or "")[:10]
                try:
                    recent = datetime.strptime(when, "%Y-%m-%d").date() >= cutoff
                except ValueError:
                    recent = False
                if recent:
                    buys += _is_purchase(kind)
                    sells += _is_sale(kind)
                name = f"{t.get('firstName') or ''} {t.get('lastName') or ''}".strip() or "Unknown"
                rows.append([when, chamber.capitalize(), name, kind,
                             str(t.get("amount") or t.get("amountRange") or "")])
        window = f"last {CONGRESS_WINDOW_DAYS // 30} months"
        if buys > sells:
            card.lines.append(Line(GOOD, f"Congress members net buyers: {buys} buys vs {sells} sales ({window})"))
        elif sells > buys:
            card.lines.append(Line(BAD, f"Congress members net sellers: {sells} sales vs {buys} buys ({window})"))
        elif buys:
            card.lines.append(Line(NEUTRAL, f"Congress trading balanced: {buys} buys, {sells} sales ({window})"))
        if rows:
            rows.sort(key=lambda r: r[0], reverse=True)
            card.tables.append(DetailTable("Congressional trades",
                                           ["Date", "Chamber", "Member", "Type", "Amount"], rows[:20]))

    if not card.lines:
        card.unavailable = "No recent insider or congressional buying"
    return card


# ---------------------------------------------------------------------------
# FactorRanker's factors -- its INPUTS, judged on absolute bars
# ---------------------------------------------------------------------------
#
# FactorRanker ranks a universe by z-scoring these against each other, so its score
# means nothing for one symbol. Its inputs do: each leg below is the exact quantity
# FactorRanker measures (same fetchers, same formulas), judged against a fixed bar
# instead of against the rest of the universe. Its fourth factor, PEAD, is the
# Earnings card.

MOMENTUM_BAND = 0.10
#: E/P 5% ~ P/E 20; below 2% ~ P/E above 50, or a loss.
EARNINGS_YIELD_GOOD = 0.05
EARNINGS_YIELD_POOR = 0.02
FCF_YIELD_GOOD = 0.05
#: Novy-Marx gross profitability; ~0.3 is a typical large-cap.
GROSS_PROFITABILITY_GOOD = 0.30
GROSS_PROFITABILITY_POOR = 0.10
#: Sloan accruals = (net income - operating cash flow) / assets. At or below zero the
#: earnings are fully backed by cash; well above it they run ahead of the cash.
ACCRUALS_POOR = 0.10


def _ratio(numerator: Any, denominator: Any) -> Optional[float]:
    """numerator / denominator, or None unless both are measured and the denominator is
    positive -- FactorRanker's own guard (a 0 or negative EV/price/assets makes the
    yield uninterpretable, while a numerator of exactly 0 is a real reading)."""
    n, d = _num(numerator), _num(denominator)
    if n is None or d is None or d <= 0:
        return None
    return n / d


def build_factors_card(factors: Optional[Dict[str, Any]]) -> SummaryCard:
    card = SummaryCard("factors", "Momentum, Value & Quality factors")
    if not factors:
        card.unavailable = "Factor data unavailable (FMP key missing or fetch failed)"
        return card

    mom = _num(factors.get("momentum_12_1"))
    if mom is not None:
        text = f"12-month return (excluding the last month) {mom * 100:+.1f}%"
        if mom >= MOMENTUM_BAND:
            card.lines.append(Line(GOOD, text + " — strong momentum"))
        elif mom <= -MOMENTUM_BAND:
            card.lines.append(Line(BAD, text + " — weak momentum"))
        else:
            card.lines.append(Line(NEUTRAL, text + " — flat"))
        card.facts.append(("Momentum 12-1", _pct(mom * 100)))

    value = factors.get("value") or {}
    ey = _ratio(value.get("eps_ttm"), value.get("price"))
    if ey is not None:
        pe = f" (P/E {1 / ey:.1f})" if ey > 0 else ""
        if ey >= EARNINGS_YIELD_GOOD:
            card.lines.append(Line(GOOD, f"Earnings yield {ey * 100:.1f}%{pe} — cheap on earnings"))
        elif ey < 0:
            card.lines.append(Line(BAD, f"Earnings yield {ey * 100:.1f}% — loss-making"))
        elif ey < EARNINGS_YIELD_POOR:
            card.lines.append(Line(BAD, f"Earnings yield {ey * 100:.1f}%{pe} — expensive on earnings"))
        else:
            card.lines.append(Line(NEUTRAL, f"Earnings yield {ey * 100:.1f}%{pe} — fairly priced"))
        card.facts.append(("Earnings yield (EPS / price)", _pct(ey * 100)))
        card.facts.append(("EPS (last fiscal year)", _money(_num(value.get("eps_ttm")))))
    fcfy = _ratio(value.get("fcf_ttm"), value.get("enterprise_value"))
    if fcfy is not None:
        if fcfy >= FCF_YIELD_GOOD:
            card.lines.append(Line(GOOD, f"Free-cash-flow yield {fcfy * 100:.1f}% of enterprise value — strong cash generation"))
        elif fcfy < 0:
            card.lines.append(Line(BAD, f"Free-cash-flow yield {fcfy * 100:.1f}% — burning cash"))
        else:
            card.lines.append(Line(NEUTRAL, f"Free-cash-flow yield {fcfy * 100:.1f}% of enterprise value"))
        card.facts.append(("FCF yield (FCF / EV)", _pct(fcfy * 100)))
        card.facts.append(("Free cash flow (last fiscal year)", _big_money(_num(value.get("fcf_ttm")))))
        card.facts.append(("Enterprise value", _big_money(_num(value.get("enterprise_value")))))

    quality = factors.get("quality") or {}
    roe = _num(quality.get("roe"))
    if roe is not None:
        if roe >= ROE_GOOD:
            card.lines.append(Line(GOOD, f"Return on equity {roe * 100:.1f}% — highly profitable"))
        elif roe < ROE_POOR:
            card.lines.append(Line(BAD, f"Return on equity {roe * 100:.1f}% — weak profitability"))
        else:
            card.lines.append(Line(NEUTRAL, f"Return on equity {roe * 100:.1f}% — moderate"))
        card.facts.append(("Return on equity", _pct(roe * 100, signed=False)))
    gpa = _ratio(quality.get("gross_profit"), quality.get("total_assets"))
    if gpa is not None:
        if gpa >= GROSS_PROFITABILITY_GOOD:
            card.lines.append(Line(GOOD, f"Gross profit {gpa * 100:.0f}% of assets — productive asset base"))
        elif gpa < GROSS_PROFITABILITY_POOR:
            card.lines.append(Line(BAD, f"Gross profit {gpa * 100:.0f}% of assets — thin"))
        else:
            card.lines.append(Line(NEUTRAL, f"Gross profit {gpa * 100:.0f}% of assets"))
        card.facts.append(("Gross profit / assets", _pct(gpa * 100, signed=False)))
    accruals = _num(quality.get("accruals_ratio"))
    if accruals is not None:
        if accruals <= 0:
            card.lines.append(Line(GOOD, "Earnings fully backed by operating cash flow"))
        elif accruals >= ACCRUALS_POOR:
            card.lines.append(Line(BAD, "Earnings run well ahead of operating cash flow"))
        else:
            card.lines.append(Line(NEUTRAL, "Earnings mostly backed by operating cash flow"))
        card.facts.append(("Accruals ((net income − op. cash flow) / assets)",
                           _pct(accruals * 100)))

    if not card.lines:
        card.unavailable = "No price history or financial statements for the factors"
    return card


# ---------------------------------------------------------------------------
# Market backdrop -- context, never a vote
# ---------------------------------------------------------------------------

def build_backdrop_card(ds_export, rvol: Optional[Dict[str, Any]]) -> SummaryCard:
    card = SummaryCard("backdrop", "Market Backdrop", votes=False)
    regime = (_raw(ds_export).get("regime") or {}) if _unavailable(ds_export) is None else {}
    score = _num(regime.get("score"))
    if score is not None:
        if score >= LEG_SIGNAL:
            card.lines.append(Line(GOOD, "Broad market supportive (risk-on)"))
        elif score <= -LEG_SIGNAL:
            card.lines.append(Line(BAD, "Broad market under stress (risk-off)"))
        else:
            card.lines.append(Line(NEUTRAL, "Broad market neutral"))
    ratio = _num((rvol or {}).get("rvol"))
    if ratio is not None:
        if ratio >= 1.5:
            card.lines.append(Line(NEUTRAL, f"Heavy volume today — {ratio:.1f}× normal"))
        elif ratio <= 0.5:
            card.lines.append(Line(NEUTRAL, f"Light volume today — {ratio:.1f}× normal"))
        else:
            card.lines.append(Line(NEUTRAL, f"Normal volume today — {ratio:.1f}× average"))
        card.facts.append(("Relative volume", f"{ratio:.2f}×"))
    if not card.lines:
        card.unavailable = "No market data"
    return card


# ---------------------------------------------------------------------------
# The whole page
# ---------------------------------------------------------------------------

def build_summary(symbol: str, results: Dict[str, Any], today: date
                  ) -> Tuple[Dict[str, str], List[SummaryCard], SummaryCard, Overall]:
    """``(header, voting_cards, backdrop_card, overall)`` from the page's fetch results."""
    header_raw = results.get("header") or {}
    quote = header_raw.get("quote") or (results.get("rvol") or {}).get("quote") or {}
    price = _num(quote.get("price"))
    ds = results.get("detscorer")

    cards = [
        _isolated("trend", "Trend & Momentum", build_trend_card,
                  ds, results.get("weinstein"), quote),
        _isolated("fundamentals", "Fundamentals & Health", build_fundamentals_card, ds),
        _isolated("valuation", "Valuation & Growth", build_valuation_card, ds, quote),
        _isolated("factors", "Momentum, Value & Quality factors", build_factors_card,
                  results.get("factors")),
        _isolated("analysts", "Analysts", build_analyst_card, results.get("analyst"), price),
        _isolated("earnings", "Earnings", build_earnings_card, results.get("earnings")),
        _isolated("insiders", "Insiders & Congress", build_insider_card,
                  results.get("insider"), results.get("congress"), today),
    ]
    backdrop = _isolated("backdrop", "Market Backdrop", build_backdrop_card,
                         ds, results.get("rvol"), votes=False)
    return build_header(symbol, header_raw), cards, backdrop, overall_from_cards(cards)


def _isolated(key: str, title: str, builder: Callable[..., SummaryCard], *args,
              votes: bool = True) -> SummaryCard:
    """``builder(*args)``, or an unavailable card if it raises.

    Each card reads a different provider's payload; one payload in a shape the builder
    did not expect must cost that card, not the whole page. The failure is logged loudly
    (it is a bug to fix, not a data gap) and the card abstains from the overall tally.
    """
    try:
        return builder(*args)
    except Exception as e:
        logger.error(f"Symbol360: could not build the '{key}' card: {e}", exc_info=True)
        return SummaryCard(key, title, votes=votes,
                           unavailable=f"Could not be built ({type(e).__name__}: {e})")
