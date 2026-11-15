import { describe, it, expect } from 'vitest';
import { buildOptimizeArgs, buildRequest, datePos, fmtDd, fmtPct, parseYears, splitArgs } from './walkForward';
import type { WfForm } from './walkForward';

const FORM: WfForm = {
  name: 'wf-ui-test', expert: 'FMPRating', strategy: 'S2', universeMode: 'static',
  universe: 'AAPL,MSFT', screenerStore: '', capBand: 'large', fitness: 'consistent_annual_return',
  population: '300', parallel: '4', interval: '5min', estimatesSource: 'fmp', historySource: 'alpaca',
  earlyStop: '5', earlyStopMinRel: '0.01', labels: '', workers: '', extraArgs: '',
  trainStart: '2020-01-01', testYears: '2024, 2025, 2026', dataEnd: '', firstGenerations: '30',
  nextGenerations: '20', topN: '3', testParallel: '1', minOosReturn: '0', exclusiveReturn: true,
  maxDdMult: '2.5', maxOosDd: '35', minFolds: '2', requireTestYears: '2026', deferLastTest: false,
  runAllFolds: false, refit: true,
};

describe('splitArgs', () => {
  it('splits on whitespace and keeps quoted groups together', () => {
    expect(splitArgs(' --a 1  --b "x y" --c \'\' ')).toEqual(['--a', '1', '--b', 'x y', '--c', '']);
  });
  it('refuses an unclosed quote', () => {
    expect(() => splitArgs('--a "x')).toThrow(/unclosed/);
  });
});

describe('parseYears', () => {
  it('reads comma or space separated years', () => {
    expect(parseYears('2024, 2025 2026', 'Test years')).toEqual([2024, 2025, 2026]);
    expect(parseYears('', 'Required')).toEqual([]);
  });
  it('names what is not a year', () => {
    expect(() => parseYears('2024, 25', 'Test years')).toThrow('Test years: 25 is not a year');
  });
});

describe('buildOptimizeArgs', () => {
  it('renders the ba2-test optimize tokens of a static-universe job', () => {
    expect(buildOptimizeArgs(FORM)).toEqual([
      '--expert', 'FMPRating', '--strategy', 'S2', '--universe', 'AAPL,MSFT',
      '--fitness', 'consistent_annual_return', '--population', '300', '--parallel', '4',
      '--interval', '5min', '--estimates-source', 'fmp', '--ohlcv-history-source', 'alpaca',
      '--early-stop', '5', '--early-stop-min-rel', '0.01']);
  });
  it('renders the screener flags and appends the extra args verbatim', () => {
    const args = buildOptimizeArgs({ ...FORM, universeMode: 'screener', screenerStore: 'C:/store',
      extraArgs: '--market-condition-profile ohlcv-v1 --labels "goal x"' });
    expect(args.slice(4, 11)).toEqual(['--universe', 'AAPL,MSFT', '--screener', '--screener-store',
      'C:/store', '--screener-cap-band', 'large']);
    expect(args.slice(-4)).toEqual(['--market-condition-profile', 'ohlcv-v1', '--labels', 'goal x']);
  });
  it('leaves out an unstated estimates source (the backend refuses it with the launcher message)', () => {
    expect(buildOptimizeArgs({ ...FORM, estimatesSource: '' })).not.toContain('--estimates-source');
  });
  it('refuses a missing required field', () => {
    expect(() => buildOptimizeArgs({ ...FORM, expert: '' })).toThrow('Expert is required');
    expect(() => buildOptimizeArgs({ ...FORM, population: '3.5' })).toThrow(/whole number/);
  });
});

describe('buildRequest', () => {
  it('maps the walk-forward fields', () => {
    const r = buildRequest(FORM);
    expect(r.test_years).toEqual([2024, 2025, 2026]);
    expect([r.first_generations, r.next_generations, r.top_n]).toEqual([30, 20, 3]);
    expect([r.min_oos_return, r.max_dd_mult, r.max_oos_dd, r.min_folds]).toEqual([0, 2.5, 35, 2]);
    expect(r.require_test_years).toEqual([2026]);
    expect(r.data_end).toBeNull();
    expect([r.exclusive_return, r.defer_last_test, r.run_all_folds, r.refit]).toEqual([true, false, false, true]);
  });
  it('sends no absolute drawdown cap when the field is empty', () => {
    expect(buildRequest({ ...FORM, maxOosDd: '' }).max_oos_dd).toBeNull();
  });
});

describe('formatting', () => {
  it('formats percents and drawdowns', () => {
    expect(fmtPct(5)).toBe('+5.00%');
    expect(fmtPct(-1.234, 1)).toBe('-1.2%');
    expect(fmtPct(null)).toBe('—');
    expect(fmtDd(12.5)).toBe('-12.50%');
    expect(fmtDd(-12.5)).toBe('-12.50%');
  });
  it('places a date on the timeline', () => {
    expect(datePos('2022-01-01', '2020-01-01', '2024-01-01')).toBeCloseTo(0.5, 2);
    expect(datePos('2030-01-01', '2020-01-01', '2024-01-01')).toBe(1);
  });
});
