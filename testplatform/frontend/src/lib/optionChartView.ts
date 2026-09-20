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
export type ZoneBand = { from: number; to: number; sign: 'profit' | 'loss' };

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
  shape: 'arrowUp' | 'arrowDown';
  text: string;
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

/** The payoff for the whole transaction, or the reason it cannot be drawn. */
export const payoffFor = (legs: TradeChartLeg[], scope?: number) =>
  buildPayoff(toPayoffLegs(legs), scope == null ? {} : { scope });

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
export function zoneBands(model: PayoffCurve): ZoneBand[] {
  const bounds = [
    model.suggestedDomain.min,
    ...model.breakevens,
    model.suggestedDomain.max,
  ].filter(value => value >= 0).sort((a, b) => a - b);

  const bands: ZoneBand[] = [];
  for (let index = 0; index < bounds.length - 1; index += 1) {
    const from = bounds[index];
    const to = bounds[index + 1];
    if (!(to > from)) continue;
    const pnl = model.payoffAt((from + to) / 2);
    bands.push({ from, to, sign: pnl >= 0 ? 'profit' : 'loss' });
  }
  return bands;
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
    text: `Structure entry`,
    kind: 'structure',
  }];
  if (exits.length > 0) {
    markers.push({
      time: exits[exits.length - 1],
      position: 'aboveBar',
      color: '#7c3aed',
      shape: 'arrowDown',
      text: `Structure exit`,
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
  const byTime = new Map<string, string[]>();
  const order: string[] = [];

  const record = (iso: string | null, text: string) => {
    const day = dayOf(iso);
    if (!day) return;
    if (!byTime.has(day)) { byTime.set(day, []); order.push(day); }
    byTime.get(day)!.push(text);
  };

  for (const leg of legs) {
    const name =
      `${leg.direction === 'long' ? 'Long' : 'Short'} ` +
      `${leg.optionType === 'call' ? 'Call' : 'Put'} $${leg.strike ?? '?'}`;
    // "premium/share", never a stock-axis price: the number beside a premium is
    // per-share and must say so.
    record(leg.entryAt, `▸ ${name} @ ${money(leg.entryPrice)}/share`);
    record(leg.exitAt, `◂ ${name} @ ${money(leg.exitPrice)}/share`);
  }

  return order.map(day => ({
    time: day,
    position: 'belowBar' as const,
    color: '#0891b2',
    shape: 'arrowUp' as const,
    text: byTime.get(day)!.join('\n'),
    kind: 'leg' as const,
  }));
}

/** Both marker sets, structure first — the toggle in the UI decides what to show. */
export const allMarkers = (legs: TradeChartLeg[], opts: { structure?: boolean; legs?: boolean } = {}) => {
  const { structure = true, legs: showLegs = true } = opts;
  return [
    ...(structure ? structureMarkers(legs) : []),
    ...(showLegs ? legMarkers(legs) : []),
  ];
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
