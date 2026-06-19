import { describe, it, expect } from 'vitest';
import { validateExitRule, createEmptyGroup, generateId } from './ConditionBuilder';
import type { ExitConditionSet, ConditionGroup, ConditionNode } from './ConditionBuilder';
import type { Vocabulary } from '../lib/btApi';

const vocab: Vocabulary = {
  flags: [{ value: 'has_position', label: 'In Position' }],
  numerics: [{ value: 'pnl_pct', label: 'PnL %' }],
  operators: ['gt', 'lt', 'gte', 'lte', 'eq', 'neq', 'between'],
  actions: [
    { value: 'close', label: 'Close', is_option: false, needs_reference: false },
    { value: 'adjust_take_profit', label: 'Adjust TP', is_option: false, needs_reference: true },
    { value: 'adjust_stop_loss', label: 'Adjust SL', is_option: false, needs_reference: true },
  ],
  reference_values: { current_price: 'Current Price' },
};

function leaf(field: string, extra: Partial<ConditionNode> = {}): ConditionNode {
  return {
    id: generateId(),
    field,
    fieldType: 'numeric',
    comparison: 'gt',
    value: 0.5,
    optimizeEnabled: false,
    ...extra,
  };
}

function group(leaves: ConditionNode[]): ConditionGroup {
  return { id: generateId(), operator: 'AND', conditions: leaves };
}

function rule(overrides: Partial<ExitConditionSet> = {}): ExitConditionSet {
  return {
    id: generateId(),
    name: 'r',
    conditions: group([leaf('pnl_pct')]),
    action: 'close',
    ...overrides,
  };
}

describe('validateExitRule', () => {
  it('returns no warnings for a valid rule', () => {
    expect(validateExitRule(rule(), vocab)).toEqual([]);
  });

  it('flags an unknown action', () => {
    const w = validateExitRule(rule({ action: 'frobnicate' as ExitConditionSet['action'] }), vocab);
    expect(w).toContain('Unknown action: frobnicate');
  });

  it('flags an unknown field on a leaf (e.g. a stale loaded rule)', () => {
    const w = validateExitRule(rule({ conditions: group([leaf('stale_field')]) }), vocab);
    expect(w).toContain('Unknown field: stale_field');
  });

  it('warns when an empty-conditions rule fires every bar (non-unconditional action)', () => {
    const w = validateExitRule(
      rule({ action: 'adjust_take_profit', conditions: createEmptyGroup('AND'), actionValue: 1 }),
      vocab,
    );
    expect(w).toContain('No conditions — fires every bar');
  });

  it('does NOT warn always-fires for the unconditional close action', () => {
    const w = validateExitRule(rule({ action: 'close', conditions: createEmptyGroup('AND') }), vocab);
    expect(w).not.toContain('No conditions — fires every bar');
  });

  it('flags an invalid leaf optimize range (min >= max)', () => {
    const w = validateExitRule(
      rule({ conditions: group([leaf('pnl_pct', { optimizeEnabled: true, valueMin: 5, valueMax: 1, valueStep: 0.5 })]) }),
      vocab,
    );
    expect(w).toContain('Invalid optimize range (min/max/step)');
  });

  it('flags an invalid leaf optimize range (step <= 0)', () => {
    const w = validateExitRule(
      rule({ conditions: group([leaf('pnl_pct', { optimizeEnabled: true, valueMin: 0, valueMax: 1, valueStep: 0 })]) }),
      vocab,
    );
    expect(w).toContain('Invalid optimize range (min/max/step)');
  });

  it('flags an invalid action-value optimize range', () => {
    const w = validateExitRule(
      rule({
        action: 'adjust_stop_loss',
        actionValueOptimize: true,
        actionValueMin: 10,
        actionValueMax: 1,
        actionValueStep: 0.5,
      }),
      vocab,
    );
    expect(w).toContain('Invalid optimize range (min/max/step)');
  });

  it('warns when an adjust action has no value and no optimize sweep', () => {
    const w = validateExitRule(rule({ action: 'adjust_take_profit' }), vocab);
    expect(w).toContain('Adjust action has no value');
  });

  it('does not warn adjust-without-value when an actionValue is set', () => {
    const w = validateExitRule(rule({ action: 'adjust_take_profit', actionValue: 2 }), vocab);
    expect(w).not.toContain('Adjust action has no value');
  });

  it('does not report unknown field/action when vocabulary is unavailable (offline)', () => {
    const w = validateExitRule(rule({ action: 'anything' as ExitConditionSet['action'], conditions: group([leaf('whatever')]) }), undefined);
    expect(w).not.toContain('Unknown action: anything');
    expect(w.some((x) => x.startsWith('Unknown field:'))).toBe(false);
  });
});
