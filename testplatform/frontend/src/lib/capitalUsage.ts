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
 *
 * OPTIONS (2026-09-20). Three rules, each of which the equity-shaped version got wrong:
 *
 * 1. **A structure is ONE position at its NET premium.** An option transaction is saved as
 *    one row per LEG, so summing the legs counted a debit spread as long $800 + short $200
 *    = $1,000 when the cash actually paid was $600 — the credit leg ADDED instead of
 *    reducing. Legs sharing a `transactionId` are now one position whose notional is the
 *    signed sum (`direction` gives the sign), and `positions` counts structures, not legs:
 *    an iron condor is one bet, not four.
 * 2. **A credit structure is not free.** Its net premium is cash RECEIVED, and what it
 *    really ties up is margin, which this metric does not model. The net premium is used
 *    as the measure and `usageExclusions().credit` counts them, so the number can be
 *    caveated rather than silently read as "this cost nothing".
 * 3. **An unrecorded multiplier is UNKNOWN, never 1.** The old `multiplier || 1` counted an
 *    option leg whose contract multiplier was never recorded at 1/100th of its notional —
 *    a silent understatement, in the direction that makes a full account look empty. Such a
 *    position is left OUT of the series and counted in `usageExclusions().unpriced`.
 *    (Equity rows legitimately have no multiplier; the rule applies to option rows only.)
 */
export interface UsagePoint {
  /** ISO day. */
  date: string;
  /** Open notional as a percent of equity that day. */
  pct: number;
  /** POSITIONS open that day (an option structure counts once) — why a spike is a spike. */
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

/** What the series could not measure, so the chart can say so instead of under-reporting. */
export interface UsageExclusions {
  /** Positions the series counted (structures count once). */
  positions: number;
  /** Positions left OUT: an option position whose contract multiplier was never recorded. */
  unpriced: number;
  /** Credit structures in the count — premium received, margin not modelled. */
  credit: number;
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
  direction?: string | null;
  /** The STRUCTURE id. A number on some payloads, a string on others. */
  transactionId?: string | number | null;
  optionType?: string | null;
  contractSymbol?: string | null;
}

interface EquityLike { date?: string | null; equity?: number | null }

/** One thing that occupied capital: a lone trade, or a whole option structure. */
interface Position {
  entryDay: string;
  exitDay: string | null;
  /** 0 when unpriced — such a position is excluded from the series entirely. */
  notional: number;
  priced: boolean;
  /** A structure whose net premium was RECEIVED (its real constraint is margin). */
  credit: boolean;
}

const day = (value: unknown): string | null => {
  const text = typeof value === 'string' ? value : '';
  return text.length >= 10 ? text.slice(0, 10) : null;
};

const num = (value: unknown): number => {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : 0;
};

const isOptionRow = (trade: TradeLike): boolean =>
  Boolean(trade?.optionType || trade?.contractSymbol);

const signOf = (trade: TradeLike): number => {
  const direction = String(trade?.direction ?? '').toLowerCase();
  if (direction === 'short') return -1;
  if (direction === 'long') return 1;
  return num(trade?.size) < 0 ? -1 : 1;
};

/**
 * The positions a trade list represents: option legs folded by `transactionId`, everything
 * else standing alone.
 *
 * Exported because the chart needs to report what it could not measure (the unpriced
 * positions) and how many credit structures it is describing, and that has to come from the
 * same grouping the series uses.
 */
export function positionsFrom(trades: readonly TradeLike[] | null | undefined): Position[] {
  const groups = new Map<string, TradeLike[]>();
  const lone: TradeLike[] = [];

  for (const trade of trades ?? []) {
    if (!trade) continue;
    const rawKey = trade.transactionId;
    const key = rawKey === null || rawKey === undefined || rawKey === ''
      ? null
      : String(rawKey).trim() || null;
    if (key) {
      const group = groups.get(key);
      if (group) group.push(trade);
      else groups.set(key, [trade]);
    } else {
      lone.push(trade);
    }
  }

  const positions: Position[] = [];
  const build = (legs: TradeLike[], isStructure: boolean) => {
    const entryDay = legs
      .map(leg => day(leg?.entryDate))
      .filter((value): value is string => value !== null)
      .sort()[0];
    if (!entryDay) return;                       // never opened: ties up nothing
    const exitDays = legs
      .map(leg => day(leg?.exitDate))
      .filter((value): value is string => value !== null)
      .sort();
    const exitDay = exitDays.length ? exitDays[exitDays.length - 1] : null;

    if (!isStructure) {
      // A lone trade keeps the original measure exactly: an equity short occupies capital,
      // so the magnitude is used and the multiplier is 1 when the row has none.
      const notional = Math.abs(
        num(legs[0]?.entryPrice) * num(legs[0]?.size) * (num(legs[0]?.multiplier) || 1));
      const unpriced = isOptionRow(legs[0]) && !num(legs[0]?.multiplier);
      positions.push({ entryDay, exitDay, notional, priced: !unpriced, credit: false });
      return;
    }

    // A structure: the SIGNED sum of its legs, i.e. the net premium. A missing multiplier
    // anywhere makes the net unknowable, so the position is unpriced rather than guessed.
    let net = 0;
    let unpriced = false;
    for (const leg of legs) {
      if (isOptionRow(leg) && !num(leg?.multiplier)) {
        unpriced = true;
        continue;
      }
      const multiplier = num(leg?.multiplier) || 1;
      net += signOf(leg) * Math.abs(num(leg?.size)) * num(leg?.entryPrice) * multiplier;
    }
    positions.push({
      entryDay, exitDay, notional: Math.abs(net), priced: !unpriced, credit: net < 0,
    });
  };

  for (const legs of groups.values()) build(legs, legs.length > 1);
  for (const trade of lone) build([trade], false);
  return positions;
}

/** What the series left out, and how many credit structures it is describing. */
export function usageExclusions(trades: readonly TradeLike[] | null | undefined): UsageExclusions {
  const positions = positionsFrom(trades);
  return {
    positions: positions.filter(position => position.priced).length,
    unpriced: positions.filter(position => !position.priced).length,
    credit: positions.filter(position => position.priced && position.credit).length,
  };
}

/**
 * Daily capital utilisation, one point per day the equity curve covers.
 *
 * A position contributes its ENTRY notional from entry day to exit day inclusive: the
 * question is how much capital the position tied up, and that is what was committed
 * when it was opened — marking it to market would blend "how much did this occupy"
 * with "how well is it doing", which the equity curve already answers.
 *
 * Options are counted at `entryPrice * size * multiplier`, so a $4.20 contract on 100
 * shares reads as the $420 it actually costs rather than as $4.20 — and a multi-leg
 * structure at its NET premium (see the module docstring).
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

  for (const position of positionsFrom(trades)) {
    if (!position.priced) continue;              // unknown notional is NOT counted as zero
    opens.push({ day: position.entryDay, notional: position.notional });
    // An OPEN position never closes: it keeps occupying capital to the end of the run,
    // which is exactly the case worth seeing (see the wheel's held stock).
    if (position.exitDay) closes.push({ day: position.exitDay, notional: position.notional });
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
