import { describe, it, expect } from 'vitest';
import {
  contractValue, groupTradesByStructure, isOptionTrade, optionBadge,
  structureKey, summariseStructure,
} from './optionTrades';

/**
 * The trade list's option reading. Asked for from live use on 2026-09-04: the
 * backtest trade list showed no option detail at all, so a long call, a short
 * put and one leg of a condor were indistinguishable from each other and from a
 * cheap stock.
 */

const leg = (over: Partial<any> = {}) => ({
  id: 1, symbol: 'AAPL240315C00150000', entryDate: '2024-03-06T09:30:00',
  exitDate: '2024-03-08T16:00:00', entryPrice: 4.2, exitPrice: 6.1, size: 1,
  direction: 'long' as const, pnl: 190, pnlPercent: 1.9, duration: 2,
  exitReason: 'take_profit', optionType: 'call' as const, strike: 150,
  expiry: '2024-03-15', multiplier: 100, underlyingSymbol: 'AAPL',
  contractSymbol: 'AAPL240315C00150000', transactionId: 42, ...over,
});

const equity = (over: Partial<any> = {}) => ({
  id: 9, symbol: 'AAPL', entryDate: '2024-03-06T09:30:00',
  exitDate: '2024-03-08T16:00:00', entryPrice: 150, exitPrice: 155, size: 10,
  direction: 'long' as const, pnl: 50, pnlPercent: 0.5, duration: 2,
  exitReason: 'exit', ...over,
});

describe('isOptionTrade', () => {
  it('reads the option type and nothing else', () => {
    expect(isOptionTrade(leg())).toBe(true);
    expect(isOptionTrade(equity())).toBe(false);
  });

  it('does not call a row an option just because it has a contract symbol', () => {
    // The discriminator has to be ONE field, or the badge and the premium can
    // disagree about what the row is.
    expect(isOptionTrade({ contractSymbol: 'AAPL240315C00150000' })).toBe(false);
  });

  it('survives a null or missing row', () => {
    expect(isOptionTrade(null)).toBe(false);
    expect(isOptionTrade(undefined)).toBe(false);
  });
});

describe('optionBadge', () => {
  it('says call or put, the strike and the expiry', () => {
    expect(optionBadge(leg())).toBe('C 150 · 2024-03-15');
    expect(optionBadge(leg({ optionType: 'put', strike: 140 })))
      .toBe('P 140 · 2024-03-15');
  });

  it('is empty for an equity row', () => {
    expect(optionBadge(equity())).toBe('');
  });

  it('marks a missing strike rather than quietly dropping it', () => {
    // "C · 2024-03-15" would read as a complete description of a contract whose
    // most important number is absent.
    expect(optionBadge(leg({ strike: null }))).toBe('C ? · 2024-03-15');
  });

  it('omits the expiry separator when there is no expiry', () => {
    expect(optionBadge(leg({ expiry: null }))).toBe('C 150');
  });
});

describe('contractValue', () => {
  it('turns a per-share premium into the money that moved', () => {
    expect(contractValue(4.2, 2, 100)).toBeCloseTo(840);
  });

  it('leaves an equity row alone', () => {
    // multiplier 1 (or absent) is the no-op, and the reason the fallback is 1
    // rather than 100: a hundred-fold equity notional is the worse error.
    expect(contractValue(150, 10, 1)).toBeCloseTo(1500);
    expect(contractValue(150, 10, null)).toBeCloseTo(1500);
    expect(contractValue(150, 10, undefined)).toBeCloseTo(1500);
  });

  it('treats unusable numbers as zero rather than NaN', () => {
    expect(contractValue(NaN, 2, 100)).toBe(0);
    expect(contractValue(4.2, NaN, 100)).toBe(0);
  });
});

describe('structureKey', () => {
  it('is the transaction id as a string, whatever type it arrived as', () => {
    expect(structureKey({ transactionId: 42 })).toBe('42');
    expect(structureKey({ transactionId: '42' })).toBe('42');
  });

  it('is null when there is no structure', () => {
    expect(structureKey({ transactionId: null })).toBeNull();
    expect(structureKey({})).toBeNull();
    expect(structureKey(null)).toBeNull();
  });
});

describe('groupTradesByStructure', () => {
  it('folds the legs of one spread into a single structure', () => {
    const legs = [leg({ id: 1 }), leg({ id: 2, optionType: 'put', strike: 140 })];
    const groups = groupTradesByStructure(legs);

    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe('structure');
    expect(groups[0].kind === 'structure' && groups[0].legs).toHaveLength(2);
  });

  it('leaves a LONE option leg as an ordinary row', () => {
    // A covered call has a transactionId too. Wrapping it in a parent row puts a
    // chevron on every single-leg trade, hiding exactly one row behind a click.
    const groups = groupTradesByStructure([leg({ id: 1 })]);
    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe('single');
  });

  it('leaves equity rows alone', () => {
    const groups = groupTradesByStructure([equity({ id: 1 }), equity({ id: 2 })]);
    expect(groups.map(g => g.kind)).toEqual(['single', 'single']);
  });

  it('keeps the order the caller sorted the list into', () => {
    // The structure appears where its FIRST leg would have. Sorting the flat list
    // and grouping afterwards is what keeps "click P&L to sort" meaning the same
    // thing whether or not the run holds options.
    const rows = [
      equity({ id: 1 }),
      leg({ id: 2, transactionId: 7 }),
      equity({ id: 3 }),
      leg({ id: 4, transactionId: 7 }),
      equity({ id: 5 }),
    ];
    const groups = groupTradesByStructure(rows);

    expect(groups.map(g => g.key)).toEqual(['t1', 's7', 't3', 't5']);
  });

  it('separates two different structures on the same underlying', () => {
    const rows = [
      leg({ id: 1, transactionId: 7 }), leg({ id: 2, transactionId: 7 }),
      leg({ id: 3, transactionId: 8 }), leg({ id: 4, transactionId: 8 }),
    ];
    const groups = groupTradesByStructure(rows);

    expect(groups.map(g => g.key)).toEqual(['s7', 's8']);
  });

  it('treats a numeric and a string id for the same structure as one', () => {
    const groups = groupTradesByStructure([
      leg({ id: 1, transactionId: 7 }), leg({ id: 2, transactionId: '7' }),
    ]);
    expect(groups).toHaveLength(1);
  });
});

describe('summariseStructure', () => {
  const shortPut = leg({
    id: 1, optionType: 'put', strike: 140, direction: 'short',
    entryPrice: 1.1, exitPrice: 0.2, pnl: 90, pnlPercent: 0.9,
  });
  const longPut = leg({
    id: 2, optionType: 'put', strike: 135, direction: 'long',
    entryPrice: 0.6, exitPrice: 0.05, pnl: -55, pnlPercent: -0.55,
  });

  it('reports the NET debit or credit, not a sum of per-share premiums', () => {
    // Short 1.10 received, long 0.60 paid -> a 0.50 credit, i.e. -$50 of cost on
    // 100-multiplier contracts. Summing the premiums (1.70) would be a number
    // that describes nothing.
    const s = summariseStructure([shortPut, longPut]);
    expect(s.netCost).toBeCloseTo(-50);
    expect(s.netValue).toBeCloseTo(-15);   // -0.20 + 0.05, x100
  });

  it('adds the P&L and the P&L percent across the legs', () => {
    const s = summariseStructure([shortPut, longPut]);
    expect(s.pnl).toBeCloseTo(35);
    // Both legs measure against the SAME account equity at entry, so they add.
    // Averaging would report a two-leg bet as half the impact it had.
    expect(s.pnlPercent).toBeCloseTo(0.35);
  });

  it('spans the earliest entry to the latest exit', () => {
    const s = summariseStructure([
      leg({ id: 1, entryDate: '2024-03-06T09:30:00', exitDate: '2024-03-08T16:00:00' }),
      leg({ id: 2, entryDate: '2024-03-05T09:30:00', exitDate: '2024-03-11T16:00:00' }),
    ]);
    expect(s.entryDate).toBe('2024-03-05T09:30:00');
    expect(s.exitDate).toBe('2024-03-11T16:00:00');
  });

  it('names the underlying rather than an OCC string', () => {
    const s = summariseStructure([shortPut, longPut]);
    expect(s.symbol).toBe('AAPL');
  });

  it('counts the legs and their sides', () => {
    const s = summariseStructure([shortPut, longPut]);
    expect(s.legCount).toBe(2);
    expect(s.shortLegs).toBe(1);
    expect(s.longLegs).toBe(1);
    expect(s.contracts).toBe(2);
  });

  it('keeps one exit reason when the legs agree', () => {
    expect(summariseStructure([shortPut, longPut]).exitReason).toBe('take_profit');
  });

  it('says how many reasons there were rather than picking one', () => {
    const s = summariseStructure([shortPut, leg({ id: 2, exitReason: 'expired' })]);
    expect(s.exitReason).toBe('2 reasons');
  });

  it('reports a debit spread as a positive cost', () => {
    // The sign convention that makes netValue - netCost the P&L: paid is
    // positive, received is negative.
    const s = summariseStructure([
      leg({ id: 1, direction: 'long', entryPrice: 4.2, exitPrice: 5.0 }),
      leg({ id: 2, direction: 'short', entryPrice: 1.2, exitPrice: 1.0 }),
    ]);
    expect(s.netCost).toBeCloseTo(300);    // (4.20 - 1.20) x 100
    expect(s.netValue).toBeCloseTo(400);   // (5.00 - 1.00) x 100
  });
});
