import { describe, it, expect } from 'vitest';
import { pick, summariseRun, runLabels, cardPosition } from './runSummary';

describe('pick', () => {
  it('reads either spelling, because the table is served both', () => {
    expect(pick({ totalReturn: 42 }, 'totalReturn', 'total_return')).toBe(42);
    expect(pick({ total_return: 42 }, 'totalReturn', 'total_return')).toBe(42);
  });

  it('prefers the first spelling when a row somehow carries both', () => {
    expect(pick({ totalReturn: 1, total_return: 2 }, 'totalReturn', 'total_return')).toBe(1);
  });

  it('treats 0 and empty string as ANSWERS, not as absence', () => {
    expect(pick({ totalReturn: 0 }, 'totalReturn', 'total_return')).toBe(0);
    expect(pick({ name: '' }, 'name')).toBe('');
  });

  it('falls through null as well as undefined', () => {
    expect(pick({ totalReturn: null, total_return: 7 }, 'totalReturn', 'total_return')).toBe(7);
  });
});

describe('summariseRun', () => {
  const row = {
    status: 'completed', engineType: 'daily_expert', fitnessMetric: 'consistent_annual_return',
    startDate: '2020-01-01T00:00:00', endDate: '2025-12-31T00:00:00',
    profitFactor: 1.834, winningTrades: 71, losingTrades: 139,
    bestTrade: 4210.5, worstTrade: -980.25, avgTradeDuration: 12.44,
    initialCapital: 10000, finalEquity: 22911.3,
    totalReturn: 129.113, maxDrawdown: -11.47, winRate: 33.809,
    createdAt: '2026-09-06T14:29:40',
  };

  const byLabel = (fields: { label: string; value: string }[]) =>
    Object.fromEntries(fields.map(f => [f.label, f.value]));

  it('surfaces what the table has no column for', () => {
    const f = byLabel(summariseRun(row));
    expect(f['Profit factor']).toBe('1.83');
    expect(f['Win / loss']).toBe('71 / 139');
    expect(f['Window']).toBe('2020-01-01 → 2025-12-31');
    expect(f['Avg duration']).toBe('12.4 d');
  });

  it('gives the visible columns MORE precision than the column shows', () => {
    const f = byLabel(summariseRun(row));
    expect(f['Return']).toBe('129.11%');
    expect(f['Win rate']).toBe('33.81%');
    expect(f['Max drawdown']).toBe('-11.47%');
  });

  it('OMITS what the row cannot answer rather than inventing a dash or a zero', () => {
    const fields = summariseRun({ status: 'running', name: 'x' });
    const labels = fields.map(f => f.label);
    expect(labels).toContain('Status');
    expect(labels).not.toContain('Profit factor');
    expect(labels).not.toContain('Final equity');
    expect(fields.every(f => f.value !== '0' && f.value !== '—')).toBe(true);
  });

  it('keeps a real zero, which is a measurement', () => {
    const f = byLabel(summariseRun({ totalReturn: 0, winRate: 0 }));
    expect(f['Return']).toBe('0.00%');
    expect(f['Win rate']).toBe('0.00%');
  });

  it('shows the error on a failed run -- the one field worth reading there', () => {
    const f = byLabel(summariseRun({ status: 'failed', errorMessage: 'cache miss: BRPR3' }));
    expect(f['Error']).toBe('cache miss: BRPR3');
  });

  it('reads a fully snake_case row identically', () => {
    const snake = {
      profit_factor: 1.834, winning_trades: 71, losing_trades: 139,
      start_date: '2020-01-01', end_date: '2025-12-31', total_return: 129.113,
    };
    const f = byLabel(summariseRun(snake));
    expect(f['Profit factor']).toBe('1.83');
    expect(f['Win / loss']).toBe('71 / 139');
    expect(f['Return']).toBe('129.11%');
  });

  it('is empty for no row at all', () => {
    expect(summariseRun(null)).toEqual([]);
    expect(summariseRun(undefined)).toEqual([]);
  });
});

describe('runLabels', () => {
  it('takes an array as given', () => {
    expect(runLabels({ labels: ['goal2020-notional', 'S2'] })).toEqual(['goal2020-notional', 'S2']);
  });

  it('parses the JSON-encoded form the column actually stores', () => {
    expect(runLabels({ labels: '["goal2020-notional","S2"]' })).toEqual(['goal2020-notional', 'S2']);
  });

  it('keeps an unparseable string as a single label rather than dropping it', () => {
    expect(runLabels({ labels: 'ForwardTest' })).toEqual(['ForwardTest']);
  });

  it('is empty when there are none', () => {
    expect(runLabels({})).toEqual([]);
    expect(runLabels({ labels: null })).toEqual([]);
  });
});

describe('cardPosition', () => {
  const W = 320, H = 260, VW = 1280, VH = 800;

  it('sits below-right of the pointer when there is room', () => {
    expect(cardPosition(100, 100, W, H, VW, VH)).toEqual({ left: 114, top: 114 });
  });

  it('flips LEFT rather than opening off the right edge', () => {
    const { left } = cardPosition(1200, 100, W, H, VW, VH);
    expect(left).toBe(1200 - 14 - W);
    expect(left + W).toBeLessThanOrEqual(VW);
  });

  it('lifts off the BOTTOM edge -- the long-table case a native title never had', () => {
    const { top } = cardPosition(100, 780, W, H, VW, VH);
    expect(top + H).toBeLessThanOrEqual(VH);
  });

  it('never goes negative in a viewport too small for it', () => {
    const { left, top } = cardPosition(10, 10, 900, 900, 400, 300);
    expect(left).toBeGreaterThanOrEqual(0);
    expect(top).toBeGreaterThanOrEqual(0);
  });
});
