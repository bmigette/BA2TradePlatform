// Unified rule model (migration 028): TradeRule = conditions + ordered actions +
// continue_processing — the EXACT shape the backend stores (Strategy.entry_rules /
// exit_rules), seeds 1:1 into live EventActions, and exports. See
// docs/plans/2026-07-08-unified-rule-model.md.
//
// This module is the frontend's single source of truth for the shape + the conversions
// between the legacy editor state (buy/sell trees + single-action exit rows + flat entry
// bracket) and TradeRule lists. Pure & dependency-free.

import type { ConditionGroup, ConditionTree } from '../components/ConditionBuilder';

// One action of a rule — same field vocabulary the exit-rule editor already uses
// (mirrors backend ActionCfg: action_type + reference/value + optimize ranges + option_*).
export interface RuleAction {
  id?: string;
  action_type: string;
  reference_value?: string;
  action_value?: number;
  action_value_optimize?: boolean;
  action_value_min?: number;
  action_value_max?: number;
  action_value_step?: number;
  toggle_optimize?: boolean;
  // option-action selection params (undefined for equity actions)
  option_strategy?: string;
  option_strike_method?: string;
  option_strike_param?: number;
  option_strike_param_optimize?: boolean;
  option_strike_param_min?: number;
  option_strike_param_max?: number;
  option_strike_param_step?: number;
  option_dte_min?: number;
  option_dte_max?: number;
  option_dte_optimize?: boolean;
  option_dte_min_range?: number;
  option_dte_max_range?: number;
  option_dte_step?: number;
  option_wing_width_pct?: number;
  option_wing_width_optimize?: boolean;
  option_wing_width_min?: number;
  option_wing_width_max?: number;
  option_wing_width_step?: number;
  option_sizing?: number;
  [k: string]: unknown; // preserve unknown backend keys verbatim on round-trip
}

export interface TradeRule {
  id: string;
  name?: string;
  conditions: ConditionGroup | null; // null = always matches (no explicit conditions)
  actions: RuleAction[];
  continue_processing: boolean; // false = first-match stops evaluation at this rule
  toggle_optimize?: boolean;    // GA may drop the whole rule (<ns>:<id>:enabled gene)
  [k: string]: unknown;
}

// ---------------------------------------------------------------------------
// Gene counting (mirrors backend strategy_param_space on the unified model):
//   cond:<id>:value / :enabled                    (condition leaves, unchanged)
//   <ns>:<rid>:enabled                            (rule toggle)
//   <ns>:<rid>:a<i>:action_value / :enabled       (per-action value/toggle)
//   <ns>:<rid>:a<i>:option_delta / :option_dte / :option_wing_width
// ---------------------------------------------------------------------------
export interface GeneInfo { name: string; choices: number; }

const span = (mn?: number, mx?: number, st?: number): number => {
  if (mn == null || mx == null || !st || st <= 0 || mx < mn) return 1;
  return Math.floor((mx - mn) / st) + 1;
};

// Open actions can never be toggled off (dropping `buy` would turn a rule into a no-op).
const UNDROPPABLE = new Set(['buy', 'sell']);

interface CondNode {
  id?: string; optimizeEnabled?: boolean; toggleOptimize?: boolean;
  optimize_enabled?: boolean; toggle_optimize?: boolean; optimize?: boolean;
  valueMin?: number; valueMax?: number; valueStep?: number;
  value_min?: number; value_max?: number; value_step?: number;
  conditions?: CondNode[];
}

function walkCond(n: CondNode | null | undefined, out: GeneInfo[]): void {
  if (!n) return;
  for (const c of (n.conditions ?? [])) walkCond(c, out);
  if (!n.id) return;
  if (n.optimizeEnabled ?? n.optimize_enabled ?? n.optimize) {
    out.push({
      name: `cond:${n.id}:value`,
      choices: span(n.valueMin ?? n.value_min, n.valueMax ?? n.value_max,
                    n.valueStep ?? n.value_step),
    });
  }
  if (n.toggleOptimize ?? n.toggle_optimize) out.push({ name: `cond:${n.id}:enabled`, choices: 2 });
}

export function countRuleGenes(ns: 'entry' | 'exit', rules: TradeRule[]): GeneInfo[] {
  const genes: GeneInfo[] = [];
  for (const r of (rules ?? [])) {
    if (r.toggle_optimize) genes.push({ name: `${ns}:${r.id}:enabled`, choices: 2 });
    (r.actions ?? []).forEach((a, i) => {
      const p = `${ns}:${r.id}:a${i}`;
      if (a.action_value_optimize) {
        genes.push({ name: `${p}:action_value`,
                     choices: span(a.action_value_min, a.action_value_max, a.action_value_step) });
      }
      if (a.toggle_optimize && !UNDROPPABLE.has(a.action_type)) {
        genes.push({ name: `${p}:enabled`, choices: 2 });
      }
      if (a.option_strike_param_optimize) {
        genes.push({ name: `${p}:option_delta`,
                     choices: span(a.option_strike_param_min, a.option_strike_param_max,
                                   a.option_strike_param_step) });
      }
      if (a.option_dte_optimize) {
        genes.push({ name: `${p}:option_dte`,
                     choices: span(a.option_dte_min_range, a.option_dte_max_range, a.option_dte_step) });
      }
      if (a.option_wing_width_optimize) {
        genes.push({ name: `${p}:option_wing_width`,
                     choices: span(a.option_wing_width_min, a.option_wing_width_max,
                                   a.option_wing_width_step) });
      }
    });
    walkCond(r.conditions as CondNode | null, genes);
  }
  return genes;
}

export function countStrategyGenes(entryRules: TradeRule[], exitRules: TradeRule[]) {
  const genes = [...countRuleGenes('entry', entryRules), ...countRuleGenes('exit', exitRules)];
  const searchSpace = genes.reduce((acc, g) => acc * Math.max(1, g.choices), 1);
  return { genes, searchSpace };
}

// ---------------------------------------------------------------------------
// Legacy editor state -> TradeRule lists (mirrors ba2_common trade_rules_from_legacy:
// one entry rule per top-level OR branch, EXPLICIT bullish+flat base gates where absent,
// the flat bracket replicated per rule; single-action exit rows lift to one-action rules).
// Used to SEND the new shape from the existing editors until they are fully replaced.
// ---------------------------------------------------------------------------
interface LegacyExitRule {
  id: string; name?: string; conditions?: ConditionGroup | null;
  action?: string; actionValue?: number; actionValueOptimize?: boolean;
  actionValueMin?: number; actionValueMax?: number; actionValueStep?: number;
  toggleOptimize?: boolean; referenceValue?: string;
  [k: string]: unknown;
}

const CAMEL_TO_SNAKE: Record<string, string> = {
  action: 'action_type',
  actionValue: 'action_value',
  actionValueOptimize: 'action_value_optimize',
  actionValueMin: 'action_value_min',
  actionValueMax: 'action_value_max',
  actionValueStep: 'action_value_step',
  referenceValue: 'reference_value',
  optionStrategy: 'option_strategy',
  optionStrikeMethod: 'option_strike_method',
  optionStrikeParam: 'option_strike_param',
  optionStrikeParamOptimize: 'option_strike_param_optimize',
  optionStrikeParamMin: 'option_strike_param_min',
  optionStrikeParamMax: 'option_strike_param_max',
  optionStrikeParamStep: 'option_strike_param_step',
  optionDteMin: 'option_dte_min',
  optionDteMax: 'option_dte_max',
  optionDteOptimize: 'option_dte_optimize',
  optionDteMinRange: 'option_dte_min_range',
  optionDteMaxRange: 'option_dte_max_range',
  optionDteStep: 'option_dte_step',
  optionWingWidthPct: 'option_wing_width_pct',
  optionWingWidthOptimize: 'option_wing_width_optimize',
  optionWingWidthMin: 'option_wing_width_min',
  optionWingWidthMax: 'option_wing_width_max',
  optionWingWidthStep: 'option_wing_width_step',
  optionSizing: 'option_sizing',
};

// One legacy rule's ACTION fields (camel or snake) -> a RuleAction.
function legacyActionOf(rule: Record<string, unknown>): RuleAction {
  const a: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(rule)) {
    if (v === undefined || k === 'id' || k === 'name' || k === 'conditions'
        || k === 'toggleOptimize' || k === 'toggle_optimize'
        || k === 'continue_processing' || k === 'continueProcessing') continue;
    a[CAMEL_TO_SNAKE[k] ?? k] = v;
  }
  return a as RuleAction;
}

export function legacyExitToTradeRule(rule: LegacyExitRule): TradeRule {
  return {
    id: rule.id,
    ...(rule.name ? { name: rule.name } : {}),
    conditions: (rule.conditions as ConditionGroup) ?? null,
    actions: [legacyActionOf(rule as Record<string, unknown>)],
    continue_processing: false,
    ...(rule.toggleOptimize != null ? { toggle_optimize: rule.toggleOptimize } : {}),
  };
}

function leafFields(node: ConditionTree | null | undefined, out: Set<string>): void {
  if (!node) return;
  const anyNode = node as { field?: string; conditions?: ConditionTree[] };
  if (anyNode.field) out.add(anyNode.field);
  for (const c of (anyNode.conditions ?? [])) leafFields(c, out);
}

function withBaseGates(branch: ConditionGroup, side: 'buy' | 'sell', rid: string): ConditionGroup {
  const present = new Set<string>();
  leafFields(branch, present);
  const signal = side === 'buy' ? 'bullish' : 'bearish';
  const base: object[] = [];
  if (!present.has(signal)) base.push({ id: `${rid}-${signal}`, field: signal, fieldType: 'flag' });
  if (!present.has('has_no_position')) {
    base.push({ id: `${rid}-flat`, field: 'has_no_position', fieldType: 'flag' });
  }
  if (!base.length) return branch;
  return { ...branch, conditions: [...(base as never[]), ...(branch.conditions ?? [])] };
}

export function legacyToTradeRules(
  buyTree: ConditionGroup | null | undefined,
  sellTree: ConditionGroup | null | undefined,
  exitRules: LegacyExitRule[] | null | undefined,
  entryActions: LegacyExitRule[] | null | undefined,
): { entry_rules: TradeRule[]; exit_rules: TradeRule[] } {
  const bracket = (entryActions ?? []).map(legacyActionOf);
  const entry: TradeRule[] = [];
  const sides: Array<[ConditionGroup | null | undefined, 'buy' | 'sell']> =
    [[buyTree, 'buy'], [sellTree, 'sell']];
  for (const [tree, side] of sides) {
    if (!tree) continue;
    const op = String((tree as { operator?: string; type?: string }).operator
                      ?? (tree as { type?: string }).type ?? 'AND').toUpperCase();
    const branches = op === 'OR'
      ? (tree.conditions ?? []).filter((c): c is ConditionGroup => !!c && typeof c === 'object')
      : [tree];
    branches.forEach((branch, j) => {
      const rid = branches.length > 1 ? `${side}-${j + 1}` : side;
      entry.push({
        id: rid,
        name: `enter-${side}${branches.length > 1 ? `-${j + 1}` : ''}`,
        conditions: withBaseGates(branch as ConditionGroup, side, rid),
        actions: [{ action_type: side }, ...bracket.map((b) => ({ ...b }))],
        continue_processing: false,
      });
    });
  }
  return {
    entry_rules: entry,
    exit_rules: (exitRules ?? []).map(legacyExitToTradeRule),
  };
}

// ---------------------------------------------------------------------------
// TradeRule lists -> the legacy EDITOR shapes (Load/quick-load pre-fill). The current
// builders edit a buy tree + single-action exit rows + one flat entry bracket, so this is
// the inverse of legacyToTradeRules — LOSSY where a rule uses capabilities the editors
// can't represent yet (multi-action exit rules keep only their FIRST action;
// continue_processing is dropped). `warnings` lists what was lost so the UI can say so.
// ---------------------------------------------------------------------------
const SNAKE_TO_CAMEL: Record<string, string> = Object.fromEntries(
  Object.entries(CAMEL_TO_SNAKE).map(([camel, snake]) => [snake, camel]),
);

function actionToLegacyFields(a: RuleAction): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(a)) {
    if (v === undefined || k === 'id' || k === 'toggle_optimize') continue;
    if (k === 'action') continue; // canonical dicts carry both spellings; keep one
    out[SNAKE_TO_CAMEL[k] ?? k] = v;
  }
  if (out.action_type) { out.action = out.action_type; delete out.action_type; }
  return out;
}

export function tradeRulesToLegacyEditor(
  entryRules: TradeRule[] | null | undefined,
  exitRules: TradeRule[] | null | undefined,
): {
  buyTree: ConditionGroup | null;
  sellTree: ConditionGroup | null;
  exitRules: Record<string, unknown>[];
  entryActions: Record<string, unknown>[];
  warnings: string[];
} {
  const warnings: string[] = [];
  const buyBranches: ConditionGroup[] = [];
  const sellBranches: ConditionGroup[] = [];
  let entryActions: Record<string, unknown>[] = [];

  (entryRules ?? []).forEach((r, idx) => {
    const open = (r.actions ?? []).find((a) => a.action_type === 'buy' || a.action_type === 'sell');
    const extras = (r.actions ?? []).filter((a) => a !== open);
    if (r.conditions) {
      (open?.action_type === 'sell' ? sellBranches : buyBranches).push(r.conditions);
    }
    if (extras.length) {
      const lifted = extras.map((a, i) => ({
        id: (a.id as string) ?? `${r.id}-a${i}`,
        ...actionToLegacyFields(a),
        ...(a.toggle_optimize != null ? { toggleOptimize: a.toggle_optimize } : {}),
      }));
      if (!entryActions.length) {
        entryActions = lifted;
        if (idx > 0 || (entryRules ?? []).length > 1) {
          warnings.push('per-rule entry brackets flattened to one shared list (editor limitation)');
        }
      }
    }
    if (r.continue_processing) {
      warnings.push(`entry rule '${r.id}': continue_processing not shown in this editor`);
    }
  });

  const legacyExits = (exitRules ?? []).map((r) => {
    const [first, ...rest] = r.actions ?? [];
    if (rest.length) {
      warnings.push(`exit rule '${r.id}': only the first of ${r.actions.length} actions shown (editor limitation)`);
    }
    if (r.continue_processing) {
      warnings.push(`exit rule '${r.id}': continue_processing not shown in this editor`);
    }
    return {
      id: r.id,
      ...(r.name ? { name: r.name } : {}),
      conditions: r.conditions ?? { id: `${r.id}-grp`, operator: 'AND', conditions: [] },
      ...(first ? actionToLegacyFields(first) : {}),
      ...(r.toggle_optimize != null ? { toggleOptimize: r.toggle_optimize } : {}),
    } as Record<string, unknown>;
  });

  const toTree = (branches: ConditionGroup[]): ConditionGroup | null =>
    branches.length === 0 ? null
      : branches.length === 1 ? branches[0]
        : ({ id: 'root', operator: 'OR', conditions: branches } as never);

  return {
    buyTree: toTree(buyBranches),
    sellTree: toTree(sellBranches),
    exitRules: legacyExits,
    entryActions,
    warnings,
  };
}

// ---------------------------------------------------------------------------
// Best-params resolution on rule lists (mirrors backend decode_params): apply the
// optimizer's flat gene map, marking dropped rules/actions with _dropped for display.
// ---------------------------------------------------------------------------
export type BestParams = Record<string, number | string>;

export function resolveTradeRules(
  ns: 'entry' | 'exit', rules: TradeRule[], params: BestParams,
): (TradeRule & { _dropped?: boolean })[] {
  const cloned: (TradeRule & { _dropped?: boolean })[] =
    JSON.parse(JSON.stringify(rules ?? []));
  for (const r of cloned) {
    if (params[`${ns}:${r.id}:enabled`] === 0) r._dropped = true;
    (r.actions ?? []).forEach((a: RuleAction & { _dropped?: boolean }, i) => {
      const p = `${ns}:${r.id}:a${i}`;
      if (params[`${p}:enabled`] === 0 && !UNDROPPABLE.has(a.action_type)) a._dropped = true;
      const v = params[`${p}:action_value`];
      if (typeof v === 'number') a.action_value = v;
      const delta = params[`${p}:option_delta`];
      if (typeof delta === 'number') a.option_strike_param = delta;
      const dte = params[`${p}:option_dte`];
      if (typeof dte === 'number') {
        a.option_dte_min = Math.round(dte);
        a.option_dte_max = Math.round(dte);
      }
      const wing = params[`${p}:option_wing_width`];
      if (typeof wing === 'number') a.option_wing_width_pct = wing;
    });
    // condition-leaf genes (unchanged namespace)
    const applyCond = (n: CondNode & { _dropped?: boolean; value?: number } | null | undefined): void => {
      if (!n) return;
      for (const c of (n.conditions ?? [])) applyCond(c as never);
      if (!n.id) return;
      if (params[`cond:${n.id}:enabled`] === 0) n._dropped = true;
      const v = params[`cond:${n.id}:value`];
      if (typeof v === 'number') n.value = v;
    };
    applyCond(r.conditions as never);
  }
  return cloned;
}
