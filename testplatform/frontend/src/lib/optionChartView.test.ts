import { describe, it, expect } from 'vitest';
import {
  allMarkers, legMarkers, payoffFor, payoffGeometry, payoffSegments, payoffSummary,
  samplePayoff, strikeLines, structureMarkers, toPayoffLegs, zoneBands,
} from './optionChartView';
import type { TradeChartLeg } from './btApi';

/**
 * What the option popup draws (spec 2026-09-20, decisions 1a and 2c). The geometry
 * is asserted against the same fixtures the payoff arithmetic uses, so a drawing
 * that disagrees with the curve it claims to render fails here.
 */

const leg = (over: Partial<TradeChartLeg> = {}): TradeChartLeg => ({
  id: 1, symbol: 'ACN', underlyingSymbol: 'ACN',
  contractSymbol: 'ACN260918C00100000', optionType: 'call', strike: 100,
  expiry: '2026-09-18', multiplier: 100, multiplierRecorded: true,
  direction: 'long', size: 1,
  entryAt: '2026-09-08T13:30:00', exitAt: '2026-09-11T19:45:00',
  entryPrice: 5, exitPrice: 9, pnl: 400, pnlPercent: 1.2, exitReason: 'take_profit',
  transactionId: '7', rowBasis: 'aggregate_round_trip', positionStatus: 'closed',
  entryUnderlying: {
    price: 100, eventAt: '2026-09-08T13:30:00', observedAt: '2026-09-04T00:00:00Z',
    availableAt: null, quality: 'last_known_bar', source: 'FMPOHLCVProvider', reason: null,
  },
  exitUnderlying: {
    price: 108, eventAt: '2026-09-11T19:45:00', observedAt: '2026-09-10T00:00:00Z',
    availableAt: null, quality: 'last_known_bar', source: 'FMPOHLCVProvider', reason: null,
  },
  unavailableFields: [],
  ...over,
});

const STRADDLE = [
  leg({ id: 1, optionType: 'call', strike: 100, entryPrice: 5 }),
  leg({ id: 2, optionType: 'put', strike: 100, entryPrice: 4 }),
];

const CONDOR = [
  leg({ id: 1, optionType: 'put', strike: 90, entryPrice: 1 }),
  leg({ id: 2, optionType: 'put', strike: 95, entryPrice: 2, direction: 'short' }),
  leg({ id: 3, optionType: 'call', strike: 105, entryPrice: 2, direction: 'short' }),
  leg({ id: 4, optionType: 'call', strike: 110, entryPrice: 1 }),
];

const spread = [
  leg({ id: 1, strike: 95, entryPrice: 8 }),
  leg({ id: 2, strike: 105, entryPrice: 2, direction: 'short' }),
];

describe('context legs -> payoff inputs', () => {
  it('passes the recorded multiplier through WITH its provenance', () => {
    const [input] = toPayoffLegs([leg({ multiplier: null, multiplierRecorded: false })]);
    expect(input.multiplier).toBeNull();
    expect(input.multiplierRecorded).toBe(false);
  });

  it('refuses to price a leg whose multiplier was never recorded', () => {
    const result = payoffFor([leg({ multiplier: null, multiplierRecorded: false })]);
    expect(result.available).toBe(false);
  });

  it('prices a complete spread', () => {
    const result = payoffFor(spread);
    expect(result.available).toBe(true);
  });
});

describe('sampling and segments', () => {
  it('samples every strike and breakeven, ascending, never negative', () => {
    const model = payoffFor(STRADDLE);
    if (!model.available) throw new Error(model.reason);
    const points = samplePayoff(model);

    expect(points.every(point => point.underlying >= 0)).toBe(true);
    expect([...points].sort((a, b) => a.underlying - b.underlying)).toEqual(points);
    for (const anchor of [100, 91, 109]) {
      expect(points.some(point => Math.abs(point.underlying - anchor) < 1e-6)).toBe(true);
    }
  });

  it('splits a straddle into profit / loss / profit, meeting on the breakevens', () => {
    const model = payoffFor(STRADDLE);
    if (!model.available) throw new Error(model.reason);
    const segments = payoffSegments(samplePayoff(model));

    expect(segments.map(segment => segment.sign)).toEqual(['profit', 'loss', 'profit']);
    // Each internal boundary is a breakeven: the fill changes sign exactly there.
    const boundary = segments[0].points[segments[0].points.length - 1];
    expect(boundary.pnl).toBeCloseTo(0, 6);
    expect(boundary.underlying).toBeCloseTo(91, 6);
  });

  it('splits an iron condor into loss / profit / loss', () => {
    const model = payoffFor(CONDOR);
    if (!model.available) throw new Error(model.reason);
    expect(payoffSegments(samplePayoff(model)).map(s => s.sign))
      .toEqual(['loss', 'profit', 'loss']);
  });

  it('gives a long call one loss run and one profit run', () => {
    const model = payoffFor([leg()]);
    if (!model.available) throw new Error(model.reason);
    expect(payoffSegments(samplePayoff(model)).map(s => s.sign)).toEqual(['loss', 'profit']);
  });
});

describe('geometry', () => {
  it('caps the curve to a fraction of the plot width', () => {
    const model = payoffFor([leg()]);
    if (!model.available) throw new Error(model.reason);
    const geometry = payoffGeometry(model);
    expect(geometry.widthFraction).toBeGreaterThan(0);
    expect(geometry.widthFraction).toBeLessThanOrEqual(0.5);
    expect(geometry.maxAbsPnl).toBeGreaterThan(0);
  });

  it('scales to the largest magnitude the curve reaches in the domain', () => {
    const model = payoffFor([leg({ entryPrice: 5 })]);
    if (!model.available) throw new Error(model.reason);
    const geometry = payoffGeometry(model);
    const largest = Math.max(...geometry.points.map(point => Math.abs(point.pnl)));
    expect(geometry.maxAbsPnl).toBeCloseTo(largest, 6);
  });

  it('carries the curve, not a stub: the extreme of an unlimited tail is still drawn', () => {
    const model = payoffFor([leg({ entryPrice: 5 })]);
    if (!model.available) throw new Error(model.reason);
    expect(model.maxProfit).toBe('unlimited');
    // "Unlimited" is a summary claim; the DRAWN curve is finite and non-trivial.
    expect(payoffGeometry(model).maxAbsPnl).toBeGreaterThan(100);
  });
});

describe('zone bands', () => {
  it('bands a straddle profit / loss / profit, agreeing with the curve', () => {
    const model = payoffFor(STRADDLE);
    if (!model.available) throw new Error(model.reason);
    const bands = zoneBands(model);

    expect(bands.map(band => band.sign)).toEqual(['profit', 'loss', 'profit']);
    for (const band of bands) {
      const midpoint = (band.from + band.to) / 2;
      const pnl = model.payoffAt(midpoint);
      expect(band.sign).toBe(pnl >= 0 ? 'profit' : 'loss');
    }
  });

  it('bands an iron condor loss / profit / loss and starts at a non-negative price', () => {
    const model = payoffFor(CONDOR);
    if (!model.available) throw new Error(model.reason);
    const bands = zoneBands(model);
    expect(bands.map(band => band.sign)).toEqual(['loss', 'profit', 'loss']);
    expect(bands[0].from).toBeGreaterThanOrEqual(0);
  });

  it('uses the breakevens as the boundaries', () => {
    const model = payoffFor(STRADDLE);
    if (!model.available) throw new Error(model.reason);
    expect(zoneBands(model).map(band => band.to).slice(0, 2)).toEqual([91, 109]);
  });
});

describe('strike lines', () => {
  it('draws one line per distinct strike, labelled with the leg', () => {
    const model = payoffFor(spread);
    if (!model.available) throw new Error(model.reason);
    const lines = strikeLines(model.legs);

    expect(lines.map(line => line.price)).toEqual([95, 105]);
    expect(lines[0].label).toBe('Long 1 Call · K $95');
    expect(lines[1].label).toBe('Short 1 Call · K $105');
    expect(lines.every(line => !line.collidesWithPrevious)).toBe(true);
  });

  it('folds two legs on one strike into a single line naming both', () => {
    const model = payoffFor(STRADDLE);
    if (!model.available) throw new Error(model.reason);
    const lines = strikeLines(model.legs);

    expect(lines).toHaveLength(1);
    expect(lines[0].contracts).toBe(2);
    expect(lines[0].label).toContain('Long 1 Call');
    expect(lines[0].label).toContain('Long 1 Put');
  });

  it('flags a label that would land on its neighbour, using the CHART price range', () => {
    const model = payoffFor([
      leg({ id: 1, strike: 100, entryPrice: 5 }),
      leg({ id: 2, strike: 100.02, entryPrice: 1, direction: 'short' }),
    ]);
    if (!model.available) throw new Error(model.reason);

    // 0.02 apart on a $2 axis is 1% of the height: the labels would overlap.
    expect(strikeLines(model.legs, { min: 99, max: 101 })[1].collidesWithPrevious).toBe(true);

    // Half a dollar apart on the same axis is a quarter of it: comfortably clear.
    const wide = payoffFor([
      leg({ id: 1, strike: 100, entryPrice: 5 }),
      leg({ id: 2, strike: 100.5, entryPrice: 1, direction: 'short' }),
    ]);
    if (!wide.available) throw new Error(wide.reason);
    expect(strikeLines(wide.legs, { min: 99, max: 101 })[1].collidesWithPrevious).toBe(false);
  });

  it('does not flag evenly spaced legs of a wide structure', () => {
    const model = payoffFor(CONDOR);
    if (!model.available) throw new Error(model.reason);
    const lines = strikeLines(model.legs, { min: 85, max: 115 });
    expect(lines.every(line => !line.collidesWithPrevious)).toBe(true);
  });
});

describe('marker sets (decision 2c)', () => {
  it('marks the structure with first entry and last exit only', () => {
    const markers = structureMarkers([
      leg({ id: 1, entryAt: '2026-09-08T13:30:00', exitAt: '2026-09-11T19:45:00' }),
      leg({ id: 2, entryAt: '2026-09-15T13:30:00', exitAt: '2026-09-18T19:45:00' }),
    ]);
    expect(markers.map(marker => marker.time)).toEqual(['2026-09-08', '2026-09-18']);
    expect(markers.every(marker => marker.kind === 'structure')).toBe(true);
  });

  it('labels every leg, premium per share, collapsing legs that share a bar', () => {
    const markers = legMarkers(spread);
    // Both legs of this fixture enter on 2026-09-08 and exit on 2026-09-11, so the
    // two sets collapse into two markers carrying both labels each.
    expect(markers.map(marker => marker.time)).toEqual(['2026-09-08', '2026-09-11']);
    expect(markers[0].text.split('\n')).toHaveLength(2);
    expect(markers[0].text).toContain('Long Call $95');
    expect(markers[0].text).toContain('$8.00/share');
    expect(markers[0].text).toContain('Short Call $105');
    expect(markers.every(marker => marker.kind === 'leg')).toBe(true);
  });

  it('keeps legs apart when they enter on different bars', () => {
    const markers = legMarkers([
      leg({ id: 1, entryAt: '2026-09-08T13:30:00', exitAt: '2026-09-11T19:45:00' }),
      leg({ id: 2, optionType: 'put', entryAt: '2026-09-09T13:30:00', exitAt: null }),
    ]);
    expect(markers.map(marker => marker.time)).toEqual(['2026-09-08', '2026-09-11', '2026-09-09']);
    expect(markers.map(marker => marker.text.split('\n').length)).toEqual([1, 1, 1]);
  });

  it('collapses same-bar leg markers into one, keeping every label', () => {
    const markers = legMarkers([
      leg({ id: 1, entryAt: '2026-09-08T13:30:00' }),
      leg({ id: 2, optionType: 'put', entryAt: '2026-09-08T13:30:00' }),
    ]);
    const entry = markers.filter(marker => marker.time === '2026-09-08');
    expect(entry).toHaveLength(1);
    expect(entry[0].text.split('\n')).toHaveLength(2);
  });

  it('does not invent an exit marker for a leg that never exited', () => {
    const markers = legMarkers([leg({ exitAt: null, exitPrice: null })]);
    expect(markers.map(marker => marker.time)).toEqual(['2026-09-08']);
  });

  it('honours the toggle: either set can be shown alone', () => {
    expect(allMarkers(spread).length).toBeGreaterThan(legMarkers(spread).length);
    expect(allMarkers(spread, { legs: false }).every(m => m.kind === 'structure')).toBe(true);
    expect(allMarkers(spread, { structure: false }).every(m => m.kind === 'leg')).toBe(true);
  });
});

describe('summary figures', () => {
  it('labels a net debit and reports both limits', () => {
    const model = payoffFor(spread);
    if (!model.available) throw new Error(model.reason);
    const summary = payoffSummary(model);

    expect(summary.netEntry).toBe(600);
    expect(summary.netEntryLabel).toBe('Debit $600.00');
    expect(summary.breakevenLabel).toBe('$101.00');
    expect(summary.maxProfit).toBe('$400.00');
    expect(summary.maxLoss).toBe('$-600.00');
  });

  it('labels a credit structure and keeps "Unlimited" distinguishable', () => {
    const model = payoffFor([leg({ direction: 'short' })]);
    if (!model.available) throw new Error(model.reason);
    const summary = payoffSummary(model);

    expect(summary.netEntryLabel).toBe('Credit $500.00');
    expect(summary.maxProfit).toBe('$500.00');
    expect(summary.maxLoss).toBe('Unlimited');
  });
});
