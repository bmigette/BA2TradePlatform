import React from 'react';
import { Plus, Trash2, ChevronDown, ChevronRight, GitBranch, Settings2, ArrowUp, ArrowDown } from 'lucide-react';
import { getRulesetVocabulary } from '../lib/btApi';
import type { Vocabulary } from '../lib/btApi';

// Types for condition tree structure
export interface ConditionNode {
  id: string;
  field: string;
  fieldType: string; // model_probability, model_class, position, time, price
  comparison: string; // gt, lt, eq, gte, lte, neq, between
  value: number | string | [number, number];
  optimizeEnabled: boolean;
  toggleOptimize?: boolean; // optimizer may enable/disable this condition (cond:<id>:enabled gene)
  valueMin?: number;
  valueMax?: number;
  valueStep?: number;
  // Confirmation: condition must be true X times in Y bars
  confirmationRequired?: number;  // X times
  confirmationBars?: number;      // in last Y bars
  confirmationBarsMin?: number;
  confirmationBarsMax?: number;
  confirmationBarsStep?: number;
}

export interface ConditionGroup {
  id: string;
  operator: 'AND' | 'OR';
  conditions: (ConditionNode | ConditionGroup)[];
}

export type ConditionTree = ConditionNode | ConditionGroup;

export interface AvailableField {
  field: string;
  fieldType: string;
  description: string;
  category?: string;
  label?: string;       // Display label (e.g., "Probability (Direction 3bar >1%)")
  isBoolean?: boolean;  // If true, show is true/is false operators only
}

// Helper to check if a tree node is a group
export function isConditionGroup(node: ConditionTree): node is ConditionGroup {
  return 'operator' in node && 'conditions' in node;
}

// Pure flag-detection for a leaf: a field is a FLAG (no operator/value) when it
// is marked boolean. Vocabulary flags set isBoolean=true; legacy boolean entry
// fields (position:in_position, etc.) also set it. Exported for unit testing.
export function isFlagField(field: AvailableField | undefined): boolean {
  return field?.isBoolean ?? false;
}

// A field originates from the exit vocabulary's flags list (fieldType 'flag').
// These render with NO operator at all (a bare {field} test). Exported for tests.
export function isVocabularyFlagField(field: AvailableField | undefined): boolean {
  return field?.fieldType === 'flag';
}

// Generate unique IDs
let idCounter = 0;
export function generateId(): string {
  idCounter += 1;
  return `cond_${Date.now()}_${idCounter}`;
}

// Create empty condition
export function createEmptyCondition(): ConditionNode {
  return {
    id: generateId(),
    field: '',
    fieldType: 'model_probability',
    comparison: '>',
    value: 0.5,
    optimizeEnabled: false,
  };
}

// Create empty group
export function createEmptyGroup(operator: 'AND' | 'OR' = 'AND'): ConditionGroup {
  return {
    id: generateId(),
    operator,
    conditions: [createEmptyCondition()],
  };
}

// Default available fields (when model not selected)
const defaultFields: AvailableField[] = [
  { field: 'position:in_position', fieldType: 'position', description: 'Currently in a position', category: 'Position', label: 'In Position', isBoolean: true },
  { field: 'position:is_buy', fieldType: 'position', description: 'Position is a long/buy trade', category: 'Position', label: 'Is Buy Position', isBoolean: true },
  { field: 'position:is_sell', fieldType: 'position', description: 'Position is a short/sell trade', category: 'Position', label: 'Is Sell Position', isBoolean: true },
  { field: 'position:buy_count', fieldType: 'position', description: 'Number of open buy positions', category: 'Position', label: 'Buy Position Count' },
  { field: 'position:sell_count', fieldType: 'position', description: 'Number of open sell positions', category: 'Position', label: 'Sell Position Count' },
  { field: 'position:total_count', fieldType: 'position', description: 'Total number of open positions', category: 'Position', label: 'Total Position Count' },
  { field: 'position:position_pnl', fieldType: 'position', description: 'Current position P&L %', category: 'Position', label: 'Position P&L %' },
  { field: 'position:bars_in_position', fieldType: 'position', description: 'Bars since entry', category: 'Position', label: 'Bars in Position' },
  // Trade timing - bars/days since last trade was opened
  { field: 'trade:bars_since_last_buy', fieldType: 'trade', description: 'Bars since last buy trade was opened', category: 'Trade Timing', label: 'Bars Since Last Buy' },
  { field: 'trade:bars_since_last_sell', fieldType: 'trade', description: 'Bars since last sell trade was opened', category: 'Trade Timing', label: 'Bars Since Last Sell' },
  { field: 'trade:days_since_last_buy', fieldType: 'trade', description: 'Days since last buy trade was opened', category: 'Trade Timing', label: 'Days Since Last Buy' },
  { field: 'trade:days_since_last_sell', fieldType: 'trade', description: 'Days since last sell trade was opened', category: 'Trade Timing', label: 'Days Since Last Sell' },
  { field: 'time:hour', fieldType: 'time', description: 'Hour of day (0-23)', category: 'Time', label: 'Hour of Day' },
  { field: 'time:day_of_week', fieldType: 'time', description: 'Day of week (0=Mon, 6=Sun)', category: 'Time', label: 'Day of Week' },
  { field: 'price:change_pct', fieldType: 'price', description: 'Price change % from previous bar', category: 'Price', label: 'Price Change %' },
];

// Comparison operators for numeric fields. VALUES are the engine symbols ('>=' etc.) — the
// single canonical comparison vocabulary shared with storage, the export, and the shared engine
// (TradeConditions). Legacy word-forms ('gte') are normalised to symbols on import.
const comparisonOperators = [
  { value: '>', label: '>' },
  { value: '>=', label: '>=' },
  { value: '<', label: '<' },
  { value: '<=', label: '<=' },
  { value: '==', label: '==' },
  { value: '!=', label: '!=' },
  { value: 'between', label: 'between' },
];

// Operators for boolean fields
const booleanOperators = [
  { value: 'is_true', label: 'is true' },
  { value: 'is_false', label: 'is false' },
];

// Sentinel comparison kept on flag leaves so that downstream validation (which
// requires a non-empty `comparison`) passes. The backend treats a flag leaf as a
// bare {field} test; the comparison/value are ignored for flags.
const FLAG_SENTINEL_COMPARISON = 'is_true';

// Convert the backend exit-ruleset vocabulary into AvailableField entries grouped
// under "Flags" and "Numerics" optgroups. Flags reuse the existing boolean-field
// rendering (no operator/value). This is ADDITIVE to the entry default/prediction
// fields so entry conditions (model_probability/model_class etc.) keep working.
function vocabularyToFields(vocab: Vocabulary | undefined): AvailableField[] {
  if (!vocab) return [];
  const flagFields: AvailableField[] = (vocab.flags || []).map((f) => ({
    field: f.value,
    fieldType: 'flag',
    description: f.label,
    category: 'Flags',
    label: f.label,
    isBoolean: true,
  }));
  const numericFields: AvailableField[] = (vocab.numerics || []).map((n) => ({
    field: n.value,
    fieldType: 'numeric',
    description: n.label,
    category: 'Numerics',
    label: n.label,
    isBoolean: false,
  }));
  return [...flagFields, ...numericFields];
}

// Operators sourced from the vocabulary (exit usage). Falls back to the static
// comparison operators when no vocabulary is provided (entry usage).
function operatorsFromVocab(vocab: Vocabulary | undefined): { value: string; label: string }[] {
  if (!vocab || !vocab.operators || vocab.operators.length === 0) return comparisonOperators;
  const labelFor = (op: string) =>
    comparisonOperators.find((c) => c.value === op)?.label ?? op;
  return vocab.operators.map((op) => ({ value: op, label: labelFor(op) }));
}

interface ConditionBuilderProps {
  value: ConditionTree;
  onChange: (value: ConditionTree) => void;
  availableFields?: AvailableField[];
  isRoot?: boolean;
  level?: number;
  onRemove?: () => void;
  showOptimization?: boolean;
  /**
   * Exit-ruleset vocabulary. When provided, leaves are vocabulary-driven: the
   * field select gains "Flags"/"Numerics" optgroups and operators come from the
   * vocabulary. Flag leaves render with no operator/value. When omitted the
   * builder behaves exactly as before (entry-condition usage).
   */
  vocabulary?: Vocabulary;
  /**
   * When true AND no `vocabulary` prop was threaded, the ROOT instance lazily
   * fetches the vocabulary once and passes it to children (self-contained exit
   * usage). Defaults to false so ENTRY builders never auto-load exit flags.
   */
  vocabularyFallback?: boolean;
}

const ConditionBuilder: React.FC<ConditionBuilderProps> = ({
  value,
  onChange,
  availableFields = [],
  isRoot = true,
  level = 0,
  onRemove,
  showOptimization = true,
  vocabulary,
  vocabularyFallback = false,
}) => {
  // Fallback fetch: only the root instance fetches, only when explicitly opted
  // in via vocabularyFallback and no vocabulary prop was threaded. The result is
  // passed down to recursive children. Entry builders leave this off so they
  // never auto-load exit flags/numerics.
  const [fetchedVocab, setFetchedVocab] = React.useState<Vocabulary | undefined>(undefined);
  React.useEffect(() => {
    if (!isRoot || vocabulary || !vocabularyFallback) return;
    let cancelled = false;
    getRulesetVocabulary()
      .then((v) => { if (!cancelled) setFetchedVocab(v); })
      .catch(() => { /* offline: silently fall back to static fields */ });
    return () => { cancelled = true; };
  }, [isRoot, vocabulary, vocabularyFallback]);

  const effectiveVocab = vocabulary ?? fetchedVocab;
  const vocabFields = vocabularyToFields(effectiveVocab);
  // Merge entry defaults + prediction fields (entry usage) with the vocabulary
  // flags/numerics (exit usage), de-duplicating by field key so a field never
  // appears twice if it exists in both lists.
  const seen = new Set<string>();
  const allFields = [...defaultFields, ...availableFields, ...vocabFields].filter((f) => {
    if (seen.has(f.field)) return false;
    seen.add(f.field);
    return true;
  });
  // Operator list: vocabulary operators when an exit vocabulary is in play,
  // otherwise the static comparison operators (entry usage).
  const numericOperators = operatorsFromVocab(effectiveVocab);

  // Group fields by category
  const groupedFields = allFields.reduce((acc, field) => {
    const category = field.category || 'Model';
    if (!acc[category]) acc[category] = [];
    acc[category].push(field);
    return acc;
  }, {} as Record<string, AvailableField[]>);

  const [isExpanded, setIsExpanded] = React.useState(true);

  // Handle condition group
  if (isConditionGroup(value)) {
    const updateCondition = (index: number, newValue: ConditionTree) => {
      const newConditions = [...value.conditions];
      newConditions[index] = newValue;
      onChange({ ...value, conditions: newConditions });
    };

    const removeCondition = (index: number) => {
      const newConditions = value.conditions.filter((_, i) => i !== index);
      if (newConditions.length === 0) {
        // If group becomes empty and we can remove ourselves, do it
        if (onRemove) {
          onRemove();
        } else {
          // Otherwise add an empty condition
          onChange({ ...value, conditions: [createEmptyCondition()] });
        }
      } else {
        onChange({ ...value, conditions: newConditions });
      }
    };

    const addCondition = () => {
      onChange({
        ...value,
        conditions: [...value.conditions, createEmptyCondition()],
      });
    };

    const addGroup = (operator: 'AND' | 'OR') => {
      onChange({
        ...value,
        conditions: [...value.conditions, createEmptyGroup(operator)],
      });
    };

    const toggleOperator = () => {
      onChange({
        ...value,
        operator: value.operator === 'AND' ? 'OR' : 'AND',
      });
    };

    const indentClass = level > 0 ? 'ml-4 pl-4 border-l-2 border-gray-300 dark:border-gray-600' : '';

    return (
      <div className={`${indentClass} ${level > 0 ? 'mt-2' : ''}`}>
        {/* Group Header */}
        <div className="flex items-center gap-2 mb-2">
          <button
            type="button"
            onClick={() => setIsExpanded(!isExpanded)}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
          >
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 text-gray-500" />
            ) : (
              <ChevronRight className="w-4 h-4 text-gray-500" />
            )}
          </button>
          <button
            type="button"
            onClick={toggleOperator}
            className={`px-2 py-1 text-xs font-bold rounded ${
              value.operator === 'AND'
                ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
                : 'bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300'
            }`}
          >
            {value.operator}
          </button>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            Group ({value.conditions.length} condition{value.conditions.length !== 1 ? 's' : ''})
          </span>
          {!isRoot && onRemove && (
            <button
              type="button"
              onClick={onRemove}
              className="p-1 hover:bg-red-100 dark:hover:bg-red-900/30 rounded text-red-500"
              title="Remove group"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>

        {/* Group Content */}
        {isExpanded && (
          <div className="space-y-2">
            {value.conditions.map((condition, index) => (
              <ConditionBuilder
                key={isConditionGroup(condition) ? condition.id : condition.id}
                value={condition}
                onChange={(newValue) => updateCondition(index, newValue)}
                availableFields={availableFields}
                isRoot={false}
                level={level + 1}
                onRemove={() => removeCondition(index)}
                showOptimization={showOptimization}
                vocabulary={effectiveVocab}
              />
            ))}

            {/* Add Buttons */}
            <div className="flex items-center gap-2 mt-2 ml-4">
              <button
                type="button"
                onClick={addCondition}
                className="flex items-center gap-1 px-2 py-1 text-xs text-gray-600 dark:text-gray-400 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700"
              >
                <Plus className="w-3 h-3" />
                Condition
              </button>
              <button
                type="button"
                onClick={() => addGroup('AND')}
                className="flex items-center gap-1 px-2 py-1 text-xs text-blue-600 dark:text-blue-400 border border-blue-300 dark:border-blue-600 rounded hover:bg-blue-50 dark:hover:bg-blue-900/30"
              >
                <GitBranch className="w-3 h-3" />
                AND Group
              </button>
              <button
                type="button"
                onClick={() => addGroup('OR')}
                className="flex items-center gap-1 px-2 py-1 text-xs text-purple-600 dark:text-purple-400 border border-purple-300 dark:border-purple-600 rounded hover:bg-purple-50 dark:hover:bg-purple-900/30"
              >
                <GitBranch className="w-3 h-3" />
                OR Group
              </button>
            </div>
          </div>
        )}
      </div>
    );
  }

  // Handle single condition
  const condition = value as ConditionNode;
  const selectedField = allFields.find((f) => f.field === condition.field);
  // A leaf is a FLAG when its selected field is a boolean/flag field. Vocabulary
  // flags are mapped to isBoolean=true (see vocabularyToFields); legacy boolean
  // entry fields (position:in_position, etc.) also set isBoolean. A flag leaf
  // renders with NO operator and NO value input.
  const isFlag = isFlagField(selectedField);
  // For a true vocabulary flag we hide the operator entirely; legacy boolean
  // entry fields keep their is_true/is_false operator select for back-compat.
  const isVocabFlag = isVocabularyFlagField(selectedField);
  const operators = isFlag ? booleanOperators : numericOperators;

  const updateField = (field: string, fieldType: string) => {
    const newField = allFields.find((f) => f.field === field);
    const becomingFlag = isFlagField(newField);
    const becomingVocabFlag = isVocabularyFlagField(newField);
    const newCondition = { ...condition, field, fieldType };
    if (becomingFlag) {
      // Becoming a flag: clear numeric-only fields. Keep a sentinel comparison so
      // validation passes; a vocab flag leaf serializes effectively to {id, field}.
      if (!['is_true', 'is_false'].includes(condition.comparison)) {
        newCondition.comparison = becomingVocabFlag ? FLAG_SENTINEL_COMPARISON : 'is_true';
      }
      newCondition.value = 1;
      // A flag has no numeric value to optimize: drop value-range optimization.
      newCondition.optimizeEnabled = false;
      delete newCondition.valueMin;
      delete newCondition.valueMax;
      delete newCondition.valueStep;
    } else if (['is_true', 'is_false'].includes(condition.comparison)) {
      // Becoming numeric from a flag: restore a numeric comparison + value.
      newCondition.comparison = numericOperators[0]?.value ?? '>';
      newCondition.value = 0.5;
    }
    onChange(newCondition);
  };

  const updateComparison = (comparison: string) => {
    const newCondition = { ...condition, comparison };
    // Reset value for between operator
    if (comparison === 'between' && !Array.isArray(condition.value)) {
      newCondition.value = [0, 1];
    } else if (comparison !== 'between' && Array.isArray(condition.value)) {
      newCondition.value = condition.value[0];
    }
    // Set value for boolean operators
    if (comparison === 'is_true') {
      newCondition.value = 1;
    } else if (comparison === 'is_false') {
      newCondition.value = 0;
    }
    onChange(newCondition);
  };

  const updateValue = (newValue: number | string | [number, number]) => {
    onChange({ ...condition, value: newValue });
  };

  const toggleOptimize = () => {
    onChange({ ...condition, optimizeEnabled: !condition.optimizeEnabled });
  };

  const updateOptRange = (key: 'valueMin' | 'valueMax' | 'valueStep', val: number) => {
    onChange({ ...condition, [key]: val });
  };

  return (
    <div className="flex flex-wrap items-start gap-2 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
      {/* Field Selection */}
      <div className="flex-shrink-0">
        <select
          value={condition.field}
          onChange={(e) => {
            const field = allFields.find((f) => f.field === e.target.value);
            updateField(e.target.value, field?.fieldType || 'model_probability');
          }}
          className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 min-w-[180px]"
        >
          <option value="">Select field...</option>
          {Object.entries(groupedFields).map(([category, fields]) => (
            <optgroup key={category} label={category}>
              {fields.map((field) => (
                <option key={field.field} value={field.field}>
                  {field.label || field.field}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>

      {/* Comparison Operator - hidden for vocabulary flag leaves (a flag is a
          bare {field} test with no operator). Legacy boolean entry fields keep
          their is_true/is_false select. */}
      {!isVocabFlag && (
        <div className="flex-shrink-0">
          <select
            value={condition.comparison}
            onChange={(e) => updateComparison(e.target.value)}
            className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
          >
            {operators.map((op) => (
              <option key={op.value} value={op.value}>
                {op.label}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Value Input - hidden for flag leaves (no numeric value) */}
      {!isFlag && condition.comparison === 'between' ? (
        <div className="flex items-center gap-1">
          <input
            type="number"
            step="0.01"
            value={Array.isArray(condition.value) ? condition.value[0] : 0}
            onChange={(e) =>
              updateValue([
                parseFloat(e.target.value),
                Array.isArray(condition.value) ? condition.value[1] : 1,
              ])
            }
            className="w-20 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
          />
          <span className="text-sm text-gray-600 dark:text-gray-400">and</span>
          <input
            type="number"
            step="0.01"
            value={Array.isArray(condition.value) ? condition.value[1] : 1}
            onChange={(e) =>
              updateValue([
                Array.isArray(condition.value) ? condition.value[0] : 0,
                parseFloat(e.target.value),
              ])
            }
            className="w-20 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
          />
        </div>
      ) : !isFlag ? (
        <input
          type="number"
          step="0.01"
          value={typeof condition.value === 'number' ? condition.value : 0}
          onChange={(e) => updateValue(parseFloat(e.target.value))}
          className="w-24 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
        />
      ) : null}

      {/* Optimization Toggle. The value-range optimize button (Settings2) only
          makes sense for numeric leaves; a flag has no numeric value to sweep.
          The per-node on/off toggle (cond:<id>:enabled) is kept for BOTH flags
          and numerics so the optimizer can drop either kind of condition. */}
      {showOptimization && (
        <div className="flex items-center gap-1">
          {!isFlag && (
            <button
              type="button"
              onClick={toggleOptimize}
              className={`p-1.5 rounded ${
                condition.optimizeEnabled
                  ? 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300'
                  : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
              }`}
              title={condition.optimizeEnabled ? 'Optimization enabled' : 'Enable optimization'}
            >
              <Settings2 className="w-4 h-4" />
            </button>
          )}
          <label
            className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400"
            title="Let the optimizer enable/disable this condition"
          >
            <input
              type="checkbox"
              checked={condition.toggleOptimize ?? false}
              onChange={(e) => onChange({ ...condition, toggleOptimize: e.target.checked })}
              className="rounded"
            />
            on/off opt
          </label>
        </div>
      )}

      {/* Remove Button */}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          className="p-1.5 hover:bg-red-100 dark:hover:bg-red-900/30 rounded text-red-500"
          title="Remove condition"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      )}

      {/* Optimization Range (if enabled) - never for flags (no numeric value) */}
      {showOptimization && !isFlag && condition.optimizeEnabled && (
        <div className="w-full flex items-center gap-2 mt-2 pt-2 border-t border-gray-200 dark:border-gray-600">
          <span className="text-xs text-gray-500 dark:text-gray-400">Optimize:</span>
          <div className="flex items-center gap-1">
            <label className="text-xs text-gray-600 dark:text-gray-400">Min:</label>
            <input
              type="number"
              step="0.01"
              value={condition.valueMin ?? 0}
              onChange={(e) => updateOptRange('valueMin', parseFloat(e.target.value))}
              className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
          </div>
          <div className="flex items-center gap-1">
            <label className="text-xs text-gray-600 dark:text-gray-400">Max:</label>
            <input
              type="number"
              step="0.01"
              value={condition.valueMax ?? 1}
              onChange={(e) => updateOptRange('valueMax', parseFloat(e.target.value))}
              className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
          </div>
          <div className="flex items-center gap-1">
            <label className="text-xs text-gray-600 dark:text-gray-400">Step:</label>
            <input
              type="number"
              step="0.01"
              value={condition.valueStep ?? 0.1}
              onChange={(e) => updateOptRange('valueStep', parseFloat(e.target.value))}
              className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
          </div>
        </div>
      )}

      {/* Confirmation Section */}
      <div className="w-full flex items-center gap-2 mt-2 pt-2 border-t border-gray-200 dark:border-gray-600">
        <span className="text-xs text-gray-500 dark:text-gray-400">Confirm:</span>
        <div className="flex items-center gap-1">
          <label className="text-xs text-gray-600 dark:text-gray-400">True</label>
          <input
            type="number"
            min="1"
            value={condition.confirmationRequired ?? 1}
            onChange={(e) => onChange({...condition, confirmationRequired: parseInt(e.target.value) || 1})}
            className="w-12 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
          />
        </div>
        <span className="text-xs text-gray-600 dark:text-gray-400">times in last</span>
        <div className="flex items-center gap-1">
          <input
            type="number"
            min="1"
            value={condition.confirmationBars ?? 1}
            onChange={(e) => onChange({...condition, confirmationBars: parseInt(e.target.value) || 1})}
            className="w-12 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
          />
          <label className="text-xs text-gray-600 dark:text-gray-400">bars</label>
        </div>
      </div>

      {/* Field Description */}
      {selectedField && (
        <div className="w-full text-xs text-gray-500 dark:text-gray-400 mt-1">
          {selectedField.description}
        </div>
      )}
    </div>
  );
};

export default ConditionBuilder;

// Also export the Exit Condition Builder for exit conditions with actions
export interface ExitConditionSet {
  id: string;
  name: string;
  conditions: ConditionGroup;
  // Action values mirror the backend ExpertActionType enum (ba2_common). The
  // exit-rule action picker is vocabulary-driven, so this union is the canonical
  // set the API serializes/round-trips (NOT the legacy adjust_tp/adjust_sl form).
  action: 'close' | 'sell' | 'buy' | 'adjust_take_profit' | 'adjust_stop_loss'
        | 'buy_call' | 'buy_put' | 'sell_covered_call' | 'sell_cash_secured_put'
        | 'buy_protective_put' | 'open_bull_call_spread' | 'open_bear_put_spread'
        | 'open_bear_call_spread' | 'open_straddle' | 'open_strangle' | 'close_option';
  actionValue?: number;
  actionValueOptimize?: boolean;
  actionValueMin?: number;
  actionValueMax?: number;
  actionValueStep?: number;
  // When true the optimizer may drop the whole rule (exit:<id>:enabled gene).
  // Maps to the backend ExitCondition.toggle_optimize field (B5 serializes it).
  toggleOptimize?: boolean;
  // reference_value for adjust_take_profit/adjust_stop_loss (needs_reference
  // actions): order_open_price | current_price | expert_target_price.
  referenceValue?: string;
  // option-action fields (undefined for equity actions)
  optionStrategy?: string;
  optionStrikeMethod?: 'delta' | 'percent_otm' | 'consensus_target';
  optionStrikeParam?: number;
  optionDteMin?: number;
  optionDteMax?: number;
  optionSizing?: number;
  optionStrikeParamOptimize?: boolean;
  optionStrikeParamMin?: number;
  optionStrikeParamMax?: number;
  optionStrikeParamStep?: number;
  optionDteOptimize?: boolean;
  optionDteMinRange?: number;
  optionDteMaxRange?: number;
  optionDteStep?: number;
}

// Actions that legitimately fire every bar with no conditions. An empty
// condition group on any OTHER action is almost always a mistake (the rule
// fires on every bar), so we warn. `close` is the canonical unconditional
// action (e.g. a stand-alone time/trailing exit), but we keep this list small
// and conservative — it's only used to SUPPRESS the always-fires warning.
const UNCONDITIONAL_ACTIONS = new Set<string>(['close']);

// Collect every leaf (ConditionNode) in a condition tree, descending groups.
function collectLeaves(node: ConditionTree): ConditionNode[] {
  if (isConditionGroup(node)) {
    return node.conditions.flatMap(collectLeaves);
  }
  return [node];
}

// Pure, dependency-free validation of a single exit rule against the current
// vocabulary. Returns a list of human-readable WARNING strings (never throws,
// never blocks). Loaded/imported rules may carry fields/actions that are no
// longer in the vocabulary, hence the unknown-field / unknown-action checks.
// Exported for unit testing — see ConditionBuilder.validation.test.ts.
export function validateExitRule(
  rule: ExitConditionSet,
  vocab: Vocabulary | undefined,
): string[] {
  const warnings: string[] = [];

  const actionValues = new Set((vocab?.actions ?? []).map((a) => a.value));
  const fieldValues = new Set([
    ...(vocab?.flags ?? []).map((f) => f.value),
    ...(vocab?.numerics ?? []).map((n) => n.value),
  ]);

  // Unknown ACTION (vocabulary known but action absent). Skip when the
  // vocabulary has no actions at all (offline / not yet loaded) to avoid a
  // false positive on every rule. Loaded canonical/live rules may carry the
  // backend `action_type` key instead of `action`, so fall back to it.
  const ruleAction = rule.action ?? (rule as { action_type?: string }).action_type;
  if (actionValues.size > 0 && !actionValues.has(ruleAction as string)) {
    warnings.push(`Unknown action: ${ruleAction}`);
  }

  // Unknown FIELD on any leaf. Only check when the vocabulary actually carries
  // fields (otherwise every leaf would look unknown while offline).
  if (fieldValues.size > 0) {
    const seenUnknown = new Set<string>();
    for (const leaf of collectLeaves(rule.conditions)) {
      const f = leaf.field;
      if (f && !fieldValues.has(f) && !seenUnknown.has(f)) {
        seenUnknown.add(f);
        warnings.push(`Unknown field: ${f}`);
      }
    }
  }

  // Always-fires: no condition leaves AND the action is not deliberately
  // unconditional. A leaf with an empty field counts as "no real condition".
  const hasRealLeaf = collectLeaves(rule.conditions).some((l) => !!l.field);
  if (!hasRealLeaf && !UNCONDITIONAL_ACTIONS.has(rule.action)) {
    warnings.push('No conditions — fires every bar');
  }

  // Invalid optimize ranges. A range is bad when min >= max or step <= 0.
  const badRange = (min?: number, max?: number, step?: number): boolean => {
    const lo = min ?? 0;
    const hi = max ?? 0;
    const st = step ?? 0;
    return lo >= hi || st <= 0;
  };
  let rangeWarned = false;
  const flagBadRange = () => {
    if (!rangeWarned) {
      warnings.push('Invalid optimize range (min/max/step)');
      rangeWarned = true;
    }
  };

  // Per-leaf numeric value optimization ranges.
  for (const leaf of collectLeaves(rule.conditions)) {
    if (leaf.optimizeEnabled && badRange(leaf.valueMin, leaf.valueMax, leaf.valueStep)) {
      flagBadRange();
    }
  }
  // Action value sweep (adjust_take_profit / adjust_stop_loss).
  if (rule.actionValueOptimize && badRange(rule.actionValueMin, rule.actionValueMax, rule.actionValueStep)) {
    flagBadRange();
  }
  // Option strike-param sweep.
  if (rule.optionStrikeParamOptimize && badRange(rule.optionStrikeParamMin, rule.optionStrikeParamMax, rule.optionStrikeParamStep)) {
    flagBadRange();
  }
  // Option DTE sweep.
  if (rule.optionDteOptimize && badRange(rule.optionDteMinRange, rule.optionDteMaxRange, rule.optionDteStep)) {
    flagBadRange();
  }

  // Adjust-without-value: an adjust_take_profit / adjust_stop_loss rule that
  // carries neither a fixed actionValue nor an optimize sweep has nothing to
  // adjust by.
  const isAdjust = rule.action === 'adjust_take_profit' || rule.action === 'adjust_stop_loss';
  if (isAdjust && rule.actionValue == null && !rule.actionValueOptimize) {
    warnings.push('Adjust action has no value');
  }

  return warnings;
}

// Warnings that should be surfaced prominently as a red (reject) chip rather
// than amber. These are the "reject" class from the spec: an unknown
// field/action means the rule references vocabulary that no longer exists.
function isRejectWarning(w: string): boolean {
  return w.startsWith('Unknown field:') || w.startsWith('Unknown action:');
}

interface ExitConditionsBuilderProps {
  value: ExitConditionSet[];
  onChange: (value: ExitConditionSet[]) => void;
  availableFields?: AvailableField[];
  showOptimization?: boolean;
  /** Exit-ruleset vocabulary threaded to each rule's ConditionBuilder. */
  vocabulary?: Vocabulary;
}

export const ExitConditionsBuilder: React.FC<ExitConditionsBuilderProps> = ({
  value,
  onChange,
  availableFields = [],
  showOptimization = true,
  vocabulary,
}) => {
  // Fallback fetch: if the caller did not thread a vocabulary prop, load it once
  // so the action picker (actions/reference_values) is still vocabulary-driven.
  // Offline failures silently degrade to an empty actions list.
  const [fetchedVocab, setFetchedVocab] = React.useState<Vocabulary | undefined>(undefined);
  React.useEffect(() => {
    if (vocabulary) return;
    let cancelled = false;
    getRulesetVocabulary()
      .then((v) => { if (!cancelled) setFetchedVocab(v); })
      .catch(() => { /* offline: no actions list */ });
    return () => { cancelled = true; };
  }, [vocabulary]);
  const effectiveVocab = vocabulary ?? fetchedVocab;
  const actions = effectiveVocab?.actions ?? [];
  const positionActions = actions.filter((a) => !a.is_option);
  const optionActions = actions.filter((a) => a.is_option);
  const referenceValues = effectiveVocab?.reference_values ?? {};

  const addExitCondition = () => {
    const newExit: ExitConditionSet = {
      id: generateId(),
      name: `Exit Rule ${value.length + 1}`,
      conditions: createEmptyGroup('AND'),
      action: 'close',
    };
    onChange([...value, newExit]);
  };

  const updateExitCondition = (index: number, updates: Partial<ExitConditionSet>) => {
    const newValue = [...value];
    newValue[index] = { ...newValue[index], ...updates };
    onChange(newValue);
  };

  const removeExitCondition = (index: number) => {
    onChange(value.filter((_, i) => i !== index));
  };

  // Reorder a rule by swapping it with its neighbour. Rules are evaluated
  // top->down (first match wins) so order is semantically meaningful. Swapping
  // whole array entries preserves each rule's full contents + stable id.
  const moveExitCondition = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= value.length) return;
    const newValue = [...value];
    [newValue[index], newValue[target]] = [newValue[target], newValue[index]];
    onChange(newValue);
  };

  return (
    <div className="space-y-4">
      {value.map((exitCond, index) => (
        <div
          key={exitCond.id}
          className="border border-gray-200 dark:border-gray-700 rounded-lg p-3"
        >
          <div className="flex items-center justify-between gap-2 mb-3">
            <div className="flex items-center gap-2 min-w-0">
              {/* Evaluation order indicator (top->down, first match wins). */}
              <span className="text-xs font-medium text-gray-400 dark:text-gray-500 w-5 text-right flex-shrink-0">
                {index + 1}.
              </span>
              <input
                type="text"
                value={exitCond.name}
                onChange={(e) => updateExitCondition(index, { name: e.target.value })}
                className="px-2 py-1 text-sm font-medium border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                placeholder="Exit rule name"
              />
              {/* Per-rule toggle_optimize: optimizer may drop the whole rule
                  (exit:<id>:enabled gene). */}
              {showOptimization && (
                <label
                  className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 flex-shrink-0"
                  title="Let the optimizer enable/disable this entire rule"
                >
                  <input
                    type="checkbox"
                    checked={exitCond.toggleOptimize ?? false}
                    onChange={(e) =>
                      updateExitCondition(index, { toggleOptimize: e.target.checked })
                    }
                    className="rounded"
                  />
                  Optimize on/off
                </label>
              )}
            </div>
            <div className="flex items-center gap-1 flex-shrink-0">
              {/* Reorder: rules evaluate top->down, first match wins. */}
              <button
                type="button"
                onClick={() => moveExitCondition(index, -1)}
                disabled={index === 0}
                className="p-1 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                title="Move rule up (evaluated earlier)"
              >
                <ArrowUp className="w-4 h-4" />
              </button>
              <button
                type="button"
                onClick={() => moveExitCondition(index, 1)}
                disabled={index === value.length - 1}
                className="p-1 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                title="Move rule down (evaluated later)"
              >
                <ArrowDown className="w-4 h-4" />
              </button>
              <button
                type="button"
                onClick={() => removeExitCondition(index)}
                className="p-1 hover:bg-red-100 dark:hover:bg-red-900/30 rounded text-red-500"
                title="Remove exit rule"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* Validation warnings (non-blocking). Unknown field/action surface as
              red "reject" chips; everything else as amber warning chips. The
              user can still run the backtest — these never hard-block. */}
          {(() => {
            const warnings = validateExitRule(exitCond, effectiveVocab);
            if (warnings.length === 0) return null;
            return (
              <div className="flex flex-wrap gap-1 mb-3">
                {warnings.map((w) => (
                  <span
                    key={w}
                    className={
                      isRejectWarning(w)
                        ? 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400 rounded px-1.5 py-0.5 text-xs'
                        : 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 rounded px-1.5 py-0.5 text-xs'
                    }
                  >
                    {w}
                  </span>
                ))}
              </div>
            );
          })()}

          {/* Conditions */}
          <div className="mb-3">
            <label className="text-xs text-gray-500 dark:text-gray-400 mb-1 block">
              When these conditions are met:
            </label>
            <ConditionBuilder
              value={exitCond.conditions}
              onChange={(conds) =>
                updateExitCondition(index, { conditions: conds as ConditionGroup })
              }
              availableFields={availableFields}
              showOptimization={showOptimization}
              vocabulary={effectiveVocab}
            />
          </div>

          {/* Action */}
          <div className="flex flex-wrap items-center gap-3 pt-3 border-t border-gray-200 dark:border-gray-600">
            <label className="text-xs text-gray-600 dark:text-gray-400">Action:</label>
            <select
              value={exitCond.action ?? (exitCond as { action_type?: string }).action_type ?? ''}
              onChange={(e) => {
                const next = e.target.value;
                const meta = actions.find((a) => a.value === next);
                const updates: Partial<ExitConditionSet> = {
                  action: next as ExitConditionSet['action'],
                };
                // Picking an option action sets optionStrategy = action (the
                // backend reads the concrete option strategy from this field).
                if (meta?.is_option) {
                  updates.optionStrategy = next;
                  // Seed sensible defaults for the option selection params so a
                  // freshly-chosen option action is immediately serializable.
                  if (!exitCond.optionStrikeMethod) updates.optionStrikeMethod = 'delta';
                } else {
                  // Leaving option-land: drop the strategy marker.
                  updates.optionStrategy = undefined;
                }
                updateExitCondition(index, updates);
              }}
              className="px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            >
              {positionActions.length > 0 && (
                <optgroup label="Position">
                  {positionActions.map((a) => (
                    <option key={a.value} value={a.value}>{a.label}</option>
                  ))}
                </optgroup>
              )}
              {optionActions.length > 0 && (
                <optgroup label="Options">
                  {optionActions.map((a) => (
                    <option key={a.value} value={a.value}>{a.label}</option>
                  ))}
                </optgroup>
              )}
            </select>

            {/* needs_reference actions (adjust_take_profit / adjust_stop_loss):
                reference_value select + value% + optimize toggle. */}
            {actions.find((a) => a.value === exitCond.action)?.needs_reference && (
              <>
                <select
                  value={exitCond.referenceValue ?? ''}
                  onChange={(e) =>
                    updateExitCondition(index, { referenceValue: e.target.value })
                  }
                  className="px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  title="Reference price the adjustment is measured from"
                >
                  <option value="">Reference...</option>
                  {Object.entries(referenceValues).map(([val, label]) => (
                    <option key={val} value={val}>{label}</option>
                  ))}
                </select>
                <input
                  type="number"
                  step="0.1"
                  value={exitCond.actionValue ?? 0}
                  onChange={(e) =>
                    updateExitCondition(index, { actionValue: parseFloat(e.target.value) })
                  }
                  className="w-20 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  placeholder="%"
                />
                <span className="text-xs text-gray-600 dark:text-gray-400">%</span>

                {showOptimization && (
                  <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400">
                    <input
                      type="checkbox"
                      checked={exitCond.actionValueOptimize ?? false}
                      onChange={(e) =>
                        updateExitCondition(index, { actionValueOptimize: e.target.checked })
                      }
                      className="rounded"
                    />
                    Optimize
                  </label>
                )}
              </>
            )}

            {/* Option actions: strike method + strike param + DTE + sizing. */}
            {actions.find((a) => a.value === exitCond.action)?.is_option && (
              <>
                <select
                  value={exitCond.optionStrikeMethod ?? 'delta'}
                  onChange={(e) =>
                    updateExitCondition(index, {
                      optionStrikeMethod: e.target.value as ExitConditionSet['optionStrikeMethod'],
                    })
                  }
                  className="px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  title="Strike selection method"
                >
                  <option value="delta">Delta</option>
                  <option value="percent_otm">% OTM</option>
                  <option value="consensus_target">Consensus Target</option>
                </select>

                {/* Strike param: Δ for delta, % OTM for percent_otm, hidden for
                    consensus_target (no scalar param). */}
                {exitCond.optionStrikeMethod !== 'consensus_target' && (
                  <>
                    <span className="text-xs text-gray-600 dark:text-gray-400">
                      {exitCond.optionStrikeMethod === 'percent_otm' ? '% OTM' : 'Δ'}
                    </span>
                    <input
                      type="number"
                      step="0.01"
                      value={exitCond.optionStrikeParam ?? 0}
                      onChange={(e) =>
                        updateExitCondition(index, {
                          optionStrikeParam: parseFloat(e.target.value),
                        })
                      }
                      className="w-20 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    />
                    {showOptimization && (
                      <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400">
                        <input
                          type="checkbox"
                          checked={exitCond.optionStrikeParamOptimize ?? false}
                          onChange={(e) =>
                            updateExitCondition(index, {
                              optionStrikeParamOptimize: e.target.checked,
                            })
                          }
                          className="rounded"
                        />
                        Optimize Δ
                      </label>
                    )}
                  </>
                )}

                {/* DTE min/max */}
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">DTE:</label>
                  <input
                    type="number"
                    step="1"
                    value={exitCond.optionDteMin ?? 0}
                    onChange={(e) =>
                      updateExitCondition(index, { optionDteMin: parseInt(e.target.value) || 0 })
                    }
                    className="w-14 px-1 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    title="Min days to expiry"
                  />
                  <span className="text-xs text-gray-600 dark:text-gray-400">-</span>
                  <input
                    type="number"
                    step="1"
                    value={exitCond.optionDteMax ?? 0}
                    onChange={(e) =>
                      updateExitCondition(index, { optionDteMax: parseInt(e.target.value) || 0 })
                    }
                    className="w-14 px-1 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    title="Max days to expiry"
                  />
                </div>

                {showOptimization && (
                  <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400" title="Optimize the DTE window">
                    <input
                      type="checkbox"
                      checked={exitCond.optionDteOptimize ?? false}
                      onChange={(e) =>
                        updateExitCondition(index, { optionDteOptimize: e.target.checked })
                      }
                      className="rounded"
                    />
                    Optimize DTE
                  </label>
                )}

                {/* Sizing % */}
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Size:</label>
                  <input
                    type="number"
                    step="1"
                    value={exitCond.optionSizing ?? 0}
                    onChange={(e) =>
                      updateExitCondition(index, { optionSizing: parseFloat(e.target.value) })
                    }
                    className="w-16 px-1 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    title="Position sizing %"
                  />
                  <span className="text-xs text-gray-600 dark:text-gray-400">%</span>
                </div>
              </>
            )}
          </div>

          {/* Action Optimization Range (adjust actions: action_value sweep) */}
          {showOptimization &&
            exitCond.actionValueOptimize &&
            actions.find((a) => a.value === exitCond.action)?.needs_reference && (
              <div className="flex items-center gap-2 mt-2 pt-2 border-t border-gray-200 dark:border-gray-600">
                <span className="text-xs text-gray-500 dark:text-gray-400">Range:</span>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Min:</label>
                  <input
                    type="number"
                    step="0.1"
                    value={exitCond.actionValueMin ?? 0}
                    onChange={(e) =>
                      updateExitCondition(index, { actionValueMin: parseFloat(e.target.value) })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Max:</label>
                  <input
                    type="number"
                    step="0.1"
                    value={exitCond.actionValueMax ?? 10}
                    onChange={(e) =>
                      updateExitCondition(index, { actionValueMax: parseFloat(e.target.value) })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Step:</label>
                  <input
                    type="number"
                    step="0.1"
                    value={exitCond.actionValueStep ?? 0.5}
                    onChange={(e) =>
                      updateExitCondition(index, { actionValueStep: parseFloat(e.target.value) })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
              </div>
            )}

          {/* Option strike-param optimization range */}
          {showOptimization &&
            exitCond.optionStrikeParamOptimize &&
            actions.find((a) => a.value === exitCond.action)?.is_option &&
            exitCond.optionStrikeMethod !== 'consensus_target' && (
              <div className="flex items-center gap-2 mt-2 pt-2 border-t border-gray-200 dark:border-gray-600">
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {exitCond.optionStrikeMethod === 'percent_otm' ? '% OTM' : 'Δ'} Range:
                </span>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Min:</label>
                  <input
                    type="number"
                    step="0.01"
                    value={exitCond.optionStrikeParamMin ?? 0}
                    onChange={(e) =>
                      updateExitCondition(index, { optionStrikeParamMin: parseFloat(e.target.value) })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Max:</label>
                  <input
                    type="number"
                    step="0.01"
                    value={exitCond.optionStrikeParamMax ?? 1}
                    onChange={(e) =>
                      updateExitCondition(index, { optionStrikeParamMax: parseFloat(e.target.value) })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Step:</label>
                  <input
                    type="number"
                    step="0.01"
                    value={exitCond.optionStrikeParamStep ?? 0.1}
                    onChange={(e) =>
                      updateExitCondition(index, { optionStrikeParamStep: parseFloat(e.target.value) })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
              </div>
            )}

          {/* Option DTE optimization range */}
          {showOptimization &&
            exitCond.optionDteOptimize &&
            actions.find((a) => a.value === exitCond.action)?.is_option && (
              <div className="flex items-center gap-2 mt-2 pt-2 border-t border-gray-200 dark:border-gray-600">
                <span className="text-xs text-gray-500 dark:text-gray-400">DTE Range:</span>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Min:</label>
                  <input
                    type="number"
                    step="1"
                    value={exitCond.optionDteMinRange ?? 0}
                    onChange={(e) =>
                      updateExitCondition(index, { optionDteMinRange: parseInt(e.target.value) || 0 })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Max:</label>
                  <input
                    type="number"
                    step="1"
                    value={exitCond.optionDteMaxRange ?? 0}
                    onChange={(e) =>
                      updateExitCondition(index, { optionDteMaxRange: parseInt(e.target.value) || 0 })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
                <div className="flex items-center gap-1">
                  <label className="text-xs text-gray-600 dark:text-gray-400">Step:</label>
                  <input
                    type="number"
                    step="1"
                    value={exitCond.optionDteStep ?? 1}
                    onChange={(e) =>
                      updateExitCondition(index, { optionDteStep: parseInt(e.target.value) || 1 })
                    }
                    className="w-16 px-1 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
              </div>
            )}
        </div>
      ))}

      <button
        type="button"
        onClick={addExitCondition}
        className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-600 dark:text-gray-400 border border-dashed border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 w-full justify-center"
      >
        <Plus className="w-4 h-4" />
        Add Exit Rule
      </button>
    </div>
  );
};
