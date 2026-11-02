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
/** The breadth of group m over fewer tickers than this means nothing: the backend drops that column. */
export const DAILY_BREADTH_MIN_TICKERS = 5;

/** Help text of the coverage threshold: what the mask is and is not. */
export const DAILY_MASK_HELP =
  'The mask is decided per calendar year over the dataset\'s own date span: a group whose data completely covers less than ' +
  'this share of the rows of a year is blanked for that year (NaN) and flagged with a <group>_missing column. It is a ' +
  'completeness rule over the rows present, not a look-ahead on any value; a different date span can mask different years.';

/** Help text of the universe: where the cross-sectional values come from. */
export const DAILY_UNIVERSE_HELP =
  'The cross-sectional ranks (x) and the market breadth (m) are taken over the tickers created together, your own ticker ' +
  'list: nothing else is in the cross-section, so a list of today\'s survivors has survivorship. A single dataset is built ' +
  'against itself only, and the breadth is dropped below 5 tickers.';

export interface DailyGroupOption {
  id: string;
  name: string;
  desc: string;
}

/** Every group of the store, in the store's order (the backend pins that this list matches ml_features.GROUPS). */
export const DAILY_GROUP_OPTIONS: DailyGroupOption[] = [
  { id: 't', name: 'Technical', desc: 'returns, moving-average distances, RSI / ADX / SAR, ATR, volume z-scores (scale-free)' },
  { id: 'mtf', name: 'Weekly multi-timeframe', desc: 'weekly trend and momentum, visible only once the week has ended' },
  { id: 'm', name: 'Market', desc: 'SPY / QQQ / IWM / TLT / GLD returns, relative strength vs SPY and the sector ETF, breadth of the batch (dropped below 5 tickers)' },
  { id: 'x', name: 'Cross-sectional', desc: "the symbol's rank and z-score among the batch's tickers (needs at least 5 tickers; your list, so survivorship)" },
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

/**
 * Size of the universe a dataset will be built over when it is edited or duplicated with `ticker`: the stored universe plus
 * the ticker when it is not in it yet (the backend always adds the dataset's own ticker); 1 when nothing is stored.
 */
export function universeSizeWith(generationConfig: Record<string, unknown> | null | undefined, ticker: string): number {
  const universe = generationConfig?.daily_universe;
  const stored = Array.isArray(universe) ? universe.map((s) => String(s).trim().toUpperCase()) : [];
  const t = ticker.trim().toUpperCase();
  if (stored.length === 0) return 1;
  return new Set(t ? [...stored, t] : stored).size;
}

/** One line for the review step. */
export function dailySummary(state: DailySourceState, nTickers: number): string {
  const names = DAILY_GROUP_OPTIONS.filter((o) => state.groups.includes(o.id)).map((o) => `${o.name} (${o.id})`);
  return `${names.join(', ') || 'no group'}; coverage threshold ${state.minShare}; universe of ${nTickers} ticker${nTickers === 1 ? '' : 's'}`;
}

/** Why the market breadth column is missing from a dataset built over fewer than 5 tickers, or null. */
export function breadthNote(groups: string[], nTickers: number): string | null {
  if (!groups.includes('m') || nTickers >= DAILY_BREADTH_MIN_TICKERS) return null;
  return `The market breadth is dropped: it would be taken over ${nTickers} ticker${nTickers === 1 ? '' : 's'} (needs ${DAILY_BREADTH_MIN_TICKERS})`;
}

/**
 * A duplicate with a new ticker joins the original's universe: its cross-sectional ranks (x) and breadth (m) are taken
 * over N+1 tickers while the original's datasets keep N. Null when nothing differs.
 */
export function duplicateUniverseNote(
  generationConfig: Record<string, unknown> | null | undefined,
  newTicker: string,
): string | null {
  const universe = generationConfig?.daily_universe;
  const groups = generationConfig?.daily_groups;
  if (!newTicker || !Array.isArray(universe) || universe.length === 0 || !Array.isArray(groups)) return null;
  if (universe.includes(newTicker) || !(groups.includes('x') || groups.includes('m'))) return null;
  return (
    `${newTicker} joins the daily universe (${universe.length} -> ${universe.length + 1} tickers): its cross-sectional ranks (x) ` +
    `and market breadth (m) are taken over the larger set and differ from those of the original's datasets, which keep ` +
    `their ${universe.length} tickers`
  );
}

/** `panel_features` of the create / batch / edit calls: the intraday list for the intraday cache, an EMPTY list for the daily
 *  store (so an intraday list inherited by an edited dataset is not kept), nothing for another provider. */
export function panelFeaturesPayload(provider: string, timeframe: string, panelFeatures: string[]): string[] | undefined {
  if (provider !== ALPACA_HIST_PROVIDER) return undefined;
  return isDailySource(provider, timeframe) ? [] : panelFeatures;
}

/** The feature selections after the timeframe select of step 1 was changed: the cache's timeframe tells the intraday panel
 *  and the daily store apart, so the selection of the source left behind is cleared (and the daily store starts with the
 *  technical group) instead of the radio of step 2 jumping to a source with nothing selected. */
export function timeframeChangeSelections(
  provider: string,
  timeframe: string,
  current: { panelFeatures: string[]; dailyGroups: string[] },
): { panelFeatures: string[]; dailyGroups: string[] } {
  if (provider !== ALPACA_HIST_PROVIDER) return current;
  if (isDailySource(provider, timeframe)) {
    return { panelFeatures: [], dailyGroups: current.dailyGroups.length > 0 ? current.dailyGroups : ['t'] };
  }
  return { panelFeatures: current.panelFeatures, dailyGroups: [] };
}

/** The `warnings` the API returns with a created dataset (a duplicate that widened a daily universe) as one line, or null. */
export function createdNotice(created: unknown): string | null {
  const warnings = (created as { warnings?: unknown } | null)?.warnings;
  if (!Array.isArray(warnings) || warnings.length === 0) return null;
  return warnings.map(String).join(' ');
}
