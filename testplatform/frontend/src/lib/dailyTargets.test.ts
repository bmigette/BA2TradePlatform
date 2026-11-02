import { describe, it, expect } from 'vitest';
import {
  CROSS_SECTIONAL_DAILY_TYPES,
  DAILY_HORIZON_CHOICES,
  DAILY_TARGET_INFO,
  DAILY_TARGET_TYPES,
  DEFAULT_DAILY_FORM,
  buildDailyTarget,
  crossSectionalIssue,
  dailyTargetLabel,
  isCrossSectionalDailyTarget,
  isDailyTarget,
  toggleHorizon,
} from './dailyTargets';

describe('daily target catalogue', () => {
  it('has the six daily targets with the exploration horizons', () => {
    expect([...DAILY_TARGET_TYPES]).toEqual([
      'daily_rel_return', 'daily_quintile', 'daily_beats', 'daily_up', 'daily_volatility', 'daily_multistep',
    ]);
    expect([...DAILY_HORIZON_CHOICES]).toEqual([1, 3, 5, 10, 20]);
    expect([...CROSS_SECTIONAL_DAILY_TYPES]).toEqual(['daily_rel_return', 'daily_quintile', 'daily_beats']);
    for (const t of DAILY_TARGET_TYPES) expect(DAILY_TARGET_INFO[t].hint.length).toBeGreaterThan(20);
  });

  it('recognises the daily targets', () => {
    expect(isDailyTarget({ type: 'daily_up' })).toBe(true);
    expect(isDailyTarget({ type: 'forward_return' })).toBe(false);
    expect(isDailyTarget(undefined)).toBe(false);
    expect(isCrossSectionalDailyTarget({ type: 'daily_beats' })).toBe(true);
    expect(isCrossSectionalDailyTarget({ type: 'daily_up' })).toBe(false);
  });
});

describe('buildDailyTarget', () => {
  it('builds each target with its category and horizon', () => {
    const form = { ...DEFAULT_DAILY_FORM, horizonSessions: 10 };
    expect(buildDailyTarget('daily_rel_return', form)).toEqual({ type: 'daily_rel_return', category: 'regression', horizonSessions: 10 });
    expect(buildDailyTarget('daily_quintile', form)).toEqual({ type: 'daily_quintile', category: 'binary_classification', horizonSessions: 10 });
    expect(buildDailyTarget('daily_beats', form)).toEqual({ type: 'daily_beats', category: 'binary_classification', horizonSessions: 10 });
    expect(buildDailyTarget('daily_up', form)).toEqual({ type: 'daily_up', category: 'binary_classification', horizonSessions: 10 });
    expect(buildDailyTarget('daily_volatility', form)).toEqual({ type: 'daily_volatility', category: 'regression', horizonSessions: 10 });
  });

  it('builds a multistep target from its horizons list, ascending and without duplicates', () => {
    const cfg = buildDailyTarget('daily_multistep', { horizonSessions: 5, horizons: [20, 1, 5, 5] });
    expect(cfg).toEqual({ type: 'daily_multistep', category: 'binary_classification', horizons: [1, 5, 20] });
  });

  it('refuses an unsupported horizon, an empty multistep list and a one-session volatility', () => {
    expect(() => buildDailyTarget('daily_up', { ...DEFAULT_DAILY_FORM, horizonSessions: 4 })).toThrow(/1 \/ 3 \/ 5 \/ 10 \/ 20/);
    expect(() => buildDailyTarget('daily_multistep', { ...DEFAULT_DAILY_FORM, horizons: [] })).toThrow(/at least one horizon/);
    expect(() => buildDailyTarget('daily_multistep', { ...DEFAULT_DAILY_FORM, horizons: [2] })).toThrow(/Horizon must be/);
    expect(() => buildDailyTarget('daily_volatility', { ...DEFAULT_DAILY_FORM, horizonSessions: 1 })).toThrow(/above 1 session/);
  });
});

describe('toggleHorizon', () => {
  it('keeps the horizons ascending without duplicates', () => {
    let hs: number[] = [];
    hs = toggleHorizon(hs, 20, true);
    hs = toggleHorizon(hs, 3, true);
    hs = toggleHorizon(hs, 20, true);
    expect(hs).toEqual([3, 20]);
    expect(toggleHorizon(hs, 3, false)).toEqual([20]);
    expect(() => toggleHorizon(hs, 7, true)).toThrow();
  });
});

describe('labels', () => {
  it('labels every daily target', () => {
    expect(dailyTargetLabel({ type: 'daily_rel_return', horizonSessions: 5 })).toBe('Daily rel. return 5s');
    expect(dailyTargetLabel({ type: 'daily_quintile', horizonSessions: 10 })).toBe('Daily quintile 10s');
    expect(dailyTargetLabel({ type: 'daily_beats', horizonSessions: 1 })).toBe('Daily beats 1s');
    expect(dailyTargetLabel({ type: 'daily_up', horizonSessions: 3 })).toBe('Daily up 3s');
    expect(dailyTargetLabel({ type: 'daily_volatility', horizonSessions: 20 })).toBe('Daily volatility 20s');
    expect(dailyTargetLabel({ type: 'daily_multistep', horizons: [1, 5, 20] })).toBe('Daily multistep 1/5/20s');
  });
});

describe('crossSectionalIssue', () => {
  const cross = [{ type: 'daily_rel_return', horizonSessions: 5 }];

  it('is silent without a cross-sectional target', () => {
    expect(crossSectionalIssue([{ type: 'daily_up', horizonSessions: 5 }], 1, 'batch')).toBeNull();
    expect(crossSectionalIssue([], 0, 'multi-series')).toBeNull();
  });

  it('refuses fewer than five datasets, naming the single ticker case', () => {
    expect(crossSectionalIssue(cross, 1, 'multi-series')).toMatch(/at least 5 datasets.*1 selected/);
    expect(crossSectionalIssue(cross, 4, 'multi-series')).toMatch(/4 selected/);
  });

  it('refuses one job per dataset', () => {
    expect(crossSectionalIssue(cross, 8, 'batch')).toMatch(/multi-series/);
  });

  it('accepts five or more datasets run together', () => {
    expect(crossSectionalIssue(cross, 5, 'multi-series')).toBeNull();
  });
});
