import { describe, expect, it } from 'vitest';
import { capitalUsageSeries, summariseUsage, IDLE_PCT, HEAVY_PCT } from './capitalUsage';

/**
 * Asked for from live use on 2026-09-05: a tab charting the percent of equity used.
 *
 * The reason it is not `exposureTime`: that stat reads 100% for every run in the
 * library, because it measures "was anything open" and not "how much was committed".
 */

const eq = (...days: [string, number][]) => days.map(([date, equity]) => ({ date, equity }));

const trade = (over: Partial<Record<string, unknown>> = {}) => ({
  entryDate: '2024-01-02T09:30:00', exitDate: '2024-01-03T16:00:00',
  entryPrice: 100, size: 10, ...over,
});

describe('capitalUsageSeries', () => {
  it('measures open notional against equity that day', () => {
    const points = capitalUsageSeries(
      [trade({ entryPrice: 100, size: 10 })],            // $1,000 committed
      eq(['2024-01-02', 10000], ['2024-01-03', 10000], ['2024-01-04', 10000]));
    expect(points.map(p => Math.round(p.pct))).toEqual([10, 10, 0]);
  });

  it('divides by equity AT THE TIME, not by starting capital', () => {
    // A run that triples would otherwise appear to wind down as it succeeds.
    const points = capitalUsageSeries(
      [trade({ entryPrice: 100, size: 10, exitDate: null })],
      eq(['2024-01-02', 10000], ['2024-01-03', 20000]));
    expect(Math.round(points[0].pct)).toBe(10);
    expect(Math.round(points[1].pct)).toBe(5);
  });

  it('counts a position on its exit day too', () => {
    // Held for any part of the day = occupied capital that day.
    const points = capitalUsageSeries(
      [trade({ exitDate: '2024-01-03T10:00:00' })],
      eq(['2024-01-02', 10000], ['2024-01-03', 10000], ['2024-01-04', 10000]));
    expect(points[1].pct).toBeGreaterThan(0);
    expect(points[2].pct).toBe(0);
  });

  it('keeps an unexited trade occupying capital to the end of the run', () => {
    // The wheel's assigned stock is exactly this: nothing closes it, and its capital
    // stays locked. A curve that quietly dropped it would hide the cost.
    const points = capitalUsageSeries(
      [trade({ exitDate: null })],
      eq(['2024-01-02', 10000], ['2024-01-03', 10000], ['2024-01-04', 10000]));
    expect(points.every(p => p.pct === 10)).toBe(true);
  });

  it('adds concurrent positions', () => {
    const points = capitalUsageSeries(
      [trade({ entryPrice: 100, size: 10 }), trade({ entryPrice: 50, size: 40 })],
      eq(['2024-01-02', 10000]));
    expect(Math.round(points[0].pct)).toBe(30);      // 1000 + 2000
    expect(points[0].positions).toBe(2);
  });

  it('values an option leg by its multiplier', () => {
    // A $4.20 contract on 100 shares ties up $420, not $4.20.
    const points = capitalUsageSeries(
      [trade({ entryPrice: 4.2, size: 1, multiplier: 100 })],
      eq(['2024-01-02', 4200]));
    expect(Math.round(points[0].pct)).toBe(10);
  });

  it('treats a short position as capital used, not as negative', () => {
    const points = capitalUsageSeries(
      [trade({ entryPrice: 100, size: -10 })], eq(['2024-01-02', 10000]));
    expect(Math.round(points[0].pct)).toBe(10);
  });

  it('collapses an intraday curve to one point per day', () => {
    const points = capitalUsageSeries([], eq(
      ['2024-01-02T09:30:00', 10000] as never, ['2024-01-02T15:00:00', 11000] as never));
    expect(points).toHaveLength(1);
  });

  it('ignores a trade that never opened', () => {
    const points = capitalUsageSeries([trade({ entryDate: null })], eq(['2024-01-02', 10000]));
    expect(points[0].pct).toBe(0);
  });

  it('reports zero rather than infinity on a wiped-out account', () => {
    const points = capitalUsageSeries([trade()], eq(['2024-01-02', 0]));
    expect(points[0].pct).toBe(0);
  });

  it('releases a trade that exits on a day the equity curve has no bar for', () => {
    // THE BUG REAL DATA FOUND. Keyed on the exact day, a Saturday exit matched no
    // curve key, so the notional was never removed and open capital grew forever:
    // backtest 1113 read 220% average against a true ~27%.
    const points = capitalUsageSeries(
      [trade({ entryDate: '2024-01-02', exitDate: '2024-01-06T16:00:00' })],  // a Saturday
      eq(['2024-01-02', 10000], ['2024-01-05', 10000], ['2024-01-08', 10000]));
    expect(Math.round(points[0].pct)).toBe(10);
    expect(Math.round(points[1].pct)).toBe(10);   // Friday: still held
    expect(points[2].pct).toBe(0);                // Monday: the Saturday exit landed
    expect(points[2].positions).toBe(0);
  });

  it('opens a trade entered on a day the curve has no bar for', () => {
    const points = capitalUsageSeries(
      [trade({ entryDate: '2024-01-06', exitDate: null })],   // a Saturday
      eq(['2024-01-05', 10000], ['2024-01-08', 10000]));
    expect(points[0].pct).toBe(0);
    expect(Math.round(points[1].pct)).toBe(10);
  });

  it('does not leak capital across many gapped exits', () => {
    // The failure mode was cumulative, so one trade is not enough to catch it.
    const trades = Array.from({ length: 50 }, (_, i) => trade({
      entryDate: '2024-01-02', exitDate: '2024-01-06T16:00:00', entryPrice: 100, size: 1,
    }));
    const points = capitalUsageSeries(trades, eq(['2024-01-02', 10000], ['2024-01-08', 10000]));
    expect(points[1].pct).toBe(0);
  });

  it('has nothing to draw without an equity curve', () => {
    expect(capitalUsageSeries([trade()], [])).toEqual([]);
    expect(capitalUsageSeries(null, null)).toEqual([]);
  });
});

describe('summariseUsage', () => {
  const points = [
    { date: '2024-01-02', pct: 5, positions: 1 },
    { date: '2024-01-03', pct: 60, positions: 4 },
    { date: '2024-01-04', pct: 25, positions: 2 },
    { date: '2024-01-05', pct: 90, positions: 6 },
  ];

  it('averages, peaks and names the peak day', () => {
    const s = summariseUsage(points);
    expect(s.avgPct).toBeCloseTo(45);
    expect(s.maxPct).toBe(90);
    expect(s.peakDate).toBe('2024-01-05');
  });

  it('counts idle days — the room another strategy could use', () => {
    expect(summariseUsage(points).idleDaysPct).toBe(25);      // one day under 10%
  });

  it('counts heavy days', () => {
    expect(summariseUsage(points).heavyDaysPct).toBe(50);     // two days over 50%
  });

  it('the thresholds are ordered and sane', () => {
    expect(IDLE_PCT).toBeLessThan(HEAVY_PCT);
  });

  it('an empty series summarises to zeroes, not NaN', () => {
    expect(summariseUsage([])).toEqual({
      avgPct: 0, maxPct: 0, idleDaysPct: 0, heavyDaysPct: 0, peakDate: null,
    });
  });
});
