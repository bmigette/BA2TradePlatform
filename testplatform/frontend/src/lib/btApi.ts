import { API_BASE } from './config';
// Backend URL. Defaults to :8000; override locally with VITE_API_BASE in frontend/.env.local
// (e.g. http://localhost:8088/api when :8000 is taken by Docker).

export interface ExpertInfo { class: string; label: string; bypasses_classic_rm: boolean; uses_risk_manager: boolean; }
export interface SettingDef { type: string; default?: unknown; choices?: unknown[]; valid_values?: unknown[]; description?: string; tooltip?: string; }

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function jpost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function jdelete<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { method: 'DELETE' });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export interface OhlcvBar { Date: string; Open: number; High: number; Low: number; Close: number; Volume?: number; }
/** Daily (or any-interval) OHLCV bars for one symbol over [start,end] — feeds the trade-list chart. */
export const getOhlcvBars = (symbol: string, start: string, end: string, interval = '1d') =>
  jget<{ symbol: string; interval: string; bars: OhlcvBar[] }>(
    `/tools/ohlcv/bars?symbol=${encodeURIComponent(symbol)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&interval=${encodeURIComponent(interval)}`,
  );

/** Re-run a saved daily-expert backtest IN PLACE (overwrites the same row's results). Returns the
 * queued task id on the dedicated re-run worker pool. */
export const rerunBacktest = (id: number) =>
  jpost<{ status: string; task_id: string; backtest_id: number }>(`/backtests/${id}/rerun`, {});

export const listExperts = () => jget<{ experts: ExpertInfo[] }>('/experts').then(r => r.experts);
export const getExpertSettings = (cls: string) => jget<{ definitions: Record<string, SettingDef> }>(`/experts/${cls}/settings-definitions`).then(r => r.definitions);
export const importRules = (json: unknown, which: 'enter' | 'exit') => jpost<{ tree: unknown }>('/strategies/import-rules', { json, which }).then(r => r.tree);
export const exportRulesUrl = (strategyId: number, which: 'enter' | 'exit') => `${API_BASE}/strategies/${strategyId}/export-rules?which=${which}`;
export interface TaskInfo {
  id: number;
  task_id: string;
  task_type?: string;
  name?: string;
  status: string;
  progress?: number;
  progress_message?: string;
}
export const listTasks = (status = 'running') =>
  jget<{ tasks: TaskInfo[] } | TaskInfo[]>(`/tasks?status=${status}&limit=100`)
    .then(r => (Array.isArray(r) ? r : r.tasks ?? []));
export const cancelTask = (id: string) => jpost<unknown>(`/tasks/${id}/cancel`, {});

export interface OptIndividual {
  rank: number;
  fitness?: number;
  nTrades?: number;
  params?: Record<string, unknown>;
}
export interface RunningOpt {
  id: number;
  name?: string;
  status: string;
  progress?: number;
  fitnessMetric?: string;
  bestFitness?: number;
  bestParams?: Record<string, unknown>;
  nEvaluated?: number;
  topIndividuals?: OptIndividual[];
}
export const listRunningOptimizations = () =>
  jget<{ optimizations: RunningOpt[] }>(`/strategies/optimizations/running`)
    .then(r => r.optimizations ?? []);

// Optimization-Jobs tab: every StrategyOptimization row + a compact `settings` summary.
// Shapes confirmed against backend/app/api/strategies.py (_opt_settings_summary).
export interface OptParamRange {
  min?: number | string;
  max?: number | string;
  step?: number | string;
  type?: string;
}
export interface OptScreenerSettings {
  screener_settings?: Record<string, number | string>;
  screener_store?: string;
  screener_cadence_days?: number;
}
export interface OptJobSettings {
  ga: Partial<Record<
    'populationSize' | 'generations' | 'crossoverProb' | 'mutationProb'
    | 'earlyStoppingGenerations' | 'elitismPercent' | 'seed', number>>;
  fitnessMetric?: string | null;
  engine?: string | null;
  startDate?: string | null;
  endDate?: string | null;
  universeMode?: string | null;
  expertRanges: Record<string, OptParamRange>;
  screener?: OptScreenerSettings;
}
export interface OptimizationJob {
  id: number;
  strategyId?: number;
  name?: string | null;
  status: string;
  optimizationType?: string;
  fitnessMetric?: string;
  bestFitness?: number | null;
  progress?: number;
  errorMessage?: string | null;
  createdAt?: string | null;
  startedAt?: string | null;
  completedAt?: string | null;
  settings: OptJobSettings;
}
export const listOptimizationJobs = () =>
  jget<{ optimizations: OptimizationJob[] }>(`/strategies/optimizations`)
    .then(r => r.optimizations ?? []);

// Full detail for a single optimization (used by the Opt-History tab to lazily fetch the
// top individuals on expand). Backend GET /optimizations/{id} returns the StrategyOptimization
// to_dict() plus a `topIndividuals` list (ranked best-first, n=15) — read-only.
export interface OptimizationDetail {
  id: number;
  status: string;
  fitnessMetric?: string;
  bestFitness?: number | null;
  bestParams?: Record<string, unknown> | null;
  allResults?: Array<Record<string, unknown>> | null;
  // Full GA + backtest config (engine/universe/window/capital). Present on the detail endpoint
  // (StrategyOptimization.to_dict()), not on the compact /optimizations list rows.
  optimizationConfig?: Record<string, unknown> | null;
  topIndividuals?: OptIndividual[];
}
export const getOptimization = (id: number) =>
  jget<OptimizationDetail>(`/strategies/optimizations/${id}`);

// Read-only opt-job settings export (GET /optimizations/{id}/export). Returns the documented
// OptSettingsExport schema (see lib/btExport.ts). The caller downloads it via a Blob.
export const fetchOptSettingsExport = (id: number) =>
  jget<Record<string, unknown>>(`/strategies/optimizations/${id}/export`);

export const listBacktests = (q: { expert?: string; optimization_id?: number; saved?: boolean; single?: boolean } = {}) => {
  const p = new URLSearchParams();
  if (q.expert) p.set('expert', q.expert);
  if (q.optimization_id != null) p.set('optimization_id', String(q.optimization_id));
  if (q.saved != null) p.set('saved', String(q.saved));
  // single=true -> standalone runs only (optimization_id IS NULL); used by BT History.
  if (q.single != null) p.set('single', String(q.single));
  return jget<{ backtests: any[] }>(`/backtests?${p.toString()}`).then(r => r.backtests);
};

// Per-run actions (confirmed against backend/app/api/backtests.py):
//   POST /backtests/{id}/save  body {name}        -> marks is_saved, returns the run dict
//   GET  /backtests/{id}/export?kind=...          -> read-only JSON payload (browser download)
//   DELETE /backtests/{id}                        -> {message}
export const saveBacktest = (id: number, name: string) =>
  jpost<any>(`/backtests/${id}/save`, { name });

// What a backtest can export: the expert + its settings, or the conditions ruleset.
export type ExportKind = 'expert_settings' | 'ruleset';
// Fetch the chosen read-only export payload (NO server-side file write). The caller turns
// this into a browser download via a Blob + temporary <a download>.
export const fetchBacktestExport = (id: number, kind: ExportKind) =>
  jget<Record<string, unknown>>(`/backtests/${id}/export?kind=${kind}`);

export const deleteBacktest = (id: number) =>
  jdelete<{ message: string }>(`/backtests/${id}`);

// Exit-ruleset UI vocabulary, presets and live-import (confirmed against backend/app/api/ruleset.py):
//   GET /ruleset/vocabulary                          -> Vocabulary
//   GET /ruleset/exit-presets                        -> {presets}
//   GET /experts/{id}/open-positions-ruleset         -> {rules} (or 503/404)
export interface VocabItem { value: string; label: string; }
export interface ActionVocab { value: string; label: string; is_option: boolean; needs_reference: boolean; }
export interface Vocabulary {
  flags: VocabItem[]; numerics: VocabItem[]; operators: string[];
  actions: ActionVocab[]; reference_values: Record<string, string>;
}
export interface ExitPreset { key: string; label: string; rule: any; }
export const getRulesetVocabulary = () => jget<Vocabulary>('/ruleset/vocabulary');
export const getExitPresets = () => jget<{ presets: ExitPreset[] }>('/ruleset/exit-presets').then(r => r.presets);
export const importLiveRuleset = (expertId: number) =>
  jget<{ rules: any[] }>(`/experts/${expertId}/open-positions-ruleset`).then(r => r.rules);
//   GET /experts/{id}/enter-market-ruleset           -> {buy_entry_conditions, sell_entry_conditions} (or 503/404)
export const importLiveEnterMarket = (expertId: number) =>
  jget<{ buy_entry_conditions: any; sell_entry_conditions: any }>(`/experts/${expertId}/enter-market-ruleset`);

// Convert a LIVE-platform ruleset EXPORT FILE (export_type rulesets/ruleset/rule) into the
// backtester's strategy shapes. DB-free — pure transform of the uploaded JSON, so it works
// without a live-DB connection (unlike the /experts/{id}/* endpoints).
//   POST /ruleset/convert-live  body {payload} -> {buy_entry_conditions, sell_entry_conditions, exit_conditions, summary}
export interface ConvertLiveResult {
  buy_entry_conditions: any;
  sell_entry_conditions: any;
  exit_conditions: any[];
  summary?: Record<string, number>;
}
export const convertLiveRuleset = (payload: unknown) =>
  jpost<ConvertLiveResult>('/ruleset/convert-live', { payload });

// ---------------------------------------------------------------------------
// Data build / prewarm endpoints (async). Each returns either {task_id} or
// {tasks:[...]}; poll GET /api/tasks/{id} (listTasks/getTask) for progress.
// Additive — these mirror the ba2-test CLI data-prep commands.
// ---------------------------------------------------------------------------
export interface TaskRef { task_id: string; }
export interface TasksRef { tasks: Array<{ task_id: string; name?: string }>; }
export type BuildResult = TaskRef | TasksRef;

export interface BuildOhlcvBody {
  symbols: string[]; timeframes: string[]; start: string; end: string; provider?: string;
}
export const buildOhlcv = (b: BuildOhlcvBody) => jpost<BuildResult>('/data/build-ohlcv', b);

export interface BuildScreenerMetricsBody {
  store: string; start: string; end: string; market_cap_min: number;
  price_min?: number; volume_min?: number; cadence_days?: number; drop_days?: number;
}
export const buildScreenerMetrics = (b: BuildScreenerMetricsBody) =>
  jpost<BuildResult>('/data/build-screener-metrics', b);

export interface BuildOptionsBody {
  underlyings: string[]; start: string; end: string; cache_db: string; feed?: string;
}
export const buildOptions = (b: BuildOptionsBody) => jpost<BuildResult>('/data/build-options', b);

export interface PrewarmBody {
  symbols: string[]; experts?: string[]; workers?: number; end?: string;
}
export const prewarmData = (b: PrewarmBody) => jpost<BuildResult>('/data/prewarm', b);

// Fetch a single task's status (poll target for the build endpoints above).
export const getTask = (id: string) => jget<TaskInfo>(`/tasks/${id}`);

// ---------------------------------------------------------------------------
// Batch optimization: launch one optimization per expert against a strategy.
// POST /api/strategies/optimize-batch -> {jobs:[{expert,optimizationId,taskId,name}],count}.
// ---------------------------------------------------------------------------
export interface OptimizeBatchBody {
  experts: string[];
  strategy_id: number;
  fitness_metric: string;
  optimization_type: 'genetic' | 'brute_force';
  optimization_config: Record<string, unknown>;
  expert_params?: Record<string, unknown>;
  screener_opt?: Record<string, unknown>;
  worker_ids?: number[];
  name_prefix?: string;
}
export interface OptimizeBatchJob { expert: string; optimizationId: number; taskId: string; name: string; }
export const optimizeBatch = (b: OptimizeBatchBody) =>
  jpost<{ jobs: OptimizeBatchJob[]; count: number }>('/strategies/optimize-batch', b);

// Remote workers (for per-optimization selection). Master is always a worker; selecting none = local.
export interface WorkerLite { id: number; name: string; isEnabled: boolean; isLocal: boolean; status: string; }
export const listWorkers = () =>
  jget<WorkerLite[]>('/workers').then(ws => ws.filter(w => !w.isLocal));

// ---------------------------------------------------------------------------
// Robustness suite (Task 6): Monte-Carlo over saved trades + schedule variants.
// Shapes confirmed EXACTLY against backend/app/api/backtests.py (_robustness_run_out,
// MonteCarloConfig/ScheduleConfig/RobustnessRequest) and services/backtest/monte_carlo.py
// (run_monte_carlo output) + robustness_handler.collect_schedule_results (schedule_summary).
// The GET/POST routes serialise snake_case (NOT the model's camelCase to_dict).
// ---------------------------------------------------------------------------
export interface RobustnessRequestBody {
  backtest_ids: number[];
  monte_carlo: {
    enabled: boolean;
    n_paths: number;
    seed: number;
    methods: string[];      // subset of "bootstrap" | "shuffle" | "jitter"
    drop_k: number[];       // e.g. [1,2,3]
    jitter_bp: number;
  };
  schedule: {
    enabled: boolean;
    day_variants: boolean;
    time_variants: string[]; // ["10:30","12:30","15:00"]
  };
}
export interface RobustnessLaunchRun {
  backtest_id: number;
  kind: 'monte_carlo' | 'schedule';
  robustness_run_id: number;
  status: string;
}
// summarize_paths(...) band: percentile keys per metric.
export interface McBand { p5: number; p25: number; p50: number; p75: number; p95: number; }
// One method summary (bands per metric + probabilities). consistency is optional (soft-dep).
export interface McMethodSummary {
  annualized_return: McBand;
  max_drawdown: McBand;
  calmar: McBand;
  n_paths: number;
  prob_target_annual: number; // fraction 0..1 (ann >= target, default target 30%)
  prob_dd_breach: number;     // fraction 0..1 (dd <= -limit, default limit 20%)
  consistency?: number;
}
export interface McDropKRow {
  k: number;
  dropped: number[];          // dropped trade pnl_pct values (highest first)
  final_equity: number;
  annualized_return: number;
  max_drawdown: number;
  calmar: number;
}
export interface McResults {
  methods: Record<string, McMethodSummary>;
  drop_k: McDropKRow[];
  n_trades: number;
  years: number;
}
export interface ScheduleVariantRow {
  backtest_id: number;
  name: string;
  status: string;
  annualized_return: number | null;
  total_return: number | null;
  max_drawdown: number | null;
  calmar: number | null;
  sharpe: number | null;
  total_trades: number | null;
}
export interface ScheduleResults {
  schedule_summary?: ScheduleVariantRow[];
  ann_return_spread?: number;
}
export interface RobustnessRun {
  robustness_run_id: number;
  backtest_id: number;
  kind: 'monte_carlo' | 'schedule';
  status: string;             // pending | running | completed | failed
  params: Record<string, unknown> | null;
  results: (McResults & ScheduleResults) | null;
  variant_backtest_ids: number[];
  error_message: string | null;
  created_at: string | null;
  completed_at: string | null;
}
export const launchRobustness = (body: RobustnessRequestBody) =>
  jpost<{ runs: RobustnessLaunchRun[] }>('/backtests/robustness', body);
export const listRobustnessRuns = (backtestId: number) =>
  jget<{ runs: RobustnessRun[] }>(`/backtests/robustness?backtest_id=${backtestId}`).then(r => r.runs);
export const getRobustnessRun = (runId: number) =>
  jget<RobustnessRun>(`/backtests/robustness/${runId}`);
