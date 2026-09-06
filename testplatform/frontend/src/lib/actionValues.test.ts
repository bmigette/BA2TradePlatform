import { describe, expect, it } from 'vitest';
import { actionRefOf, actionValueOf } from './actionValues';

/**
 * Found from live use on 2026-09-04: the Strategy tab's Take Profit / Stop Loss cards
 * disagreed with the Entry Actions listed directly beneath them (cards said -5%, rules said
 * -10%). Both were reading the same object — one preferred the camel key, the other the snake
 * key, and on an optimized run those hold different numbers.
 */

// Verbatim from backtest 1331's stored strategyParams.
const optimizedTakeProfit = {
  id: 's1_tp_target',
  action: 'adjust_take_profit',
  action_type: 'adjust_take_profit',
  referenceValue: 'expert_target_price',
  reference_value: 'expert_target_price',
  actionValue: -5.0,     // template default, never re-written by the GA
  action_value: -10.0,   // what actually ran
  value: -10.0,
};

describe('actionValueOf', () => {
  it('prefers the GA-tuned snake value over the stale camel default', () => {
    expect(actionValueOf(optimizedTakeProfit)).toBe(-10);
  });

  it('falls back to camel for a legacy row that has no snake copy', () => {
    expect(actionValueOf({ action: 'adjust_stop_loss', actionValue: -8 })).toBe(-8);
  });

  it('reads `value` when only the generic key is present', () => {
    expect(actionValueOf({ value: -12 })).toBe(-12);
  });

  it('returns undefined rather than a number for a valueless action', () => {
    // `close` carries no value; a 0 here would render as a real "0%" bracket.
    expect(actionValueOf({ action: 'close' })).toBeUndefined();
    expect(actionValueOf({ action_value: null })).toBeUndefined();
    expect(actionValueOf(null)).toBeUndefined();
  });

  it('ignores a non-numeric value instead of stringifying it', () => {
    expect(actionValueOf({ action_value: 'n/a' })).toBeUndefined();
  });

  it('keeps a legitimate zero', () => {
    // 0 is a real offset ("stop loss AT entry"), so it must survive the ?? chain.
    expect(actionValueOf({ action_value: 0 })).toBe(0);
  });
});

describe('actionRefOf', () => {
  it('names the reference the offset is measured from', () => {
    expect(actionRefOf(optimizedTakeProfit)).toBe('analyst target');
    expect(actionRefOf({ reference_value: 'order_open_price' })).toBe('entry');
  });

  it('accepts the camel spelling too', () => {
    expect(actionRefOf({ referenceValue: 'current_price' })).toBe('price');
  });

  it('passes an unknown reference through rather than hiding it', () => {
    expect(actionRefOf({ reference_value: 'some_new_ref' })).toBe('some_new_ref');
  });

  it('is empty when there is no reference', () => {
    expect(actionRefOf({ action: 'close' })).toBe('');
    expect(actionRefOf(null)).toBe('');
  });
});
