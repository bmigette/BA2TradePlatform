import { describe, it, expect } from 'vitest';
import { applyBestParams } from './resolveRuleset';
import type { ConditionGroup, ExitConditionSet } from '../components/ConditionBuilder';

// Minimal leaf/group/rule builders so tests read clearly.
const leaf = (id: string, value: number) => ({
  id, field: 'position:position_pnl', fieldType: 'position', comparison: 'gt',
  value, optimizeEnabled: true,
});
const group = (id: string, conditions: any[]): ConditionGroup => ({
  id, operator: 'AND', conditions,
});
const rule = (id: string, conds: ConditionGroup, extra: Partial<ExitConditionSet> = {}): ExitConditionSet => ({
  id, name: id, conditions: conds, action: 'close', ...extra,
});

describe('applyBestParams', () => {
  it('drops a rule whose exit:<id>:enabled gene is 0 (kept for display)', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5)]));
    const out = applyBestParams([r], undefined, undefined, { 'exit:r1:enabled': 0 });
    expect(out.exitRules).toHaveLength(1);
    expect(out.exitRules[0]._dropped).toBe(true);
  });

  it('keeps a rule whose exit:<id>:enabled gene is 1', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5)]));
    const out = applyBestParams([r], undefined, undefined, { 'exit:r1:enabled': 1 });
    expect(out.exitRules[0]._dropped).toBeUndefined();
  });

  it('applies exit:<id>:action_value to the rule actionValue', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5)]), { action: 'adjust_take_profit', actionValue: 0 });
    const out = applyBestParams([r], undefined, undefined, { 'exit:r1:action_value': -8 });
    expect(out.exitRules[0].actionValue).toBe(-8);
  });

  it('applies option_delta -> optionStrikeParam and option_dte -> min/max (int)', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5)]), { action: 'buy_call' });
    const out = applyBestParams([r], undefined, undefined, {
      'exit:r1:option_delta': 0.3, 'exit:r1:option_dte': 45.0,
    });
    expect(out.exitRules[0].optionStrikeParam).toBe(0.3);
    expect(out.exitRules[0].optionDteMin).toBe(45);
    expect(out.exitRules[0].optionDteMax).toBe(45);
  });

  it('applies cond:<id>:value to the matching leaf threshold', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5), leaf('c2', 9)]));
    const out = applyBestParams([r], undefined, undefined, { 'cond:c1:value': 7 });
    const leaves = (out.exitRules[0].conditions as ConditionGroup).conditions as any[];
    expect(leaves.find(l => l.id === 'c1').value).toBe(7);
    expect(leaves.find(l => l.id === 'c2').value).toBe(9); // untouched
  });

  it('marks a leaf _dropped when cond:<id>:enabled is 0', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5)]));
    const out = applyBestParams([r], undefined, undefined, { 'cond:c1:enabled': 0 });
    const l0 = (out.exitRules[0].conditions as ConditionGroup).conditions[0] as any;
    expect(l0._dropped).toBe(true);
  });

  it('resolves genes in buy/sell entry trees too', () => {
    const buy = group('bg', [leaf('b1', 1)]);
    const sell = group('sg', [leaf('s1', 2)]);
    const out = applyBestParams([], buy, sell, { 'cond:b1:value': 11, 'cond:s1:enabled': 0 });
    const b0 = (out.buyTree as ConditionGroup).conditions[0] as any;
    const s0 = (out.sellTree as ConditionGroup).conditions[0] as any;
    expect(b0.value).toBe(11);
    expect(s0._dropped).toBe(true);
  });

  it('is pure — does not mutate the input rule', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5)]), { actionValue: 0 });
    applyBestParams([r], undefined, undefined, { 'exit:r1:action_value': -8, 'cond:c1:value': 7 });
    expect(r.actionValue).toBe(0);
    expect((r.conditions.conditions[0] as any).value).toBe(5);
  });

  it('ignores tp/sl/model:* and malformed keys', () => {
    const r = rule('r1', group('g1', [leaf('c1', 5)]));
    const out = applyBestParams([r], undefined, undefined, {
      tp: 3, sl: 2, 'model:lookback': 20, bogus: 1,
    } as any);
    expect(out.exitRules[0]._dropped).toBeUndefined();
    expect((out.exitRules[0].conditions as ConditionGroup).conditions[0]).toMatchObject({ value: 5 });
  });

  it('handles empty / undefined inputs', () => {
    const out = applyBestParams(undefined, undefined, undefined, {});
    expect(out.exitRules).toEqual([]);
    expect(out.buyTree).toBeUndefined();
    expect(out.sellTree).toBeUndefined();
  });
});
