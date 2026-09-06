/**
 * How much of the account a strategy actually occupies, over time.
 *
 * The run's `exposureTime` stat does NOT answer this: it reports the share of bars on
 * which any position was open, which is 100% for essentially every strategy here and
 * says nothing about how much capital was committed. Two strategies both at "100%
 * exposure" can sit at 12% and 32% of equity — and that difference decides whether
 * they can share an account, which is the question this chart exists for.
 *
 * Measured as open notional over equity AT THE TIME, not over starting capital. A run
 * that triples its equity would otherwise appear to wind down as it succeeds, since the
 * same dollar position is a shrinking share of a growing account. Utilisation is a
 * statement about the account as it stood that day.
 */
export interface UsagePoint {
  /** ISO day. */
  date: string;
  /** Open notional as a percent of equity that day. */
  pct: number;
  /** Positions open that day — the reason a spike is a spike. */
  positions: number;
}

export interface UsageSummary {
  avgPct: number;
  maxPct: number;
  /** Share of days below IDLE_PCT — the room another strategy could use. */
  idleDaysPct: number;
  /** Share of days above HEAVY_PCT. */
  heavyDaysPct: number;
  peakDate: string | null;
}

/** Below this, the account is doing essentially nothing that day. */
export const IDLE_PCT = 10;
/** Above this, a second strategy would be competing for cash. */
export const HEAVY_PCT = 50;

interface TradeLike {
  entryDate?: string | null;
  exitDate?: string | null;
  entryPrice?: number | null;
  size?: number | null;
  multiplier?: number | null;
}

interface EquityLike { date?: string | null; equity?: number | null }

const day = (value: unknown): string | null => {
  const text = typeof value === 'string' ? value : '';
  return text.length >= 10 ? text.slice(0, 10) : null;
};

const num = (value: unknown): number => {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : 0;
};

/**
 * Daily capital utilisation, one point per day the equity curve covers.
 *
 * A trade contributes its ENTRY notional from entry day to exit day inclusive: the
 * question is how much capital the position tied up, and that is what was committed
 * when it was opened — marking it to market would blend "how much did this occupy"
 * with "how well is it doing", which the equity curve already answers.
 *
 * Options are counted at `entryPrice * size * multiplier`, so a $4.20 contract on 100
 * shares reads as the $420 it actually costs rather than as $4.20.
 */
export function capitalUsageSeries(trades: readonly TradeLike[] | null | undefined,
                                   equityCurve: readonly EquityLike[] | null | undefined): UsagePoint[] {
  const curve = (equityCurve ?? []).filter(p => day(p?.date) !== null);
  if (!curve.length) return [];

  // Equity per day, last value wins — an intraday curve reports many bars per day and
  // the day's close is the honest denominator.
  const equityByDay = new Map<string, number>();
  for (const point of curve) {
    const d = day(point?.date);
    if (d) equityByDay.set(d, num(point?.equity));
  }

  // Events as SORTED LISTS walked by a pointer, not as a map keyed on the exact day.
  //
  // Keying on the day was wrong in a way that only real data showed: a trade exiting on
  // a Saturday, a holiday, or any date the equity curve has no bar for never matched a
  // curve key, so its notional was added and never removed. Open notional then grew
  // monotonically and backtest 1113 reported 220% average utilisation against a true
  // ~27%. Every event has to be CONSUMED by the first curve day at or after it.
  const opens: { day: string; notional: number }[] = [];
  const closes: { day: string; notional: number }[] = [];

  for (const trade of trades ?? []) {
    const entry = day(trade?.entryDate);
    if (!entry) continue;                     // an unopened trade ties up nothing
    const notional = Math.abs(num(trade?.entryPrice) * num(trade?.size)
      * (num(trade?.multiplier) || 1));
    opens.push({ day: entry, notional });
    const exit = day(trade?.exitDate);
    // An OPEN trade never closes: it keeps occupying capital to the end of the run,
    // which is exactly the case worth seeing (see the wheel's held stock).
    if (exit) closes.push({ day: exit, notional });
  }
  opens.sort((a, b) => a.day.localeCompare(b.day));
  closes.sort((a, b) => a.day.localeCompare(b.day));

  const days = [...equityByDay.keys()].sort();
  const out: UsagePoint[] = [];
  let open = 0;
  let count = 0;
  let o = 0;
  let c = 0;
  for (const d of days) {
    // A position occupies capital on day D when entry <= D <= exit. So by the time D is
    // recorded, every entry ON OR BEFORE it is in, and every exit STRICTLY BEFORE it is
    // out — an exit dated D still counts for D, because it was held part of that day.
    while (o < opens.length && opens[o].day <= d) { open += opens[o].notional; count += 1; o += 1; }
    while (c < closes.length && closes[c].day < d) { open -= closes[c].notional; count -= 1; c += 1; }
    if (open < 0) open = 0;                   // float dust on a fully closed book
    if (count < 0) count = 0;
    const equity = equityByDay.get(d) ?? 0;
    out.push({
      date: d,
      // Equity can legitimately be 0 or negative in a blown-up run; a division there
      // is meaningless rather than infinite, so it reports 0 and the curve is flat.
      pct: equity > 0 ? (open / equity) * 100 : 0,
      positions: count,
    });
  }
  return out;
}

export function summariseUsage(points: readonly UsagePoint[]): UsageSummary {
  if (!points.length) {
    return { avgPct: 0, maxPct: 0, idleDaysPct: 0, heavyDaysPct: 0, peakDate: null };
  }
  let total = 0;
  let peak = points[0];
  let idle = 0;
  let heavy = 0;
  for (const p of points) {
    total += p.pct;
    if (p.pct > peak.pct) peak = p;
    if (p.pct < IDLE_PCT) idle += 1;
    if (p.pct > HEAVY_PCT) heavy += 1;
  }
  return {
    avgPct: total / points.length,
    maxPct: peak.pct,
    idleDaysPct: (idle / points.length) * 100,
    heavyDaysPct: (heavy / points.length) * 100,
    peakDate: peak.date,
  };
}
