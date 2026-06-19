import { API_BASE } from '../lib/config';
import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Play,
  Loader2,
  AlertCircle,
  TrendingUp,
  TrendingDown,
  Target,
  Calendar,
  Settings,
  ChevronDown,
  ChevronUp,
  BarChart3,
  Activity,
  Brain,
  Clock,
  Award,
  ArrowDownRight,
  Filter,
  Save,
  X,
  Database,
  Layers,
  Sliders,
  Shield,
  Download,
  Upload
} from 'lucide-react';
import Tooltip from '../components/Tooltip';
import ConfirmDialog from '../components/ConfirmDialog';
import ConditionBuilder, {
  ExitConditionsBuilder,
  createEmptyGroup,
  createEmptyCondition,
  isConditionGroup
} from '../components/ConditionBuilder';
// BacktestChart removed - price chart tab not used
import type {
  ConditionGroup,
  ConditionNode,
  ConditionTree,
  ExitConditionSet,
  AvailableField
} from '../components/ConditionBuilder';
import { ExpertPicker } from '../components/ExpertPicker';
import { ExitPresetPicker } from '../components/ExitPresetPicker';
import { ExpertSettingsForm } from '../components/ExpertSettingsForm';
import type { ExpertSettingsValue } from '../components/ExpertSettingsForm';
import { UniversePicker } from '../components/UniversePicker';
import type { UniverseValue } from '../components/UniversePicker';
import { CollapsibleSection } from '../components/CollapsibleSection';
import { RuleIO } from '../components/RuleIO';
import TradeChartModal from '../components/TradeChartModal';
import { GeneCountPreview } from '../components/GeneCountPreview';
import { RunHistoryTable } from '../components/RunHistoryTable';
import ResolvedRulesetView from '../components/ResolvedRulesetView';
import type { BestParams } from '../lib/resolveRuleset';
import { getRulesetVocabulary, importLiveEnterMarket, importLiveRuleset, convertLiveRuleset, listTasks, listBacktests, fetchOptSettingsExport, listExperts, optimizeBatch, listRunningOptimizations, fetchBacktestExport } from '../lib/btApi';
import type { ExpertInfo, OptimizeBatchJob, OptimizeBatchBody, RunningOpt } from '../lib/btApi';
import { RunningJobsPanel } from '../components/RunningJobsPanel';
import { OptimizationJobsTable, OptJobSettingsDetail } from '../components/OptimizationJobsTable';
import { TopIndividualsTable } from '../components/TopIndividualsTable';
import type { Vocabulary, OptimizationJob, OptimizationDetail, OptIndividual } from '../lib/btApi';
import {
  downloadJson,
  buildOptSettingsExport,
  buildIndividualExport,
  parseExport,
} from '../lib/btExport';
import type { OptSettingsExport, IndividualExport, BtExportCommon, ExportUniverse } from '../lib/btExport';
import {
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ResponsiveContainer,
  AreaChart,
  Area,
  ReferenceLine,
} from 'recharts';

interface PredictionTarget {
  type: string;
  category?: string;
  direction?: string;
  horizon?: number;
  profitPct?: number;
  maxDd?: number;
  indicator?: string;
  indicatorType?: string;
  [key: string]: unknown;
}

interface Model {
  id: string;
  name: string;
  modelType: string;
  threshold?: number; // Classification threshold (default 0.5)
  predictionTargets?: PredictionTarget[];
  predictionHorizon?: number;
  datasetId?: number;
  datasetName?: string;
  symbol?: string;
  performanceMetrics: {
    accuracy: number;
    sharpeRatio: number | null;
  };
}

interface Dataset {
  id: number;
  name: string;
  ticker: string;
  timeframe: string;
  startDate: string;
  endDate: string;
  rowsCount: number;
}

interface Strategy {
  id: number;
  name: string;
  description: string | null;
  requiredFields: string[];
  entryConditions?: ConditionTree;  // Deprecated, for backwards compatibility
  buyEntryConditions?: ConditionTree;
  sellEntryConditions?: ConditionTree;
  exitConditions: ExitConditionSet[];
  initialTpPercent: number;
  initialTpOptimize: boolean;
  initialTpMin: number | null;
  initialTpMax: number | null;
  initialTpStep: number | null;
  initialSlPercent: number;
  initialSlOptimize: boolean;
  initialSlMin: number | null;
  initialSlMax: number | null;
  initialSlStep: number | null;
  // Classic-RM params with optimization ranges (Phase 4 joint optimizer)
  rmRiskPerTradePct?: number;
  rmRiskPerTradePctOptimize?: boolean;
  rmRiskPerTradePctMin?: number | null;
  rmRiskPerTradePctMax?: number | null;
  rmRiskPerTradePctStep?: number | null;
  rmPerInstrumentCapPct?: number;
  rmPerInstrumentCapPctOptimize?: boolean;
  rmPerInstrumentCapPctMin?: number | null;
  rmPerInstrumentCapPctMax?: number | null;
  rmPerInstrumentCapPctStep?: number | null;
  rmMinStopPct?: number;
  rmMinStopPctOptimize?: boolean;
  rmMinStopPctMin?: number | null;
  rmMinStopPctMax?: number | null;
  rmMinStopPctStep?: number | null;
  rmAtrStopMult?: number;
  rmAtrStopMultOptimize?: boolean;
  rmAtrStopMultMin?: number | null;
  rmAtrStopMultMax?: number | null;
  rmAtrStopMultStep?: number | null;
  rmMaxConcurrentPositions?: number;
  rmMaxConcurrentPositionsOptimize?: boolean;
  rmMaxConcurrentPositionsMin?: number | null;
  rmMaxConcurrentPositionsMax?: number | null;
  rmMaxConcurrentPositionsStep?: number | null;
  createdAt: string;
  updatedAt: string | null;
}

// Fitness metrics map 1:1 onto the backend strategy_fitness._FITNESS_KEYS.
const FITNESS_METRICS: Array<{ value: string; label: string }> = [
  { value: 'sharpe', label: 'Sharpe Ratio' },
  { value: 'return', label: 'Total Return' },
  { value: 'profit_factor', label: 'Profit Factor' },
  { value: 'win_rate', label: 'Win Rate' },
  { value: 'sortino', label: 'Sortino Ratio' },
  { value: 'calmar', label: 'Calmar Ratio' },
  { value: 'sqn', label: 'SQN' },
  { value: 'max_drawdown', label: 'Max Drawdown (minimize)' },
];

interface Trade {
  id: string | number;
  symbol?: string;
  entryDate: string;
  exitDate: string;
  entryPrice: number;
  exitPrice: number;
  size: number;
  direction: 'long' | 'short';
  pnl: number;
  pnlPercent: number;
  duration: number;
  exitReason: string;
}

interface BacktestResults {
  equityCurve: Array<{ date: string; equity: number }>;
  drawdownCurve: Array<{ date: string; drawdown: number }>;
  trades: Trade[];
  priceData: Array<{ date: string; open: number; high: number; low: number; close: number; signal: number }>;
}

interface Backtest {
  id: number;
  name: string;
  description?: string;
  // 'ml' = legacy model-driven backtesting.py run (modelId set);
  // 'daily_expert' = Phase-2 daily multi-asset expert engine (modelId null).
  engineType?: string;
  modelId: number | null;
  predictionDatasetId: number;
  executionDatasetId: number;
  strategyId: number | null;
  strategyParams: Record<string, unknown> | null;
  startDate: string;
  endDate: string;
  initialCapital: number;
  positionSizingType: string;
  positionSizingValue: number;
  commission: number;
  slippage: number;
  fitnessMetric: string | null;
  status: string;
  isSaved: boolean;
  totalReturn: number | null;
  sharpeRatio: number | null;
  maxDrawdown: number | null;
  winRate: number | null;
  profitFactor: number | null;
  totalTrades: number | null;
  winningTrades: number | null;
  losingTrades: number | null;
  avgTradeDuration: number | null;
  bestTrade: number | null;
  worstTrade: number | null;
  results: BacktestResults | null;
  errorMessage: string | null;
  createdAt: string;
  completedAt: string | null;
}


// Human-readable trade duration. The stored avg_trade_duration / per-trade duration are in
// fill-clock BARS (meaningless to read on a 5min clock — "30010 bars"), so we derive the real
// elapsed TIME from the entry/exit timestamps instead (interval-agnostic).
const formatDuration = (ms: number): string => {
  if (!isFinite(ms) || ms <= 0) return '—';
  const min = ms / 60000;
  if (min < 60) return `${Math.round(min)}m`;
  const hours = min / 60;
  if (hours < 24) {
    const h = Math.floor(hours);
    const m = Math.round(min - h * 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  const days = hours / 24;
  if (days < 10) {
    const d = Math.floor(days);
    const h = Math.round(hours - d * 24);
    return h ? `${d}d ${h}h` : `${d}d`;
  }
  return `${Math.round(days)}d`;
};

const tradeDurationMs = (t: { entryDate?: string; exitDate?: string }): number => {
  if (!t?.entryDate || !t?.exitDate) return NaN;
  return Date.parse(t.exitDate) - Date.parse(t.entryDate);
};

// Generate random backtest name
const generateBacktestName = (): string => {
  const adjectives = ['Quick', 'Swift', 'Bold', 'Sharp', 'Smooth', 'Steady', 'Active', 'Rapid', 'Dynamic', 'Agile'];
  const nouns = ['Trade', 'Signal', 'Strategy', 'Alpha', 'Edge', 'Flow', 'Wave', 'Pulse', 'Trend', 'Momentum'];
  const adj = adjectives[Math.floor(Math.random() * adjectives.length)];
  const noun = nouns[Math.floor(Math.random() * nouns.length)];
  const num = Math.floor(Math.random() * 1000);
  return `${adj}${noun}_${num}`;
};

// Serialize an exit-rule editor object to the snake_case shape the backend rule
// engine consumes (action_from_rule / strategy_param_space / strategy_executor).
// Used by the expert create path which sends exit_conditions straight through.
const exitConditionToSnake = (ec: ExitConditionSet): Record<string, unknown> => ({
  id: ec.id,
  name: ec.name,
  conditions: ec.conditions,
  action: ec.action,
  action_value: ec.actionValue,
  action_value_optimize: ec.actionValueOptimize,
  action_value_min: ec.actionValueMin,
  action_value_max: ec.actionValueMax,
  action_value_step: ec.actionValueStep,
  toggle_optimize: ec.toggleOptimize,
  reference_value: ec.referenceValue,
  option_strategy: ec.optionStrategy,
  option_strike_method: ec.optionStrikeMethod,
  option_strike_param: ec.optionStrikeParam,
  option_strike_param_optimize: ec.optionStrikeParamOptimize,
  option_strike_param_min: ec.optionStrikeParamMin,
  option_strike_param_max: ec.optionStrikeParamMax,
  option_strike_param_step: ec.optionStrikeParamStep,
  option_dte_min: ec.optionDteMin,
  option_dte_max: ec.optionDteMax,
  option_dte_optimize: ec.optionDteOptimize,
  option_dte_min_range: ec.optionDteMinRange,
  option_dte_max_range: ec.optionDteMaxRange,
  option_dte_step: ec.optionDteStep,
  option_sizing: ec.optionSizing,
});

// Round-trip a stored exit rule (snake_case, as persisted by save/run) back into
// the camelCase ExitConditionSet the editor + ConditionBuilder expect. Tolerates
// either casing on read so loading older saved strategies still works.
const exitConditionFromStored = (raw: Record<string, unknown>): ExitConditionSet => {
  const pick = (camel: string, snake: string): unknown =>
    raw[camel] !== undefined ? raw[camel] : raw[snake];
  return {
    id: raw.id as string,
    name: raw.name as string,
    conditions: raw.conditions as ExitConditionSet['conditions'],
    action: raw.action as ExitConditionSet['action'],
    actionValue: pick('actionValue', 'action_value') as number | undefined,
    actionValueOptimize: pick('actionValueOptimize', 'action_value_optimize') as boolean | undefined,
    actionValueMin: pick('actionValueMin', 'action_value_min') as number | undefined,
    actionValueMax: pick('actionValueMax', 'action_value_max') as number | undefined,
    actionValueStep: pick('actionValueStep', 'action_value_step') as number | undefined,
    toggleOptimize: pick('toggleOptimize', 'toggle_optimize') as boolean | undefined,
    referenceValue: pick('referenceValue', 'reference_value') as string | undefined,
    optionStrategy: pick('optionStrategy', 'option_strategy') as string | undefined,
    optionStrikeMethod: pick('optionStrikeMethod', 'option_strike_method') as ExitConditionSet['optionStrikeMethod'],
    optionStrikeParam: pick('optionStrikeParam', 'option_strike_param') as number | undefined,
    optionStrikeParamOptimize: pick('optionStrikeParamOptimize', 'option_strike_param_optimize') as boolean | undefined,
    optionStrikeParamMin: pick('optionStrikeParamMin', 'option_strike_param_min') as number | undefined,
    optionStrikeParamMax: pick('optionStrikeParamMax', 'option_strike_param_max') as number | undefined,
    optionStrikeParamStep: pick('optionStrikeParamStep', 'option_strike_param_step') as number | undefined,
    optionDteMin: pick('optionDteMin', 'option_dte_min') as number | undefined,
    optionDteMax: pick('optionDteMax', 'option_dte_max') as number | undefined,
    optionDteOptimize: pick('optionDteOptimize', 'option_dte_optimize') as boolean | undefined,
    optionDteMinRange: pick('optionDteMinRange', 'option_dte_min_range') as number | undefined,
    optionDteMaxRange: pick('optionDteMaxRange', 'option_dte_max_range') as number | undefined,
    optionDteStep: pick('optionDteStep', 'option_dte_step') as number | undefined,
    optionSizing: pick('optionSizing', 'option_sizing') as number | undefined,
  };
};

// Serialize a ConditionBuilder tree to the shape the optimizer reads: it walks each
// leaf and adds the snake_case optimize-metadata keys the strategy_param_space builder
// consumes (optimize, value_min/max/step, toggle_optimize, confirmation_bars_min/max/step)
// ALONGSIDE the existing camelCase keys (so the editor still round-trips on reload). Each
// node keeps its stable `id` (the optimizer keys genes by it: cond:<id>:*).
const serializeConditionTree = (node: ConditionTree): Record<string, unknown> => {
  if (isConditionGroup(node)) {
    return {
      ...node,
      conditions: node.conditions.map(serializeConditionTree),
    };
  }
  const leaf = node as unknown as Record<string, unknown> & {
    optimizeEnabled?: boolean; valueMin?: number; valueMax?: number; valueStep?: number;
    toggleOptimize?: boolean;
    confirmationBars?: number; confirmationBarsMin?: number; confirmationBarsMax?: number; confirmationBarsStep?: number;
  };
  return {
    ...leaf,
    // optimize-metadata snake keys (additive; camelCase preserved by the spread above).
    optimize: leaf.optimizeEnabled ?? false,
    value_min: leaf.valueMin,
    value_max: leaf.valueMax,
    value_step: leaf.valueStep,
    toggle_optimize: leaf.toggleOptimize,
    confirmation_bars: leaf.confirmationBars,
    confirmation_bars_min: leaf.confirmationBarsMin,
    confirmation_bars_max: leaf.confirmationBarsMax,
    confirmation_bars_step: leaf.confirmationBarsStep,
  };
};

// Minimal snake->camel normalize for an imported ruleset JSON (#159). Loaded ruleset files
// may carry the backend leaf vocabulary (event_type->field, operator->comparison) and snake
// optimize keys; map them so the camelCase ConditionBuilder populates correctly. Tolerant of
// either casing (already-camel files pass through unchanged).
// Legacy builder word-forms -> the canonical engine symbol (single comparison vocabulary).
const WORD_TO_SYMBOL: Record<string, string> = {
  gt: '>', gte: '>=', lt: '<', lte: '<=', eq: '==', neq: '!=', ne: '!=',
};
const normalizeLeaf = (raw: Record<string, unknown>): ConditionTree => {
  // Group node: detect by conditions[] + an AND/OR on EITHER 'operator' (builder/canonical) or
  // 'type' (storage / optimizer-decoded). Reading only 'operator' before was the cause of
  // "Empty field" when a storage-format ('type') group was parsed as a leaf.
  const grpOp = (raw.operator === 'AND' || raw.operator === 'OR') ? (raw.operator as 'AND' | 'OR')
    : (raw.type === 'AND' || raw.type === 'OR') ? (raw.type as 'AND' | 'OR')
    : null;
  if (Array.isArray(raw.conditions) && grpOp) {
    return {
      id: (raw.id as string) ?? createEmptyGroup('AND').id,
      operator: grpOp,
      conditions: (raw.conditions as Record<string, unknown>[]).map(normalizeLeaf),
    } as ConditionGroup;
  }
  const pick = (...keys: string[]): unknown => {
    for (const k of keys) if (raw[k] !== undefined) return raw[k];
    return undefined;
  };
  const rawComp = pick('comparison', 'op', 'operator') as string | undefined;
  const explicitType = pick('fieldType', 'field_type') as string | undefined;
  // Infer fieldType when absent (storage format carries none): a flag has no numeric value AND no
  // comparison/op (storage flags are bare {id, field}); 'is_true'/'is_false' is an explicit flag
  // sentinel. Numerics have a comparison or value. Truly-empty leaves keep the ML default.
  const isFlag = explicitType === 'flag' || rawComp === 'is_true' || rawComp === 'is_false'
    || (explicitType == null && raw.value == null && rawComp == null);
  const fieldType = explicitType
    ?? (isFlag ? 'flag' : (rawComp != null || raw.value != null ? 'numeric' : 'model_probability'));
  return {
    id: (raw.id as string) ?? createEmptyCondition().id,
    field: (pick('field', 'event_type') as string) ?? '',
    fieldType,
    // Flag triggers (e.g. has_position / bullish) carry NO operator; give them the sentinel
    // comparison ('is_true'). Numeric leaves map any word-form (gte) to the canonical symbol (>=);
    // default '>'.
    comparison: isFlag ? 'is_true' : (rawComp != null ? (WORD_TO_SYMBOL[String(rawComp).toLowerCase()] ?? rawComp) : '>'),
    value: (raw.value as ConditionNode['value']) ?? (isFlag ? 1 : 0),
    optimizeEnabled: (pick('optimizeEnabled', 'optimize') as boolean) ?? false,
    toggleOptimize: pick('toggleOptimize', 'toggle_optimize') as boolean | undefined,
    valueMin: pick('valueMin', 'value_min') as number | undefined,
    valueMax: pick('valueMax', 'value_max') as number | undefined,
    valueStep: pick('valueStep', 'value_step') as number | undefined,
    confirmationRequired: pick('confirmationRequired', 'confirmation_required') as number | undefined,
    confirmationBars: pick('confirmationBars', 'confirmation_bars') as number | undefined,
    confirmationBarsMin: pick('confirmationBarsMin', 'confirmation_bars_min') as number | undefined,
    confirmationBarsMax: pick('confirmationBarsMax', 'confirmation_bars_max') as number | undefined,
    confirmationBarsStep: pick('confirmationBarsStep', 'confirmation_bars_step') as number | undefined,
  } as ConditionNode;
};

// Normalize an imported entry tree into a ConditionGroup (the builders require a group root).
const normalizeEntryTree = (raw: unknown): ConditionGroup => {
  if (raw && typeof raw === 'object') {
    const node = normalizeLeaf(raw as Record<string, unknown>);
    if (isConditionGroup(node)) return node;
    return { ...createEmptyGroup('AND'), conditions: [node] };
  }
  return createEmptyGroup('AND');
};

const Backtesting: React.FC = () => {
  const _navigate = useNavigate();
  void _navigate;

  // State
  const [models, setModels] = useState<Model[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [backtests, setBacktests] = useState<Backtest[]>([]);

  // Form state
  const [selectedSymbol, setSelectedSymbol] = useState<string>('');
  const [selectedDatasetId, setSelectedDatasetId] = useState<number | ''>('');
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [showAllModels, setShowAllModels] = useState(false);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [predictionDatasetId, setPredictionDatasetId] = useState<number | ''>('');
  const [executionDatasetId, setExecutionDatasetId] = useState<number | ''>('');
  const [startDate, setStartDate] = useState('2025-01-01');
  const [endDate, setEndDate] = useState('2025-12-31');

  // Strategy configuration
  const [buyEntryConditions, setBuyEntryConditions] = useState<ConditionGroup>(createEmptyGroup('AND'));
  const [sellEntryConditions, setSellEntryConditions] = useState<ConditionGroup>(createEmptyGroup('AND'));
  // "Allow short" gates the short-entry ruleset. Default OFF (long-only). When ON, the Short
  // Entry Conditions block is shown and sell_entry_conditions is sent in the payload; when OFF,
  // an empty group is sent (the backend seeds no SELL/short enter rule for an empty tree).
  // NOTE: the engine ALSO has an `enable_short` config flag (daily_backtest_handler) that gates
  // the RM enable_sell + symmetric SELL rule, but the public BacktestCreate / optimize request
  // schemas do NOT expose it (Pydantic drops unknown fields), so it cannot be wired from the
  // frontend without a backend change. Gating the sell tree is the supported lever here.
  const [allowShort, setAllowShort] = useState(false);
  const [exitConditions, setExitConditions] = useState<ExitConditionSet[]>([]);
  // Import-from-live-expert control (B8): the backtest UI picks an expert CLASS, but live import
  // needs a live expert INSTANCE id, so we collect it explicitly. Graceful on 503 (live DB not
  // configured) / 404 / any error — surfaces the JSON-paste fallback hint instead of crashing.
  const [liveExpertId, setLiveExpertId] = useState<string>('');
  const [liveImporting, setLiveImporting] = useState(false);
  const [liveImportNote, setLiveImportNote] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const handleImportFromLive = useCallback(async () => {
    const id = Number(liveExpertId);
    if (!Number.isFinite(id) || id <= 0) {
      setLiveImportNote({ kind: 'err', text: 'Enter a valid live expert id first.' });
      return;
    }
    setLiveImporting(true);
    setLiveImportNote(null);
    setSource('expert');  // live expert rules use expert-event vocabulary in the entry builders
    try {
      const [enter, rules] = await Promise.all([
        importLiveEnterMarket(id),
        importLiveRuleset(id),
      ]);
      let buyCount = 0;
      let sellCount = 0;
      if (enter.buy_entry_conditions && isConditionGroup(enter.buy_entry_conditions)) {
        const g = normalizeEntryTree(enter.buy_entry_conditions);
        setBuyEntryConditions(g);
        buyCount = g.conditions.length;
      } else {
        setBuyEntryConditions(createEmptyGroup('AND'));
      }
      if (enter.sell_entry_conditions && isConditionGroup(enter.sell_entry_conditions)) {
        const g = normalizeEntryTree(enter.sell_entry_conditions);
        setSellEntryConditions(g);
        sellCount = g.conditions.length;
      } else {
        setSellEntryConditions(createEmptyGroup('AND'));
      }
      // Reflect THIS import: short on only when it carries a short tree (no stale empty Short block).
      setAllowShort(sellCount > 0);
      const exitRules = Array.isArray(rules) ? rules : [];
      setExitConditions(
        exitRules.map((r, i) => {
          const rec = r as Record<string, unknown>;
          const ec = exitConditionFromStored(rec);
          const conds = rec.conditions ? normalizeEntryTree(rec.conditions) : ec.conditions;
          return { ...ec, conditions: conds, id: `exit-live-${Date.now()}-${i}` };
        }),
      );
      setLiveImportNote({
        kind: 'ok',
        text: `Imported from live expert #${id}: ${buyCount} buy / ${sellCount} sell condition(s), ${exitRules.length} exit rule(s).`,
      });
    } catch (e) {
      const is503 = String(e).includes('503');
      setLiveImportNote({
        kind: 'err',
        text: is503
          ? 'Live DB not configured (503). Paste the ruleset JSON via the Import JSON buttons below instead.'
          : 'Live import failed (instance not found / unreachable). Paste the ruleset JSON via the Import JSON buttons below instead.',
      });
    } finally {
      setLiveImporting(false);
    }
  }, [liveExpertId]);
  const [initialTpPercent, setInitialTpPercent] = useState(5.0);
  // TP reference mode (null/'' -> percent-off-entry; 'expert_target_price' -> anchor on the
  // recommendation target). Threaded through Load->Run so an optimized run that used the
  // expert-target bracket reproduces faithfully. No dedicated UI field; carried on load.
  const [initialTpReference, setInitialTpReference] = useState<string | null>(null);
  const [initialSlPercent, setInitialSlPercent] = useState(2.0);
  const [initialTpOptimize, setInitialTpOptimize] = useState(false);
  const [initialSlOptimize, setInitialSlOptimize] = useState(false);
  const [initialTpMin, setInitialTpMin] = useState(2.0);
  const [initialTpMax, setInitialTpMax] = useState(15.0);
  const [initialTpStep, setInitialTpStep] = useState(1.0);
  const [initialSlMin, setInitialSlMin] = useState(1.0);
  const [initialSlMax, setInitialSlMax] = useState(10.0);
  const [initialSlStep, setInitialSlStep] = useState(0.5);

  // The saved strategy currently loaded into the editor — required to target
  // POST /api/strategies/{id}/optimize. Cleared whenever the editor is edited away
  // from a saved strategy is NOT tracked (optimization always runs against the saved row).
  const [loadedStrategyId, setLoadedStrategyId] = useState<number | null>(null);
  const [loadedStrategyName, setLoadedStrategyName] = useState<string>('');

  // Run Joint Optimization dialog state
  const [showOptimizeDialog, setShowOptimizeDialog] = useState(false);
  const [optFitnessMetric, setOptFitnessMetric] = useState('sharpe');
  const [optType, setOptType] = useState<'genetic' | 'brute_force'>('genetic');
  const [optPopulationSize, setOptPopulationSize] = useState(20);
  const [optGenerations, setOptGenerations] = useState(10);
  const [optCrossoverProb, setOptCrossoverProb] = useState(0.7);
  const [optMutationProb, setOptMutationProb] = useState(0.2);
  const [optEarlyStopping, setOptEarlyStopping] = useState(5);
  const [optElitismPercent, setOptElitismPercent] = useState(10.0);
  const [optSeed, setOptSeed] = useState(42);
  const [launchingOpt, setLaunchingOpt] = useState(false);
  const [optNotice, setOptNotice] = useState<string | null>(null);

  // Optimize-batch dialog (P3.8): launch one optimization per selected expert against the loaded
  // strategy, reusing the same GA config + fitness + screener_opt assembly as runOptimization.
  const [showBatchDialog, setShowBatchDialog] = useState(false);
  // The single "Optimize" button fires the single joint-optimization dialog when OFF, or the
  // multi-expert batch dialog when ON. Both code paths are kept intact.
  const [optimizeAcrossExperts, setOptimizeAcrossExperts] = useState(false);
  const [batchExperts, setBatchExperts] = useState<ExpertInfo[]>([]);
  const [batchSelected, setBatchSelected] = useState<Set<string>>(new Set());
  const [batchLaunching, setBatchLaunching] = useState(false);
  const [batchNotice, setBatchNotice] = useState<string | null>(null);
  const [batchJobs, setBatchJobs] = useState<OptimizeBatchJob[]>([]);
  useEffect(() => {
    if (!showBatchDialog || batchExperts.length) return;
    listExperts().then(setBatchExperts).catch(() => setBatchExperts([]));
  }, [showBatchDialog, batchExperts.length]);

  const runBatchOptimization = async () => {
    if (loadedStrategyId == null) { setBatchNotice('Load or save a strategy first.'); return; }
    if (batchSelected.size === 0) { setBatchNotice('Select at least one expert.'); return; }
    try {
      setBatchLaunching(true);
      setBatchNotice(null);
      setBatchJobs([]);
      const backtestBlock: Record<string, unknown> = {
        engine: 'daily',
        universe,
        start_date: startDate,
        end_date: endDate,
        initial_capital: initialCapital,
        execution_interval: executionInterval,
        commission,
        slippage,
        enable_short: allowShort,  // seed symmetric short entry + RM sell gate when shorting on
      };
      const body: OptimizeBatchBody = {
        experts: [...batchSelected],
        strategy_id: loadedStrategyId,
        fitness_metric: optFitnessMetric,
        optimization_type: optType,
        expert_params: expertSettings.expert_params,
        optimization_config: {
          populationSize: optPopulationSize,
          generations: optGenerations,
          crossoverProb: optCrossoverProb,
          mutationProb: optMutationProb,
          earlyStoppingGenerations: optEarlyStopping,
          elitismPercent: optElitismPercent,
          seed: optSeed,
          backtest: backtestBlock,
        },
        name_prefix: `Batch ${loadedStrategyName}`,
      };
      if (universe.mode === 'screener' && universe.screener_param_ranges
          && Object.values(universe.screener_param_ranges).some(r => r.optimize)) {
        body.screener_opt = {
          param_ranges: universe.screener_param_ranges,
          cadence_days: screenerCadenceDays,
          base_settings: universe.screener_settings,
          // store omitted -> backend defaults to ba2_common SCREENER_STORE_DIR (a sub-path of the
          // shared cache folder). Only sent when the user overrides the path.
          ...(screenerStore.trim() ? { store: screenerStore.trim() } : {}),
        };
      }
      const res = await optimizeBatch(body);
      setBatchJobs(res.jobs ?? []);
      setBatchNotice(`Queued ${res.count} optimization job(s).`);
    } catch (e) {
      setBatchNotice(e instanceof Error ? e.message : 'Failed to launch batch optimization.');
    } finally {
      setBatchLaunching(false);
    }
  };

  // Backtest settings
  const [initialCapital, setInitialCapital] = useState(10000);
  const [positionSizingType, setPositionSizingType] = useState('fixed');
  const [positionSizingValue, setPositionSizingValue] = useState(1000);
  const [commission, setCommission] = useState(0.1);
  const [slippage, setSlippage] = useState(0.05);
  // Expert-engine simulation bar size (execution_interval).
  const [executionInterval, setExecutionInterval] = useState('5m');

  // Available fields from model
  const [availableFields, setAvailableFields] = useState<AvailableField[]>([]);

  // Exit-ruleset vocabulary (flags / numerics / operators / actions). Fetched
  // once on mount and threaded into the exit-condition builder so its leaves are
  // vocabulary-driven. Entry-condition builders intentionally do NOT receive it,
  // keeping entry fields scoped to the model's prediction fields.
  const [rulesetVocabulary, setRulesetVocabulary] = useState<Vocabulary | undefined>(undefined);

  // UI state
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showConditionModal, setShowConditionModal] = useState<'buy' | 'sell' | 'exit' | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Results view
  const [selectedBacktest, setSelectedBacktest] = useState<Backtest | null>(null);
  // Opt-History: the currently-selected optimization job. When set (and no backtest is
  // selected), the RIGHT panel shows this job's settings + top individuals. Selecting a
  // backtest from the job's saved-backtests table clears this and shows the full result.
  const [selectedOptJob, setSelectedOptJob] = useState<
    { job: OptimizationJob; detail?: OptimizationDetail } | null
  >(null);
  // Opt-History right panel: which sub-tab is active when a job is selected, and which top
  // individual (by rank) the user clicked. The Individual Backtest tab renders that
  // individual's persisted full backtest (loaded via viewBacktest) or, if it has none, a
  // note + its params.
  const [optSubTab, setOptSubTab] = useState<'optimization' | 'individual'>('optimization');
  const [selectedIndividual, setSelectedIndividual] = useState<OptIndividual | null>(null);
  // Set when the clicked individual has no persisted full backtest (rank beyond the saved
  // top-N): the Individual Backtest tab shows this note + the individual's params instead.
  const [individualNoBacktest, setIndividualNoBacktest] = useState<OptIndividual | null>(null);
  const [activeTab, setActiveTab] = useState<'equity' | 'drawdown' | 'trades' | 'strategy'>('equity');
  const [tradeFilter, setTradeFilter] = useState<'all' | 'profit' | 'loss'>('all');
  const [tradeSortField, setTradeSortField] = useState<'pnl' | 'date' | 'duration'>('date');
  const [tradeSortAsc, setTradeSortAsc] = useState(false);
  const [chartTrade, setChartTrade] = useState<Trade | null>(null);  // trade-list click -> daily chart

  // Dialogs
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [saveStrategyName, setSaveStrategyName] = useState('');
  const [saveStrategyDescription, setSaveStrategyDescription] = useState('');
  const [savingStrategy, setSavingStrategy] = useState(false);

  // Tab state for New Backtest card
  const [backtestCardTab, setBacktestCardTab] = useState<'new' | 'history' | 'saved' | 'jobs' | 'optjobs'>('new');
  const [runningJobCount, setRunningJobCount] = useState(0);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [all, runningOpts] = await Promise.all([
          listTasks('running'),
          listRunningOptimizations().catch(() => [] as RunningOpt[]),
        ]);
        const bt = all.filter(t => !t.task_type || ['daily_backtest', 'backtest', 'strategy_optimization'].includes(t.task_type));
        // Include running optimizations that have no API task (CLI-launched) — counted by the
        // RunningJobsPanel as orphan rows, so the badge must match.
        const jobNames = new Set(bt.map(t => t.name).filter(Boolean) as string[]);
        const orphanOpts = runningOpts.filter(o => !(o.name && jobNames.has(o.name)));
        if (alive) setRunningJobCount(bt.length + orphanOpts.length);
      } catch { /* ignore */ }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  // Source selector: 'expert' = daily multi-asset expert engine; 'ml' = model-driven.
  const [source, setSource] = useState<'expert' | 'ml'>('expert');
  const [expertClass, setExpertClass] = useState<string>('');
  // True when the selected expert bypasses classic RM (e.g. FactorRanker rebalances
  // to target weights, so per-position TP/SL is not applied). Set from ExpertPicker.
  const [expertBypassesRm, setExpertBypassesRm] = useState(false);
  const [expertSettings, setExpertSettings] = useState<ExpertSettingsValue>({ settings: {}, expert_params: {} });
  const [universe, setUniverse] = useState<UniverseValue>({ mode: 'static', symbols: [] });
  // Required by the daily_expert engine on the backend.
  const [fillModel, setFillModel] = useState<string>('next_bar_open');
  const [runSeed, setRunSeed] = useState<number>(42);
  // Optional expert-engine New-Backtest fields (P1.2). warmupDays = extra history bars before the
  // window; runSchedule controls how often the expert is invoked (daily vs a single weekday).
  const [warmupDays, setWarmupDays] = useState<string>('');
  const [runSchedule, setRunSchedule] = useState<'daily' | 'weekly'>('daily');
  const [runScheduleDay, setRunScheduleDay] = useState<string>('Monday');
  // Screener metric-store path for screener-settings optimization (P1.4). Defaults to the
  // backend default; required when screener_opt is sent.
  // Blank -> the backend uses ba2_common's SCREENER_STORE_DIR (a sub-path of the shared cache
  // folder). Only set this to override the metric-store location.
  const [screenerStore, setScreenerStore] = useState<string>('');
  // Cadence (days) for rebuilding the screener universe during screener-settings optimization.
  const [screenerCadenceDays, setScreenerCadenceDays] = useState<number>(7);

  // New-Backtest "Import settings" control: outcome note after importing an exported
  // opt-settings / individual JSON into the form fields (part 5).
  const [importNote, setImportNote] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const importFileRef = useRef<HTMLInputElement>(null);
  const expertSettingsFileRef = useRef<HTMLInputElement>(null);

  // Import expert settings JSON exported from the live trade platform
  // (expert_settings_<type>_<id>_<ts>.json). Maps expert_settings key-value pairs to form fields
  // AND selects the exported expert in the Source/Expert picker.
  const importExpertSettingsJson = async (raw: string) => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(raw);
    } catch {
      setImportNote({ kind: 'err', text: 'Invalid JSON — could not parse the expert settings file.' });
      return;
    }
    // Accept BOTH the live-platform shape ({expert_type, expert_settings}) AND the saved-backtest
    // export shape ({expert, settings:{initial_tp_percent, initial_sl_percent, expert_params}}), so a
    // file exported from the Saved tab round-trips back in here.
    const settings = (parsed.expert_settings ?? parsed.settings ?? {}) as Record<string, unknown>;
    if (!parsed.expert_type && !parsed.expert && !parsed.expert_settings && !parsed.settings) {
      setImportNote({ kind: 'err', text: 'Unrecognized format. Expected an expert-settings / saved-backtest export JSON.' });
      return;
    }
    applyIndividualParams(settings);
    // The expert class may be exported under expert_type (test-platform export) or expert
    // (live settings_export_import). Select it in the ExpertPicker (which keys on the expert CLASS).
    const expertType = (['expert_type', 'expert', 'expert_class']
      .map((k) => parsed[k])
      .find((v) => typeof v === 'string' && v) as string | undefined) ?? '';
    let matched = '';
    if (expertType) {
      try {
        const experts = await listExperts();
        const info = experts.find((e) => e.class === expertType)
          ?? experts.find((e) => e.class.toLowerCase() === expertType.toLowerCase());
        if (info) {
          setSource('expert');
          setExpertClass(info.class);
          setExpertBypassesRm(info.bypasses_classic_rm ?? false);
          matched = info.class;
        }
      } catch {
        /* offline / no experts list: leave the picker for manual selection */
      }
    }
    // Pre-fill the expert's CONCRETE settings from the exported expert_params (the optimized model:*
    // genes), so a saved/optimized run loads its tuned params. The ExpertSettingsForm's defaults
    // effect MERGES (only seeds keys not already set), so these survive. Strip the model: prefix.
    const ep = (settings.expert_params ?? {}) as Record<string, unknown>;
    if (ep && typeof ep === 'object' && Object.keys(ep).length) {
      const concrete: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(ep)) concrete[k.startsWith('model:') ? k.slice(6) : k] = v;
      setExpertSettings((prev) => ({ settings: { ...prev.settings, ...concrete }, expert_params: prev.expert_params }));
    }
    setImportNote({
      kind: 'ok',
      text: matched
        ? `Imported expert settings for "${matched}": selected the expert and pre-filled TP/SL + params.`
        : `Imported expert settings${expertType ? ` from "${expertType}" (not a known expert — select it manually)` : ''}: pre-filled TP/SL + params.`,
    });
  };
  const handleExpertSettingsFile = (file: File | undefined) => {
    if (!file) return;
    file.text().then(importExpertSettingsJson).catch(() =>
      setImportNote({ kind: 'err', text: 'Could not read the selected expert settings file.' }),
    );
  };

  // Import-JSON ruleset (#159): load an expert ruleset JSON file (buy/sell enter trees + exit
  // rules) into the condition builders, normalizing snake->camel so loaded rules populate.
  const rulesetFileRef = useRef<HTMLInputElement>(null);
  const importRulesetJson = async (raw: string) => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(raw);
    } catch {
      setLiveImportNote({ kind: 'err', text: 'Invalid JSON — could not parse the ruleset file.' });
      return;
    }
    // Live-platform trigger/action export (export_type rulesets/ruleset/rule). Convert it via
    // the shared ba2_common converter (POST /api/ruleset/convert-live) and apply the result the
    // SAME way handleImportFromLive does — so rules round-trip between the live and test platforms.
    const exportType = parsed.export_type as string | undefined;
    if (exportType === 'rulesets' || exportType === 'ruleset' || exportType === 'rule') {
      try {
        const res = await convertLiveRuleset(parsed);
        // A live ruleset is expert-event rules (confidence/bullish/…); ensure the expert source is
        // active so the entry builders thread the ruleset vocabulary (else the fields show blank).
        setSource('expert');
        let buyCount = 0;
        let sellCount = 0;
        // normalizeEntryTree maps the converter's snake optimize keys (field_type/optimize/
        // value_min) -> the camelCase the ConditionBuilder reads, so numeric leaves render their
        // operator/value + optimize range (the field itself already matches).
        if (res.buy_entry_conditions && isConditionGroup(res.buy_entry_conditions)) {
          const g = normalizeEntryTree(res.buy_entry_conditions);
          setBuyEntryConditions(g);
          buyCount = g.conditions.length;
        } else {
          setBuyEntryConditions(createEmptyGroup('AND'));
        }
        if (res.sell_entry_conditions && isConditionGroup(res.sell_entry_conditions)) {
          const g = normalizeEntryTree(res.sell_entry_conditions);
          setSellEntryConditions(g);
          sellCount = g.conditions.length;
        } else {
          setSellEntryConditions(createEmptyGroup('AND'));
        }
        // Reset "Allow short" to reflect THIS import: on when it carries a short tree, OFF for a
        // buy-only ruleset (otherwise a stale allowShort=true leaves an empty Short Entry block shown).
        setAllowShort(sellCount > 0);
        const exitRules = Array.isArray(res.exit_conditions) ? res.exit_conditions : [];
        setExitConditions(
          exitRules.map((r, i) => {
            const rec = r as Record<string, unknown>;
            const ec = exitConditionFromStored(rec);
            // exitConditionFromStored passes `conditions` RAW; normalize them like the non-live
            // path so flag triggers get the sentinel comparison (else "Empty comparison in Exit
            // Rule …" on run) and numeric leaves get camelCase keys (so they render).
            const conds = rec.conditions ? normalizeEntryTree(rec.conditions) : ec.conditions;
            return { ...ec, conditions: conds, id: `exit-live-${Date.now()}-${i}` };
          }),
        );
        const skipped = res.summary?.skipped_rules ?? 0;
        setLiveImportNote({
          kind: 'ok',
          text: `Imported live ruleset: ${buyCount} buy / ${sellCount} sell condition(s), ${exitRules.length} exit rule(s)`
            + (skipped ? ` (${skipped} rule(s) skipped: no backtester equivalent).` : '.'),
        });
      } catch (e) {
        setLiveImportNote({
          kind: 'err',
          text: 'Could not convert the live ruleset file. ' + (e instanceof Error ? e.message : ''),
        });
      }
      return;
    }
    try {
      const buyRaw = parsed.buy_entry_conditions ?? parsed.buyEntryConditions;
      const sellRaw = parsed.sell_entry_conditions ?? parsed.sellEntryConditions;
      const exitRaw = (parsed.exit_rules ?? parsed.exit_conditions ?? parsed.rules ?? parsed.exitConditions) as unknown;
      let buyCount = 0;
      let sellCount = 0;
      if (buyRaw) { const g = normalizeEntryTree(buyRaw); setBuyEntryConditions(g); buyCount = g.conditions.length; }
      else { setBuyEntryConditions(createEmptyGroup('AND')); }
      if (sellRaw) { const g = normalizeEntryTree(sellRaw); setSellEntryConditions(g); sellCount = g.conditions.length; }
      else { setSellEntryConditions(createEmptyGroup('AND')); }
      // Reset "Allow short" to match THIS import (OFF for a buy-only file; avoids a stale empty Short block).
      setAllowShort(sellCount > 0);
      const exitArr = Array.isArray(exitRaw) ? (exitRaw as Record<string, unknown>[]) : [];
      if (exitArr.length) {
        setExitConditions(exitArr.map((r, i) => {
          const ec = exitConditionFromStored(r);
          // exitConditionFromStored already maps action/action_value*; also resolve action_type
          // and normalize the conditions tree (event_type->field, operator->comparison).
          const action = (ec.action ?? (r.action_type as ExitConditionSet['action'])) as ExitConditionSet['action'];
          const conds = r.conditions ? normalizeEntryTree(r.conditions) : ec.conditions;
          return { ...ec, action, conditions: conds, id: ec.id ?? `exit-import-${Date.now()}-${i}` };
        }));
      }
      setLiveImportNote({
        kind: 'ok',
        text: `Imported ruleset JSON: ${buyCount} buy / ${sellCount} sell condition(s), ${exitArr.length} exit rule(s).`,
      });
    } catch (e) {
      setLiveImportNote({ kind: 'err', text: e instanceof Error ? e.message : 'Failed to apply the ruleset JSON.' });
    }
  };
  const handleRulesetFile = (file: File | undefined) => {
    if (!file) return;
    file.text().then(importRulesetJson).catch(() =>
      setLiveImportNote({ kind: 'err', text: 'Could not read the selected ruleset file.' }),
    );
  };

  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });

  // Fetch initial data
  useEffect(() => {
    fetchData();
  }, []);

  // Poll for backtest status updates when there are pending/running backtests
  useEffect(() => {
    const hasPendingOrRunning = backtests.some(bt => bt.status === 'pending' || bt.status === 'running');
    if (!hasPendingOrRunning) return;

    const pollInterval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/backtests`);
        if (res.ok) {
          const data = await res.json();
          const updatedBacktests = data.backtests || [];
          setBacktests(updatedBacktests);

          // Update selected backtest if it was updated
          if (selectedBacktest) {
            const updated = updatedBacktests.find((bt: Backtest) => bt.id === selectedBacktest.id);
            if (updated && updated.status !== selectedBacktest.status) {
              // Fetch full details for the selected backtest
              const detailsRes = await fetch(`${API_BASE}/backtests/${selectedBacktest.id}`);
              if (detailsRes.ok) {
                const details = await detailsRes.json();
                setSelectedBacktest(details);
              }
            }
          }
        }
      } catch (err) {
        console.error('Failed to poll backtests:', err);
      }
    }, 2000); // Poll every 2 seconds

    return () => clearInterval(pollInterval);
  }, [backtests, selectedBacktest]);

  // Fetch prediction fields when model changes
  const fetchPredictionFields = useCallback(async (modelId: string) => {
    if (!modelId) {
      setAvailableFields([]);
      return;
    }

    try {
      const res = await fetch(`${API_BASE}/models/${modelId}/prediction-fields`);
      if (res.ok) {
        const data = await res.json();
        setAvailableFields(data.fields || []);
      }
    } catch (err) {
      console.error('Failed to fetch prediction fields:', err);
    }
  }, []);

  useEffect(() => {
    if (selectedModel) {
      fetchPredictionFields(selectedModel);
    }
  }, [selectedModel, fetchPredictionFields]);

  // Fetch the exit-ruleset vocabulary once on mount.
  useEffect(() => {
    let cancelled = false;
    getRulesetVocabulary()
      .then((v) => { if (!cancelled) setRulesetVocabulary(v); })
      .catch((err) => console.error('Failed to fetch ruleset vocabulary:', err));
    return () => { cancelled = true; };
  }, []);

  const fetchData = async () => {
    try {
      setLoading(true);

      // Fetch models, datasets, strategies, and backtests in parallel
      const [modelsRes, datasetsRes, strategiesRes, backtestsRes] = await Promise.all([
        fetch(`${API_BASE}/models`),
        fetch(`${API_BASE}/datasets`),
        fetch(`${API_BASE}/strategies`),
        fetch(`${API_BASE}/backtests`)
      ]);

      if (modelsRes.ok) {
        const data = await modelsRes.json();
        setModels(data.models || []);
      }

      if (datasetsRes.ok) {
        const data = await datasetsRes.json();
        // Transform snake_case to camelCase
        const transformedDatasets = (data.datasets || []).map((d: Record<string, unknown>) => ({
          id: d.id,
          name: d.name,
          ticker: d.ticker,
          timeframe: d.timeframe,
          startDate: d.start_date,
          endDate: d.end_date,
          rowsCount: d.rows_count
        }));
        setDatasets(transformedDatasets.slice().sort((a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name)));
      }

      if (strategiesRes.ok) {
        const data = await strategiesRes.json();
        setStrategies(data.strategies || []);
      }

      if (backtestsRes.ok) {
        const data = await backtestsRes.json();
        setBacktests(data.backtests || []);
      }

      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load data');
    } finally {
      setLoading(false);
    }
  };

  const runBacktest = async () => {
    if (source === 'expert') {
      if (!expertClass) {
        setError('Please select an expert');
        return;
      }
      if (universe.mode === 'static' && universe.symbols.length === 0) {
        setError('Please provide at least one symbol in the universe');
        return;
      }
    } else {
      if (selectedModels.size === 0) {
        setError('Please select at least one model');
        return;
      }

      if (!predictionDatasetId) {
        setError('Please select a dataset');
        return;
      }

      if (!executionDatasetId) {
        setError('Please select an execution dataset');
        return;
      }
    }

    // Validate conditions - check for empty fields
    const validateConditions = (tree: ConditionTree, path: string): string | null => {
      if (isConditionGroup(tree)) {
        for (let i = 0; i < tree.conditions.length; i++) {
          const error = validateConditions(tree.conditions[i], `${path}[${i}]`);
          if (error) return error;
        }
        return null;
      } else {
        // It's a ConditionNode
        if (!tree.field || tree.field.trim() === '') {
          return `Empty field in ${path}. Please select a field or remove the condition.`;
        }
        if (!tree.comparison || tree.comparison.trim() === '') {
          return `Empty comparison in ${path}. Please select a comparison operator.`;
        }
        return null;
      }
    };

    const buyError = validateConditions(buyEntryConditions, 'Buy Entry');
    if (buyError) {
      setError(buyError);
      return;
    }

    // Short entry is only validated/sent when "Allow short" is on. When off, an empty group is
    // sent so the backend seeds no SELL/short enter rule.
    if (allowShort) {
      const sellError = validateConditions(sellEntryConditions, 'Short Entry');
      if (sellError) {
        setError(sellError);
        return;
      }
    }
    const effectiveSellEntryConditions: ConditionGroup = allowShort
      ? sellEntryConditions
      : createEmptyGroup('AND');

    for (let i = 0; i < exitConditions.length; i++) {
      const exitError = validateConditions(exitConditions[i].conditions, `Exit Rule "${exitConditions[i].name}"`);
      if (exitError) {
        setError(exitError);
        return;
      }
    }

    try {
      setRunning(true);
      setError(null);

      // Build strategy params from current form state
      const strategyParams = {
        buyEntryConditions,
        sellEntryConditions: effectiveSellEntryConditions,
        exitConditions: exitConditions.map(ec => ({
          id: ec.id,
          name: ec.name,
          conditions: ec.conditions,
          action: ec.action,
          actionValue: ec.actionValue,
          actionValueOptimize: ec.actionValueOptimize,
          actionValueMin: ec.actionValueMin,
          actionValueMax: ec.actionValueMax,
          actionValueStep: ec.actionValueStep,
          // snake_case fields the backend consumers read directly
          // (strategy_executor.action_value, strategy_param_space, action_from_rule).
          action_value: ec.actionValue,
          action_value_optimize: ec.actionValueOptimize,
          action_value_min: ec.actionValueMin,
          action_value_max: ec.actionValueMax,
          action_value_step: ec.actionValueStep,
          toggle_optimize: ec.toggleOptimize,
          reference_value: ec.referenceValue,
          option_strategy: ec.optionStrategy,
          option_strike_method: ec.optionStrikeMethod,
          option_strike_param: ec.optionStrikeParam,
          option_strike_param_optimize: ec.optionStrikeParamOptimize,
          option_strike_param_min: ec.optionStrikeParamMin,
          option_strike_param_max: ec.optionStrikeParamMax,
          option_strike_param_step: ec.optionStrikeParamStep,
          option_dte_min: ec.optionDteMin,
          option_dte_max: ec.optionDteMax,
          option_dte_optimize: ec.optionDteOptimize,
          option_dte_min_range: ec.optionDteMinRange,
          option_dte_max_range: ec.optionDteMaxRange,
          option_dte_step: ec.optionDteStep,
          option_sizing: ec.optionSizing
        })),
        initialTpPercent,
        initialTpOptimize,
        initialTpMin: initialTpOptimize ? initialTpMin : null,
        initialTpMax: initialTpOptimize ? initialTpMax : null,
        initialTpStep: initialTpOptimize ? initialTpStep : null,
        initialSlPercent,
        initialSlOptimize,
        initialSlMin: initialSlOptimize ? initialSlMin : null,
        initialSlMax: initialSlOptimize ? initialSlMax : null,
        initialSlStep: initialSlOptimize ? initialSlStep : null
      };

      let lastBacktest: Backtest | null = null;

      if (source === 'expert') {
        // Daily multi-asset expert engine. The backend requires fill_model + seed.
        const res = await fetch(`${API_BASE}/backtests`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            engine: 'daily_expert',
            name: generateBacktestName(),
            start_date: startDate,
            end_date: endDate,
            expert: { class: expertClass, settings: expertSettings.settings },
            universe,
            initial_capital: initialCapital,
            commission,
            slippage,
            buy_entry_conditions: buyEntryConditions,
            sell_entry_conditions: effectiveSellEntryConditions,
            enable_short: allowShort,  // seed symmetric short entry + RM sell gate when shorting on
            // snake_case so the daily-engine rule builder (action_from_rule) reads
            // the action + reference_value + option_* selection params.
            exit_conditions: exitConditions.map(exitConditionToSnake),
            initial_tp_percent: initialTpPercent,
            initial_sl_percent: initialSlPercent,
            fill_model: fillModel,
            execution_interval: executionInterval,
            seed: runSeed,
            // Optional expert-engine fields (P1.2) — omit when blank/default so existing runs
            // are unchanged. warmup_days is a positive integer; run_schedule_day only matters
            // when run_schedule is weekly.
            ...(warmupDays.trim() !== '' && Number.isFinite(Number(warmupDays)) && Number(warmupDays) > 0
              ? { warmup_days: Number(warmupDays) }
              : {}),
            ...(runSchedule === 'weekly'
              ? { run_schedule: 'weekly', run_schedule_day: runScheduleDay }
              : {}),
            ...(initialTpReference ? { initial_tp_reference: initialTpReference } : {}),
          })
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.detail || 'Failed to run expert backtest');
        }

        const backtest = await res.json();
        const detailsRes = await fetch(`${API_BASE}/backtests/${backtest.id}`);
        if (detailsRes.ok) {
          const details = await detailsRes.json();
          setBacktests(prev => [details, ...prev]);
          lastBacktest = details;
        }
      } else {
        // Submit one backtest per selected model (ML engine).
        const modelIds = [...selectedModels];

        for (const modelId of modelIds) {
          const autoName = generateBacktestName();
          const res = await fetch(`${API_BASE}/backtests`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name: autoName,
              model_id: modelId,
              prediction_dataset_id: predictionDatasetId,
              execution_dataset_id: executionDatasetId,
              strategy_params: strategyParams,
              start_date: startDate,
              end_date: endDate,
              initial_capital: initialCapital,
              position_sizing_type: positionSizingType,
              position_sizing_value: positionSizingValue,
              commission,
              slippage
            })
          });

          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Failed to run backtest for model ${modelId}`);
          }

          const backtest = await res.json();
          const detailsRes = await fetch(`${API_BASE}/backtests/${backtest.id}`);
          if (detailsRes.ok) {
            const details = await detailsRes.json();
            setBacktests(prev => [details, ...prev]);
            lastBacktest = details;
          }
        }
      }

      if (lastBacktest) {
        setSelectedBacktest(lastBacktest);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run backtest');
    } finally {
      setRunning(false);
    }
  };

  const viewBacktest = async (id: number) => {
    try {
      const res = await fetch(`${API_BASE}/backtests/${id}`);
      if (res.ok) {
        const data = await res.json();
        // A selected backtest takes over the right panel from any job-settings view.
        setSelectedOptJob(null);
        setSelectedBacktest(data);
      }
    } catch (err) {
      setError('Failed to load backtest details');
    }
  };

  // Opt-History: a top-individual row was clicked. Only the top-N (~5) individuals are persisted
  // as full Backtest rows (optimization_id == job.id, named `TOP{rank}-...`). Match the clicked
  // individual to its persisted backtest by the TOP{rank} name (primary) or strategy_params
  // equality (fallback), then load it into the Individual Backtest tab via the existing
  // viewBacktest flow. If none exists (rank beyond the saved top-N) show a clear note + params.
  const selectTopIndividual = async (ind: OptIndividual) => {
    if (!selectedOptJob) return;
    const jobId = selectedOptJob.job.id;
    setSelectedIndividual(ind);
    setIndividualNoBacktest(null);
    setOptSubTab('individual');
    try {
      const rows = await listBacktests({ optimization_id: jobId });
      // Primary: the persisted top-N are named TOP{rank}-... and ranked the SAME way the
      // top-individuals list is (distinct fitness, best first), so rank lines up 1:1.
      const byName = rows.find(
        (r: any) => typeof r.name === 'string' && new RegExp(`^TOP${ind.rank}\\b`).test(r.name),
      );
      // Fallback: positional match (rows come back newest-first; the persisted set is small).
      const target = byName ?? rows[ind.rank - 1];
      if (target?.id != null) {
        const res = await fetch(`${API_BASE}/backtests/${target.id}`);
        if (res.ok) {
          const data = await res.json();
          setSelectedBacktest(data);  // full result for the Individual Backtest tab
          return;
        }
      }
      // No persisted full backtest for this individual.
      setSelectedBacktest(null);
      setIndividualNoBacktest(ind);
    } catch {
      setSelectedBacktest(null);
      setIndividualNoBacktest(ind);
    }
  };

  // Export the selected optimization JOB's settings as JSON. Prefer the backend export endpoint
  // (it carries the full static-universe symbol list); fall back to a client-side build from the
  // already-loaded job if the endpoint is unavailable. Browser download — no server file write.
  const exportOptSettings = async (job: OptimizationJob, detail?: OptimizationDetail) => {
    let payload: Record<string, unknown> | OptSettingsExport;
    try {
      payload = await fetchOptSettingsExport(job.id);
    } catch {
      payload = buildOptSettingsExport(job, detail?.optimizationConfig ?? null);
    }
    downloadJson(`opt-${job.id}-settings.json`, payload);
  };

  // Export ONE top-individual's concrete params (tp/sl/model:*/cond:*/exit:*/screener:*) as JSON.
  const exportIndividual = (job: OptimizationJob, ind: OptIndividual, detail?: OptimizationDetail) => {
    const payload: IndividualExport = buildIndividualExport(job, ind, detail?.optimizationConfig ?? null);
    downloadJson(`opt-${job.id}-individual-${ind.rank}.json`, payload);
  };

  // Apply the universe block from an imported export to the New-Backtest universe picker.
  const applyImportedUniverse = (u: ExportUniverse | undefined) => {
    if (!u) return;
    if (u.mode === 'static' && Array.isArray((u as any).symbols)) {
      setUniverse({ mode: 'static', symbols: (u as any).symbols });
    } else if (u.mode === 'screener') {
      setUniverse({ mode: 'screener', screener_settings: (u as any).screener_settings ?? {} });
    }
  };

  // Map the flat gene dict {tp, sl, model:*, ...} of an individual export onto the form's TP/SL.
  const applyIndividualParams = (params: Record<string, unknown>) => {
    const tp = params.tp ?? params.initialTpPercent ?? params.initial_tp_percent;
    const sl = params.sl ?? params.initialSlPercent ?? params.initial_sl_percent;
    if (typeof tp === 'number' && isFinite(tp)) setInitialTpPercent(tp);
    if (typeof sl === 'number' && isFinite(sl)) setInitialSlPercent(sl);
    // An individual carries concrete (resolved) values, not ranges — turn opt toggles off.
    setInitialTpOptimize(false);
    setInitialSlOptimize(false);
  };

  // Part 5: read an exported opt-settings / individual JSON and populate the New-Backtest form.
  // Round-trips the schema produced by part 4 (lib/btExport.ts). Switches to the New tab so the
  // user sees the populated fields.
  const importSettingsJson = (raw: string) => {
    // A saved-backtest RULESET export ({buy_entry_conditions, sell_entry_conditions, exit_conditions,
    // …}) isn't an opt-settings/opt-individual schema — route it to the ruleset importer so it
    // round-trips (the conditions load into the builders).
    try {
      const probe = JSON.parse(raw) as Record<string, unknown>;
      if (probe && (probe.buy_entry_conditions !== undefined || probe.sell_entry_conditions !== undefined
        || probe.exit_conditions !== undefined || probe.export_type !== undefined)) {
        void importRulesetJson(raw);
        return;
      }
    } catch { /* fall through to parseExport, which reports a clear error */ }
    let parsed;
    try {
      parsed = parseExport(raw);
    } catch (e) {
      setImportNote({ kind: 'err', text: e instanceof Error ? e.message : 'Failed to parse import.' });
      return;
    }
    setSource('expert');
    setBacktestCardTab('new');
    // Common backtest context lives on both shapes (BtExportCommon).
    const d: BtExportCommon = parsed.data;
    if (d.executionInterval) setExecutionInterval(d.executionInterval);
    if (d.startDate) setStartDate(d.startDate);
    if (d.endDate) setEndDate(d.endDate);
    if (typeof d.initialCapital === 'number') setInitialCapital(d.initialCapital);
    applyImportedUniverse(d.universe);
    if (parsed.kind === 'individual') {
      const ind = parsed.data;
      applyIndividualParams(ind.params ?? {});
      setImportNote({
        kind: 'ok',
        text: `Imported individual #${ind.rank} from optimization #${ind.optimizationId}: pre-filled dates, universe, capital, and concrete TP/SL. Set the expert before running (exported params don't carry the expert class).`,
      });
    } else {
      const opt = parsed.data;
      // Opt-settings carry RANGES (min/max/step), not concrete values — pre-fill TP/SL ranges if
      // the export's expertRanges include them; otherwise leave the form's current TP/SL.
      const tpR = opt.expertRanges?.['tp'] ?? opt.expertRanges?.['initialTpPercent'];
      const slR = opt.expertRanges?.['sl'] ?? opt.expertRanges?.['initialSlPercent'];
      if (tpR && tpR.min != null && tpR.max != null) {
        setInitialTpOptimize(true);
        setInitialTpMin(Number(tpR.min)); setInitialTpMax(Number(tpR.max));
        if (tpR.step != null) setInitialTpStep(Number(tpR.step));
      }
      if (slR && slR.min != null && slR.max != null) {
        setInitialSlOptimize(true);
        setInitialSlMin(Number(slR.min)); setInitialSlMax(Number(slR.max));
        if (slR.step != null) setInitialSlStep(Number(slR.step));
      }
      const nRanges = Object.keys(opt.expertRanges ?? {}).length;
      setImportNote({
        kind: 'ok',
        text: `Imported opt-settings from optimization #${opt.optimizationId} ("${opt.name ?? 'unnamed'}"): pre-filled dates, universe, capital${nRanges ? `, and ${nRanges} optimized param range(s)` : ''}. Set the expert before running.`,
      });
    }
  };

  const handleImportFile = (file: File | undefined) => {
    if (!file) return;
    file.text().then(importSettingsJson).catch(() =>
      setImportNote({ kind: 'err', text: 'Could not read the selected file.' }),
    );
  };

  // Quick Load: pre-fill a NEW backtest from a saved/historical run in one click — expert + tuned
  // settings (TP/SL + model:*), conditions, dates, capital, and (from the originating optimization)
  // the universe + interval. Reuses the import handlers so the field-mapping stays in one place.
  const loadBacktestIntoForm = async (id: number) => {
    try {
      const res = await fetch(`${API_BASE}/backtests/${id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const bt = await res.json();
      setSource('expert');
      setBacktestCardTab('new');
      if (bt.startDate) setStartDate(String(bt.startDate).slice(0, 10));
      if (bt.endDate) setEndDate(String(bt.endDate).slice(0, 10));
      if (typeof bt.initialCapital === 'number') setInitialCapital(bt.initialCapital);
      // Expert + concrete settings (TP/SL + the optimized expert_params) via the expert-settings export.
      // Keep the parsed payload so we can also recover the standalone run's universe + interval below.
      let expertExport: Record<string, unknown> | null = null;
      try {
        expertExport = await fetchBacktestExport(id, 'expert_settings');
        await importExpertSettingsJson(JSON.stringify(expertExport));
      } catch { /* ignore */ }
      // Conditions via the ruleset export (structured trees load directly; for opt-derived runs the
      // conditions are flat cond:/exit: genes and don't render as trees — the expert/RM params above
      // still load).
      try { await importRulesetJson(JSON.stringify(await fetchBacktestExport(id, 'ruleset'))); } catch { /* ignore */ }
      // Universe + interval + the full execution config (seed/fill_model/warmup/enable_short/
      // run_schedule/tp_reference) — the expert_settings export now carries these for BOTH
      // opt-derived runs (sourced from the optimization) and standalone runs (persisted on the
      // row). Restoring them is what makes Run reproduce the saved result faithfully.
      if (expertExport) {
        applyImportedUniverse(expertExport.universe as ExportUniverse | undefined);
        const iv = expertExport.execution_interval;
        if (typeof iv === 'string') setExecutionInterval(iv);
        const ex = (expertExport.execution ?? {}) as Record<string, unknown>;
        if (typeof ex.seed === 'number') setRunSeed(ex.seed);
        if (typeof ex.fill_model === 'string') setFillModel(ex.fill_model);
        if (ex.warmup_days != null) setWarmupDays(String(ex.warmup_days));
        if (typeof ex.enable_short === 'boolean') setAllowShort(ex.enable_short);
        if (typeof ex.initial_tp_reference === 'string') setInitialTpReference(ex.initial_tp_reference);
        else setInitialTpReference(null);
        // run_schedule_override {days:{monday:bool,...}, times:[...]} -> the form's daily/weekly +
        // weekday. One weekday true -> weekly on that day; otherwise daily (analyse every bar).
        const rso = ex.run_schedule_override as { days?: Record<string, boolean> } | null | undefined;
        const days = rso?.days ?? null;
        const onDays = days ? Object.entries(days).filter(([, v]) => v).map(([k]) => k) : [];
        if (onDays.length === 1) {
          setRunSchedule('weekly');
          setRunScheduleDay(onDays[0].charAt(0).toUpperCase() + onDays[0].slice(1));
        } else {
          setRunSchedule('daily');
        }
      }
      setImportNote({
        kind: 'ok',
        text: `Loaded "${bt.name ?? `#${id}`}" — expert, settings, conditions, universe, interval, seed & schedule restored. Run to reproduce.`,
      });
    } catch (e) {
      setImportNote({ kind: 'err', text: `Could not load backtest #${id}: ${e instanceof Error ? e.message : ''}` });
    }
  };

  const getFilteredTrades = () => {
    if (!selectedBacktest?.results?.trades) return [];

    let trades = [...selectedBacktest.results.trades];

    // Filter
    if (tradeFilter === 'profit') {
      trades = trades.filter(t => t.pnl > 0);
    } else if (tradeFilter === 'loss') {
      trades = trades.filter(t => t.pnl < 0);
    }

    // Sort
    trades.sort((a, b) => {
      let cmp = 0;
      if (tradeSortField === 'pnl') {
        cmp = a.pnl - b.pnl;
      } else if (tradeSortField === 'date') {
        cmp = a.entryDate.localeCompare(b.entryDate);
      } else if (tradeSortField === 'duration') {
        cmp = a.duration - b.duration;
      }
      return tradeSortAsc ? cmp : -cmp;
    });

    return trades;
  };

  const saveStrategy = async () => {
    if (!saveStrategyName.trim()) {
      setError('Please enter a strategy name');
      return;
    }

    try {
      setSavingStrategy(true);
      setError(null);

      const res = await fetch(`${API_BASE}/strategies`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: saveStrategyName,
          description: saveStrategyDescription || null,
          // Serialize the entry trees so each leaf carries the snake_case optimize metadata
          // (optimize / value_min/max/step / toggle_optimize / confirmation_bars_*) the
          // optimizer's strategy_param_space reads. camelCase keys are preserved too so the
          // editor round-trips on reload.
          buy_entry_conditions: serializeConditionTree(buyEntryConditions),
          // When "Allow short" is off, persist an empty short tree so the saved strategy is
          // long-only (and round-trips with allowShort=false on reload).
          sell_entry_conditions: serializeConditionTree(
            allowShort ? sellEntryConditions : createEmptyGroup('AND'),
          ),
          exit_conditions: exitConditions.map(ec => ({
            id: ec.id,
            name: ec.name,
            conditions: serializeConditionTree(ec.conditions),
            action: ec.action,
            action_value: ec.actionValue,
            action_value_optimize: ec.actionValueOptimize,
            action_value_min: ec.actionValueMin,
            action_value_max: ec.actionValueMax,
            action_value_step: ec.actionValueStep,
            toggle_optimize: ec.toggleOptimize,
            reference_value: ec.referenceValue,
            option_strategy: ec.optionStrategy,
            option_strike_method: ec.optionStrikeMethod,
            option_strike_param: ec.optionStrikeParam,
            option_strike_param_optimize: ec.optionStrikeParamOptimize,
            option_strike_param_min: ec.optionStrikeParamMin,
            option_strike_param_max: ec.optionStrikeParamMax,
            option_strike_param_step: ec.optionStrikeParamStep,
            option_dte_min: ec.optionDteMin,
            option_dte_max: ec.optionDteMax,
            option_dte_optimize: ec.optionDteOptimize,
            option_dte_min_range: ec.optionDteMinRange,
            option_dte_max_range: ec.optionDteMaxRange,
            option_dte_step: ec.optionDteStep,
            option_sizing: ec.optionSizing
          })),
          initial_tp_percent: initialTpPercent,
          initial_tp_optimize: initialTpOptimize,
          initial_tp_min: initialTpOptimize ? initialTpMin : null,
          initial_tp_max: initialTpOptimize ? initialTpMax : null,
          initial_tp_step: initialTpOptimize ? initialTpStep : null,
          initial_sl_percent: initialSlPercent,
          initial_sl_optimize: initialSlOptimize,
          initial_sl_min: initialSlOptimize ? initialSlMin : null,
          initial_sl_max: initialSlOptimize ? initialSlMax : null,
          initial_sl_step: initialSlOptimize ? initialSlStep : null
        })
      });

      if (!res.ok) {
        throw new Error('Failed to save strategy');
      }

      const saved = await res.json();
      setStrategies(prev => [saved, ...prev]);
      // Track the freshly-saved strategy so it can be optimized immediately.
      if (typeof saved.id === 'number') {
        setLoadedStrategyId(saved.id);
        setLoadedStrategyName(saved.name);
      }
      setShowSaveDialog(false);
      setSaveStrategyName('');
      setSaveStrategyDescription('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save strategy');
    } finally {
      setSavingStrategy(false);
    }
  };

  const loadStrategy = (strategy: Strategy) => {
    // Load buy entry conditions - ensure it's a valid group
    if (strategy.buyEntryConditions && isConditionGroup(strategy.buyEntryConditions)) {
      setBuyEntryConditions(strategy.buyEntryConditions);
    } else if (strategy.entryConditions && isConditionGroup(strategy.entryConditions)) {
      // Backwards compatibility: load old entryConditions as buyEntryConditions
      setBuyEntryConditions(strategy.entryConditions);
    } else {
      setBuyEntryConditions(createEmptyGroup('AND'));
    }

    // Load sell entry conditions. Auto-enable "Allow short" when the saved strategy has a
    // non-empty short tree so it round-trips (otherwise default to long-only).
    if (strategy.sellEntryConditions && isConditionGroup(strategy.sellEntryConditions)) {
      setSellEntryConditions(strategy.sellEntryConditions);
      setAllowShort(strategy.sellEntryConditions.conditions.length > 0);
    } else {
      setSellEntryConditions(createEmptyGroup('AND'));
      setAllowShort(false);
    }

    // Load exit conditions. Stored rules carry snake_case fields (save/run write
    // snake), so map each back into the camelCase ExitConditionSet the editor edits
    // — this round-trips toggle_optimize/reference_value + the option_* params.
    setExitConditions(
      (strategy.exitConditions || []).map((ec) =>
        exitConditionFromStored(ec as unknown as Record<string, unknown>)
      )
    );

    // Load TP/SL settings
    setInitialTpPercent(strategy.initialTpPercent ?? 5.0);
    setInitialTpOptimize(strategy.initialTpOptimize ?? false);
    setInitialTpMin(strategy.initialTpMin ?? 2.0);
    setInitialTpMax(strategy.initialTpMax ?? 15.0);
    setInitialTpStep(strategy.initialTpStep ?? 1.0);
    setInitialSlPercent(strategy.initialSlPercent ?? 2.0);
    setInitialSlOptimize(strategy.initialSlOptimize ?? false);
    setInitialSlMin(strategy.initialSlMin ?? 1.0);
    setInitialSlMax(strategy.initialSlMax ?? 10.0);
    setInitialSlStep(strategy.initialSlStep ?? 0.5);

    // Track which saved strategy is loaded so "Run Joint Optimization" can target it
    setLoadedStrategyId(strategy.id);
    setLoadedStrategyName(strategy.name);
  };

  // Launch a joint genetic optimization for the loaded strategy. Builds the GA
  // config + a backtest block from the current form, then POSTs to
  // /api/strategies/{id}/optimize. The route folds the strategy's RM ranges in.
  const runOptimization = async () => {
    if (loadedStrategyId == null) {
      setOptNotice('Load or save a strategy first, then run optimization against it.');
      return;
    }
    try {
      setLaunchingOpt(true);
      setOptNotice(null);
      // Backend OptimizeRequest folds top-level expert_params into optimization_config.
      // The backtest block is source-aware: expert sends expert/universe; ml sends model/datasets.
      const backtestBlock: Record<string, unknown> = source === 'expert'
        ? {
            engine: 'daily',
            expert: { class: expertClass, settings: expertSettings.settings },
            universe,
            start_date: startDate,
            end_date: endDate,
            initial_capital: initialCapital,
            execution_interval: executionInterval,
            commission,
            slippage,
            enable_short: allowShort,  // seed symmetric short entry + RM sell gate when shorting on
          }
        : {
            engine: 'ml',
            model_id: selectedModel || null,
            prediction_dataset_id: predictionDatasetId || null,
            execution_dataset_id: executionDatasetId || null,
            start_date: startDate,
            end_date: endDate,
            initial_capital: initialCapital,
            position_sizing_type: positionSizingType,
            position_sizing_value: positionSizingValue,
            commission,
            slippage,
          };
      const body: Record<string, unknown> = {
        name: `Optimize ${loadedStrategyName} (${optFitnessMetric})`,
        fitness_metric: optFitnessMetric,
        optimization_type: optType,
        // expert_params already carries the Opt-on expert settings + RM genes (keyed by real
        // ba2 names) from ExpertSettingsForm.
        expert_params: expertSettings.expert_params,
        optimization_config: {
          populationSize: optPopulationSize,
          generations: optGenerations,
          crossoverProb: optCrossoverProb,
          mutationProb: optMutationProb,
          earlyStoppingGenerations: optEarlyStopping,
          elitismPercent: optElitismPercent,
          seed: optSeed,
          backtest: backtestBlock,
        },
      };

      // Screener-settings optimization (P1.4): only when the universe is a screener AND at least
      // one screener metric range is toggled to optimize. param_ranges keys are the unprefixed
      // metric-store names produced by UniversePicker (market_cap_min, relative_volume_min, ...).
      if (universe.mode === 'screener' && universe.screener_param_ranges
          && Object.values(universe.screener_param_ranges).some(r => r.optimize)) {
        body.screener_opt = {
          param_ranges: universe.screener_param_ranges,
          cadence_days: screenerCadenceDays,
          base_settings: universe.screener_settings,
          // store omitted -> backend defaults to ba2_common SCREENER_STORE_DIR (under the shared
          // cache folder). Only sent when the user overrides the path.
          ...(screenerStore.trim() ? { store: screenerStore.trim() } : {}),
        };
      }

      const res = await fetch(`${API_BASE}/strategies/${loadedStrategyId}/optimize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || 'Failed to launch optimization');
      }
      const data = await res.json();
      setShowOptimizeDialog(false);
      setOptNotice(null);
      setError(null);
      // Surface the queued job; results land on the StrategyOptimization row + tasks.
      alert(
        `Joint optimization #${data.optimizationId} queued (task ${data.taskId}).\n` +
        `Metric: ${optFitnessMetric} · ${optType} · seed ${optSeed}.`
      );
    } catch (err) {
      setOptNotice(err instanceof Error ? err.message : 'Failed to launch optimization');
    } finally {
      setLaunchingOpt(false);
    }
  };

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center min-h-96">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  // The full backtest result view (header + metrics + chart/trade/strategy tabs). Reused by
  // BOTH the standalone result panel AND the Opt-History "Individual Backtest" sub-tab so a
  // top individual's persisted backtest renders identically. The param shadows the outer
  // `selectedBacktest` state with a non-null Backtest so all references inside stay valid.
  function renderBacktestResult(selectedBacktest: Backtest) {
    // A failed run has no metrics/curves — show the backend error instead of an empty
    // dashboard (which previously read as "ran fine but produced nothing").
    if (selectedBacktest.status === 'failed') {
      return (
        <>
          <div className="flex items-start justify-between flex-wrap gap-2">
            <div className="min-w-0">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
                {selectedBacktest.name}
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                {selectedBacktest.startDate} → {selectedBacktest.endDate}
              </p>
            </div>
            <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">
              Failed
            </span>
          </div>
          <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
            <p className="text-sm font-medium text-red-800 dark:text-red-300 mb-1">Backtest failed</p>
            <pre className="text-xs text-red-700 dark:text-red-400 whitespace-pre-wrap break-words font-mono">
              {selectedBacktest.errorMessage || 'No error message recorded.'}
            </pre>
          </div>
        </>
      );
    }
    // A pending/running run has no results yet — show a live "running" state with a spinner
    // (the panel polls every 2s and swaps to the full dashboard on completion) instead of an
    // empty metrics dashboard, which read as "finished with nothing".
    if (selectedBacktest.status === 'pending' || selectedBacktest.status === 'running') {
      return (
        <>
          <div className="flex items-start justify-between flex-wrap gap-2">
            <div className="min-w-0">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
                {selectedBacktest.name}
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                {selectedBacktest.startDate} → {selectedBacktest.endDate}
              </p>
            </div>
            <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
              {selectedBacktest.status === 'pending' ? 'Queued' : 'Running'}
            </span>
          </div>
          <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
            <Loader2 className="w-10 h-10 animate-spin text-blue-500" />
            <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {selectedBacktest.status === 'pending' ? 'Queued — waiting for a worker…' : 'Running the backtest…'}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400 max-w-sm">
              Results, equity curve and trades will appear here automatically when the run completes.
            </p>
          </div>
        </>
      );
    }
    return (
            <>
              {/* Header: name + engine-type badge (daily expert = multi-asset; ml = model-driven) */}
              <div className="flex items-start justify-between flex-wrap gap-2">
                <div className="min-w-0">
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
                    {selectedBacktest.name}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                    {selectedBacktest.startDate} → {selectedBacktest.endDate}
                    {(selectedBacktest.completedAt || selectedBacktest.createdAt) && (
                      <> &middot; ran {new Date((selectedBacktest.completedAt || selectedBacktest.createdAt) as string).toLocaleString()}</>
                    )}
                  </p>
                </div>
                {selectedBacktest.engineType === 'daily_expert' ? (
                  <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300">
                    Daily expert &middot; multi-asset
                  </span>
                ) : (
                  <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
                    ML strategy{selectedBacktest.modelId != null ? ` · Model #${selectedBacktest.modelId}` : ''}
                  </span>
                )}
              </div>
              {/* Open-positions note: total_trades counts CLOSED round-trips, so a buy-and-hold
                  (no exit rule) shows 0 trades while equity still moved (entry commission + the
                  held position's mark-to-market). Surface the open count so that isn't confusing. */}
              {(() => {
                const openPos = (selectedBacktest.results as { open_positions?: Array<{ symbol?: string }> } | undefined)?.open_positions;
                if (!openPos || openPos.length === 0) return null;
                const syms = openPos.map(p => p.symbol).filter(Boolean).slice(0, 12).join(', ');
                return (
                  <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-3 text-xs text-amber-800 dark:text-amber-300">
                    <span className="font-medium">{openPos.length} position(s) still open</span> at the end of the run
                    {syms ? ` (${syms}${openPos.length > 12 ? '…' : ''})` : ''}. “Total Trades” counts only
                    closed round-trips, so these aren’t included — but their mark-to-market <em>is</em> in equity & return.
                    {(selectedBacktest.totalTrades ?? 0) === 0 && ' This is why a run with 0 trades can still show a P&L.'}
                  </div>
                );
              })()}
              {/* Metrics Summary */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Total Return</p>
                      <p className={`text-2xl font-bold ${(selectedBacktest.totalReturn || 0) >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                        {(selectedBacktest.totalReturn || 0) >= 0 ? '+' : ''}{selectedBacktest.totalReturn?.toFixed(1)}%
                      </p>
                    </div>
                    {(selectedBacktest.totalReturn || 0) >= 0 ? (
                      <TrendingUp className="w-8 h-8 text-green-500" />
                    ) : (
                      <TrendingDown className="w-8 h-8 text-red-500" />
                    )}
                  </div>
                </div>

                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Sharpe Ratio</p>
                      <p className="text-2xl font-bold text-blue-600">{selectedBacktest.sharpeRatio?.toFixed(2)}</p>
                    </div>
                    <Activity className="w-8 h-8 text-blue-500" />
                  </div>
                </div>

                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Max Drawdown</p>
                      <p className="text-2xl font-bold text-red-600">-{selectedBacktest.maxDrawdown?.toFixed(1)}%</p>
                    </div>
                    <ArrowDownRight className="w-8 h-8 text-red-500" />
                  </div>
                </div>

                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Win Rate</p>
                      <p className="text-2xl font-bold text-purple-600">{selectedBacktest.winRate?.toFixed(1)}%</p>
                    </div>
                    <Award className="w-8 h-8 text-purple-500" />
                  </div>
                </div>
              </div>

              {/* Additional Metrics */}
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-3 text-center">
                  <p className="text-xs text-gray-500 dark:text-gray-400">Profit Factor</p>
                  <p className="text-lg font-bold text-gray-900 dark:text-gray-100">{selectedBacktest.profitFactor?.toFixed(2)}</p>
                </div>
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-3 text-center">
                  <p className="text-xs text-gray-500 dark:text-gray-400">Total Trades</p>
                  <p className="text-lg font-bold text-gray-900 dark:text-gray-100">{selectedBacktest.totalTrades}</p>
                </div>
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-3 text-center">
                  <p className="text-xs text-gray-500 dark:text-gray-400">Avg Duration</p>
                  <p className="text-lg font-bold text-gray-900 dark:text-gray-100">{(() => {
                    const ts = (selectedBacktest.results?.trades || [])
                      .map(tradeDurationMs).filter(ms => isFinite(ms) && ms > 0);
                    return ts.length ? formatDuration(ts.reduce((a, b) => a + b, 0) / ts.length) : '—';
                  })()}</p>
                </div>
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-3 text-center">
                  <p className="text-xs text-gray-500 dark:text-gray-400">Best Trade</p>
                  <p className="text-lg font-bold text-green-600">+{selectedBacktest.bestTrade?.toFixed(1)}%</p>
                </div>
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-3 text-center">
                  <p className="text-xs text-gray-500 dark:text-gray-400">Worst Trade</p>
                  <p className="text-lg font-bold text-red-600">{selectedBacktest.worstTrade?.toFixed(1)}%</p>
                </div>
              </div>

              {/* Description / Notes */}
              {selectedBacktest.description && (
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">Notes</h3>
                  <p className="text-sm text-gray-600 dark:text-gray-400 whitespace-pre-wrap">{selectedBacktest.description}</p>
                </div>
              )}

              {/* Chart Tabs */}
              <div className="bg-white dark:bg-gray-800 rounded-lg shadow">
                <div className="border-b border-gray-200 dark:border-gray-700">
                  <nav className="flex">
                    {[
                      { id: 'equity', label: 'Equity Curve', icon: TrendingUp },
                      { id: 'drawdown', label: 'Drawdown', icon: TrendingDown },
                      { id: 'trades', label: 'Trade List', icon: Activity },
                      { id: 'strategy', label: 'Strategy', icon: Award }
                    ].map(tab => (
                      <button
                        key={tab.id}
                        onClick={() => setActiveTab(tab.id as 'equity' | 'drawdown' | 'trades' | 'strategy')}
                        className={`flex items-center gap-2 px-4 py-3 border-b-2 transition-colors text-sm ${
                          activeTab === tab.id
                            ? 'border-blue-500 text-blue-600'
                            : 'border-transparent text-gray-500 hover:text-gray-700'
                        }`}
                      >
                        <tab.icon className="w-4 h-4" />
                        {tab.label}
                      </button>
                    ))}
                  </nav>
                </div>

                <div className="p-4">
                  {activeTab === 'equity' && selectedBacktest.results?.equityCurve && (
                    <div className="h-80">
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={selectedBacktest.results.equityCurve}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                          <XAxis
                            dataKey="date"
                            tickFormatter={(d: string) => {
                              const date = new Date(d);
                              return `${(date.getMonth() + 1).toString().padStart(2, '0')}/${date.getDate().toString().padStart(2, '0')}`;
                            }}
                            tick={{ fontSize: 11 }}
                            interval="preserveStartEnd"
                          />
                          <YAxis
                            domain={['auto', 'auto']}
                            tickFormatter={(v: number) => `$${(v / 1000).toFixed(1)}k`}
                            width={65}
                            tick={{ fontSize: 11 }}
                          />
                          <RechartsTooltip
                            formatter={(value) => [`$${(value as number)?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) ?? '0'}`, 'Equity']}
                            labelFormatter={(label) => {
                              const date = new Date(String(label));
                              return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
                            }}
                          />
                          <Area type="monotone" dataKey="equity" stroke="#22c55e" fill="#22c55e" fillOpacity={0.2} />
                          <ReferenceLine y={selectedBacktest.initialCapital || 10000} stroke="#888" strokeDasharray="3 3" label={{ value: 'Initial', position: 'right', fontSize: 11 }} />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  )}

                  {activeTab === 'drawdown' && selectedBacktest.results?.drawdownCurve && (
                    <div className="h-80">
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={selectedBacktest.results.drawdownCurve}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                          <XAxis
                            dataKey="date"
                            tickFormatter={(d: string) => {
                              const date = new Date(d);
                              return `${(date.getMonth() + 1).toString().padStart(2, '0')}/${date.getDate().toString().padStart(2, '0')}`;
                            }}
                            tick={{ fontSize: 11 }}
                            interval="preserveStartEnd"
                          />
                          <YAxis
                            domain={[0, 'auto']}
                            tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                            width={50}
                            tick={{ fontSize: 11 }}
                            reversed
                          />
                          <RechartsTooltip
                            formatter={(value) => [`${((value as number) ?? 0).toFixed(2)}%`, 'Drawdown']}
                            labelFormatter={(label) => {
                              const date = new Date(String(label));
                              return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
                            }}
                          />
                          <Area type="monotone" dataKey="drawdown" stroke="#ef4444" fill="#ef4444" fillOpacity={0.3} />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  )}

                  {activeTab === 'trades' && selectedBacktest.results?.trades && (
                    <div>
                      {/* Trade Filters */}
                      <div className="flex items-center gap-4 mb-4">
                        <div className="flex items-center gap-2">
                          <Filter className="w-4 h-4 text-gray-500" />
                          <select
                            value={tradeFilter}
                            onChange={e => setTradeFilter(e.target.value as 'all' | 'profit' | 'loss')}
                            className="text-sm border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                          >
                            <option value="all">All Trades</option>
                            <option value="profit">Profitable</option>
                            <option value="loss">Losing</option>
                          </select>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-sm text-gray-500 dark:text-gray-400">Sort:</span>
                          <select
                            value={tradeSortField}
                            onChange={e => setTradeSortField(e.target.value as 'pnl' | 'date' | 'duration')}
                            className="text-sm border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                          >
                            <option value="date">Date</option>
                            <option value="pnl">P&L</option>
                            <option value="duration">Duration</option>
                          </select>
                          <button
                            onClick={() => setTradeSortAsc(!tradeSortAsc)}
                            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                          >
                            {tradeSortAsc ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                          </button>
                        </div>
                      </div>

                      {/* Trade Table — fills the viewport height down to the bottom of the
                          page (matching the left BT-History list's full-height convention)
                          so more trades are visible without a cramped inner scroll. */}
                      <div className="overflow-x-auto h-[calc(100vh-22rem)] overflow-y-auto">
                        <table className="w-full text-sm">
                          <thead className="bg-gray-50 dark:bg-gray-700/50 sticky top-0">
                            <tr>
                              <th className="px-3 py-2 text-left text-gray-700 dark:text-gray-300">Symbol</th>
                              <th className="px-3 py-2 text-left text-gray-700 dark:text-gray-300">Entry</th>
                              <th className="px-3 py-2 text-left text-gray-700 dark:text-gray-300">Exit</th>
                              <th className="px-3 py-2 text-right text-gray-700 dark:text-gray-300">Entry $</th>
                              <th className="px-3 py-2 text-right text-gray-700 dark:text-gray-300">Exit $</th>
                              <th className="px-3 py-2 text-center text-gray-700 dark:text-gray-300">Dir</th>
                              <th className="px-3 py-2 text-right text-gray-700 dark:text-gray-300">Size</th>
                              <th className="px-3 py-2 text-right text-gray-700 dark:text-gray-300">P&L</th>
                              <th className="px-3 py-2 text-right text-gray-700 dark:text-gray-300">P&L %</th>
                              <th className="px-3 py-2 text-center text-gray-700 dark:text-gray-300">Duration</th>
                              <th className="px-3 py-2 text-center text-gray-700 dark:text-gray-300">Reason</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                            {getFilteredTrades().map(trade => (
                              <tr key={trade.id}
                                  onClick={() => setChartTrade(trade)}
                                  title="Click to view the daily chart with entry/exit markers"
                                  className="cursor-pointer hover:bg-blue-50 dark:hover:bg-blue-900/20">
                                <td className="px-3 py-2 font-medium text-gray-900 dark:text-gray-100">{trade.symbol || '—'}</td>
                                <td className="px-3 py-2 text-gray-900 dark:text-gray-100">{trade.entryDate}</td>
                                <td className="px-3 py-2 text-gray-900 dark:text-gray-100">{trade.exitDate}</td>
                                <td className="px-3 py-2 text-right text-gray-900 dark:text-gray-100">${trade.entryPrice.toFixed(2)}</td>
                                <td className="px-3 py-2 text-right text-gray-900 dark:text-gray-100">${trade.exitPrice.toFixed(2)}</td>
                                <td className="px-3 py-2 text-center">
                                  <span className={`px-2 py-0.5 rounded text-xs font-semibold text-white ${
                                    trade.direction === 'long' ? 'bg-green-600' : 'bg-red-600'
                                  }`}>
                                    {trade.direction}
                                  </span>
                                </td>
                                <td className="px-3 py-2 text-right text-gray-900 dark:text-gray-100">{trade.size}</td>
                                <td className={`px-3 py-2 text-right font-medium ${trade.pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                  {trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(2)}
                                </td>
                                <td className={`px-3 py-2 text-right font-medium ${trade.pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                  {trade.pnl >= 0 ? '+' : ''}{trade.pnlPercent.toFixed(2)}%
                                </td>
                                <td className="px-3 py-2 text-center text-gray-900 dark:text-gray-100">{formatDuration(tradeDurationMs(trade))}</td>
                                <td className="px-3 py-2 text-center">
                                  <span className="px-2 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-xs text-gray-700 dark:text-gray-300">
                                    {trade.exitReason}
                                  </span>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {activeTab === 'strategy' && (
                    <div className="p-4 space-y-4">
                      {/* Strategy info from strategyParams or strategyId */}
                      {(() => {
                        // Try strategyParams first, fall back to loading from strategies list
                        let sp = selectedBacktest.strategyParams as any;
                        if (!sp && selectedBacktest.strategyId) {
                          const strat = strategies.find(s => s.id === selectedBacktest.strategyId);
                          if (strat) {
                            sp = {
                              initialTpPercent: strat.initialTpPercent,
                              initialSlPercent: strat.initialSlPercent,
                              buyEntryConditions: strat.buyEntryConditions,
                              sellEntryConditions: strat.sellEntryConditions,
                              exitConditions: strat.exitConditions,
                              strategyName: strat.name,
                            };
                          }
                        }
                        if (!sp && !selectedBacktest.strategyId) {
                          return <p className="text-sm text-gray-500 dark:text-gray-400">No strategy information available for this backtest.</p>;
                        }
                        // Optimization-derived backtests store the GA's flat gene dict
                        // ({tp, sl, model:*, cond:*, exit:*}) in strategyParams, not the
                        // structured {initialTpPercent, buyEntryConditions} shape — so fall
                        // back to the flat tp/sl keys.
                        const tp = sp?.initialTpPercent ?? sp?.initial_tp_percent ?? sp?.tp;
                        const sl = sp?.initialSlPercent ?? sp?.initial_sl_percent ?? sp?.sl;
                        const buyConditions = sp?.buyEntryConditions?.conditions || [];
                        const sellConditions = sp?.sellEntryConditions?.conditions || [];
                        const exitConditions = sp?.exitConditions || [];
                        const stratName = sp?.strategyName;
                        // Flat optimized genes (model:*/cond:*/exit:*) — surfaced as a readable
                        // list so the tab is informative for optimization runs (which carry no
                        // structured buy/sell/exit conditions).
                        const optimizedGenes = sp && typeof sp === 'object'
                          ? Object.entries(sp as Record<string, unknown>)
                              .filter(([k]) => /^(model:|cond:|exit:)/.test(k))
                              .map(([k, v]) => [k, typeof v === 'number' ? (Number.isInteger(v) ? String(v) : (v as number).toFixed(2)) : String(v)] as [string, string])
                          : [];
                        // Resolved-ruleset read-back (B10): when this run came from a
                        // finished optimization that surfaced its flat best-params gene
                        // map (cond:*/exit:* -> value), render the ruleset that ACTUALLY
                        // ran (dropped rules greyed, tuned values filled). The backtest
                        // results object does not yet carry best_params on its own — this
                        // renders only when strategyParams includes a bestParams/
                        // best_params dict (e.g. surfaced via /jobs/{id}/individuals
                        // best_individual.params). See lib/resolveRuleset.ts.
                        const bestParams = (sp?.bestParams ?? sp?.best_params) as BestParams | undefined;
                        return (
                          <>
                            {stratName && (
                              <div className="text-sm">
                                <span className="text-gray-500 dark:text-gray-400">Strategy: </span>
                                <span className="font-medium text-gray-900 dark:text-gray-100">{stratName}</span>
                              </div>
                            )}
                            {!sp && selectedBacktest.strategyId && (
                              <p className="text-sm text-gray-500 dark:text-gray-400">Strategy ID: {selectedBacktest.strategyId} (strategy not found)</p>
                            )}
                            <div className="grid grid-cols-2 gap-4">
                              <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                                <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">Take Profit</div>
                                <div className="text-lg font-bold text-green-600">{tp != null ? `${tp}%` : 'None'}</div>
                              </div>
                              <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3">
                                <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">Stop Loss</div>
                                <div className="text-lg font-bold text-red-600">{sl != null ? `${sl}%` : 'None'}</div>
                              </div>
                            </div>
                            {buyConditions.length > 0 && (
                              <div>
                                <h4 className="text-sm font-semibold text-green-600 mb-2">Entry Conditions ({buyConditions.length})</h4>
                                <div className="space-y-1">
                                  {buyConditions.map((c: any, i: number) => (
                                    <div key={i} className="text-sm bg-green-50 dark:bg-green-900/20 rounded px-3 py-1.5 text-green-800 dark:text-green-300">
                                      {c.field} <span className="font-mono">{c.comparison}</span> {c.value}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {sellConditions.length > 0 && (
                              <div>
                                <h4 className="text-sm font-semibold text-red-600 mb-2">Short Entry Conditions ({sellConditions.length})</h4>
                                <div className="space-y-1">
                                  {sellConditions.map((c: any, i: number) => (
                                    <div key={i} className="text-sm bg-red-50 dark:bg-red-900/20 rounded px-3 py-1.5 text-red-800 dark:text-red-300">
                                      {c.field} <span className="font-mono">{c.comparison}</span> {c.value}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {exitConditions.length > 0 && !bestParams && (
                              <div>
                                <h4 className="text-sm font-semibold text-yellow-600 mb-2">Exit Conditions ({exitConditions.length})</h4>
                                <div className="space-y-1">
                                  {exitConditions.map((rule: any, i: number) => (
                                    <div key={i} className="text-sm bg-yellow-50 dark:bg-yellow-900/20 rounded px-3 py-1.5 text-yellow-800 dark:text-yellow-300">
                                      {rule.name || `Exit Rule ${i + 1}`}: {rule.conditions?.conditions?.map((c: any) => `${c.field} ${c.comparison} ${c.value}`).join(' AND ') || 'N/A'}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {exitConditions.length > 0 && bestParams && (
                              <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
                                <ResolvedRulesetView exitRules={exitConditions} bestParams={bestParams} />
                              </div>
                            )}
                            {buyConditions.length === 0 && sellConditions.length === 0 && exitConditions.length === 0 && optimizedGenes.length > 0 && (
                              <div>
                                <h4 className="text-sm font-semibold text-gray-600 dark:text-gray-300 mb-2">Optimized Parameters ({optimizedGenes.length})</h4>
                                <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                                  {optimizedGenes.map(([k, v]) => (
                                    <div key={k} className="flex justify-between text-sm bg-gray-50 dark:bg-gray-700/50 rounded px-3 py-1.5">
                                      <span className="font-mono text-gray-600 dark:text-gray-400 truncate mr-2">{k}</span>
                                      <span className="font-medium text-gray-900 dark:text-gray-100">{v}</span>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                          </>
                        );
                      })()}
                    </div>
                  )}
                </div>
              </div>
            </>
    );
  }

  return (
    <div className="p-6 space-y-6">
      <TradeChartModal trade={chartTrade} onClose={() => setChartTrade(null)} />
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold flex items-center gap-2 text-gray-900 dark:text-gray-100">
          <BarChart3 className="w-8 h-8 text-blue-500" />
          Backtesting
        </h1>
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
            <AlertCircle className="w-5 h-5" />
            <span>{error}</span>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* Configuration Panel */}
        <div className="xl:col-span-1 space-y-4">
          {/* New Backtest Form */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            {/* Tabs for New Backtest vs Saved Backtests */}
            <div className="flex border-b border-gray-200 dark:border-gray-700 mb-4">
              <button
                onClick={() => setBacktestCardTab('new')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  backtestCardTab === 'new'
                    ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                <Play className="w-4 h-4 inline mr-1" />
                New Backtest
              </button>
              <button
                onClick={() => setBacktestCardTab('history')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  backtestCardTab === 'history'
                    ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                <Clock className="w-4 h-4 inline mr-1" />
                BT History
              </button>
              <button
                onClick={() => setBacktestCardTab('optjobs')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  backtestCardTab === 'optjobs'
                    ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                <Sliders className="w-4 h-4 inline mr-1" />
                Opt History
              </button>
              <button
                onClick={() => setBacktestCardTab('saved')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  backtestCardTab === 'saved'
                    ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                <Save className="w-4 h-4 inline mr-1" />
                Saved ({backtests.filter(bt => bt.isSaved).length})
              </button>
              <button
                onClick={() => setBacktestCardTab('jobs')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  backtestCardTab === 'jobs'
                    ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'
                }`}
              >
                <Activity className="w-4 h-4 inline mr-1" />
                Running
                {runningJobCount > 0 && (
                  <span className="ml-1.5 px-1.5 py-0.5 text-xs rounded-full bg-blue-500 text-white">{runningJobCount}</span>
                )}
              </button>
            </div>

            {backtestCardTab === 'new' ? (
            <div className="space-y-4">
              {/* Import optimization / individual settings (part 5). Accepts the JSON exported
                  from the Opt-History tab (opt-settings OR individual) and pre-fills the form
                  fields below (dates, universe, capital, interval, TP/SL + any mappable params).
                  Also accepts expert_settings_*.json from the live trade platform. */}
              <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/60 p-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs font-medium text-gray-700 dark:text-gray-200">Import settings:</span>
                  <button
                    type="button"
                    onClick={() => importFileRef.current?.click()}
                    className="flex items-center gap-1 px-2.5 py-1 text-sm rounded border border-blue-300 dark:border-blue-600 bg-blue-50 dark:bg-blue-900/50 text-blue-700 dark:text-blue-200 hover:bg-blue-100 dark:hover:bg-blue-800/60"
                  >
                    <Upload className="w-4 h-4" /> Import opt/individual JSON
                  </button>
                  <button
                    type="button"
                    onClick={() => expertSettingsFileRef.current?.click()}
                    className="flex items-center gap-1 px-2.5 py-1 text-sm rounded border border-purple-300 dark:border-purple-600 bg-purple-50 dark:bg-purple-900/50 text-purple-700 dark:text-purple-200 hover:bg-purple-100 dark:hover:bg-purple-800/60"
                  >
                    <Upload className="w-4 h-4" /> Import expert settings
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const txt = window.prompt('Paste exported opt-settings / individual JSON:');
                      if (txt != null && txt.trim()) importSettingsJson(txt);
                    }}
                    className="px-2.5 py-1 text-sm rounded border border-gray-300 dark:border-gray-500 text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700"
                  >
                    Paste JSON
                  </button>
                  <input
                    ref={importFileRef}
                    type="file"
                    accept=".json,application/json"
                    className="hidden"
                    onChange={(e) => { handleImportFile(e.target.files?.[0]); e.currentTarget.value = ''; }}
                  />
                  <input
                    ref={expertSettingsFileRef}
                    type="file"
                    accept=".json,application/json"
                    className="hidden"
                    onChange={(e) => { handleExpertSettingsFile(e.target.files?.[0]); e.currentTarget.value = ''; }}
                  />
                </div>
                {importNote && (
                  <p className={`mt-2 text-xs ${importNote.kind === 'ok'
                    ? 'text-green-600 dark:text-green-400'
                    : 'text-amber-600 dark:text-amber-400'}`}>
                    {importNote.text}
                  </p>
                )}
              </div>

              {/* Source selector: Expert engine vs ML model */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Source
                </label>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setSource('expert')}
                    className={`flex-1 px-3 py-2 text-sm rounded-lg border ${
                      source === 'expert'
                        ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400'
                        : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'
                    }`}
                  >
                    Expert
                  </button>
                  <button
                    type="button"
                    onClick={() => setSource('ml')}
                    className={`flex-1 px-3 py-2 text-sm rounded-lg border ${
                      source === 'ml'
                        ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400'
                        : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300'
                    }`}
                  >
                    ML model
                  </button>
                </div>
              </div>

              {source === 'expert' && (
                <div className="space-y-3">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      <Brain className="w-4 h-4 inline mr-1" />
                      Expert
                    </label>
                    <ExpertPicker value={expertClass} onChange={(cls, info) => { setExpertClass(cls); setExpertBypassesRm(info?.bypasses_classic_rm ?? false); }} />
                  </div>
                  {expertClass && (
                    <CollapsibleSection title="Expert Settings">
                      <ExpertSettingsForm expertClass={expertClass} value={expertSettings} onChange={setExpertSettings} usesRiskManager={!expertBypassesRm} />
                    </CollapsibleSection>
                  )}
                  <CollapsibleSection title="Universe">
                    <UniversePicker value={universe} onChange={setUniverse} />
                  </CollapsibleSection>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Fill model</label>
                      <select
                        value={fillModel}
                        onChange={e => setFillModel(e.target.value)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      >
                        <option value="next_bar_open">Next bar open</option>
                        <option value="same_bar_close">Same bar close</option>
                      </select>
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Seed</label>
                      <input
                        type="number"
                        value={runSeed}
                        onChange={e => setRunSeed(parseInt(e.target.value) || 0)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                  </div>
                  {/* Warmup + run schedule (P1.2). warmup_days is optional (blank => engine default);
                      run_schedule daily/weekly controls how often the expert is invoked. */}
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Warmup days (optional)</label>
                      <input
                        type="number"
                        min={0}
                        step={1}
                        value={warmupDays}
                        placeholder="engine default"
                        onChange={e => setWarmupDays(e.target.value)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Run schedule</label>
                      <select
                        value={runSchedule}
                        onChange={e => setRunSchedule(e.target.value as 'daily' | 'weekly')}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      >
                        <option value="daily">Daily</option>
                        <option value="weekly">Weekly</option>
                      </select>
                    </div>
                    {runSchedule === 'weekly' && (
                      <div>
                        <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Run day (weekly)</label>
                        <select
                          value={runScheduleDay}
                          onChange={e => setRunScheduleDay(e.target.value)}
                          className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                        >
                          {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map(d => (
                            <option key={d} value={d}>{d}</option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {source === 'ml' && (
              <>
              {/* Step 1: Symbol Selection */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  <TrendingUp className="w-4 h-4 inline mr-1" />
                  Symbol
                </label>
                <select
                  value={selectedSymbol}
                  onChange={e => {
                    setSelectedSymbol(e.target.value);
                    setSelectedDatasetId('');
                    setSelectedModels(new Set());
                    setSelectedModel('');
                    setPredictionDatasetId('');
                    setExecutionDatasetId('');
                  }}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                >
                  <option value="">-- Select a symbol --</option>
                  {[...new Set(datasets.map(d => d.ticker))].sort().map(ticker => (
                    <option key={ticker} value={ticker}>{ticker}</option>
                  ))}
                </select>
              </div>

              {/* Step 2: Dataset Selection */}
              {selectedSymbol && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    <Database className="w-4 h-4 inline mr-1" />
                    Dataset
                  </label>
                  <select
                    value={selectedDatasetId}
                    onChange={e => {
                      const dsId = e.target.value ? parseInt(e.target.value) : '';
                      setSelectedDatasetId(dsId);
                      setPredictionDatasetId(dsId);
                      setExecutionDatasetId(dsId);
                      setSelectedModels(new Set());
                      setSelectedModel('');
                    }}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  >
                    <option value="">-- Select a dataset --</option>
                    {datasets.filter(d => d.ticker === selectedSymbol).map(ds => (
                      <option key={ds.id} value={ds.id}>
                        {ds.name} ({ds.timeframe})
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {/* Step 3: Model Selection */}
              {selectedDatasetId !== '' && (() => {
                const datasetModels = models.filter(m => m.datasetId === selectedDatasetId);
                const allCompatibleModels = showAllModels
                  ? models.filter(m => m.symbol === selectedSymbol)
                  : datasetModels;
                return (
                  <div>
                    <div className="flex items-center justify-between mb-2">
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                        <Brain className="w-4 h-4 inline mr-1" />
                        Models ({allCompatibleModels.length})
                      </label>
                      <label className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={showAllModels}
                          onChange={e => {
                            setShowAllModels(e.target.checked);
                            setSelectedModels(new Set());
                            setSelectedModel('');
                          }}
                          className="rounded"
                        />
                        Show all models for {selectedSymbol}
                      </label>
                    </div>
                    {allCompatibleModels.length === 0 ? (
                      <p className="text-sm text-gray-400 dark:text-gray-500 italic">No models found.</p>
                    ) : (
                      <div className="space-y-1 max-h-48 overflow-y-auto border border-gray-200 dark:border-gray-600 rounded-lg p-2">
                        {allCompatibleModels.map(model => {
                          const isFromDataset = model.datasetId === selectedDatasetId;
                          return (
                            <label
                              key={model.id}
                              className={`flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 ${
                                selectedModels.has(model.id) ? 'bg-blue-50 dark:bg-blue-900/20' : ''
                              }`}
                            >
                              <input
                                type="checkbox"
                                checked={selectedModels.has(model.id)}
                                onChange={e => {
                                  const next = new Set(selectedModels);
                                  if (e.target.checked) next.add(model.id);
                                  else next.delete(model.id);
                                  setSelectedModels(next);
                                  // Keep selectedModel in sync (use first selected)
                                  const arr = [...next];
                                  setSelectedModel(arr[0] || '');
                                }}
                                className="rounded"
                              />
                              <span className="flex-1 text-sm text-gray-800 dark:text-gray-200">{model.name}</span>
                              <span className="text-xs text-gray-400 dark:text-gray-500">{model.modelType}</span>
                              {!isFromDataset && (
                                <span className="text-xs px-1.5 py-0.5 bg-amber-100 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 rounded">other dataset</span>
                              )}
                            </label>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* Execution Dataset (override) */}
              {selectedDatasetId !== '' && (
                <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
                  <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">
                    Execution Dataset (for price simulation)
                  </label>
                  <select
                    value={executionDatasetId}
                    onChange={e => setExecutionDatasetId(e.target.value ? parseInt(e.target.value) : '')}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  >
                    {datasets.filter(d => d.ticker === selectedSymbol).map(ds => (
                      <option key={ds.id} value={ds.id}>
                        {ds.name} ({ds.timeframe})
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Defaults to the selected dataset above</p>
                </div>
              )}
              </>
              )}

              {/* Date Range */}
              <CollapsibleSection title="Date Range" icon={<Calendar className="w-4 h-4 text-gray-500" />}>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    <Calendar className="w-4 h-4 inline mr-1" />
                    Start Date
                  </label>
                  <input
                    type="date"
                    value={startDate}
                    onChange={e => setStartDate(e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    End Date
                  </label>
                  <input
                    type="date"
                    value={endDate}
                    onChange={e => setEndDate(e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  />
                </div>
              </div>
              </CollapsibleSection>

              {/* Strategy (load + conditions) — one foldable block */}
              <CollapsibleSection title="Strategy" icon={<Layers className="w-4 h-4 text-purple-500" />}>
                {/* Load Strategy Dropdown */}
                <div className="flex items-center gap-2 mb-3">
                  <div className="relative flex-1">
                    <select
                      value=""
                      onChange={e => {
                        const stratId = parseInt(e.target.value);
                        const strat = strategies.find(s => s.id === stratId);
                        if (strat) loadStrategy(strat);
                      }}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                    >
                      <option value="">Load from saved strategy...</option>
                      {strategies.map(strat => (
                        <option key={strat.id} value={strat.id}>
                          {strat.name}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

              {/* Entry/Exit Condition Buttons */}
              <div className="space-y-2 border border-gray-200 dark:border-gray-700 rounded-lg p-3">
                  <div className="flex items-center justify-between mb-2">
                    <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                      Strategy Conditions
                    </h4>
                    {/* Allow short: when off the strategy is long-only (Entry/Exit). When on, the
                        Short Entry Conditions block is shown and its tree is sent. */}
                    <label className="flex items-center gap-1.5 text-xs font-medium text-gray-600 dark:text-gray-300 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={allowShort}
                        onChange={(e) => setAllowShort(e.target.checked)}
                        className="rounded border-gray-300 dark:border-gray-600 text-red-600 focus:ring-red-500"
                      />
                      Allow short
                    </label>
                  </div>

                  {/* Import from a LIVE expert instance (B8). The backtest UI picks an expert CLASS,
                      so the live INSTANCE id is collected here. Loads buy/sell entry trees + exit
                      rules into the editor (then editable + previewed below). Graceful on 503/error:
                      falls back to the Import JSON buttons on each rule section. */}
                  <div className="mb-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-700/40 p-2">
                    <div className="flex items-center gap-2">
                      <input
                        type="number"
                        min={1}
                        step={1}
                        value={liveExpertId}
                        onChange={(e) => setLiveExpertId(e.target.value)}
                        placeholder="Live expert id"
                        className="w-32 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
                      />
                      <button
                        type="button"
                        onClick={handleImportFromLive}
                        disabled={liveImporting}
                        className="flex items-center gap-1 px-3 py-1 text-sm rounded border border-blue-300 dark:border-blue-700 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-900/50 disabled:opacity-50"
                      >
                        {liveImporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Database className="w-4 h-4" />}
                        Import from live
                      </button>
                      {/* Import-JSON ruleset (#159): load buy/sell enter trees + exit rules from a
                          ruleset JSON file (normalized snake->camel) into the builders below. */}
                      <button
                        type="button"
                        onClick={() => rulesetFileRef.current?.click()}
                        className="flex items-center gap-1 px-3 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                      >
                        <Upload className="w-4 h-4" />
                        Import JSON
                      </button>
                      <input
                        ref={rulesetFileRef}
                        type="file"
                        accept=".json,application/json"
                        className="hidden"
                        onChange={(e) => { handleRulesetFile(e.target.files?.[0]); e.currentTarget.value = ''; }}
                      />
                    </div>
                    {liveImportNote && (
                      <p
                        className={`mt-2 text-xs ${liveImportNote.kind === 'ok'
                          ? 'text-green-600 dark:text-green-400'
                          : 'text-amber-600 dark:text-amber-400'}`}
                      >
                        {liveImportNote.text}
                      </p>
                    )}
                  </div>

                  <button
                    onClick={() => setShowConditionModal('buy')}
                    className="flex items-center gap-2 text-sm font-semibold text-white w-full p-2 bg-green-700 hover:bg-green-800 rounded-lg border border-green-800 shadow-sm"
                  >
                    <TrendingUp className="w-4 h-4 text-white" />
                    <span className="flex-1 text-left">Entry Conditions</span>
                    <span className="text-xs font-medium text-green-100">{buyEntryConditions.conditions.length} condition{buyEntryConditions.conditions.length !== 1 ? 's' : ''}</span>
                    <ChevronDown className="w-4 h-4 text-white" />
                  </button>
                  <div className="flex justify-end gap-1 text-xs">
                    <RuleIO
                      which="enter"
                      tree={buyEntryConditions}
                      onImport={(tree) => { if (isConditionGroup(tree)) setBuyEntryConditions(tree); }}
                    />
                  </div>
                  {allowShort && (
                    <>
                      <button
                        onClick={() => setShowConditionModal('sell')}
                        className="flex items-center gap-2 text-sm font-semibold text-white w-full p-2 bg-red-700 hover:bg-red-800 rounded-lg border border-red-800 shadow-sm"
                      >
                        <TrendingDown className="w-4 h-4 text-white" />
                        <span className="flex-1 text-left">Short Entry Conditions</span>
                        <span className="text-xs font-medium text-red-100">{sellEntryConditions.conditions.length} condition{sellEntryConditions.conditions.length !== 1 ? 's' : ''}</span>
                        <ChevronDown className="w-4 h-4 text-white" />
                      </button>
                      <div className="flex justify-end gap-1 text-xs">
                        <RuleIO
                          which="enter"
                          tree={sellEntryConditions}
                          onImport={(tree) => { if (isConditionGroup(tree)) setSellEntryConditions(tree); }}
                        />
                      </div>
                    </>
                  )}
                  <button
                    onClick={() => setShowConditionModal('exit')}
                    className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 w-full p-2 bg-gray-50 dark:bg-gray-700/50 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg border border-gray-200 dark:border-gray-600"
                  >
                    <Target className="w-4 h-4 text-orange-500" />
                    <span className="flex-1 text-left">Exit Conditions</span>
                    <span className="text-xs text-gray-500">{exitConditions.length} rule{exitConditions.length !== 1 ? 's' : ''}</span>
                    <ChevronDown className="w-4 h-4 text-gray-400" />
                  </button>
                  <div className="flex justify-end gap-1 text-xs">
                    <RuleIO
                      which="exit"
                      tree={exitConditions[0]?.conditions ?? createEmptyGroup('AND')}
                      onImport={(tree) => {
                        if (!isConditionGroup(tree)) return;
                        setExitConditions(prev => {
                          if (prev.length === 0) {
                            return [{ id: `exit-${Date.now()}`, name: 'Exit Rule 1', conditions: tree, action: 'close' }];
                          }
                          const next = [...prev];
                          next[0] = { ...next[0], conditions: tree };
                          return next;
                        });
                      }}
                    />
                  </div>

                  {/* Live optimizer gene-count / search-space preview — recomputes from the
                      same buy/sell/exit state, so it updates as Optimize toggles change. */}
                  <GeneCountPreview
                    buyTree={buyEntryConditions}
                    sellTree={allowShort ? sellEntryConditions : createEmptyGroup('AND')}
                    exitRules={exitConditions}
                  />
                </div>
              </CollapsibleSection>


              {/* Save Strategy + Optimize (single joint, or batch across experts) */}
              <div className="flex items-center gap-2 border-t border-gray-200 dark:border-gray-700 pt-4">
                <Tooltip content="Save current strategy configuration for later use">
                  <button
                    onClick={() => setShowSaveDialog(true)}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-700 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
                  >
                    <Save className="w-4 h-4" />
                    Save Strategy
                  </button>
                </Tooltip>
                {/* One Optimize button. The "across multiple experts" toggle selects which existing
                    flow it fires: OFF -> single joint-optimization dialog (runOptimization path);
                    ON -> multi-expert batch dialog (optimizeBatch path). Both paths are unchanged. */}
                <Tooltip content={loadedStrategyId == null
                  ? 'Load or save a strategy first to optimize it'
                  : optimizeAcrossExperts
                    ? 'Launch one optimization per selected expert against this strategy'
                    : `Run joint genetic optimization for "${loadedStrategyName}"`}>
                  <button
                    onClick={() => {
                      if (optimizeAcrossExperts) {
                        setBatchNotice(null);
                        setBatchJobs([]);
                        setShowBatchDialog(true);
                      } else {
                        setOptNotice(null);
                        setShowOptimizeDialog(true);
                      }
                    }}
                    disabled={loadedStrategyId == null}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-white bg-amber-500 rounded-lg hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {optimizeAcrossExperts ? <Layers className="w-4 h-4" /> : <Sliders className="w-4 h-4" />}
                    {optimizeAcrossExperts ? 'Optimize Batch' : 'Optimize'}
                  </button>
                </Tooltip>
                <label className="flex items-center gap-1.5 text-xs font-medium text-gray-600 dark:text-gray-300 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={optimizeAcrossExperts}
                    onChange={(e) => setOptimizeAcrossExperts(e.target.checked)}
                    className="rounded border-gray-300 dark:border-gray-600 text-indigo-600 focus:ring-indigo-500"
                  />
                  across multiple experts
                </label>
              </div>

              {/* Advanced Options Toggle */}
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
              >
                <Settings className="w-4 h-4" />
                Backtest Settings
                {showAdvanced ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
              </button>

              {/* Advanced Options */}
              {showAdvanced && (
                <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-3">
                  <div>
                    <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Initial Capital</label>
                    <input
                      type="number"
                      min="1000"
                      step="1000"
                      value={initialCapital}
                      onChange={e => setInitialCapital(parseFloat(e.target.value))}
                      className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    />
                  </div>

                  {source === 'expert' ? (
                    <>
                      <div>
                        <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Run interval (bar size)</label>
                        <select
                          value={executionInterval}
                          onChange={e => setExecutionInterval(e.target.value)}
                          className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                        >
                          <option value="5m">5 min — default</option>
                          <option value="15m">15 min</option>
                          <option value="30m">30 min</option>
                          <option value="1h">Hourly (1h)</option>
                          <option value="1d">Daily (1d)</option>
                        </select>
                        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Simulation bar size. Intraday needs intraday OHLCV history and is slower; pick 1d for daily/fundamental experts.</p>
                      </div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-700/40 border border-gray-200 dark:border-gray-700 rounded p-2">
                        Position sizing is governed by the expert's risk manager (its <code>sizing_mode</code> / <code>risk_per_trade_pct</code> in Expert Settings), not here.
                      </div>
                    </>
                  ) : (
                    <>
                      <div>
                        <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Position Sizing</label>
                        <select
                          value={positionSizingType}
                          onChange={e => setPositionSizingType(e.target.value)}
                          className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                        >
                          <option value="fixed">Fixed Amount</option>
                          <option value="percent">Percent of Capital</option>
                        </select>
                      </div>

                      <div>
                        <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">
                          {positionSizingType === 'percent' ? 'Position %' : 'Position Size ($)'}
                        </label>
                        <input
                          type="number"
                          min="0"
                          step={positionSizingType === 'percent' ? '1' : '100'}
                          value={positionSizingValue}
                          onChange={e => setPositionSizingValue(parseFloat(e.target.value))}
                          className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                        />
                      </div>
                    </>
                  )}

                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Commission %</label>
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        value={commission}
                        onChange={e => setCommission(parseFloat(e.target.value))}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Slippage %</label>
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        value={slippage}
                        onChange={e => setSlippage(parseFloat(e.target.value))}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                  </div>
                </div>
              )}

              {/* Run Button */}
              <button
                onClick={runBacktest}
                disabled={running || (source === 'expert'
                  ? (!expertClass || (universe.mode === 'static' && universe.symbols.length === 0))
                  : (selectedModels.size === 0 || !predictionDatasetId || !executionDatasetId))}
                className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-green-500 text-white font-medium rounded-lg hover:bg-green-600 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {running ? (
                  <>
                    <Loader2 className="w-5 h-5 animate-spin" />
                    Running...
                  </>
                ) : (
                  <>
                    <Play className="w-5 h-5" />
                    {selectedModels.size > 1 ? `Run ${selectedModels.size} Backtests` : 'Run Backtest'}
                  </>
                )}
              </button>
            </div>
            ) : backtestCardTab === 'history' ? (
              /* History Tab — all runs (fills the viewport height) */
              <div className="h-[calc(100vh-15rem)] overflow-y-auto pr-4 [scrollbar-gutter:stable]">
                <RunHistoryTable savedOnly={false} onSelect={viewBacktest} onLoad={loadBacktestIntoForm} />
              </div>
            ) : backtestCardTab === 'jobs' ? (
              /* Running Jobs Tab — live per-generation + total progress */
              <div className="h-[calc(100vh-15rem)] overflow-y-auto pr-4 [scrollbar-gutter:stable]">
                <RunningJobsPanel />
              </div>
            ) : backtestCardTab === 'optjobs' ? (
              /* Opt History Tab — 2 areas: jobs table (top) + selected job's saved backtests
                 (bottom). Selecting a job shows its settings + top individuals on the RIGHT;
                 selecting a saved backtest loads its full result on the RIGHT. */
              <div className="h-[calc(100vh-15rem)] overflow-y-auto pr-4 [scrollbar-gutter:stable]">
                <OptimizationJobsTable
                  selectedJobId={selectedOptJob?.job.id ?? null}
                  onSelectJob={(job, detail) => {
                    // Reset the sub-tab + individual selection only when a DIFFERENT job is
                    // picked — OptimizationJobsTable re-fires this (detail=undefined then loaded)
                    // for the same job as it lazily fetches the top individuals.
                    const isNewJob = selectedOptJob?.job.id !== job.id;
                    if (isNewJob) {
                      setSelectedBacktest(null);
                      setSelectedIndividual(null);
                      setIndividualNoBacktest(null);
                      setOptSubTab('optimization');
                    }
                    setSelectedOptJob({ job, detail });
                  }}
                  onSelectBacktest={viewBacktest}
                />
              </div>
            ) : (
              /* Saved Backtests Tab — fills the viewport height like History */
              <div className="h-[calc(100vh-15rem)] overflow-y-auto pr-4 [scrollbar-gutter:stable]">
                <RunHistoryTable savedOnly={true} onSelect={viewBacktest} onLoad={loadBacktestIntoForm} />
              </div>
            )}
          </div>
        </div>

        {/* Results Panel */}
        <div className="xl:col-span-1 space-y-4">
          {selectedOptJob ? (
            /* Opt-History: 2 sub-tabs — "Optimization" (settings + top individuals) and
               "Individual Backtest" (the selected top individual's full backtest result). */
            <div className="space-y-4">
              <div className="flex items-start justify-between flex-wrap gap-2">
                <div className="min-w-0">
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
                    {selectedOptJob.job.name || `Optimization #${selectedOptJob.job.id}`}
                  </h3>
                  <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">
                    Optimization job #{selectedOptJob.job.id}
                    {selectedOptJob.job.fitnessMetric ? ` · ${selectedOptJob.job.fitnessMetric}` : ''}
                    {selectedOptJob.job.bestFitness != null ? ` · best ${selectedOptJob.job.bestFitness.toFixed(4)}` : ''}
                  </p>
                </div>
                <span className="px-2 py-0.5 text-xs font-semibold rounded-full border bg-indigo-100 text-indigo-800 border-indigo-300 dark:bg-indigo-500/20 dark:text-indigo-200 dark:border-indigo-500/40">
                  {selectedOptJob.job.status}
                </span>
              </div>

              {/* Sub-tab bar */}
              <div className="flex border-b border-gray-200 dark:border-gray-700">
                <button
                  onClick={() => setOptSubTab('optimization')}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    optSubTab === 'optimization'
                      ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                      : 'border-transparent text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200'
                  }`}
                >
                  <Sliders className="w-4 h-4 inline mr-1" />
                  Optimization
                </button>
                <button
                  onClick={() => setOptSubTab('individual')}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    optSubTab === 'individual'
                      ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                      : 'border-transparent text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200'
                  }`}
                >
                  <BarChart3 className="w-4 h-4 inline mr-1" />
                  Individual Backtest
                </button>
              </div>

              {optSubTab === 'optimization' ? (
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4 space-y-4">
                  <div className="flex items-center justify-between gap-2">
                    <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-1">
                      <Sliders className="w-4 h-4" /> Optimization settings
                    </h4>
                    <button
                      type="button"
                      onClick={() => exportOptSettings(selectedOptJob.job, selectedOptJob.detail)}
                      title="Download this optimization's settings as JSON"
                      className="flex items-center gap-1 px-2.5 py-1 text-xs font-medium border border-gray-300 dark:border-gray-600 rounded text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
                    >
                      <Download className="w-3.5 h-3.5" /> Export settings
                    </button>
                  </div>
                  <OptJobSettingsDetail s={selectedOptJob.job.settings} />

                  <div>
                    <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center gap-1">
                      <Award className="w-4 h-4" /> Top individuals
                    </h4>
                    {selectedOptJob.detail === undefined ? (
                      <div className="text-xs text-gray-500 dark:text-gray-400">Loading…</div>
                    ) : (
                      <TopIndividualsTable
                        individuals={selectedOptJob.detail.topIndividuals}
                        fitnessMetric={selectedOptJob.job.fitnessMetric ?? undefined}
                        selectedRank={selectedIndividual?.rank ?? null}
                        onSelect={selectTopIndividual}
                        onExport={(ind) => exportIndividual(selectedOptJob.job, ind, selectedOptJob.detail)}
                        note="Click a row to load that individual's full backtest in the Individual Backtest tab. Only the top ~5 are saved as full backtests."
                      />
                    )}
                  </div>
                </div>
              ) : selectedBacktest ? (
                renderBacktestResult(selectedBacktest)
              ) : individualNoBacktest ? (
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4 space-y-3">
                  <div className="text-sm text-gray-700 dark:text-gray-300">
                    Only the top N individuals are saved as full backtests; this one
                    (#{individualNoBacktest.rank}) wasn't — its params are shown below.
                  </div>
                  <div className="flex items-center justify-between">
                    <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                      Individual #{individualNoBacktest.rank} params
                    </h4>
                    <button
                      type="button"
                      onClick={() => exportIndividual(selectedOptJob.job, individualNoBacktest, selectedOptJob.detail)}
                      className="flex items-center gap-1 px-2.5 py-1 text-xs font-medium border border-gray-300 dark:border-gray-600 rounded text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
                    >
                      <Download className="w-3.5 h-3.5" /> Export
                    </button>
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                    {Object.entries(individualNoBacktest.params ?? {}).map(([k, v]) => (
                      <div key={k} className="flex justify-between text-sm bg-gray-50 dark:bg-gray-700/50 rounded px-3 py-1.5">
                        <span className="font-mono text-gray-700 dark:text-gray-300 truncate mr-2">{k}</span>
                        <span className="font-medium text-gray-900 dark:text-gray-100">
                          {typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-8 text-center">
                  <BarChart3 className="w-16 h-16 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
                  <h3 className="text-lg font-medium text-gray-700 dark:text-gray-300 mb-2">No individual selected</h3>
                  <p className="text-sm text-gray-600 dark:text-gray-400">
                    Pick a row in the Top Individuals table (Optimization tab) to view its full backtest here.
                  </p>
                </div>
              )}
            </div>
          ) : selectedBacktest ? (
            renderBacktestResult(selectedBacktest)
          ) : (
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-8 text-center">
              <BarChart3 className="w-16 h-16 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
              <h3 className="text-lg font-medium text-gray-600 dark:text-gray-400 mb-2">No Backtest Selected</h3>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Configure and run a new backtest, or select a previous backtest to view results.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Save Strategy Dialog */}
      {showSaveDialog && (
        <div className="fixed inset-0 z-50 overflow-y-auto">
          <div
            className="fixed inset-0 bg-black bg-opacity-50 transition-opacity"
            onClick={() => setShowSaveDialog(false)}
          />
          <div className="flex min-h-full items-center justify-center p-4">
            <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
              <button
                onClick={() => setShowSaveDialog(false)}
                className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                <X className="w-5 h-5" />
              </button>

              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
                <Save className="w-5 h-5 text-blue-500" />
                Save Strategy
              </h3>

              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Strategy Name *
                  </label>
                  <input
                    type="text"
                    value={saveStrategyName}
                    onChange={e => setSaveStrategyName(e.target.value)}
                    placeholder="e.g., Conservative Trend Follower"
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    autoFocus
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Description (optional)
                  </label>
                  <textarea
                    value={saveStrategyDescription}
                    onChange={e => setSaveStrategyDescription(e.target.value)}
                    placeholder="Describe the strategy approach..."
                    rows={3}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 resize-none"
                  />
                </div>

                <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-3 text-sm">
                  <p className="font-medium text-gray-700 dark:text-gray-300 mb-2">Current Configuration:</p>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-gray-600 dark:text-gray-400">
                    <span>Entry conditions: {buyEntryConditions.conditions.length}</span>
                    {allowShort && <span>Short conditions: {sellEntryConditions.conditions.length}</span>}
                    <span>Exit rules: {exitConditions.length}</span>
                    <span>TP: {initialTpPercent}% / SL: {initialSlPercent}%</span>
                  </div>
                </div>
              </div>

              <div className="flex justify-end gap-3 mt-6">
                <button
                  onClick={() => setShowSaveDialog(false)}
                  className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600"
                >
                  Cancel
                </button>
                <button
                  onClick={saveStrategy}
                  disabled={savingStrategy || !saveStrategyName.trim()}
                  className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                >
                  {savingStrategy ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Saving...
                    </>
                  ) : (
                    <>
                      <Save className="w-4 h-4" />
                      Save
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Run Joint Optimization Dialog */}
      {showOptimizeDialog && (
        <div className="fixed inset-0 z-50 overflow-y-auto">
          <div
            className="fixed inset-0 bg-black bg-opacity-50 transition-opacity"
            onClick={() => setShowOptimizeDialog(false)}
          />
          <div className="flex min-h-full items-center justify-center p-4">
            <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full p-6">
              <button
                onClick={() => setShowOptimizeDialog(false)}
                className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                <X className="w-5 h-5" />
              </button>

              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center gap-2">
                <Sliders className="w-5 h-5 text-amber-500" />
                Run Joint Optimization
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
                Optimizes the saved strategy "{loadedStrategyName}" over its enabled
                expert / RM / TP-SL / condition ranges, scored by one backtest metric.
              </p>

              {optNotice && (
                <div className="mb-3 text-sm text-red-600 dark:text-red-400 flex items-center gap-2">
                  <AlertCircle className="w-4 h-4" />
                  {optNotice}
                </div>
              )}

              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Fitness Metric</label>
                    <select
                      value={optFitnessMetric}
                      onChange={e => setOptFitnessMetric(e.target.value)}
                      className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    >
                      {FITNESS_METRICS.map(m => (
                        <option key={m.value} value={m.value}>{m.label}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Search</label>
                    <select
                      value={optType}
                      onChange={e => setOptType(e.target.value as 'genetic' | 'brute_force')}
                      className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                    >
                      <option value="genetic">Genetic</option>
                      <option value="brute_force">Brute Force</option>
                    </select>
                  </div>
                </div>

                <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-3">
                  <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Genetic Algorithm
                  </h4>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Population Size</label>
                      <input
                        type="number" min="2" step="1" value={optPopulationSize}
                        onChange={e => setOptPopulationSize(parseInt(e.target.value) || 0)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Generations</label>
                      <input
                        type="number" min="1" step="1" value={optGenerations}
                        onChange={e => setOptGenerations(parseInt(e.target.value) || 0)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Crossover Prob</label>
                      <input
                        type="number" min="0" max="1" step="0.05" value={optCrossoverProb}
                        onChange={e => setOptCrossoverProb(parseFloat(e.target.value))}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Mutation Prob</label>
                      <input
                        type="number" min="0" max="1" step="0.05" value={optMutationProb}
                        onChange={e => setOptMutationProb(parseFloat(e.target.value))}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Early Stopping</label>
                      <input
                        type="number" min="1" step="1" value={optEarlyStopping}
                        onChange={e => setOptEarlyStopping(parseInt(e.target.value) || 0)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Elitism %</label>
                      <input
                        type="number" min="0" max="100" step="1" value={optElitismPercent}
                        onChange={e => setOptElitismPercent(parseFloat(e.target.value))}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Seed (determinism)</label>
                      <input
                        type="number" step="1" value={optSeed}
                        onChange={e => setOptSeed(parseInt(e.target.value) || 0)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                  </div>
                </div>

                {/* Screener-settings optimization (P1.4). Shown when the universe is a screener
                    with at least one metric range toggled to Opt — an OPTIONAL metric-store path
                    override (blank => backend default under the shared cache folder) + the rebuild
                    cadence, sent as screener_opt. */}
                {universe.mode === 'screener'
                  && universe.screener_param_ranges
                  && Object.values(universe.screener_param_ranges).some(r => r.optimize) && (
                  <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-3">
                    <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                      Screener optimization
                    </h4>
                    <div>
                      <label className="block text-xs text-gray-600 dark:text-gray-400 mb-1">Screener metric-store path <span className="text-gray-400 dark:text-gray-500">(optional — defaults to the shared cache folder)</span></label>
                      <input
                        type="text"
                        value={screenerStore}
                        onChange={e => setScreenerStore(e.target.value)}
                        placeholder="default: <cache>/screener/metric_store"
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Universe rebuild cadence (days)</label>
                      <input
                        type="number" min="1" step="1" value={screenerCadenceDays}
                        onChange={e => setScreenerCadenceDays(parseInt(e.target.value) || 1)}
                        className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                    </div>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      {Object.values(universe.screener_param_ranges).filter(r => r.optimize).length} screener metric range(s) will be optimized.
                    </p>
                  </div>
                )}

                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Backtest window {startDate} → {endDate}, capital ${initialCapital.toLocaleString()},
                  engine {selectedModel ? 'ML (model-driven)' : 'daily expert (multi-asset)'}.
                  Adjust these in the New Backtest form before launching.
                </p>
              </div>

              <div className="flex justify-end gap-3 mt-6">
                <button
                  onClick={() => setShowOptimizeDialog(false)}
                  className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600"
                >
                  Cancel
                </button>
                <button
                  onClick={runOptimization}
                  disabled={launchingOpt}
                  className="px-4 py-2 bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                >
                  {launchingOpt ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Launching...
                    </>
                  ) : (
                    <>
                      <Play className="w-4 h-4" />
                      Launch
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Optimize-Batch dialog (P3.8) */}
      {showBatchDialog && (
        <div className="fixed inset-0 z-50 overflow-y-auto">
          <div className="fixed inset-0 bg-black bg-opacity-50" onClick={() => setShowBatchDialog(false)} />
          <div className="flex min-h-full items-center justify-center p-4">
            <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full p-6">
              <button onClick={() => setShowBatchDialog(false)}
                className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                <X className="w-5 h-5" />
              </button>
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-1 flex items-center gap-2">
                <Layers className="w-5 h-5 text-indigo-500" />
                Optimize Batch
              </h3>
              <p className="text-xs text-gray-600 dark:text-gray-400 mb-4">
                Launches one optimization per selected expert against "{loadedStrategyName}" using the GA
                config + fitness metric below (set in the Run Joint Optimization dialog).
              </p>
              {batchNotice && (
                <div className="mb-3 text-sm text-amber-600 dark:text-amber-400 flex items-center gap-2">
                  <AlertCircle className="w-4 h-4" /> {batchNotice}
                </div>
              )}
              <div className="grid grid-cols-2 gap-3 mb-3">
                <div>
                  <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Fitness Metric</label>
                  <select value={optFitnessMetric} onChange={e => setOptFitnessMetric(e.target.value)}
                    className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100">
                    {FITNESS_METRICS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Search</label>
                  <select value={optType} onChange={e => setOptType(e.target.value as 'genetic' | 'brute_force')}
                    className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100">
                    <option value="genetic">Genetic</option>
                    <option value="brute_force">Brute Force</option>
                  </select>
                </div>
              </div>
              <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-2 max-h-56 overflow-y-auto mb-3">
                {batchExperts.length === 0 ? (
                  <p className="text-sm text-gray-500 dark:text-gray-400 p-2">Loading experts…</p>
                ) : batchExperts.map(ex => (
                  <label key={ex.class} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer">
                    <input type="checkbox" className="rounded" checked={batchSelected.has(ex.class)}
                      onChange={e => {
                        const next = new Set(batchSelected);
                        if (e.target.checked) next.add(ex.class); else next.delete(ex.class);
                        setBatchSelected(next);
                      }} />
                    <span className="flex-1 text-sm text-gray-800 dark:text-gray-200">{ex.label}</span>
                    {ex.bypasses_classic_rm && (
                      <span className="text-xs px-1.5 py-0.5 bg-amber-100 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 rounded">bypass RM</span>
                    )}
                  </label>
                ))}
              </div>
              {batchJobs.length > 0 && (
                <div className="mb-3 text-xs text-gray-700 dark:text-gray-300 space-y-1">
                  {batchJobs.map(j => (
                    <div key={j.optimizationId} className="flex justify-between bg-gray-50 dark:bg-gray-700/50 rounded px-2 py-1">
                      <span className="font-medium">{j.expert}</span>
                      <span className="text-gray-500 dark:text-gray-400">#{j.optimizationId} · task {j.taskId}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex justify-end gap-3">
                <button onClick={() => setShowBatchDialog(false)}
                  className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600">
                  Close
                </button>
                <button onClick={runBatchOptimization} disabled={batchLaunching || batchSelected.size === 0}
                  className="px-4 py-2 bg-indigo-500 text-white rounded-lg hover:bg-indigo-600 disabled:opacity-50 flex items-center gap-2">
                  {batchLaunching ? <><Loader2 className="w-4 h-4 animate-spin" /> Launching…</> : <><Play className="w-4 h-4" /> Launch {batchSelected.size || ''}</>}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Condition Builder Modal */}
      {showConditionModal && (
        <div className="fixed inset-0 z-50 overflow-y-auto">
          <div
            className="fixed inset-0 bg-black bg-opacity-50 transition-opacity"
            onClick={() => setShowConditionModal(null)}
          />
          <div className="flex min-h-full items-center justify-center p-4">
            <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-3xl w-full max-h-[80vh] flex flex-col">
              <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                  {showConditionModal === 'buy' && (
                    <>
                      <TrendingUp className="w-5 h-5 text-green-500" />
                      Entry Conditions
                    </>
                  )}
                  {showConditionModal === 'sell' && (
                    <>
                      <TrendingDown className="w-5 h-5 text-red-500" />
                      Short Entry Conditions
                    </>
                  )}
                  {showConditionModal === 'exit' && (
                    <>
                      <Target className="w-5 h-5 text-orange-500" />
                      Exit Conditions
                    </>
                  )}
                </h3>
                <button
                  onClick={() => setShowConditionModal(null)}
                  className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="p-4 overflow-y-auto flex-1">
                {/* Model Info: Targets and Threshold */}
                {selectedModel && (() => {
                  const model = models.find(m => m.id === selectedModel);
                  const targets = model?.predictionTargets || [];
                  const threshold = model?.threshold ?? 0.5;
                  const horizon = model?.predictionHorizon;

                  return (
                    <div className="mb-4 space-y-3">
                      {/* Prediction Targets Info */}
                      {targets.length > 0 && (
                        <div className="p-3 bg-purple-50 dark:bg-purple-900/20 rounded-lg border border-purple-200 dark:border-purple-800">
                          <p className="text-xs font-semibold text-purple-700 dark:text-purple-300 mb-2 flex items-center gap-1">
                            <Target className="w-3 h-3" />
                            Prediction Targets
                            {horizon !== undefined && (
                              <span className="ml-2 font-normal text-purple-600 dark:text-purple-400">
                                (Horizon: {horizon} bar{horizon !== 1 ? 's' : ''})
                              </span>
                            )}
                          </p>
                          <div className="space-y-2">
                            {targets.map((target, idx) => {
                              const targetType = target.type || 'unknown';
                              const category = target.category || 'binary_classification';

                              // Build readable description
                              let description = '';
                              if (targetType === 'directional') {
                                description = `${target.direction === 'up' ? 'Price Up' : 'Price Down'} prediction`;
                              } else if (targetType === 'price_based') {
                                description = `Profit ${target.profitPct}%${target.maxDd ? `, Max DD ${target.maxDd}%` : ''}`;
                              } else if (targetType === 'trend_reversal') {
                                description = `${target.indicator || 'Indicator'} ${target.indicatorType || 'reversal'}`;
                              } else {
                                // Show raw properties for other types
                                const props = Object.entries(target)
                                  .filter(([k]) => !['type', 'category', 'enabled', 'color'].includes(k))
                                  .map(([k, v]) => `${k}: ${v}`)
                                  .join(', ');
                                description = props || targetType;
                              }

                              return (
                                <div key={idx} className="flex items-center gap-2 text-xs">
                                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                                    category === 'binary_classification' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300' :
                                    category === 'multiclass_classification' ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300' :
                                    'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
                                  }`}>
                                    {targetType.replace(/_/g, ' ')}
                                  </span>
                                  <span className="text-purple-600 dark:text-purple-400">
                                    {description}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}

                      {/* Model Threshold Info */}
                      <div className="p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-200 dark:border-blue-800">
                        <p className="text-xs text-blue-700 dark:text-blue-300">
                          <strong>Model Threshold:</strong> {threshold.toFixed(2)}
                        </p>
                        <p className="text-xs text-blue-600 dark:text-blue-400 mt-1">
                          <strong>Prediction</strong> fields use this threshold (Prediction = 1 when Probability ≥ {(threshold * 100).toFixed(0)}%).
                          Use <strong>Probability</strong> fields for custom thresholds.
                        </p>
                      </div>
                    </div>
                  );
                })()}

                {showConditionModal === 'buy' && (
                  <div>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
                      Define conditions for opening a long (buy) position. All conditions in a group must be met.
                    </p>
                    <ConditionBuilder
                      value={buyEntryConditions}
                      onChange={(val) => {
                        if (isConditionGroup(val)) {
                          setBuyEntryConditions(val);
                        }
                      }}
                      availableFields={availableFields}
                      showOptimization={true}
                      // Expert-source entry conditions ARE expert events (confidence/bullish/…), so
                      // give the builder the ruleset vocabulary — otherwise an imported live rule's
                      // field has no <option> and the dropdown renders blank. ML source keeps the
                      // prediction-field-only boundary.
                      vocabulary={source === 'expert' ? rulesetVocabulary : undefined}
                    />
                  </div>
                )}

                {showConditionModal === 'sell' && (
                  <div>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
                      Define conditions for opening a short (sell) position. All conditions in a group must be met.
                    </p>
                    <ConditionBuilder
                      value={sellEntryConditions}
                      onChange={(val) => {
                        if (isConditionGroup(val)) {
                          setSellEntryConditions(val);
                        }
                      }}
                      availableFields={availableFields}
                      showOptimization={true}
                      vocabulary={source === 'expert' ? rulesetVocabulary : undefined}
                    />
                  </div>
                )}

                {showConditionModal === 'exit' && (
                  <div>
                    <div className="flex items-center justify-between mb-4">
                      <p className="text-sm text-gray-500 dark:text-gray-400">
                        Define conditions for closing positions. Each rule can trigger a close or adjust TP/SL.
                      </p>
                      <ExitPresetPicker
                        onAdd={(p) =>
                          setExitConditions((prev) => [
                            ...prev,
                            {
                              ...exitConditionFromStored(p.rule as Record<string, unknown>),
                              id: `exit-${Date.now()}-${prev.length}`,
                            },
                          ])
                        }
                      />
                    </div>
                    <ExitConditionsBuilder
                      value={exitConditions}
                      onChange={setExitConditions}
                      availableFields={availableFields}
                      showOptimization={true}
                      vocabulary={rulesetVocabulary}
                    />
                  </div>
                )}
              </div>

              <div className="flex justify-end p-4 border-t border-gray-200 dark:border-gray-700">
                <button
                  onClick={() => setShowConditionModal(null)}
                  className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600"
                >
                  Done
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Confirm Dialog */}
      <ConfirmDialog
        isOpen={confirmDialog.isOpen}
        onClose={() => setConfirmDialog(prev => ({ ...prev, isOpen: false }))}
        onConfirm={confirmDialog.onConfirm}
        title={confirmDialog.title}
        message={confirmDialog.message}
        variant={confirmDialog.variant}
        confirmText="Delete"
      />
    </div>
  );
};

export default Backtesting;
