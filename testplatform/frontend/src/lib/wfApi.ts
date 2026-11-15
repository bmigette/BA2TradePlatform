// Walk-forward API (/api/walk-forward). Backend: testplatform/backend/app/api/walk_forward.py.
import { API_BASE } from './config';

/** The JSON error body's `detail` string (a refusal's own message), else status + body text. */
async function errorText(r: Response): Promise<string> {
  const text = await r.text();
  try {
    const detail = JSON.parse(text)?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (detail) return JSON.stringify(detail);
  } catch { /* not JSON */ }
  return `${r.status} ${text}`;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}

export type WfStatus = 'pending' | 'running' | 'deferred-pending' | 'pass' | 'fail' | 'error' | 'cancelled';

export interface WfRunLight {
  id: number;
  name: string;
  source: 'ui' | 'cli' | 'import';
  expert: string | null;
  strategy: string | null;
  band: string | null;
  status: WfStatus;
  plannedFolds: number | null;
  foldsDone: number | null;
  currentFold: number | null;
  progressMessage: string | null;
  stitchedReturn: number | null;
  worstOosDd: number | null;
  stitchedTrades: number | null;
  errorMessage: string | null;
  taskId: string | null;
  createdAt: string | null;
  startedAt: string | null;
  updatedAt: string | null;
  completedAt: string | null;
}

/** One genome's in-sample vs out-of-sample record (walk_forward.GenomeFold). Percent units. */
export interface WfRank {
  fold: number;
  rank: number;
  is_fitness: number;
  is_return: number;
  is_car: number;
  is_max_dd: number;
  is_trades: number;
  oos_return: number;
  oos_car: number;
  oos_max_dd: number;
  oos_trades: number;
  oos_backtest_id: number | null;
  efficiency: number | null;
  passed: boolean;
  fail_reasons: string[];
}

export type WfFoldState = 'planned' | 'running' | 'done' | 'deferred' | 'eliminated';

export interface WfFold {
  index: number;
  train_start: string | null;
  train_end: string | null;
  test_start: string | null;
  test_end: string | null;
  state: WfFoldState;
  train_job: string;
  train_optimization_id: number | null;
  generations: string | number | null;
  seeded_from: string | null;
  winner_passed: boolean | null;
  ranks: WfRank[];
}

export interface WfStitch {
  folds: number[];
  total_return: number;
  oos_car: number;
  worst_fold_dd: number;
  trades: number;
  is_car_mean: number;
  efficiency: number | null;
  years: number;
}

export interface WfReport {
  schema?: number;
  final?: boolean;
  verdict?: string | null;
  folds: WfFold[];
  winner_stitch?: WfStitch | null;
  stitch_partial?: boolean;
  rank_stitches?: Record<string, WfStitch>;
  winners_passed?: number;
  overall_passed?: boolean | null;
  notes?: string[];
  required_failed?: number[];
  refit_job?: string | null;
  refit?: { job: string; train: [string, string]; seeded_from: string | null } | null;
  refit_running?: string | null;
  deferred?: { fold: number; job: string; train: [string, string]; test: [string, string] } | null;
  eliminated?: { before_fold: number; reason: string } | null;
  data_end?: string | null;
  thresholds?: Record<string, unknown> | null;
  selection?: Record<string, unknown>;
  top_n?: number | null;
}

export interface WfJobInfo {
  id: number;
  status: string;
  progress: number | null;
  bestFitness: number | null;
  startedAt: string | null;
  completedAt: string | null;
}

export interface WfRunDetail extends WfRunLight {
  spec: Record<string, unknown> | null;
  report: WfReport;
  reportDir: string | null;
  driverHost: string | null;
  driverPid: number | null;
  driverState: string | null;
  taskStatus: string | null;
  jobs: Record<string, WfJobInfo>;
  backtests: Record<string, boolean>;
  live: {
    fold: number | null;
    job: string | null;
    optimization: WfJobInfo | null;
    generation: number | null;
    generations: number | null;
    phase: string | null;
  } | null;
  interrupted: boolean;
  canCancel: boolean;
  canResume: boolean;
  logTail: string[] | null;
}

export interface WfRequest {
  name: string;
  optimize_args: string[];
  train_start: string;
  test_years: number[];
  data_end: string | null;
  first_generations: number;
  next_generations: number;
  top_n: number;
  test_parallel: number;
  min_oos_return: number;
  exclusive_return: boolean;
  max_dd_mult: number;
  max_oos_dd: number | null;
  min_folds: number;
  require_test_years: number[];
  defer_last_test: boolean;
  run_all_folds: boolean;
  refit: boolean;
}

export interface WfPreviewFold {
  index: number;
  train_start: string;
  train_end: string;
  test_start: string;
  test_end: string;
  generations: string;
  deferred: boolean;
  train_job: string;
  command: string[];
}

export interface WfPreview {
  folds: WfPreviewFold[];
  refit: { job: string; train_start: string; train_end: string; command: string[] } | null;
  deferred_fold: number | null;
  jobs: string[];
  existingJobs: { id: number; name: string; status: string }[];
  nameTaken: boolean;
}

export interface WfOptions {
  experts: string[];
  strategies: string[];
  optionStrategies: string[];
  estimatesSources: string[];
  historySources: string[];
  defaultHistorySource: string;
  capBands: string[];
  fitness: string[];
  forbiddenFlags: Record<string, string>;
  chainSetFlags: string[];
  prefill: {
    train_start: string; test_years: number[]; first_generations: number; next_generations: number;
    top_n: number; test_parallel: number; min_oos_return: number; exclusive_return: boolean;
    max_dd_mult: number; max_oos_dd: number | null; min_folds: number; require_test_years: number[];
    defer_last_test: boolean; run_all_folds: boolean; refit: boolean; population: number;
    fitness: string; interval: string; early_stop: number; early_stop_min_rel: number; parallel: number;
  };
  reportDir: string;
}

export interface WfEquity {
  available: boolean;
  missing: string[];
  folds: { fold: number; backtestId: number; start: string; end: string }[];
  points: { date: string; equity: number; fold: number }[];
}

export const getWfOptions = () => get<WfOptions>('/walk-forward/options');
export const listWfRuns = () => get<{ runs: WfRunLight[] }>('/walk-forward/runs').then(r => r.runs);
export const getWfRun = (id: number) => get<WfRunDetail>(`/walk-forward/runs/${id}`);
export const getWfEquity = (id: number) => get<WfEquity>(`/walk-forward/runs/${id}/oos-equity`);
export const previewWfRun = (req: WfRequest) => post<WfPreview>('/walk-forward/preview', req);
export const createWfRun = (req: WfRequest) => post<WfRunDetail>('/walk-forward/runs', req);
export const cancelWfRun = (id: number) => post<WfRunDetail>(`/walk-forward/runs/${id}/cancel`);
/** Resume; `dataEnd` given (string or null) replaces the stored data end (a deferred two-phase run
 * tests its last fold once the data exists: pass null). */
export const resumeWfRun = (id: number, dataEnd?: string | null) =>
  post<WfRunDetail>(`/walk-forward/runs/${id}/resume`,
    dataEnd === undefined ? undefined : { change_data_end: true, data_end: dataEnd });
