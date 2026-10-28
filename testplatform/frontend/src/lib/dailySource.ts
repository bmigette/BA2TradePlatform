/**
 * The DAILY source of the dataset wizard: the Alpaca history cache's point-in-time daily feature store
 * (data provider alpaca_hist, timeframe 1d). Its feature groups and coverage threshold are persisted in the dataset's
 * generation_config as daily_groups / daily_min_share (backend: app/services/ml_dataset_source.py).
 */

export const ALPACA_HIST_PROVIDER = 'alpaca_hist';
/** The intraday panel (panel_features) of the same provider. */
export const ALPACA_HIST_INTRADAY_TIMEFRAMES = ['1h', '15m'];
export const DAILY_TIMEFRAME = '1d';
/** Coverage threshold when the request names none: the store's own. */
export const DAILY_DEFAULT_MIN_SHARE = 0.7;
/** Group x ranks a symbol among its universe; the store refuses a cross-section thinner than this. */
export const DAILY_X_MIN_TICKERS = 5;

export interface DailyGroupOption {
  id: string;
  name: string;
  desc: string;
}

/** Every group of the store, in the store's order (the backend pins that this list matches ml_features.GROUPS). */
export const DAILY_GROUP_OPTIONS: DailyGroupOption[] = [
  { id: 't', name: 'Technical', desc: 'returns, moving-average distances, RSI / ADX / SAR, ATR, volume z-scores (scale-free)' },
  { id: 'mtf', name: 'Weekly multi-timeframe', desc: 'weekly trend and momentum, visible only once the week has ended' },
  { id: 'm', name: 'Market', desc: 'SPY / QQQ / IWM / TLT / GLD returns, relative strength vs SPY and the sector ETF, universe breadth' },
  { id: 'x', name: 'Cross-sectional', desc: "the symbol's rank and z-score among the batch's tickers (needs at least 5 tickers)" },
  { id: 'mac', name: 'Macro', desc: 'FRED first-release series (VIX, yields, spreads, CPI, payrolls, unemployment), as published' },
  { id: 'f', name: 'Fundamentals', desc: 'earnings surprise and growth, margins, balance-sheet ratios, after the filing date' },
  { id: 'ag', name: 'Analyst grades', desc: 'net upgrades and number of graders over the last 30 / 90 days' },
  { id: 'at', name: 'Analyst price targets', desc: 'consensus target vs price, target revisions, number of analysts' },
  { id: 'i', name: 'Insiders', desc: 'net open-market insider buying and selling over the market cap' },
  { id: 's', name: 'Market structure', desc: 'support / resistance distance, break of structure and change of character' },
];

const GROUP_IDS = DAILY_GROUP_OPTIONS.map((o) => o.id);

export interface DailySourceState {
  groups: string[];
  /** kept as text so the field can be edited freely; parsed by validateDailySource / dailyPayload */
  minShare: string;
}

export const DEFAULT_DAILY_STATE: DailySourceState = { groups: [], minShare: String(DAILY_DEFAULT_MIN_SHARE) };

/** The dataset is built from the daily feature store (and not from the intraday panel or another provider). */
export function isDailySource(provider: string, timeframe: string): boolean {
  return provider === ALPACA_HIST_PROVIDER && timeframe === DAILY_TIMEFRAME;
}

/** Timeframes the dataset can have for a provider: the cache offers the intraday panel and the daily store. */
export function allowedTimeframes(provider: string, all: string[]): string[] {
  if (provider !== ALPACA_HIST_PROVIDER) return all;
  return all.filter((tf) => ALPACA_HIST_INTRADAY_TIMEFRAMES.includes(tf) || tf === DAILY_TIMEFRAME);
}

/** Tick or untick a group; the result stays in the store's order, without duplicates. */
export function toggleDailyGroup(groups: string[], id: string, on: boolean): string[] {
  if (!GROUP_IDS.includes(id)) throw new Error(`unknown daily group ${id}`);
  const next = new Set(groups);
  if (on) next.add(id);
  else next.delete(id);
  return GROUP_IDS.filter((g) => next.has(g));
}

function parseShare(text: string): number {
  return text.trim() === '' ? NaN : Number(text);
}

/**
 * The reason the daily options cannot be sent, or null. `nTickers` is the size of the universe the dataset is built
 * in: the batch's symbols, the stored universe when editing or duplicating, else 1.
 */
export function validateDailySource(state: DailySourceState, nTickers: number): string | null {
  if (state.groups.length === 0) return 'Select at least one feature group of the daily store';
  const bad = state.groups.filter((g) => !GROUP_IDS.includes(g));
  if (bad.length > 0) return `Unknown feature group: ${bad.join(', ')}`;
  const share = parseShare(state.minShare);
  if (!(share > 0 && share <= 1)) return 'The coverage threshold must be a number above 0 and at most 1';
  if (state.groups.includes('x') && nTickers < DAILY_X_MIN_TICKERS) {
    return `The cross-sectional group needs at least ${DAILY_X_MIN_TICKERS} tickers created together (batch mode); this dataset has ${nTickers}`;
  }
  return null;
}

/** Request fields of the create / batch / edit calls: empty unless the source is the daily store. */
export function dailyPayload(
  provider: string,
  timeframe: string,
  state: DailySourceState,
): { daily_groups?: string[]; daily_min_share?: number } {
  if (!isDailySource(provider, timeframe)) return {};
  return { daily_groups: state.groups, daily_min_share: parseShare(state.minShare) };
}

/** The daily options of a dataset's generation_config (edit / duplicate); empty when it carries none. */
export function hydrateDailyState(generationConfig: Record<string, unknown> | null | undefined): DailySourceState {
  const groups = generationConfig?.daily_groups;
  const share = generationConfig?.daily_min_share;
  return {
    groups: Array.isArray(groups) ? GROUP_IDS.filter((g) => groups.includes(g)) : [],
    minShare: typeof share === 'number' ? String(share) : String(DAILY_DEFAULT_MIN_SHARE),
  };
}

/** Size of the stored universe of a dataset (edit / duplicate); 1 when it carries none. */
export function storedUniverseSize(generationConfig: Record<string, unknown> | null | undefined): number {
  const universe = generationConfig?.daily_universe;
  return Array.isArray(universe) && universe.length > 0 ? universe.length : 1;
}

/** One line for the review step. */
export function dailySummary(state: DailySourceState, nTickers: number): string {
  const names = DAILY_GROUP_OPTIONS.filter((o) => state.groups.includes(o.id)).map((o) => `${o.name} (${o.id})`);
  return `${names.join(', ') || 'no group'}; coverage threshold ${state.minShare}; universe of ${nTickers} ticker${nTickers === 1 ? '' : 's'}`;
}
