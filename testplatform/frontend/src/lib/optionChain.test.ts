import { describe, it, expect } from 'vitest';
import {
  NA,
  COMPUTED_MARKER,
  renderCell,
  priceColumnIds,
  headerFor,
  legendLines,
  type Cell,
  type ChainColumns,
  type ChainResponse,
} from './optionChain';

const LEGACY_COLS: ChainColumns = {
  quote: 'close',
  has_quote_spread: false,
  iv: false,
  open_interest: false,
  volume: false,
  greeks: 'unavailable',
};

const PARQUET_COLS: ChainColumns = {
  quote: 'ohlc',
  has_quote_spread: false,
  iv: true,
  open_interest: true,
  volume: true,
  greeks: 'computed',
};

const cell = (c: Partial<Cell>): Cell =>
  ({ value: null, source: 'unavailable', reason: null, ...c }) as Cell;

describe('renderCell — an absent value is never a zero', () => {
  it('renders a NULL greek as n/a, not 0.00', () => {
    const r = renderCell(cell({ value: null, source: 'unavailable', reason: 'iv is NULL' }));
    expect(r.text).toBe(NA);
    expect(r.text).not.toBe('0.00');
    expect(r.text).not.toBe('0');
    expect(r.tone).toBe('muted');
  });

  it('carries the reason so the user can see WHY it is absent', () => {
    const r = renderCell(cell({ reason: 'the build never recorded this column' }));
    expect(r.title).toContain('never recorded');
  });

  it('renders a missing cell entirely as n/a rather than throwing', () => {
    expect(renderCell(undefined).text).toBe(NA);
    expect(renderCell(null).text).toBe(NA);
  });

  it('renders a RECORDED zero as zero — a fact, not an absence', () => {
    const r = renderCell(cell({ value: 0, source: 'cache' }), 0);
    expect(r.text).toBe('0');
    expect(r.tone).toBe('value');
    // and it is distinguishable from the absent case above
    expect(r.text).not.toBe(NA);
  });

  it('formats a cached number at the requested precision', () => {
    expect(renderCell(cell({ value: 12.5, source: 'cache' }), 2).text).toBe('12.50');
    expect(renderCell(cell({ value: 3410, source: 'cache' }), 0).text).toBe('3410');
  });
});

describe('renderCell — a computed greek is not exchange data', () => {
  it('marks a computed value and says so in the tooltip', () => {
    const r = renderCell(
      cell({ value: 0.4231, source: 'computed', reason: 'Black-Scholes-Merton' }),
      4,
    );
    expect(r.text).toBe(`0.4231${COMPUTED_MARKER}`);
    expect(r.tone).toBe('computed');
    expect(r.title.toLowerCase()).toContain('computed');
    expect(r.title.toLowerCase()).toContain('not exchange data');
  });

  it('never labels a computed value as exchange/cache data', () => {
    const r = renderCell(cell({ value: 0.5, source: 'computed', reason: 'BSM' }));
    expect(r.tone).not.toBe('value');
    // "exchange data" may appear ONLY as part of a denial ("not exchange data").
    expect(r.title.toLowerCase()).not.toMatch(/(?<!not )exchange data/);
    expect(r.title.toLowerCase()).not.toContain('as stored');
  });

  it('does NOT mark a cached value as computed', () => {
    const r = renderCell(cell({ value: 0.5, source: 'cache' }), 2);
    expect(r.text).toBe('0.50');
    expect(r.text).not.toContain(COMPUTED_MARKER);
    expect(r.tone).toBe('value');
  });

  it('marks a derived summary distinctly from both', () => {
    const r = renderCell(cell({ value: 0.3, source: 'derived', reason: 'median of 2' }), 2);
    expect(r.tone).toBe('derived');
    expect(r.title).toContain('median of 2');
    expect(r.title.toLowerCase()).toContain('derived');
  });
});

describe('priceColumnIds — the legacy bid==ask is never shown as a spread', () => {
  it('gives the legacy chain a single close column and no bid/ask', () => {
    const ids = priceColumnIds(LEGACY_COLS);
    expect(ids).toEqual(['close']);
    expect(ids).not.toContain('bid');
    expect(ids).not.toContain('ask');
  });

  it('gives an OHLC store its four price columns and still no bid/ask', () => {
    const ids = priceColumnIds(PARQUET_COLS);
    expect(ids).toEqual(['open', 'high', 'low', 'close']);
    expect(ids).not.toContain('bid');
    expect(ids).not.toContain('ask');
  });

  it('only ever shows bid/ask for a store that declares a real spread', () => {
    const ids = priceColumnIds({ ...PARQUET_COLS, has_quote_spread: true });
    expect(ids).toContain('bid');
    expect(ids).toContain('ask');
  });
});

describe('headerFor — the column labels tell the truth about the store', () => {
  it('marks greek headers as computed when they are computed', () => {
    const h = headerFor('delta', PARQUET_COLS);
    expect(h.marker).toBe(COMPUTED_MARKER);
    expect(h.title.toLowerCase()).toContain('computed');
  });

  it('says greeks are not computable when the store has no IV', () => {
    const h = headerFor('delta', LEGACY_COLS);
    expect(h.marker).toBe('');
    expect(h.title.toLowerCase()).toContain('not computable');
  });

  it('labels IV as absent for a store that records none', () => {
    expect(headerFor('iv', LEGACY_COLS).title.toLowerCase()).toContain('no implied volatility');
    expect(headerFor('iv', PARQUET_COLS).title.toLowerCase()).toContain('vendor');
  });

  it('labels open interest as absent for a store that records none', () => {
    expect(headerFor('open_interest', LEGACY_COLS).title.toLowerCase()).toContain('not recorded');
  });

  it('labels the legacy close as a placeholder-free single price', () => {
    expect(headerFor('close', LEGACY_COLS).label).toBe('Close');
  });
});

describe('legendLines — the footnotes match what is on screen', () => {
  const base: ChainResponse = {
    symbol: 'AAPL',
    as_of: '2026-06-09',
    store: 'alpaca-chain',
    store_label: 'Legacy chain snapshots (Alpaca sqlite)',
    columns: LEGACY_COLS,
    spot: { value: null, source: 'unavailable', reason: 'no spot' },
    greeks_model: 'Black-Scholes-Merton',
    greeks_inputs: { rate: 0, dividend_yield: 0, day_count: 'actual/365' },
    contracts: 12,
    expiries: [],
    notes: ['bid == ask == close in every quoted row'],
  };

  it('passes the backend notes through verbatim', () => {
    expect(legendLines(base)).toContain('bid == ask == close in every quoted row');
  });

  it('adds the computed-greek marker legend only when greeks are computed', () => {
    const off = legendLines(base).join(' ');
    expect(off).not.toContain(COMPUTED_MARKER);

    const on = legendLines({
      ...base,
      columns: PARQUET_COLS,
      store: 'tastytrade-parquet',
    }).join(' ');
    expect(on).toContain(COMPUTED_MARKER);
    expect(on.toLowerCase()).toContain('not exchange data');
  });

  it('always names the store the rows came from', () => {
    expect(legendLines(base).join(' ')).toContain('Legacy chain snapshots (Alpaca sqlite)');
  });
});
