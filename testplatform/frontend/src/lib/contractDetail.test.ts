/**
 * Contract detail consumption (spec 2026-09-20, review finding R6(3)).
 *
 * The API returned entryContract/exitContract and nothing displayed them. These tests use the
 * review's own fixture values, so "the greeks arrive" is asserted with the numbers the review
 * supplied rather than with invented ones.
 */
import { describe, expect, it } from 'vitest';

import type { ContractDetail, TradeChartLeg } from './btApi';
import {
  contractDetailRows, contractQualityLabel, greekText, hasContractDetail,
} from './contractDetail';

const detail = (over: Partial<ContractDetail> = {}): ContractDetail => ({
  asOf: '2026-09-04', observedAt: '2026-09-04',
  iv: 0.42, delta: 0.65, gamma: 0.03, theta: -0.05, vega: 0.12,
  openInterest: 1234, volume: 88,
  quality: 'approximate_prior_session', source: 'C:/store.sqlite', reason: null,
  ...over,
});

const leg = (over: Partial<TradeChartLeg> = {}): TradeChartLeg => ({
  id: 1, direction: 'long', optionType: 'call', strike: 95,
  entryContract: detail(), exitContract: detail({ asOf: '2026-09-10', iv: 0.5 }),
  ...over,
} as unknown as TradeChartLeg);

describe('contract detail rows', () => {
  it('carries the recorded greeks through instead of dropping them', () => {
    const rows = contractDetailRows([leg()]);
    expect(rows).toHaveLength(2); // entry and exit
    const entry = rows[0];
    expect(entry.at).toBe('entry');
    expect(entry.iv).toBe('0.420');
    expect(entry.delta).toBe('0.650');
    expect(entry.gamma).toBe('0.0300');
    expect(entry.theta).toBe('-0.0500');
    expect(entry.vega).toBe('0.1200');
    expect(entry.openInterest).toBe('1234');
  });

  it('shows the observation date, so an approximation is not read as an entry quote', () => {
    const rows = contractDetailRows([leg()]);
    expect(rows[0].asOf).toBe('2026-09-04');
    expect(rows[0].observation).toBe('prior session (approximate)');
    expect(rows[1].asOf).toBe('2026-09-10');
  });

  it('renders a null greek as a dash, never as zero', () => {
    const rows = contractDetailRows([leg({
      entryContract: detail({ iv: null, delta: null, gamma: null, theta: null, vega: null,
                              openInterest: null }),
    })]);
    for (const field of ['iv', 'delta', 'gamma', 'theta', 'vega', 'openInterest'] as const) {
      expect(rows[0][field]).toBe('—');
    }
  });

  it('renders a MEASURED zero as zero', () => {
    // The distinction the whole "never substitute" rule rests on: 0 is data, null is absence.
    expect(greekText(0)).toBe('0.000');
    expect(greekText(null)).toBe('—');
    expect(greekText(undefined)).toBe('—');
    expect(greekText(Number.NaN)).toBe('—');
  });

  it('says a leg has no detail rather than showing empty greeks', () => {
    const rows = contractDetailRows([leg({ entryContract: null, exitContract: null })]);
    expect(rows[0].supplied).toBe(false);
    expect(rows[0].observation).toBe('not supplied');
    expect(rows[0].iv).toBe('—');
  });

  it('carries the reason for an unavailable read', () => {
    const rows = contractDetailRows([leg({
      entryContract: detail({ iv: null, delta: null, quality: 'unavailable',
                              reason: 'no cached contract bar at or before 2026-09-04' }),
    })]);
    expect(rows[0].observation).toBe('unavailable');
    expect(rows[0].reason).toContain('no cached contract bar');
  });

  it('labels every quality explicitly', () => {
    expect(contractQualityLabel.cache_bar).toBe('own session');
    expect(contractQualityLabel.daily_reference).toBe('daily reference');
    expect(contractQualityLabel.partial).toBe('bar without greeks');
  });

  it('only renders the block when there is something to render', () => {
    expect(hasContractDetail([leg()])).toBe(true);
    expect(hasContractDetail([leg({ entryContract: null, exitContract: null })])).toBe(false);
    expect(hasContractDetail([])).toBe(false);
  });
});
