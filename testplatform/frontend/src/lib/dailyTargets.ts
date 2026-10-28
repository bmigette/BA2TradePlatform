/**
 * Daily targets of the dataset / job wizards (source: the daily feature store, alpaca_hist 1d). The decision is taken at
 * a session's close and filled at the next open; the outcome runs to the close of session S+H. Backend:
 * app/services/ml_wizard_targets.py (build_daily_target, build_cross_sectional_daily_targets).
 */
import type {
  DailyBeatsTarget,
  DailyMultistepTarget,
  DailyQuintileTarget,
  DailyRelReturnTarget,
  DailyUpTarget,
  DailyVolatilityTarget,
  TargetCategory,
} from '../types/targets';

export const DAILY_HORIZON_CHOICES = [1, 3, 5, 10, 20] as const;
export const DEFAULT_DAILY_HORIZON = 5;
/** A session's cross-section needs this many tickers holding an outcome. */
export const MIN_CROSS_SECTION_TICKERS = 5;

export const DAILY_TARGET_TYPES = [
  'daily_rel_return',
  'daily_quintile',
  'daily_beats',
  'daily_up',
  'daily_volatility',
  'daily_multistep',
] as const;
export type DailyTargetType = (typeof DAILY_TARGET_TYPES)[number];

/** Compared with the other tickers of the same session: computed across all the job's datasets. */
export const CROSS_SECTIONAL_DAILY_TYPES: readonly DailyTargetType[] = ['daily_rel_return', 'daily_quintile', 'daily_beats'];

export type DailyTargetConfig =
  | DailyRelReturnTarget
  | DailyQuintileTarget
  | DailyBeatsTarget
  | DailyUpTarget
  | DailyVolatilityTarget
  | DailyMultistepTarget;

export interface DailyTargetForm {
  horizonSessions: number;
  horizons: number[];
}

export const DEFAULT_DAILY_FORM: DailyTargetForm = { horizonSessions: DEFAULT_DAILY_HORIZON, horizons: [1, 5, 20] };

export interface DailyTargetInfo {
  tabLabel: string;
  category: TargetCategory;
  hint: string;
}

export const DAILY_TARGET_INFO: Record<DailyTargetType, DailyTargetInfo> = {
  daily_rel_return: {
    tabLabel: 'Daily Rel. Return',
    category: 'regression',
    hint: "Regression: the return from the next open to the close of session S+H minus the mean of the job's tickers on the same session. Needs at least 5 tickers in the job (multi-series mode).",
  },
  daily_quintile: {
    tabLabel: 'Daily Quintile',
    category: 'binary_classification',
    hint: 'Binary: 1 = top fifth of the session by relative return, 0 = bottom fifth; the middle three fifths carry no label. Needs at least 5 tickers in the job (multi-series mode).',
  },
  daily_beats: {
    tabLabel: 'Daily Beats',
    category: 'binary_classification',
    hint: 'Binary: 1 = the ticker beats the mean of the job on the same session. Needs at least 5 tickers in the job (multi-series mode).',
  },
  daily_up: {
    tabLabel: 'Daily Up',
    category: 'binary_classification',
    hint: 'Binary: 1 = the return from the next open to the close of session S+H is positive. Checked on the training rows (a single class is refused).',
  },
  daily_volatility: {
    tabLabel: 'Daily Volatility',
    category: 'regression',
    hint: 'Regression: the std of the daily log returns of the next H sessions (one daily return has no spread, so H of 3 or more).',
  },
  daily_multistep: {
    tabLabel: 'Daily Multistep',
    category: 'binary_classification',
    hint: 'One binary up column per horizon; the shortest horizon is the trained column and every column is purged by the longest.',
  },
};

export function isDailyTarget(config: Record<string, unknown> | undefined | null): boolean {
  return !!config && DAILY_TARGET_TYPES.includes(config.type as DailyTargetType);
}

export function isCrossSectionalDailyTarget(config: Record<string, unknown> | undefined | null): boolean {
  return !!config && CROSS_SECTIONAL_DAILY_TYPES.includes(config.type as DailyTargetType);
}

function checkHorizon(h: number): number {
  if (!(DAILY_HORIZON_CHOICES as readonly number[]).includes(h)) {
    throw new Error(`Horizon must be one of ${DAILY_HORIZON_CHOICES.join(' / ')} sessions, got ${h}`);
  }
  return h;
}

/** Tick or untick a multistep horizon; the result is ascending (so the first, trained horizon is the shortest). */
export function toggleHorizon(horizons: number[], h: number, on: boolean): number[] {
  checkHorizon(h);
  const next = new Set(horizons);
  if (on) next.add(h);
  else next.delete(h);
  return DAILY_HORIZON_CHOICES.filter((c) => next.has(c));
}

/** The target config of a tab's form; throws when the form cannot make one. */
export function buildDailyTarget(type: DailyTargetType, form: DailyTargetForm): DailyTargetConfig {
  const category = DAILY_TARGET_INFO[type].category;
  if (type === 'daily_multistep') {
    if (form.horizons.length === 0) throw new Error('Select at least one horizon for the multistep target');
    const horizons = [...new Set(form.horizons.map(checkHorizon))].sort((a, b) => a - b);
    return { type, category: 'binary_classification', horizons } as DailyMultistepTarget;
  }
  const horizonSessions = checkHorizon(form.horizonSessions);
  if (type === 'daily_volatility' && horizonSessions < 2) {
    throw new Error('Volatility needs a horizon above 1 session (one daily return has no spread)');
  }
  return { type, category, horizonSessions } as DailyTargetConfig;
}

export function dailyTargetLabel(config: Record<string, unknown>): string {
  const h = config.horizonSessions;
  switch (config.type) {
    case 'daily_rel_return':
      return `Daily rel. return ${h}s`;
    case 'daily_quintile':
      return `Daily quintile ${h}s`;
    case 'daily_beats':
      return `Daily beats ${h}s`;
    case 'daily_up':
      return `Daily up ${h}s`;
    case 'daily_volatility':
      return `Daily volatility ${h}s`;
    case 'daily_multistep':
      return `Daily multistep ${(Array.isArray(config.horizons) ? config.horizons : []).join('/')}s`;
    default:
      return `Target: ${String(config.type)}`;
  }
}

/**
 * Why a job cannot run its cross-sectional daily targets, or null: they compare the tickers of a session, so the job
 * needs at least 5 datasets in ONE run (multi-series mode, not one job per dataset).
 */
export function crossSectionalIssue(
  targets: Record<string, unknown>[],
  nDatasets: number,
  mode: 'batch' | 'multi-series',
): string | null {
  const cross = targets.filter(isCrossSectionalDailyTarget);
  if (cross.length === 0) return null;
  const names = [...new Set(cross.map((t) => dailyTargetLabel(t)))].join(', ');
  if (nDatasets < MIN_CROSS_SECTION_TICKERS) {
    return `${names} compare each ticker with the others of the same session and need at least ${MIN_CROSS_SECTION_TICKERS} datasets (tickers) in the job; ${nDatasets} selected`;
  }
  if (mode === 'batch') {
    return `${names} compare the tickers of a session: run the datasets together (multi-series), not as one job per dataset`;
  }
  return null;
}
