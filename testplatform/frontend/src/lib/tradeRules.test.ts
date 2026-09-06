import { describe, expect, it } from 'vitest';
import {
  countStrategyGenes,
  legacyToTradeRules,
  resolveTradeRules,
  type TradeRule,
} from './tradeRules';
import type { ConditionGroup } from '../components/ConditionBuilder';

const buyTree: ConditionGroup = {
  id: 'root', operator: 'OR',
  conditions: [
    { id: 'g1', operator: 'AND', conditions: [
      { id: 'c1', field: 'bullish', fieldType: 'flag' } as never,
    ] } as never,
    { id: 'g2', operator: 'AND', conditions: [
      { id: 'c2', field: 'confidence', fieldType: 'numeric', comparison: '>=', value: 90,
        optimizeEnabled: true, valueMin: 60, valueMax: 95, valueStep: 5 } as never,
    ] } as never,
  ],
} as never;

describe('legacyToTradeRules', () => {
  it('one entry rule per OR branch, base gates made explicit, bracket replicated', () => {
    const { entry_rules } = legacyToTradeRules(buyTree, null, [], [
      { id: 'tp', action: 'adjust_take_profit', referenceValue: 'expert_target_price',
        actionValue: -2 },
    ]);
    expect(entry_rules).toHaveLength(2);
    for (const r of entry_rules) {
      expect(r.actions.map((a) => a.action_type)).toEqual(['buy', 'adjust_take_profit']);
      expect(r.continue_processing).toBe(false);
    }
    // branch 1 already had bullish -> only flat added; branch 2 gains both base gates
    const fields = (r: TradeRule) =>
      new Set((r.conditions?.conditions ?? []).map((c) => (c as { field?: string }).field));
    expect(fields(entry_rules[0])).toEqual(new Set(['bullish', 'has_no_position']));
    expect(fields(entry_rules[1])).toEqual(new Set(['bullish', 'has_no_position', 'confidence']));
    // camel action fields converted to the backend snake shape
    expect(entry_rules[0].actions[1].reference_value).toBe('expert_target_price');
    expect(entry_rules[0].actions[1].action_value).toBe(-2);
  });

  it('lifts single-action exit rows to one-action rules', () => {
    const { exit_rules } = legacyToTradeRules(null, null, [
      { id: 'x1', action: 'close', toggleOptimize: true,
        conditions: { id: 'ec', operator: 'AND', conditions: [] } as never },
    ], []);
    expect(exit_rules).toHaveLength(1);
    expect(exit_rules[0].actions[0].action_type).toBe('close');
    expect(exit_rules[0].toggle_optimize).toBe(true);
  });
});

describe('countStrategyGenes', () => {
  it('emits per-rule and per-action genes; open actions never droppable', () => {
    const rules: TradeRule[] = [{
      id: 'tier1', conditions: null, continue_processing: false, toggle_optimize: true,
      actions: [
        { action_type: 'buy', toggle_optimize: true }, // ignored: undroppable
        { action_type: 'adjust_take_profit', action_value: -5,
          action_value_optimize: true, action_value_min: -20, action_value_max: 10,
          action_value_step: 2, toggle_optimize: true },
      ],
    }];
    const { genes } = countStrategyGenes(rules, []);
    const names = genes.map((g) => g.name);
    expect(names).toContain('entry:tier1:enabled');
    expect(names).toContain('entry:tier1:a1:action_value');
    expect(names).toContain('entry:tier1:a1:enabled');
    expect(names).not.toContain('entry:tier1:a0:enabled');
    const tp = genes.find((g) => g.name === 'entry:tier1:a1:action_value');
    expect(tp?.choices).toBe(16); // (10 - -20)/2 + 1
  });
});

describe('resolveTradeRules', () => {
  it('marks dropped rules/actions and applies decoded values without mutating input', () => {
    const rules: TradeRule[] = [{
      id: 'r1', conditions: {
        id: 'grp', operator: 'AND',
        conditions: [{ id: 'c9', field: 'confidence', value: 50 } as never],
      } as never,
      continue_processing: true,
      actions: [
        { action_type: 'buy' },
        { action_type: 'adjust_stop_loss', action_value: -8 },
      ],
    }];
    const out = resolveTradeRules('entry', rules, {
      'entry:r1:a1:action_value': -12,
      'entry:r1:a1:enabled': 0,
      'cond:c9:value': 65,
    });
    expect(out[0].actions[1].action_value).toBe(-12);
    expect((out[0].actions[1] as { _dropped?: boolean })._dropped).toBe(true);
    expect((out[0].conditions?.conditions?.[0] as { value?: number }).value).toBe(65);
    expect(out[0].continue_processing).toBe(true);
    // input untouched
    expect(rules[0].actions[1].action_value).toBe(-8);
  });
});

describe('tradeRulesToLegacyEditor', () => {
  it('round-trips the legacy shapes (branches, bracket, exits)', () => {
    const { entry_rules, exit_rules } = legacyToTradeRules(buyTree, null, [
      { id: 'x1', action: 'close', toggleOptimize: true,
        conditions: { id: 'ec', operator: 'AND', conditions: [] } as never },
    ], [
      { id: 'tp', action: 'adjust_take_profit', referenceValue: 'expert_target_price',
        actionValue: -2, toggleOptimize: true },
    ]);
    const back = (async () => (await import('./tradeRules')).tradeRulesToLegacyEditor)();
    return back.then((fn) => {
      const legacy = fn(entry_rules, exit_rules);
      expect(legacy.buyTree?.conditions).toHaveLength(2); // 2 OR branches survive
      expect(legacy.entryActions).toHaveLength(1);
      expect(legacy.entryActions[0].action).toBe('adjust_take_profit');
      expect(legacy.entryActions[0].actionValue).toBe(-2);
      expect(legacy.exitRules).toHaveLength(1);
      expect(legacy.exitRules[0].action).toBe('close');
      // 2 branches shared one bracket -> flatten warning present
      expect(legacy.warnings.some((w) => w.includes('flattened'))).toBe(true);
    });
  });

  it('carries every condition of a single AND entry rule into buyTree', () => {
    // The shape a daily_expert optimization run actually stores (backtest 1001,
    // DeterministicScorer S2): ONE `enter-buy` rule whose conditions group ANDs five gates,
    // and no legacy buyEntryConditions key at all. The Strategy tab counts the header from
    // this tree, so if it came back empty the panel printed "Entry Conditions (5)" over a
    // body reading "always" -- a run whose entry gates were plainly doing work described as
    // unconditional.
    return import('./tradeRules').then(({ tradeRulesToLegacyEditor }) => {
      const entryRule = {
        id: 'enter-buy', name: 'enter-buy', continue_processing: false,
        actions: [{ action_type: 'buy' }],
        conditions: {
          id: 'root', operator: 'AND', type: 'AND',
          conditions: [
            { id: 'buy-bullish', field: 'bullish', field_type: 'flag', comparison: 'is_true' },
            { id: 'buy-flat', field: 'has_no_position', field_type: 'flag', comparison: 'is_true' },
            { id: 'gate_expected_profit', field: 'expected_profit', field_type: 'numeric', comparison: '>', value: 9 },
            { id: 'gate_days_since_close', field: 'days_since_last_close', field_type: 'numeric', comparison: '>', value: 0 },
            { id: 'gate_days_since_profit', field: 'days_since_last_profitable_close', field_type: 'numeric', comparison: '>', value: 25 },
          ],
        },
      } as never as TradeRule;

      const legacy = tradeRulesToLegacyEditor([entryRule], []);
      const conds = legacy.buyTree?.conditions ?? [];
      expect(conds).toHaveLength(5);
      expect(new Set(conds.map((c) => (c as { field?: string }).field))).toEqual(new Set([
        'bullish', 'has_no_position', 'expected_profit',
        'days_since_last_close', 'days_since_last_profitable_close',
      ]));
    });
  });

  it('keeps only the first action of a multi-action exit rule, with a warning', () => {
    return import('./tradeRules').then(({ tradeRulesToLegacyEditor }) => {
      const legacy = tradeRulesToLegacyEditor([], [{
        id: 'tier3', conditions: null, continue_processing: false,
        actions: [
          { action_type: 'adjust_stop_loss', action_value: 20 },
          { action_type: 'adjust_take_profit', action_value: 50 },
        ],
      }]);
      expect(legacy.exitRules[0].action).toBe('adjust_stop_loss');
      expect(legacy.warnings.some((w) => w.includes('first of 2 actions'))).toBe(true);
    });
  });
});
