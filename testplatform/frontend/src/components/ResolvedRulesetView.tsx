// Read-only "resolved ruleset" read-back for a FINISHED optimization. Given the
// strategy's exit rules + the optimizer's flat best-params gene map, render what
// ACTUALLY ran: rules dropped by the optimizer (exit:<id>:enabled == 0) are shown
// greyed + struck with a "dropped by optimizer" tag; tuned values
// (cond:<id>:value, exit:<id>:action_value, option_delta, option_dte) are filled
// in with the resolved number. See lib/resolveRuleset.ts for the pure resolver.

import type { ConditionGroup, ConditionTree, ExitConditionSet } from './ConditionBuilder';
import { applyBestParams } from '../lib/resolveRuleset';
import type { BestParams, ResolvedRule } from '../lib/resolveRuleset';

const OP_LABELS: Record<string, string> = {
  gt: '>', gte: '>=', lt: '<', lte: '<=', eq: '==', neq: '!=', between: 'between',
};

const ACTION_LABELS: Record<string, string> = {
  close: 'Close', sell: 'Sell', buy: 'Buy',
  adjust_take_profit: 'Adjust Take Profit', adjust_stop_loss: 'Adjust Stop Loss',
  buy_call: 'Buy Call', buy_put: 'Buy Put', sell_covered_call: 'Sell Covered Call',
  sell_cash_secured_put: 'Sell Cash-Secured Put', buy_protective_put: 'Buy Protective Put',
  open_bull_call_spread: 'Bull Call Spread', open_bear_put_spread: 'Bear Put Spread',
  open_bear_call_spread: 'Bear Call Spread', open_straddle: 'Straddle',
  open_strangle: 'Strangle', close_option: 'Close Option',
};

type ResolvedLeaf = ConditionTree & { _dropped?: boolean };

function isGroup(node: ConditionTree): node is ConditionGroup {
  return Array.isArray((node as ConditionGroup).conditions);
}

function fmtVal(v: unknown): string {
  if (Array.isArray(v)) return v.join('..');
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4).replace(/\.?0+$/, '');
  return String(v ?? '');
}

// Render a single condition leaf as "field op value", greyed/struck if dropped.
function LeafChip({ node }: { node: ResolvedLeaf }) {
  const leaf = node as ResolvedLeaf & {
    field?: string; comparison?: string; value?: unknown; fieldType?: string;
  };
  const dropped = !!leaf._dropped;
  const op = OP_LABELS[leaf.comparison ?? ''] ?? leaf.comparison ?? '';
  const isFlag = leaf.fieldType === 'flag';
  return (
    <span
      className={[
        'inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-mono',
        dropped
          ? 'bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500 line-through'
          : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200',
      ].join(' ')}
    >
      <span>{leaf.field || '(empty)'}</span>
      {!isFlag && (
        <>
          <span className="opacity-70">{op}</span>
          <span className="font-semibold">{fmtVal(leaf.value)}</span>
        </>
      )}
      {dropped && <span className="not-italic font-sans text-[10px] uppercase opacity-80">dropped</span>}
    </span>
  );
}

// Render a condition tree (groups joined by their operator) flat as a row of chips.
function ConditionRow({ tree }: { tree: ConditionTree | undefined }) {
  if (!tree) return <span className="text-xs text-gray-400 italic">no conditions</span>;
  if (isGroup(tree)) {
    const children = tree.conditions ?? [];
    if (children.length === 0) return <span className="text-xs text-gray-400 italic">no conditions</span>;
    return (
      <span className="inline-flex flex-wrap items-center gap-1">
        {children.map((c, i) => (
          <span key={(c as { id?: string }).id ?? i} className="inline-flex items-center gap-1">
            {i > 0 && <span className="text-[10px] uppercase text-gray-400 px-0.5">{tree.operator}</span>}
            <ConditionRow tree={c} />
          </span>
        ))}
      </span>
    );
  }
  return <LeafChip node={tree as ResolvedLeaf} />;
}

function RuleCard({ rule }: { rule: ResolvedRule }) {
  const dropped = !!rule._dropped;
  const isAdjust = rule.action === 'adjust_take_profit' || rule.action === 'adjust_stop_loss';
  const isOption = rule.optionStrikeParam != null || rule.optionDteMin != null || rule.action.startsWith('open_') || rule.action.includes('call') || rule.action.includes('put') || rule.action.includes('straddle') || rule.action.includes('strangle');
  return (
    <div
      className={[
        'rounded-lg border p-3 space-y-2',
        dropped
          ? 'border-gray-200 dark:border-gray-700 bg-gray-50/60 dark:bg-gray-800/40 opacity-70'
          : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800',
      ].join(' ')}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={[
            'text-sm font-semibold',
            dropped ? 'text-gray-400 dark:text-gray-500 line-through' : 'text-gray-900 dark:text-gray-100',
          ].join(' ')}
        >
          {rule.name || rule.id}
        </span>
        {dropped && (
          <span className="text-[10px] uppercase tracking-wide rounded px-1.5 py-0.5 bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400">
            dropped by optimizer
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1">
        <span className="text-[11px] uppercase text-gray-400 mr-1">if</span>
        <ConditionRow tree={rule.conditions} />
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-[11px] uppercase text-gray-400">then</span>
        <span
          className={[
            'rounded px-2 py-0.5 font-medium',
            dropped
              ? 'bg-gray-100 dark:bg-gray-800 text-gray-400'
              : 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-800 dark:text-yellow-300',
          ].join(' ')}
        >
          {ACTION_LABELS[rule.action] ?? rule.action}
        </span>
        {isAdjust && rule.actionValue != null && (
          <span className="rounded px-2 py-0.5 font-mono bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300">
            value = {fmtVal(rule.actionValue)}
          </span>
        )}
        {isOption && rule.optionStrikeParam != null && (
          <span className="rounded px-2 py-0.5 font-mono bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300">
            delta = {fmtVal(rule.optionStrikeParam)}
          </span>
        )}
        {isOption && rule.optionDteMin != null && (
          <span className="rounded px-2 py-0.5 font-mono bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300">
            dte = {rule.optionDteMin === rule.optionDteMax ? fmtVal(rule.optionDteMin) : `${fmtVal(rule.optionDteMin)}..${fmtVal(rule.optionDteMax)}`}
          </span>
        )}
        {rule.referenceValue && (
          <span className="rounded px-2 py-0.5 font-mono bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
            ref = {rule.referenceValue}
          </span>
        )}
      </div>
    </div>
  );
}

export interface ResolvedRulesetViewProps {
  exitRules: ExitConditionSet[] | undefined;
  bestParams: BestParams;
  // Entry trees are optional — included for completeness; entry leaf genes are
  // resolved the same way but typically shown elsewhere.
  buyTree?: ConditionGroup;
  sellTree?: ConditionGroup;
}

export default function ResolvedRulesetView({ exitRules, bestParams, buyTree, sellTree }: ResolvedRulesetViewProps) {
  const resolved = applyBestParams(exitRules, buyTree, sellTree, bestParams);
  const rules = resolved.exitRules;
  if (rules.length === 0) {
    return (
      <div className="text-sm text-gray-500 dark:text-gray-400">
        No exit rules to resolve.
      </div>
    );
  }
  const kept = rules.filter(r => !r._dropped).length;
  const droppedCount = rules.length - kept;
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-200">
          Resolved Exit Ruleset
        </h4>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {kept} active{droppedCount > 0 ? ` · ${droppedCount} dropped` : ''}
        </span>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        What the optimizer actually ran for the best result — tuned values filled in, dropped rules greyed.
      </p>
      <div className="space-y-2">
        {rules.map((r, i) => (
          <RuleCard key={r.id || i} rule={r} />
        ))}
      </div>
    </div>
  );
}
