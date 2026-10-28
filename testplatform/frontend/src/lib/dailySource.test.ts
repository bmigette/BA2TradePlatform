import { describe, it, expect } from 'vitest';
import {
  DAILY_GROUP_OPTIONS,
  DEFAULT_DAILY_STATE,
  DAILY_MASK_HELP,
  DAILY_UNIVERSE_HELP,
  allowedTimeframes,
  breadthNote,
  duplicateUniverseNote,
  dailyPayload,
  dailySummary,
  hydrateDailyState,
  isDailySource,
  storedUniverseSize,
  toggleDailyGroup,
  validateDailySource,
} from './dailySource';

const ALL_TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w', '1mo'];

describe('daily source groups', () => {
  it('offers every store group, in the store order, each with a one-line description', () => {
    expect(DAILY_GROUP_OPTIONS.map((o) => o.id)).toEqual(['t', 'mtf', 'm', 'x', 'mac', 'f', 'ag', 'at', 'i', 's']);
    for (const o of DAILY_GROUP_OPTIONS) {
      expect(o.name.length).toBeGreaterThan(2);
      expect(o.desc.length).toBeGreaterThan(10);
      expect(o.desc).not.toContain('\n');
    }
  });

  it('toggles a group and keeps the store order without duplicates', () => {
    let groups: string[] = [];
    groups = toggleDailyGroup(groups, 'mac', true);
    groups = toggleDailyGroup(groups, 't', true);
    groups = toggleDailyGroup(groups, 'mac', true);
    expect(groups).toEqual(['t', 'mac']);
    expect(toggleDailyGroup(groups, 't', false)).toEqual(['mac']);
    expect(() => toggleDailyGroup(groups, 'zz', true)).toThrow(/unknown daily group/);
  });
});

describe('daily source selection', () => {
  it('is the alpaca_hist provider at 1d only', () => {
    expect(isDailySource('alpaca_hist', '1d')).toBe(true);
    expect(isDailySource('alpaca_hist', '1h')).toBe(false);
    expect(isDailySource('yfinance', '1d')).toBe(false);
  });

  it('lets the cache offer 1h, 15m and 1d', () => {
    expect(allowedTimeframes('alpaca_hist', ALL_TIMEFRAMES)).toEqual(['15m', '1h', '1d']);
    expect(allowedTimeframes('yfinance', ALL_TIMEFRAMES)).toEqual(ALL_TIMEFRAMES);
  });
});

describe('validateDailySource', () => {
  const ok = { groups: ['t', 'm'], minShare: '0.7' };

  it('accepts a valid request', () => {
    expect(validateDailySource(ok, 1)).toBeNull();
  });

  it('refuses no group, an unknown group and a bad threshold', () => {
    expect(validateDailySource({ ...ok, groups: [] }, 1)).toMatch(/at least one feature group/);
    expect(validateDailySource({ ...ok, groups: ['zz'] }, 1)).toMatch(/Unknown feature group/);
    for (const bad of ['', '0', '-0.1', '1.5', 'abc']) {
      expect(validateDailySource({ ...ok, minShare: bad }, 1)).toMatch(/coverage threshold/);
    }
    expect(validateDailySource({ ...ok, minShare: '1' }, 1)).toBeNull();
  });

  it('needs five tickers for the cross-sectional group', () => {
    const x = { groups: ['t', 'x'], minShare: '0.7' };
    expect(validateDailySource(x, 4)).toMatch(/at least 5 tickers/);
    expect(validateDailySource(x, 5)).toBeNull();
  });
});

describe('payload, hydration and summary', () => {
  it('sends the daily options for the daily source only', () => {
    const state = { groups: ['t', 'mac'], minShare: '0.6' };
    expect(dailyPayload('alpaca_hist', '1d', state)).toEqual({ daily_groups: ['t', 'mac'], daily_min_share: 0.6 });
    expect(dailyPayload('alpaca_hist', '1h', state)).toEqual({});
    expect(dailyPayload('yfinance', '1d', state)).toEqual({});
  });

  it('round-trips through the generation_config of a dataset', () => {
    const state = { groups: ['s', 't'], minShare: '0.55' };
    const payload = dailyPayload('alpaca_hist', '1d', state);
    const generationConfig = { data_provider: 'alpaca_hist', ...payload, daily_universe: ['AAA', 'BBB', 'CCC'] };
    expect(hydrateDailyState(generationConfig)).toEqual({ groups: ['t', 's'], minShare: '0.55' });
    expect(storedUniverseSize(generationConfig)).toBe(3);
  });

  it('hydrates defaults from a dataset that has no daily options', () => {
    expect(hydrateDailyState({ data_provider: 'yfinance' })).toEqual({ groups: [], minShare: '0.7' });
    expect(hydrateDailyState(undefined)).toEqual(DEFAULT_DAILY_STATE);
    expect(storedUniverseSize({})).toBe(1);
  });

  it('summarises the groups, the threshold and the universe', () => {
    const text = dailySummary({ groups: ['t', 'x'], minShare: '0.7' }, 12);
    expect(text).toContain('Technical (t)');
    expect(text).toContain('Cross-sectional (x)');
    expect(text).toContain('coverage threshold 0.7');
    expect(text).toContain('12 tickers');
    expect(dailySummary({ groups: ['t'], minShare: '0.7' }, 1)).toContain('1 ticker');
  });
});

describe('notes about the universe and the mask', () => {
  const gc = { data_provider: 'alpaca_hist', daily_groups: ['t', 'x', 'm'], daily_universe: ['AAA', 'BBB', 'CCC', 'DDD', 'EEE'] };

  it('warns when a duplicate widens the universe of a dataset that uses x or the breadth', () => {
    const note = duplicateUniverseNote(gc, 'FFF');
    expect(note).toContain('FFF');
    expect(note).toContain('5 -> 6');
    expect(note).toMatch(/differ/);
  });

  it('is silent for a ticker already in the universe, an empty ticker or groups that do not depend on it', () => {
    expect(duplicateUniverseNote(gc, 'AAA')).toBeNull();
    expect(duplicateUniverseNote(gc, '')).toBeNull();
    expect(duplicateUniverseNote({ ...gc, daily_groups: ['t', 'mtf'] }, 'FFF')).toBeNull();
    expect(duplicateUniverseNote({ data_provider: 'yfinance' }, 'FFF')).toBeNull();
  });

  it('says the breadth is dropped for a universe below five tickers', () => {
    expect(breadthNote(['t', 'm'], 1)).toMatch(/breadth.*dropped/);
    expect(breadthNote(['t', 'm'], 4)).toMatch(/dropped/);
    expect(breadthNote(['t', 'm'], 5)).toBeNull();
    expect(breadthNote(['t'], 1)).toBeNull();
  });

  it('documents the per-year mask and the survivorship of the universe', () => {
    expect(DAILY_MASK_HELP).toMatch(/calendar year/);
    expect(DAILY_MASK_HELP).toMatch(/not a look-ahead/);
    expect(DAILY_UNIVERSE_HELP).toMatch(/your own ticker list/);
    expect(DAILY_UNIVERSE_HELP).toMatch(/survivorship/);
  });

  it('describes group m with the breadth rule and x with the survivorship', () => {
    const m = DAILY_GROUP_OPTIONS.find((o) => o.id === 'm')!;
    expect(m.desc).toMatch(/breadth/);
    expect(m.desc).toMatch(/5 tickers/);
  });
});
