// Presentation rules for the option-cache chain viewer.
//
// A broker-style layout invites filling every column, and for the caches this platform
// actually holds, most columns have no data. The rules that keep that honest live here as
// pure functions so they are testable without a DOM:
//
//   * an absent value renders as "n/a" WITH the backend's reason, never as 0.00 — a strike
//     with no open interest recorded is not a strike with zero open interest, and a
//     recorded zero must still render as zero;
//   * the legacy Alpaca store writes bid == ask == close in every quoted row, so it gets
//     ONE close column. Bid/Ask columns appear only for a store that declares a real
//     spread (`has_quote_spread`), which none currently does;
//   * a computed greek is model output. It is marked, and the tooltip says it is not
//     exchange data.
//
// Contract mirrors app/services/option_cache_reader.py.

export type CellSource = 'cache' | 'computed' | 'derived' | 'unavailable';

export interface Cell {
  value: number | null;
  source: CellSource;
  reason: string | null;
}

export interface ChainColumns {
  quote: 'close' | 'ohlc';
  has_quote_spread: boolean;
  iv: boolean;
  open_interest: boolean;
  volume: boolean;
  greeks: 'computed' | 'unavailable';
}

export interface Leg {
  occ_symbol: string;
  option_type: 'call' | 'put';
  strike: number;
  expiry: string;
  store: string;
  [field: string]: Cell | string | number;
}

export interface StrikeRow {
  strike: number;
  call: Leg | null;
  put: Leg | null;
}

export interface ExpiryGroup {
  expiry: string;
  dte: number;
  iv_median: Cell;
  rows: StrikeRow[];
}

export interface ChainResponse {
  symbol: string;
  as_of: string;
  store: string;
  store_label: string;
  columns: ChainColumns;
  spot: Cell;
  greeks_model: string;
  greeks_inputs: { rate: number; dividend_yield: number; day_count: string };
  contracts: number;
  expiries: ExpiryGroup[];
  notes: string[];
}

export interface StoreInfo {
  id: string;
  label: string;
  present: boolean;
  path: string;
  bytes: number | null;
  absent_reason: string | null;
  symbols: number | null;
  has_iv: boolean;
  has_open_interest: boolean;
  has_greeks: boolean;
  has_volume: boolean;
  has_quote_spread: boolean;
  quote_note: string;
  description: string;
}

export interface DateEntry {
  as_of: string;
  rows: number;
}

/** The one string an unavailable value ever renders as. Never "0", never "0.00", never "". */
export const NA = 'n/a';

/** Suffix on any number this platform computed rather than read out of a cache. */
export const COMPUTED_MARKER = '†'; // dagger

export type Tone = 'value' | 'muted' | 'computed' | 'derived';

export interface Rendered {
  text: string;
  title: string;
  tone: Tone;
}

const COMPUTED_TITLE =
  'Computed by this viewer from cached implied volatility — model output, not exchange data.';
const DERIVED_TITLE = 'Derived by this viewer from the cached rows — not a vendor figure.';

/**
 * One cell, ready to print.
 *
 * The `value == null` branch is the load-bearing one: it must not fall through to a
 * numeric format, because `Number(null).toFixed(2)` is "0.00" and that reads as a real
 * zero. A missing cell object is treated identically — a field the store does not have
 * at all is still absent, not zero.
 */
export function renderCell(c: Cell | null | undefined, digits = 2): Rendered {
  if (!c || c.value === null || c.value === undefined) {
    return {
      text: NA,
      title: c?.reason || 'No value recorded for this field.',
      tone: 'muted',
    };
  }
  const num = c.value.toFixed(digits);
  if (c.source === 'computed') {
    return {
      text: `${num}${COMPUTED_MARKER}`,
      title: `${COMPUTED_TITLE}${c.reason ? ` (${c.reason})` : ''}`,
      tone: 'computed',
    };
  }
  if (c.source === 'derived') {
    return {
      text: num,
      title: `${DERIVED_TITLE}${c.reason ? ` (${c.reason})` : ''}`,
      tone: 'derived',
    };
  }
  return { text: num, title: c.reason || 'Value as stored in the cache.', tone: 'value' };
}

/**
 * The price columns this store can honestly fill.
 *
 * Bid/Ask are gated on `has_quote_spread` alone. The legacy Alpaca cache sets it false
 * because bid, ask and close are the same number in all 4,328,587 of its quoted rows —
 * a placeholder from the cache build, not a market quote.
 */
export function priceColumnIds(cols: ChainColumns): string[] {
  const ids = cols.quote === 'ohlc' ? ['open', 'high', 'low', 'close'] : ['close'];
  return cols.has_quote_spread ? ['bid', 'ask', ...ids] : ids;
}

export interface Header {
  label: string;
  marker: string;
  title: string;
}

const GREEK_LABELS: Record<string, string> = {
  delta: 'Δ',
  gamma: 'Γ',
  theta: 'Θ',
  vega: 'V',
};

const PRICE_LABELS: Record<string, string> = {
  open: 'Open',
  high: 'High',
  low: 'Low',
  close: 'Close',
  bid: 'Bid',
  ask: 'Ask',
  volume: 'Vol',
  iv: 'IV',
  open_interest: 'OI',
};

/** A column header plus the tooltip that says where its numbers come from — or do not. */
export function headerFor(field: string, cols: ChainColumns): Header {
  if (field in GREEK_LABELS) {
    const computed = cols.greeks === 'computed';
    return {
      label: GREEK_LABELS[field],
      marker: computed ? COMPUTED_MARKER : '',
      title: computed
        ? `${COMPUTED_TITLE} No store here publishes vendor greeks.`
        : 'Not computable: this store records no implied volatility, or no spot was supplied.',
    };
  }
  if (field === 'iv') {
    return {
      label: 'IV',
      marker: '',
      title: cols.iv
        ? 'Vendor implied volatility, as stored in the parquet partition.'
        : 'No implied volatility: this store never recorded the column.',
    };
  }
  if (field === 'open_interest') {
    return {
      label: 'OI',
      marker: '',
      title: cols.open_interest
        ? 'Open interest, as stored. A recorded 0 means nobody holds the strike.'
        : 'Open interest not recorded by this store — absent, not zero.',
    };
  }
  if (field === 'volume') {
    return {
      label: 'Vol',
      marker: '',
      title: cols.volume
        ? 'Contracts traded on this date, as stored.'
        : 'Volume not recorded by this store — absent, not zero.',
    };
  }
  return {
    label: PRICE_LABELS[field] || field,
    marker: '',
    title: cols.has_quote_spread
      ? 'Quote as stored.'
      : 'Price as stored. This store records no bid/ask spread.',
  };
}

/** The footnotes under the table: the backend's notes, plus the marker legend. */
export function legendLines(chain: ChainResponse): string[] {
  const lines = [`Rows from: ${chain.store_label}.`, ...(chain.notes || [])];
  if (chain.columns.greeks === 'computed') {
    lines.push(
      `${COMPUTED_MARKER} computed by this viewer (${chain.greeks_model}) — model output, ` +
        `not exchange data.`,
    );
  }
  return lines;
}
