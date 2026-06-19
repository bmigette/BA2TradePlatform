// Pure resolver: apply an optimizer's flat best-params gene map back onto the
// editor's rule shapes so a finished optimization can be read back as the
// ruleset that ACTUALLY ran. Mirrors the backend decode_params resolution in
// backend/app/services/strategy_param_space.py (decode_params, ~L215-283) and
// the gene namespaces emitted by geneCount.ts / strategy_param_space.py:
//
//   cond:<id>:value     -> leaf threshold value
//   cond:<id>:enabled   -> 0 marks the leaf _dropped (kept for display, greyed)
//   exit:<id>:enabled   -> 0 marks the whole rule _dropped (kept, greyed/struck)
//   exit:<id>:action_value -> rule.actionValue
//   exit:<id>:option_delta -> rule.optionStrikeParam
//   exit:<id>:option_dte   -> rule.optionDteMin = optionDteMax (int)
//
// Pure & dependency-free: never mutates its inputs (deep-clones), returns new
// trees/rules. Unit-tested in resolveRuleset.test.ts.

import type { ConditionGroup, ConditionTree, ExitConditionSet } from '../components/ConditionBuilder';

// The flat gene->value map an optimizer produces (best_individual.params /
// best_params). Values are the DECODED concrete values (e.g. {"exit:r1:enabled":0,
// "cond:c1:value":7, "exit:r1:action_value":-8}).
export type BestParams = Record<string, number | string>;

// _dropped marks an element the optimizer turned off. We keep the element for
// display (greyed/struck) rather than removing it, so the user sees what was
// dropped. The marker is a non-enumerable concern of this module only.
export interface DroppedFlag {
  _dropped?: boolean;
}

export type ResolvedLeaf = ConditionTree & DroppedFlag;
export type ResolvedRule = ExitConditionSet & DroppedFlag;

export interface ResolvedRuleset {
  exitRules: ResolvedRule[];
  buyTree: ConditionGroup | undefined;
  sellTree: ConditionGroup | undefined;
}

// Partition a flat params map into the per-id buckets decode_params uses.
interface Buckets {
  condValue: Record<string, number | string>;
  condEnabled: Record<string, number | string>;
  exitEnabled: Record<string, number | string>;
  exitActionValue: Record<string, number | string>;
  exitOptionDelta: Record<string, number | string>;
  exitOptionDte: Record<string, number | string>;
}

function partition(bestParams: BestParams): Buckets {
  const b: Buckets = {
    condValue: {}, condEnabled: {}, exitEnabled: {},
    exitActionValue: {}, exitOptionDelta: {}, exitOptionDte: {},
  };
  for (const [key, val] of Object.entries(bestParams ?? {})) {
    const parts = key.split(':');
    if (parts.length !== 3) continue; // ignore tp/sl/model:* and malformed keys
    const [ns, id, field] = parts;
    if (ns === 'cond') {
      if (field === 'value') b.condValue[id] = val;
      else if (field === 'enabled') b.condEnabled[id] = val;
    } else if (ns === 'exit') {
      if (field === 'enabled') b.exitEnabled[id] = val;
      else if (field === 'action_value') b.exitActionValue[id] = val;
      else if (field === 'option_delta') b.exitOptionDelta[id] = val;
      else if (field === 'option_dte') b.exitOptionDte[id] = val;
    }
  }
  return b;
}

// Recursively clone a condition tree, applying cond:* genes to leaves.
function applyToTree(node: ConditionTree | undefined, b: Buckets): ConditionTree | undefined {
  if (!node) return undefined;
  // Group: clone children. We treat a node as a group when it has a conditions[].
  const asGroup = node as ConditionGroup;
  if (Array.isArray(asGroup.conditions)) {
    return {
      ...asGroup,
      conditions: asGroup.conditions.map(c => applyToTree(c, b) as ConditionTree),
    } as ConditionGroup;
  }
  // Leaf
  const leaf: ResolvedLeaf = { ...node };
  const id = (node as { id: string }).id;
  if (id in b.condValue) {
    (leaf as { value: number | string }).value = b.condValue[id];
  }
  if (id in b.condEnabled && Number(b.condEnabled[id]) === 0) {
    leaf._dropped = true;
  }
  return leaf as ConditionTree;
}

/**
 * Apply an optimizer's flat best-params gene map onto the editor rule shapes,
 * producing a resolved (read-back) ruleset. Pure: inputs are never mutated.
 *
 * @param exitRules the strategy's exit rules (camelCase ExitConditionSet[])
 * @param buyTree   the buy entry condition group (optional)
 * @param sellTree  the sell entry condition group (optional)
 * @param bestParams flat gene->value map from the finished optimization
 */
export function applyBestParams(
  exitRules: ExitConditionSet[] | undefined,
  buyTree: ConditionGroup | undefined,
  sellTree: ConditionGroup | undefined,
  bestParams: BestParams,
): ResolvedRuleset {
  const b = partition(bestParams);

  const resolvedExit: ResolvedRule[] = (exitRules ?? []).map(rule => {
    const r: ResolvedRule = { ...rule };
    const id = rule.id;
    // exit:<id>:enabled == 0 -> the optimizer dropped the whole rule.
    if (id in b.exitEnabled && Number(b.exitEnabled[id]) === 0) {
      r._dropped = true;
    }
    if (id in b.exitActionValue) {
      r.actionValue = Number(b.exitActionValue[id]);
    }
    if (id in b.exitOptionDelta) {
      r.optionStrikeParam = Number(b.exitOptionDelta[id]);
    }
    if (id in b.exitOptionDte) {
      const dte = Math.trunc(Number(b.exitOptionDte[id]));
      r.optionDteMin = dte;
      r.optionDteMax = dte;
    }
    if (rule.conditions) {
      r.conditions = applyToTree(rule.conditions, b) as ConditionGroup;
    }
    return r;
  });

  return {
    exitRules: resolvedExit,
    buyTree: applyToTree(buyTree, b) as ConditionGroup | undefined,
    sellTree: applyToTree(sellTree, b) as ConditionGroup | undefined,
  };
}
