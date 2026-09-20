/**
 * WHAT THE OPTION POPUP DRAWS (spec 2026-09-20, decisions 1a and 2c).
 *
 * The bridge between the trade-chart context the API serves and the things on the
 * chart: the legs the payoff is built from, the rotated payoff overlay, the
 * sign-only zone bands, the strike lines and the two marker sets. Pure, so the
 * geometry is testable without mounting a chart — and so the drawing layer cannot
 * quietly disagree with the arithmetic in `optionTradeChart.ts`, which it calls
 * rather than reimplements.
 *
 * THE ONE GEOMETRY DECISION WORTH NAMING: the overlay is drawn with the underlying
 * price on the chart's OWN y axis and the position P&L running HORIZONTALLY, out
 * from a vertical zero line. That is what makes it one chart instead of two, and it
 * is why the curve's horizontal reach is CAPPED — a curve that spans the full plot
 * width would sit on top of the candles it is supposed to be read against.
 */

import { buildPayoff, type OptionLegInput, type PayoffCurve } from './optionTradeChart';
import type { TradeChartLeg } from './btApi';

/** Most of the plot's half-width the payoff curve may occupy. */
export const OVERLAY_WIDTH_FRACTION = 0.35;

/** Two strikes closer than this fraction of the price range get colliding labels. */
const LABEL_COLLISION_FRACTION = 0.02;

export type OverlayPoint = { underlying: number; pnl: number };
export type OverlaySegment = { sign: 'profit' | 'loss'; points: OverlayPoint[] };
export type ZoneBand = { from: number; to: number; sign: 'profit' | 'loss' | 'neutral' };

/**
 * What a strike line needs: the strike and enough identity to label it. NOT the
 * payoff's validated leg -- a strike is geometry, so it is still drawn for a leg
 * whose multiplier was never recorded (only the money is unpricable).
 */
export type StrikeLineLeg = {
  direction: 'long' | 'short';
  optionType: 'call' | 'put';
  strike: number;
  contracts: number;
};

export type StrikeLine = {
  price: number;
  label: string;
  direction: 'long' | 'short';
  optionType: 'call' | 'put';
  strike: number;
  contracts: number;
  /** True when this label would sit on top of the previous one. */
  collidesWithPrevious: boolean;
};

export type ChartMarker = {
  time: string;
  position: 'aboveBar' | 'belowBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown' | 'circle';
  /** SHORT canvas label. Long labels overlap into an unreadable smear on the bars. */
  text: string;
  /** Full sentence, shown on hover. The detail lives here, not on the canvas. */
  title?: string;
  kind: 'structure' | 'leg';
};

export type OverlayGeometry = {
  points: OverlayPoint[];
  segments: OverlaySegment[];
  /** Symmetric P&L bound the horizontal scale is built from. */
  maxAbsPnl: number;
  /** Half-width of the zero line's reach, as a fraction of the plot width. */
  widthFraction: number;
};

const dayOf = (iso?: string | null) => (iso || '').slice(0, 10);

/**
 * Map a context's legs onto the payoff model's inputs.
 *
 * The recorded multiplier is passed through WITH its provenance: a leg whose
 * multiplier was not recorded is not priceable, and `buildPayoff` is the thing that
 * decides so — this adapter must not paper over it with a default.
 */
export const toPayoffLegs = (legs: TradeChartLeg[]): OptionLegInput[] =>
  (legs || []).map(leg => ({
    direction: leg.direction,
    optionType: leg.optionType,
    strike: leg.strike,
    entryPremium: leg.entryPrice,
    contracts: leg.size,
    multiplier: leg.multiplier,
    multiplierRecorded: leg.multiplierRecorded,
    expiry: leg.expiry,
    underlyingSymbol: leg.underlyingSymbol,
  }));

/**
 * Why a set of legs cannot be combined into ONE expiration curve, or null.
 *
 * Mirrors the Python view (`ba2_common.core.option_payoff_chart`): a curve is a statement
 * about what happens at ONE expiration, so legs with different expiries are not a structure
 * with one payoff -- and a structure where an expiry is missing on some legs cannot be
 * ASSUMED to be single-expiry either. A missing term is unprovable, not compatible.
 */
export function combinationProblem(legs: TradeChartLeg[]): string | null {
  if (legs.length < 2) return null;

  const expiries = legs.map(leg => (leg.expiry || '').trim());
  const present = Array.from(new Set(expiries.filter(Boolean))).sort();
  if (present.length > 1) {
    return `${present.length} different expiries (${present.join(', ')}) cannot share one `
      + `expiration curve`;
  }
  if (present.length === 1 && expiries.some(expiry => !expiry)) {
    const missing = expiries.filter(expiry => !expiry).length;
    return `the expiry is missing on ${missing} leg${missing === 1 ? '' : 's'}, so a `
      + `single-expiry curve cannot be assumed`;
  }

  const underlyings = Array.from(new Set(
    legs.map(leg => (leg.underlyingSymbol || leg.symbol || '').trim()).filter(Boolean),
  )).sort();
  if (underlyings.length > 1) {
    return `${underlyings.length} different underlyings (${underlyings.join(', ')}) cannot `
      + `share one expiration curve`;
  }
  return null;
}

/** The payoff for the whole transaction, or the reason it cannot be drawn. */
export const payoffFor = (legs: TradeChartLeg[], scope?: number) => {
  // A single-leg scope isolates one leg, so compatibility is a STRUCTURE question only.
  if (scope == null) {
    const problem = combinationProblem(legs);
    if (problem) return { available: false as const, reason: problem };
  }
  return buildPayoff(toPayoffLegs(legs), scope == null ? {} : { scope });
};

/**
 * Sample the payoff across the display domain.
 *
 * The sample set includes every strike and every breakeven, so no kink or crossing
 * can fall between two samples and be drawn as a straight line through it.
 */
export function samplePayoff(model: PayoffCurve, samples = 160): OverlayPoint[] {
  const anchors = new Set<number>([model.suggestedDomain.min, model.suggestedDomain.max]);
  for (const leg of model.legs) anchors.add(leg.strike);
  for (const breakeven of model.breakevens) anchors.add(breakeven);

  const ordered = Array.from(anchors).sort((a, b) => a - b);
  const { min, max } = model.suggestedDomain;
  const step = (max - min) / Math.max(1, samples);
  for (let value = min; value <= max; value += step) ordered.push(value);

  const unique = Array.from(new Set(ordered.map(value => Math.round(value * 1e6) / 1e6)))
    .sort((a, b) => a - b);

  return unique
    .filter(underlying => underlying >= 0)
    .map(underlying => ({ underlying, pnl: model.payoffAt(underlying) }));
}

/**
 * Split the sampled curve into contiguous runs of one sign.
 *
 * The point where the sign flips belongs to BOTH runs, so the filled areas meet the
 * zero line exactly instead of leaving a gap the width of one sample.
 */
export function payoffSegments(points: OverlayPoint[]): OverlaySegment[] {
  const segments: OverlaySegment[] = [];
  let current: OverlaySegment | null = null;
  // The PREVIOUS sample, whatever its sign. A zero point belongs to the run it ends
  // AND to the run it starts, so tracking the previous sample directly makes the two
  // filled areas meet on the zero line instead of leaving a sample-wide gap.
  let previous: OverlayPoint | null = null;

  for (const point of points) {
    const sign = point.pnl > 0 ? 'profit' : point.pnl < 0 ? 'loss' : null;
    if (sign == null) {
      if (current) current.points.push(point);
      previous = point;
      current = null;
      continue;
    }
    if (current && current.sign === sign) {
      current.points.push(point);
    } else {
      const startsAtCrossing = previous !== null && previous !== point;
      current = { sign, points: startsAtCrossing ? [previous as OverlayPoint, point] : [point] };
      segments.push(current);
    }
    previous = point;
  }
  return segments.filter(segment => segment.points.length > 1);
}

/**
 * The horizontal P&L scale, symmetric about zero and capped to a fraction of the
 * plot's half-width.
 *
 * `maxAbsPnl` is the largest magnitude the curve actually reaches inside the display
 * domain — not the "unlimited" tail, which no chart can draw. A curve whose extreme
 * is off-screen is reported as unlimited by the summary, and the drawing simply
 * stops at the edge of its cap.
 */
export function payoffGeometry(model: PayoffCurve, samples = 160): OverlayGeometry {
  const points = samplePayoff(model, samples);
  const maxAbsPnl = points.reduce((peak, point) => Math.max(peak, Math.abs(point.pnl)), 0);
  return {
    points,
    segments: payoffSegments(points),
    maxAbsPnl,
    widthFraction: OVERLAY_WIDTH_FRACTION,
  };
}

/**
 * Where the position would be profitable at expiration, as price bands.
 *
 * This is the sign-only reading of the same curve — the fallback when the rotated
 * overlay cannot be drawn, and the low-opacity band underneath it when it can.
 * Band boundaries are the breakevens, so a two-tailed structure yields three bands
 * and the middle one is the loss.
 */
export function zoneBands(
  model: PayoffCurve,
  priceRange?: { min: number; max: number } | null,
): ZoneBand[] {
  // The bands used to STOP at the payoff's suggested domain, so a 95/105 spread shaded only
  // ~94-106 and the candles at 108-110 had no profit shading even though the position is
  // profitable there at expiration (review: incomplete shading domain).
  //
  // The sign can only change AT a breakeven, so the mathematically complete partition is the
  // breakevens between the edges; the tails run to the edge of what is on screen. A band whose
  // payoff is exactly zero is NEUTRAL, not green: a flat interval is not a profit.
  // A SUPPLIED but empty range means nothing is on screen, so there is nothing to shade;
  // only an ABSENT range falls back to the payoff's own suggested domain.
  if (priceRange && !(priceRange.max > priceRange.min)) return [];
  const range = priceRange ?? null;
  const low = Math.max(range ? range.min : model.suggestedDomain.min, 0);
  const high = range ? range.max : model.suggestedDomain.max;
  if (!(high > low)) return [];

  const edges = [
    low,
    ...model.breakevens.filter(value => value > low && value < high),
    high,
  ].sort((a, b) => a - b);

  const bands: ZoneBand[] = [];
  for (let index = 0; index < edges.length - 1; index += 1) {
    const from = edges[index];
    const to = edges[index + 1];
    if (!(to > from)) continue;
    const pnl = model.payoffAt((from + to) / 2);
    const sign: ZoneBand['sign'] =
      Math.abs(pnl) < FLAT_ZERO_EPSILON ? 'neutral' : pnl > 0 ? 'profit' : 'loss';
    bands.push({ from, to, sign });
  }
  return bands;
}

/** Below this the payoff is treated as flat, not as a very small profit. */
export const FLAT_ZERO_EPSILON = 0.005;

/**
 * A readable step for the horizontal P&L scale.
 *
 * The locked one-chart design puts a P&L scale along the top, and a scale needs round numbers:
 * the step is the 1/2/5 x 10^k value closest to a quarter of the reach, in log space, so a
 * $900 reach ticks every $200 rather than every $500 (too sparse) or $100 (too dense).
 */
export function nicePnlStep(maxAbsPnl: number): number {
  if (!(maxAbsPnl > 0) || !Number.isFinite(maxAbsPnl)) return 0;
  const rough = maxAbsPnl / 4;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const candidates = [1, 2, 5, 10].map(factor => factor * magnitude);
  let best = candidates[0];
  for (const candidate of candidates) {
    if (Math.abs(Math.log(candidate / rough)) < Math.abs(Math.log(best / rough))) best = candidate;
  }
  return best;
}

/** Signed tick values for the P&L scale, ascending. Symmetric, from the step outwards. */
export function pnlTicks(maxAbsPnl: number): number[] {
  const step = nicePnlStep(maxAbsPnl);
  if (step <= 0) return [];
  const ticks: number[] = [];
  for (let value = step; value <= maxAbsPnl + 1e-9; value += step) {
    ticks.push(value, -value);
  }
  return ticks.sort((a, b) => a - b);
}

/**
 * One dashed line per distinct strike, labelled with the leg it belongs to.
 *
 * Two legs sharing a strike produce ONE line whose label names both, and a label
 * that would land on its neighbour is flagged rather than drawn over it.
 *
 * Collision is measured against the CHART's price range when the caller knows it,
 * because that is what decides whether two labels overlap on screen. Measuring
 * against the strikes' own span -- the fallback when the range is unknown -- gets
 * it backwards: a tight pair of strikes would never be flagged, while a wide condor
 * would flag its own evenly spaced legs.
 */
export function strikeLines(
  legs: StrikeLineLeg[],
  priceRange?: { min: number; max: number },
): StrikeLine[] {
  const byStrike = new Map<number, StrikeLineLeg[]>();
  for (const leg of legs) {
    const bucket = byStrike.get(leg.strike);
    if (bucket) bucket.push(leg); else byStrike.set(leg.strike, [leg]);
  }

  const prices = Array.from(byStrike.keys()).sort((a, b) => a - b);
  const span = priceRange && priceRange.max > priceRange.min
    ? priceRange.max - priceRange.min
    : prices.length > 1 ? prices[prices.length - 1] - prices[0] : 0;

  return prices.map((price, index) => {
    const group = byStrike.get(price) || [];
    const first = group[0];
    const label = group
      .map(leg =>
        `${leg.direction === 'long' ? 'Long' : 'Short'} ${leg.contracts} ` +
        `${leg.optionType === 'call' ? 'Call' : 'Put'} · K $${leg.strike}`)
      .join(' / ');
    return {
      price,
      label,
      direction: first.direction,
      optionType: first.optionType,
      strike: first.strike,
      contracts: group.reduce((total, leg) => total + leg.contracts, 0),
      collidesWithPrevious:
        index > 0 && span > 0 &&
        (price - prices[index - 1]) / span < LABEL_COLLISION_FRACTION,
    };
  });
}

const money = (value: number | null | undefined) =>
  value == null ? '—' : `$${value.toFixed(2)}`;

/**
 * The structure pair: first entry and last exit of the whole transaction.
 *
 * Deliberately NOT every fill — the rows are aggregate round trips, so these two
 * markers say when the position was opened and closed, and nothing more.
 */
export function structureMarkers(legs: TradeChartLeg[]): ChartMarker[] {
  const entries = legs.map(leg => dayOf(leg.entryAt)).filter(Boolean).sort();
  const exits = legs.map(leg => dayOf(leg.exitAt)).filter(Boolean).sort();
  if (entries.length === 0) return [];

  const markers: ChartMarker[] = [{
    time: entries[0],
    position: 'belowBar',
    color: '#2563eb',
    shape: 'arrowUp',
    text: 'Entry',
    title: `Structure entry — ${entries[0]}`,
    kind: 'structure',
  }];

  // `open_at_end` is a MARK-TO-RUN-END value, not a fill: the position was still open when
  // the run stopped. Labelling it "Structure exit" claimed an exit that never happened.
  if (legs.some(leg => leg.positionStatus === 'open_at_end')) {
    const last = [...entries, ...exits].sort().pop()!;
    markers.push({
      time: last,
      position: 'aboveBar',
      color: '#f59e0b',
      shape: 'circle',
      text: 'Run-end',
      title: 'Run-end valuation — the position was still open when the run ended; '
        + 'this is where it was marked, not an exit fill',
      kind: 'structure',
    });
  } else if (exits.length > 0) {
    markers.push({
      time: exits[exits.length - 1],
      position: 'aboveBar',
      color: '#7c3aed',
      shape: 'arrowDown',
      text: 'Exit',
      title: `Structure exit — ${exits[exits.length - 1]}`,
      kind: 'structure',
    });
  }
  return markers;
}

/**
 * Per-leg markers (decision 2c), labelled with the leg so a leg that opened weeks
 * after its sibling is visible on the bars.
 *
 * Same-bar markers COLLAPSE into one marker carrying every label: two arrows drawn
 * at the same time hide each other, and the tooltip is where the detail belongs.
 */
export function legMarkers(legs: TradeChartLeg[]): ChartMarker[] {
  // ENTRY and EXIT are separate markers. Collapsing both onto one below-bar up-arrow made an
  // exit look like an entry, and put two long labels on the same bar.
  const byDay = new Map<string, { entries: string[]; exits: string[] }>();
  const touch = (day: string) => {
    if (!byDay.has(day)) byDay.set(day, { entries: [], exits: [] });
    return byDay.get(day)!;
  };

  for (const leg of legs) {
    const name =
      `${leg.direction === 'long' ? 'Long' : 'Short'} ` +
      `${leg.optionType === 'call' ? 'Call' : 'Put'} $${leg.strike ?? '?'}`;
    // "premium/share", never a stock-axis price: the number beside a premium is
    // per-share and must say so.
    const entryDay = dayOf(leg.entryAt);
    if (entryDay) touch(entryDay).entries.push(`${name} @ ${money(leg.entryPrice)}/share`);
    const exitDay = dayOf(leg.exitAt);
    if (exitDay) touch(exitDay).exits.push(`${name} @ ${money(leg.exitPrice)}/share`);
  }

  const shortLabel = (legs_: string[], verb: string) =>
    legs_.length === 1 ? `${verb} leg` : `${verb} ${legs_.length} legs`;

  const markers: ChartMarker[] = [];
  for (const [day, group] of byDay) {
    if (group.entries.length > 0) {
      markers.push({
        time: day, position: 'belowBar', color: '#0891b2', shape: 'arrowUp',
        text: shortLabel(group.entries, 'Entry'),
        title: `Leg entry — ${day}\n${group.entries.join('\n')}`,
        kind: 'leg',
      });
    }
    if (group.exits.length > 0) {
      markers.push({
        time: day, position: 'aboveBar', color: '#db2777', shape: 'arrowDown',
        text: shortLabel(group.exits, 'Exit'),
        title: `Leg exit — ${day}\n${group.exits.join('\n')}`,
        kind: 'leg',
      });
    }
  }
  return markers;
}

/** Both marker sets, structure first — the toggle in the UI decides what to show. */
export const allMarkers = (legs: TradeChartLeg[], opts: { structure?: boolean; legs?: boolean } = {}) => {
  const { structure = true, legs: showLegs = true } = opts;
  // Sorted by time: the marker sequence used to be entry, exit, entry, exit, which the chart
  // processes in order and which put same-bar markers in an arbitrary order. Same-day
  // structure/leg markers keep the structure first (position: 'aboveBar' < 'belowBar').
  return [
    ...(structure ? structureMarkers(legs) : []),
    ...(showLegs ? legMarkers(legs) : []),
  ].sort((left, right) =>
    left.time.localeCompare(right.time)
    || (left.kind === right.kind ? 0 : left.kind === 'structure' ? -1 : 1)
    || left.position.localeCompare(right.position),
  );
};

/**
 * Strike lines for a context's legs, dropping any leg that cannot be placed on the
 * chart (no strike, no right, no direction). Nothing is defaulted: an unplaceable
 * leg is left out rather than drawn at an invented strike.
 */
export const strikeLineLegs = (legs: TradeChartLeg[]): StrikeLineLeg[] =>
  (legs || [])
    .filter(leg =>
      (leg.direction === 'long' || leg.direction === 'short') &&
      (leg.optionType === 'call' || leg.optionType === 'put') &&
      leg.strike != null && leg.strike > 0)
    .map(leg => ({
      direction: leg.direction as 'long' | 'short',
      optionType: leg.optionType as 'call' | 'put',
      strike: leg.strike as number,
      contracts: leg.size ?? 0,
    }));

/** Breakeven and max profit/loss figures for the summary row under the chart. */
export function payoffSummary(model: PayoffCurve) {
  const limit = (value: number | 'unlimited') =>
    value === 'unlimited' ? 'Unlimited' : money(value);
  return {
    netEntry: model.netEntryDebit,
    netEntryLabel: `${model.netEntryDebit >= 0 ? 'Debit' : 'Credit'} ${money(Math.abs(model.netEntryDebit))}`,
    breakevens: model.breakevens,
    breakevenLabel: model.breakevens.length
      ? model.breakevens.map(value => `$${value.toFixed(2)}`).join(' / ')
      : 'None',
    maxProfit: limit(model.maxProfit),
    maxLoss: limit(model.maxLoss),
    flatZeroIntervals: model.flatZeroIntervals,
  };
}
