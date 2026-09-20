/**
 * OPTION PAYOFF AND MONEYNESS VIEW MODEL (2026-09-20).
 *
 * The arithmetic behind the option trade popup: what the recorded legs are worth
 * at expiration, where the position breaks even, and how far in or out of the
 * money a leg was at a given underlying price. Pure, and in `lib/` beside
 * `optionTrades.ts`, so every number the popup prints is testable without
 * mounting React.
 *
 * TWO RULES THIS MODULE EXISTS TO ENFORCE, both of which a presentation-layer
 * default gets wrong:
 *
 *  1. MISSING IS NOT ZERO. A leg with no strike, no premium, no contract count or
 *     no RECORDED multiplier cannot be priced, and it makes the STRUCTURE payoff
 *     unavailable rather than being quietly dropped from the sum. `contractValue`
 *     in `optionTrades.ts` substitutes 0/1 because it feeds a table cell; the same
 *     substitution here would report a four-leg condor as three legs and call the
 *     result a payoff.
 *  2. A MULTIPLIER IS NEVER ASSUMED. `100` is the most expensive guess in this
 *     codebase (it is 100x on every dollar figure), so a leg whose multiplier is
 *     absent, non-positive or of unproven provenance refuses to price. A RECORDED
 *     multiplier of 1 is accepted: 1 is a real value, absent is not.
 *
 * The curve is the textbook expiration payoff, not a reconstruction of the trade:
 *
 *     payoff(S) = sum[ d_i * q_i * m_i * (intrinsic_i(S, K_i) - p_i) ]
 *
 * with `d = +1` long / `-1` short, `q` contracts, `m` multiplier and `p` the
 * AVERAGE entry premium per share. Fees are excluded, so this is a hypothetical
 * result and must never be presented as the recorded P&L of the run.
 */

export type OptionDirection = 'long' | 'short';
export type OptionRight = 'call' | 'put';

/** One recorded leg, as much or as little of it as the result actually carries. */
export type OptionLegInput = {
  direction?: OptionDirection | null;
  optionType?: OptionRight | null;
  strike?: number | null;
  /** AVERAGE entry premium PER SHARE -- what the chain quotes, never the cash moved. */
  entryPremium?: number | null;
  contracts?: number | null;
  multiplier?: number | null;
  /**
   * False when the multiplier is a serialization default rather than a recorded
   * contract term. Refusing to price on `false` is the point: the number looks
   * identical to a real one and is 100x wrong when it is not.
   */
  multiplierRecorded?: boolean;
  expiry?: string | null;
  underlyingSymbol?: string | null;
};

/** A leg whose terms are all present, finite and usable. */
export type ValidatedLeg = {
  direction: OptionDirection;
  optionType: OptionRight;
  strike: number;
  entryPremium: number;
  contracts: number;
  multiplier: number;
};

export type PayoffUnavailable = { available: false; reason: string };

export type PayoffCurve = {
  available: true;
  /** The legs the curve was built from, in the order they were supplied. */
  legs: ValidatedLeg[];
  /** Net entry cash: > 0 is a debit PAID, < 0 a credit RECEIVED. */
  netEntryDebit: number;
  /** Position P&L in dollars if the underlying settles at `S`. */
  payoffAt: (underlyingPrice: number) => number;
  /** Finite zero crossings, ascending. Multiple regions are normal (a straddle has two). */
  breakevens: number[];
  /** `'unlimited'` only for a mathematically unbounded tail, never for "we stopped looking". */
  maxProfit: number | 'unlimited';
  maxLoss: number | 'unlimited';
  /** Intervals where the payoff is exactly zero -- an interval, not a point. */
  flatZeroIntervals: Array<[number, number]>;
  /** Bounds that contain every strike and breakeven, with padding; never negative. */
  suggestedDomain: { min: number; max: number };
};

export type PayoffScope = { scope?: number };

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

/**
 * ATM is EQUALITY at the recorded precision, so the tolerance is floating-point
 * dust scaled to the numbers involved -- not a "near the money" band. A
 * discretionary band would relabel a leg whose strike and spot genuinely differ.
 */
export const moneynessTolerance = (spot: number, strike: number): number =>
  1e-9 * Math.max(1, Math.abs(spot), Math.abs(strike));

export type Moneyness = 'ITM' | 'ATM' | 'OTM' | 'unknown';

/**
 * ITM/ATM/OTM for one leg against one underlying reference.
 *
 * LONG/SHORT DOES NOT INVERT THESE. A short call is still ITM above its strike;
 * moneyness describes the contract, not the position's exposure.
 *
 * Unknown inputs give `'unknown'` rather than a guess: an option whose spot or
 * strike is missing is not "OTM", it is unclassified, and the popup must say so.
 */
export function moneyness(
  optionType: OptionRight | null | undefined,
  strike: number | null | undefined,
  spot: number | null | undefined,
): Moneyness {
  if (optionType !== 'call' && optionType !== 'put') return 'unknown';
  if (!isFiniteNumber(strike) || !isFiniteNumber(spot)) return 'unknown';
  if (strike <= 0 || spot <= 0) return 'unknown';

  const difference = spot - strike;
  if (Math.abs(difference) <= moneynessTolerance(spot, strike)) return 'ATM';
  if (optionType === 'call') return difference > 0 ? 'ITM' : 'OTM';
  return difference < 0 ? 'ITM' : 'OTM';
}

/**
 * `100 * (S - K) / K` -- the distance between spot and strike as a percentage of
 * the STRIKE. Labelled "spot vs strike" wherever it is shown: it is not a return,
 * and it is not the position's P&L.
 */
export function spotVsStrikePercent(
  spot: number | null | undefined,
  strike: number | null | undefined,
): number | null {
  if (!isFiniteNumber(spot) || !isFiniteNumber(strike) || strike <= 0) return null;
  return (100 * (spot - strike)) / strike;
}

const intrinsic = (leg: ValidatedLeg, underlyingPrice: number): number =>
  leg.optionType === 'call'
    ? Math.max(underlyingPrice - leg.strike, 0)
    : Math.max(leg.strike - underlyingPrice, 0);

/**
 * A leg's contribution to the position's P&L at expiration: the intrinsic value it
 * settles at, MINUS the premium that was paid (long) or received (short) for it.
 * Dropping the premium term turns the curve into the settlement value of the
 * position rather than its profit -- every breakeven disappears, because nothing
 * ever crosses zero.
 */
const valueAtExpiry = (leg: ValidatedLeg, underlyingPrice: number): number =>
  (leg.direction === 'long' ? 1 : -1) *
  leg.contracts *
  leg.multiplier *
  (intrinsic(leg, underlyingPrice) - leg.entryPremium);

/** Slope comparison tolerance: payoffs are `strike x contracts x multiplier` sized. */
const SLOPE_EPSILON = 1e-9;

function validateLeg(leg: OptionLegInput, index: number): ValidatedLeg | PayoffUnavailable {
  const label = `leg ${index + 1}`;

  if (leg.direction !== 'long' && leg.direction !== 'short') {
    return { available: false, reason: `${label}: direction not recorded` };
  }
  if (leg.optionType !== 'call' && leg.optionType !== 'put') {
    return { available: false, reason: `${label}: option type not recorded` };
  }
  if (!isFiniteNumber(leg.strike) || leg.strike <= 0) {
    return { available: false, reason: `${label}: strike missing or invalid` };
  }
  if (!isFiniteNumber(leg.entryPremium) || leg.entryPremium < 0) {
    return { available: false, reason: `${label}: entry premium missing or invalid` };
  }
  if (!isFiniteNumber(leg.contracts) || leg.contracts <= 0) {
    return { available: false, reason: `${label}: contract count missing or invalid` };
  }
  if (!isFiniteNumber(leg.multiplier) || leg.multiplier <= 0) {
    return { available: false, reason: `${label}: contract multiplier not recorded` };
  }
  if (leg.multiplierRecorded === false) {
    return { available: false, reason: `${label}: contract multiplier is unverified` };
  }

  return {
    direction: leg.direction,
    optionType: leg.optionType,
    strike: leg.strike,
    entryPremium: leg.entryPremium,
    contracts: leg.contracts,
    multiplier: leg.multiplier,
  };
}

/** Distinct non-null values, used for the shared-expiry / shared-underlying checks. */
function distinct(values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  for (const value of values) {
    if (typeof value === 'string' && value.trim() !== '') seen.add(value);
  }
  return Array.from(seen);
}

/**
 * The expiration payoff of the recorded legs.
 *
 * `scope: n` prices that one leg alone; without it the whole structure is priced
 * and EVERY leg must validate, because "we left out the leg we could not read"
 * silently changes the position. A single leg with good terms is still inspectable
 * when a sibling is unreadable -- the limitation is in the combined view.
 *
 * A combined payoff additionally requires one expiry and one underlying across the
 * legs: pricing a diagonal at the earlier expiry's intrinsic would be an invented
 * number, so a mixed-expiry structure reports the reason and offers the per-leg
 * view instead.
 */
export function buildPayoff(
  legs: OptionLegInput[],
  options: PayoffScope = {},
): PayoffCurve | PayoffUnavailable {
  const all = Array.isArray(legs) ? legs : [];
  if (all.length === 0) return { available: false, reason: 'no legs recorded' };

  const singleLegScope = isFiniteNumber(options.scope) ? options.scope : null;
  const selected = singleLegScope == null ? all : [all[singleLegScope]];
  if (selected.length === 0 || selected[0] == null) {
    return { available: false, reason: 'selected leg not found' };
  }

  const validated: ValidatedLeg[] = [];
  for (let index = 0; index < selected.length; index += 1) {
    const offset = singleLegScope == null ? index : singleLegScope;
    const result = validateLeg(selected[index], offset);
    if ('available' in result) return result;
    validated.push(result);
  }

  if (singleLegScope == null) {
    const expiries = distinct(all.map((leg) => leg.expiry));
    if (expiries.length > 1) {
      return {
        available: false,
        reason: 'combined expiration payoff unavailable for different expiries',
      };
    }
    const underlyings = distinct(all.map((leg) => leg.underlyingSymbol));
    if (underlyings.length > 1) {
      return {
        available: false,
        reason: 'combined expiration payoff unavailable for different underlyings',
      };
    }
  }

  const payoffAt = (underlyingPrice: number): number =>
    validated.reduce((total, leg) => total + valueAtExpiry(leg, underlyingPrice), 0);

  const netEntryDebit = validated.reduce(
    (total, leg) =>
      total +
      (leg.direction === 'long' ? 1 : -1) *
        leg.contracts *
        leg.multiplier *
        leg.entryPremium,
    0,
  );

  const strikes = Array.from(new Set(validated.map((leg) => leg.strike))).sort(
    (a, b) => a - b,
  );

  // Bounded intervals start at 0 -- an underlying price cannot go negative, which
  // is also what keeps a long put's maximum profit finite (K - p at S = 0) instead
  // of reporting an unbounded tail.
  const breakpoints = [0, ...strikes];

  const breakevens: number[] = [];
  const flatZeroIntervals: Array<[number, number]> = [];
  const candidates: number[] = [payoffAt(0)];

  let maxProfit: number | 'unlimited' = payoffAt(0);
  let maxLoss: number | 'unlimited' = payoffAt(0);

  for (let index = 0; index < breakpoints.length; index += 1) {
    const low = breakpoints[index];
    const isLast = index === breakpoints.length - 1;
    const high = isLast ? low + 1 : breakpoints[index + 1];
    if (high === low) continue;

    const lowValue = payoffAt(low);
    const highValue = payoffAt(high);

    if (!isLast) {
      // A bounded segment: its extremes are at its ends, because the payoff is
      // piecewise linear and continuous.
      candidates.push(lowValue, highValue);
    } else {
      const slope = highValue - lowValue;
      if (slope > SLOPE_EPSILON) maxProfit = 'unlimited';
      else if (slope < -SLOPE_EPSILON) maxLoss = 'unlimited';
      candidates.push(lowValue);
    }

    const slope = (highValue - lowValue) / (high - low);
    if (Math.abs(slope) <= SLOPE_EPSILON) {
      if (Math.abs(lowValue) <= SLOPE_EPSILON) {
        flatZeroIntervals.push([low, isLast ? Number.POSITIVE_INFINITY : high]);
      }
      continue;
    }

    const root = low - lowValue / slope;
    const inSegment = isLast ? root >= low : root >= low && root <= high;
    if (inSegment) {
      const tolerance = moneynessTolerance(root, Math.max(1, Math.abs(root)));
      if (!breakevens.some((existing) => Math.abs(existing - root) <= tolerance)) {
        breakevens.push(root);
      }
    }
  }

  if (maxProfit !== 'unlimited') {
    maxProfit = Math.max(...candidates);
  }
  if (maxLoss !== 'unlimited') {
    maxLoss = Math.min(...candidates);
  }

  breakevens.sort((a, b) => a - b);

  // Bounds hold every strike and every FINITE breakeven, so a distant breakeven
  // cannot fall off the chart while still being reported in the summary.
  const anchors = [...strikes, ...breakevens];
  const lowest = Math.min(...anchors);
  const highest = Math.max(...anchors);
  const padding = Math.max(1, 0.1 * (highest - lowest));

  return {
    available: true,
    legs: validated,
    netEntryDebit,
    payoffAt,
    breakevens,
    maxProfit,
    maxLoss,
    flatZeroIntervals,
    suggestedDomain: {
      min: Math.max(0, lowest - padding),
      max: highest + padding,
    },
  };
}
