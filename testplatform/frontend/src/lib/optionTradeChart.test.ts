import { describe, it, expect } from 'vitest';
import {
  buildPayoff, moneyness, moneynessTolerance, spotVsStrikePercent,
  type OptionLegInput, type PayoffCurve,
} from './optionTradeChart';

/**
 * The option popup's payoff arithmetic, against the spec's own fixture table
 * (docs/plans/2026-09-20-option-trade-chart-spec.md §8): USD, one contract per
 * leg, multiplier 100, no fees unless a case says otherwise.
 */

const leg = (over: Partial<OptionLegInput> = {}): OptionLegInput => ({
  direction: 'long', optionType: 'call', strike: 100, entryPremium: 5,
  contracts: 1, multiplier: 100, ...over,
});

/** Narrowing helper: a fixture that must be priceable. */
const priced = (legs: OptionLegInput[], scope?: number): PayoffCurve => {
  const result = buildPayoff(legs, scope == null ? {} : { scope });
  if (!result.available) throw new Error(`expected a payoff, got: ${result.reason}`);
  return result;
};

const reasonOf = (legs: OptionLegInput[], scope?: number): string => {
  const result = buildPayoff(legs, scope == null ? {} : { scope });
  if (result.available) throw new Error('expected the payoff to be unavailable');
  return result.reason;
};

describe('single legs', () => {
  it('long call K100 at 5: breakeven 105, max loss 500, unlimited profit', () => {
    const curve = priced([leg()]);
    expect(curve.netEntryDebit).toBe(500);
    expect(curve.breakevens).toEqual([105]);
    expect(curve.maxLoss).toBe(-500);
    expect(curve.maxProfit).toBe('unlimited');
    // ITM is not profitable: at S=102 the call is in the money and the position is down 300.
    expect(curve.payoffAt(102)).toBe(-300);
    expect(curve.payoffAt(100)).toBe(-500);
  });

  it('short call K100 at 5: same breakeven, max profit 500, unlimited loss', () => {
    const curve = priced([leg({ direction: 'short' })]);
    expect(curve.netEntryDebit).toBe(-500);
    expect(curve.breakevens).toEqual([105]);
    expect(curve.maxProfit).toBe(500);
    expect(curve.maxLoss).toBe('unlimited');
    expect(curve.payoffAt(102)).toBe(300);
  });

  it('long put K100 at 5: breakeven 95, max loss 500, max profit 9500 at S=0', () => {
    const curve = priced([leg({ optionType: 'put' })]);
    expect(curve.breakevens).toEqual([95]);
    expect(curve.maxLoss).toBe(-500);
    expect(curve.maxProfit).toBe(9500);
    expect(curve.payoffAt(0)).toBe(9500);
    expect(curve.payoffAt(96)).toBe(-100);
  });

  it('short put K100 at 5: max profit 500, max loss 9500 -- bounded by S >= 0', () => {
    const curve = priced([leg({ optionType: 'put', direction: 'short' })]);
    expect(curve.breakevens).toEqual([95]);
    expect(curve.maxProfit).toBe(500);
    expect(curve.maxLoss).toBe(-9500);
    expect(curve.maxLoss).not.toBe('unlimited');
  });

  it('scales with contracts', () => {
    const curve = priced([leg({ contracts: 2 })]);
    expect(curve.netEntryDebit).toBe(1000);
    expect(curve.maxLoss).toBe(-1000);
    expect(curve.payoffAt(102)).toBe(-600);
  });
});

describe('structures', () => {
  it('call debit spread K95/K105: net debit 600, breakeven 101, 400/600', () => {
    const curve = priced([
      leg({ strike: 95, entryPremium: 8 }),
      leg({ strike: 105, entryPremium: 2, direction: 'short' }),
    ]);
    expect(curve.netEntryDebit).toBe(600);
    expect(curve.breakevens).toEqual([101]);
    expect(curve.maxProfit).toBe(400);
    expect(curve.maxLoss).toBe(-600);
    // The spec's exit scenario: the recorded gross was +370 while the expiration
    // curve at the same spot is +400. The two are different numbers by design.
    expect(curve.payoffAt(108)).toBe(400);
  });

  it('put credit spread K100/K95: credit 300, breakeven 97, 300/200', () => {
    const curve = priced([
      leg({ optionType: 'put', direction: 'short', entryPremium: 4 }),
      leg({ optionType: 'put', strike: 95, entryPremium: 1 }),
    ]);
    expect(curve.netEntryDebit).toBe(-300);
    expect(curve.breakevens).toEqual([97]);
    expect(curve.maxProfit).toBe(300);
    expect(curve.maxLoss).toBe(-200);
  });

  it('long straddle K100: breakevens 91/109, max loss 900, profit at BOTH tails', () => {
    const curve = priced([
      leg({ optionType: 'call', entryPremium: 5 }),
      leg({ optionType: 'put', entryPremium: 4 }),
    ]);
    expect(curve.breakevens).toEqual([91, 109]);
    expect(curve.maxLoss).toBe(-900);
    expect(curve.maxProfit).toBe('unlimited');
    expect(curve.payoffAt(80)).toBeGreaterThan(0);
    expect(curve.payoffAt(120)).toBeGreaterThan(0);
    expect(curve.payoffAt(100)).toBe(-900);
  });

  it('iron condor: credit 200, breakevens 93/107, max loss 300, both outer tails lose', () => {
    const curve = priced([
      leg({ optionType: 'put', strike: 90, entryPremium: 1 }),
      leg({ optionType: 'put', strike: 95, entryPremium: 2, direction: 'short' }),
      leg({ optionType: 'call', strike: 105, entryPremium: 2, direction: 'short' }),
      leg({ optionType: 'call', strike: 110, entryPremium: 1 }),
    ]);
    expect(curve.netEntryDebit).toBe(-200);
    expect(curve.breakevens).toEqual([93, 107]);
    expect(curve.maxProfit).toBe(200);
    expect(curve.maxLoss).toBe(-300);
    expect(curve.payoffAt(80)).toBeLessThan(0);
    expect(curve.payoffAt(120)).toBeLessThan(0);
    expect(curve.payoffAt(100)).toBe(200);
  });

  it('supports unequal leg quantities, including a ratio with a reversed tail', () => {
    // A 1x2 ratio: long 1 call K100 at 5, short 2 calls K110 at 2. Below 100 the
    // position is a flat -100; the extra short call makes the upside unbounded.
    const curve = priced([
      leg({ entryPremium: 5 }),
      leg({ strike: 110, entryPremium: 2, direction: 'short', contracts: 2 }),
    ]);
    expect(curve.netEntryDebit).toBe(100);
    expect(curve.maxLoss).toBe('unlimited');
    expect(curve.maxProfit).toBe(900);
    expect(curve.breakevens).toEqual([101, 119]);
    expect(curve.payoffAt(105)).toBe(400);
  });
});

describe('validation: missing is never zero, a multiplier is never assumed', () => {
  it.each([
    ['direction', { direction: null }],
    ['option type', { optionType: null }],
    ['strike', { strike: null }],
    ['premium', { entryPremium: null }],
    ['contracts', { contracts: null }],
    ['multiplier', { multiplier: null }],
  ])('refuses a leg with no %s', (_label, over) => {
    expect(reasonOf([leg(over)])).toMatch(/leg 1/);
  });

  it('names the offending leg and refuses the whole structure', () => {
    const reason = reasonOf([
      leg({ strike: 95 }),
      leg({ strike: 105, entryPremium: 2, direction: 'short', multiplier: null }),
    ]);
    expect(reason).toMatch(/leg 2/);
    expect(reason).toMatch(/multiplier/);
  });

  it('still prices a good leg alone when its sibling is unreadable', () => {
    const broken = [leg({ strike: 95 }), leg({ strike: 105, multiplier: null })];
    expect(reasonOf(broken)).toMatch(/leg 2/);
    const single = priced(broken, 0);
    expect(single.legs).toHaveLength(1);
    expect(single.netEntryDebit).toBe(500);
  });

  it('refuses an unverified multiplier, but accepts a recorded 1', () => {
    expect(reasonOf([leg({ multiplierRecorded: false })])).toMatch(/unverified/);
    const curve = priced([leg({ multiplier: 1, multiplierRecorded: true })]);
    expect(curve.netEntryDebit).toBe(5);
    expect(curve.maxLoss).toBe(-5);
  });

  it('accepts a genuine zero premium -- a worthless close is a real price', () => {
    const curve = priced([leg({ entryPremium: 0 })]);
    expect(curve.netEntryDebit).toBe(0);
    expect(curve.maxLoss).toBe(0);
    expect(curve.breakevens).toEqual([100]);
    expect(curve.flatZeroIntervals).toEqual([[0, 100]]);
  });

  it('rejects a negative strike or a non-positive contract count', () => {
    expect(reasonOf([leg({ strike: -100 })])).toMatch(/strike/);
    expect(reasonOf([leg({ contracts: 0 })])).toMatch(/contract count/);
    expect(reasonOf([leg({ entryPremium: -1 })])).toMatch(/premium/);
  });

  it('refuses to price nothing at all', () => {
    expect(reasonOf([])).toMatch(/no legs/);
  });
});

describe('structures that cannot be combined', () => {
  it('refuses a combined payoff across different expiries, but prices a leg', () => {
    const legs = [
      leg({ expiry: '2026-09-18' }),
      leg({ strike: 110, entryPremium: 2, direction: 'short', expiry: '2026-10-16' }),
    ];
    expect(reasonOf(legs)).toMatch(/different expiries/);
    expect(priced(legs, 1).legs).toHaveLength(1);
  });

  it('refuses a combined payoff across different underlyings', () => {
    const legs = [
      leg({ underlyingSymbol: 'ACN' }),
      leg({ strike: 110, entryPremium: 2, direction: 'short', underlyingSymbol: 'MSFT' }),
    ];
    expect(reasonOf(legs)).toMatch(/different underlyings/);
  });
});

describe('moneyness', () => {
  it('classifies calls and puts against the strike, without inverting on direction', () => {
    expect(moneyness('call', 100, 102)).toBe('ITM');
    expect(moneyness('call', 100, 100)).toBe('ATM');
    expect(moneyness('call', 100, 98)).toBe('OTM');
    expect(moneyness('put', 100, 98)).toBe('ITM');
    expect(moneyness('put', 100, 100)).toBe('ATM');
    expect(moneyness('put', 100, 102)).toBe('OTM');
  });

  it('uses floating-point tolerance, not a near-the-money band', () => {
    const dust = 100 + moneynessTolerance(100, 100) / 2;
    expect(moneyness('call', 100, dust)).toBe('ATM');
    expect(moneyness('call', 100, 100.001)).toBe('ITM');
  });

  it('returns unknown rather than guessing', () => {
    expect(moneyness('call', null, 100)).toBe('unknown');
    expect(moneyness('call', 100, null)).toBe('unknown');
    expect(moneyness(null, 100, 100)).toBe('unknown');
    expect(moneyness('call', 0, 100)).toBe('unknown');
    expect(moneyness('call', 100, Number.NaN)).toBe('unknown');
  });

  it('reports spot vs strike as a percentage of the strike', () => {
    expect(spotVsStrikePercent(108, 101)).toBeCloseTo(6.9307, 3);
    expect(spotVsStrikePercent(95, 100)).toBe(-5);
    expect(spotVsStrikePercent(null, 100)).toBeNull();
  });
});

describe('bounds and limits', () => {
  it('never suggests a negative underlying price and keeps every breakeven inside', () => {
    const curve = priced([leg({ optionType: 'put' })]);
    expect(curve.suggestedDomain.min).toBeGreaterThanOrEqual(0);
    expect(curve.suggestedDomain.min).toBeLessThan(95);
    expect(curve.suggestedDomain.max).toBeGreaterThan(100);
    for (const breakeven of curve.breakevens) {
      expect(breakeven).toBeGreaterThanOrEqual(curve.suggestedDomain.min);
      expect(breakeven).toBeLessThanOrEqual(curve.suggestedDomain.max);
    }
  });

  it('a distant breakeven stays in the summary bounds', () => {
    const curve = priced([
      leg({ strike: 100, entryPremium: 5 }),
      leg({ strike: 200, entryPremium: 1, direction: 'short' }),
    ]);
    expect(curve.suggestedDomain.max).toBeGreaterThanOrEqual(
      Math.max(...curve.breakevens),
    );
  });

  it('unlimited is reserved for a mathematically unbounded tail', () => {
    // Bounded on both sides: a condor. Neither limit may read "unlimited".
    const condor = priced([
      leg({ optionType: 'put', strike: 90, entryPremium: 1 }),
      leg({ optionType: 'put', strike: 95, entryPremium: 2, direction: 'short' }),
      leg({ optionType: 'call', strike: 105, entryPremium: 2, direction: 'short' }),
      leg({ optionType: 'call', strike: 110, entryPremium: 1 }),
    ]);
    expect(condor.maxProfit).not.toBe('unlimited');
    expect(condor.maxLoss).not.toBe('unlimited');
  });
});
