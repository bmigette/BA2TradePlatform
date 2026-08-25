// Read-only broker-style view of the option data the backtest actually holds.
//
// Layout is the Schwab shape: expiries as collapsible groups, each with a header row
// carrying DTE and a volatility summary, then one row per strike with the strike DOWN THE
// MIDDLE, calls to its left and puts to its right.
//
// The presentation rules that keep it honest (n/a-with-a-reason instead of 0.00, no
// bid/ask spread for a store that has none, computed greeks marked as model output) live
// in ../lib/optionChain.ts and are unit-tested there. This file is wiring and markup.
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Info,
  Loader,
  Search,
  XCircle,
} from 'lucide-react';
import { API_BASE } from '../lib/config';
import {
  COMPUTED_MARKER,
  NA,
  headerFor,
  legendLines,
  priceColumnIds,
  renderCell,
  type Cell,
  type ChainResponse,
  type DateEntry,
  type Leg,
  type StoreInfo,
} from '../lib/optionChain';

const API = `${API_BASE}/cache/options`;

/** Digits per field. Prices 2dp, greeks 4dp, counts 0dp. */
const DIGITS: Record<string, number> = {
  open: 2, high: 2, low: 2, close: 2, bid: 2, ask: 2,
  iv: 4, delta: 4, gamma: 4, theta: 4, vega: 4,
  volume: 0, open_interest: 0,
};

const GREEK_FIELDS = ['delta', 'gamma', 'theta', 'vega'];

const TONE_CLASS: Record<string, string> = {
  value: 'text-gray-900 dark:text-gray-100',
  muted: 'text-gray-400 dark:text-gray-500 italic',
  computed: 'text-indigo-600 dark:text-indigo-400',
  derived: 'text-teal-700 dark:text-teal-400',
};

const CellText: React.FC<{ cell?: Cell | null; digits: number }> = ({ cell, digits }) => {
  const r = renderCell(cell, digits);
  return (
    <span className={TONE_CLASS[r.tone]} title={r.title}>
      {r.text}
    </span>
  );
};

const OptionChainViewer: React.FC = () => {
  const [stores, setStores] = useState<StoreInfo[]>([]);
  const [storeId, setStoreId] = useState<string>('alpaca-chain');
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<{ symbol: string; stores: string[] }[]>([]);
  const [symbol, setSymbol] = useState<string>('');
  const [dates, setDates] = useState<Record<string, { present: boolean; dates: DateEntry[]; absent_reason: string | null }>>({});
  const [asOf, setAsOf] = useState('');
  const [spot, setSpot] = useState('');
  const [rate, setRate] = useState('0.04');
  const [chain, setChain] = useState<ChainResponse | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // -- stores -------------------------------------------------------------
  useEffect(() => {
    fetch(`${API}/stores`)
      .then((r) => r.json())
      .then((d) => setStores(d.stores || []))
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not read cache stores'));
  }, []);

  const store = useMemo(() => stores.find((s) => s.id === storeId), [stores, storeId]);

  // -- symbol search (debounced) -----------------------------------------
  // The empty-query case is handled in the input's onChange, not here: clearing state
  // synchronously inside an effect body cascades a second render for nothing.
  useEffect(() => {
    const q = query.trim();
    if (!q) return;
    const t = setTimeout(() => {
      fetch(`${API}/symbols?q=${encodeURIComponent(q)}`)
        .then((r) => r.json())
        .then((d) => setMatches(d.symbols || []))
        .catch(() => setMatches([]));
    }, 200);
    return () => clearTimeout(t);
  }, [query]);

  // -- available as-of dates ---------------------------------------------
  const loadDates = useCallback((sym: string) => {
    setDates({});
    setAsOf('');
    setChain(null);
    fetch(`${API}/dates?symbol=${encodeURIComponent(sym)}`)
      .then((r) => r.json())
      .then((d) => setDates(d.stores || {}))
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not read as-of dates'));
  }, []);

  const pickSymbol = (sym: string) => {
    setSymbol(sym);
    setQuery(sym);
    setMatches([]);
    loadDates(sym);
  };

  const storeDates = dates[storeId];
  // The picker offers ONLY dates that exist. A free calendar over a store holding three
  // snapshot dates would miss on almost every click and read as broken.
  const options = useMemo(() => storeDates?.dates || [], [storeDates]);

  // The selection is RESET at its two sources (store switch, symbol switch) rather than
  // reconciled in an effect — a date that exists in one store need not exist in another.
  const selectStore = (id: string) => {
    setStoreId(id);
    setAsOf('');
    setChain(null);
  };

  // -- chain --------------------------------------------------------------
  const loadChain = async () => {
    if (!symbol || !asOf) return;
    setLoading(true);
    setError(null);
    setChain(null);
    try {
      const p = new URLSearchParams({ symbol, as_of: asOf, store: storeId });
      if (spot.trim()) p.set('spot', spot.trim());
      if (rate.trim()) p.set('rate', rate.trim());
      const r = await fetch(`${API}/chain?${p.toString()}`);
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || `chain request failed (${r.status})`);
      setChain(body);
      setCollapsed({});
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error');
    } finally {
      setLoading(false);
    }
  };

  const priceCols = chain ? priceColumnIds(chain.columns) : [];
  const sideCols = chain ? [...GREEK_FIELDS, 'iv', 'open_interest', 'volume', ...priceCols] : [];

  const legHeader = (field: string, side: 'call' | 'put') => {
    if (!chain) return null;
    const h = headerFor(field, chain.columns);
    return (
      <th
        key={`${side}-${field}`}
        title={h.title}
        className="px-2 py-1 text-right text-[11px] font-semibold text-gray-600 dark:text-gray-300 whitespace-nowrap"
      >
        {h.label}
        {h.marker}
      </th>
    );
  };

  const legCells = (leg: Leg | null, side: 'call' | 'put') =>
    sideCols.map((f) => (
      <td key={`${side}-${f}`} className="px-2 py-0.5 text-right text-xs font-mono whitespace-nowrap">
        {leg ? <CellText cell={leg[f] as Cell} digits={DIGITS[f] ?? 2} /> : (
          <span className="text-gray-300 dark:text-gray-600" title="No contract at this strike on this side.">
            —
          </span>
        )}
      </td>
    ));

  return (
    <div className="space-y-4">
      {/* ---- store picker ---- */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        {stores.map((s) => (
          <button
            key={s.id}
            onClick={() => s.present && selectStore(s.id)}
            disabled={!s.present}
            className={`text-left p-3 rounded-lg border transition ${
              storeId === s.id
                ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800'
            } ${s.present ? 'hover:border-blue-400' : 'opacity-60 cursor-not-allowed'}`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold text-sm text-gray-900 dark:text-gray-100">{s.label}</span>
              {s.present ? (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300">
                  present
                </span>
              ) : (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
                  absent
                </span>
              )}
            </div>
            <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">{s.description}</p>
            {!s.present && s.absent_reason && (
              <p className="mt-1 text-xs text-orange-600 dark:text-orange-400">{s.absent_reason}</p>
            )}
            <div className="mt-2 flex flex-wrap gap-1 text-[10px]">
              {[
                ['IV', s.has_iv],
                ['Open interest', s.has_open_interest],
                ['Vendor greeks', s.has_greeks],
                ['Bid/ask spread', s.has_quote_spread],
              ].map(([label, ok]) => (
                <span
                  key={label as string}
                  className={`px-1.5 py-0.5 rounded ${
                    ok
                      ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 line-through'
                  }`}
                >
                  {label as string}
                </span>
              ))}
            </div>
          </button>
        ))}
      </div>

      {store && !store.has_quote_spread && (
        <div className="flex items-start gap-2 p-3 rounded-md bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-300 text-xs">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          <span>{store.quote_note}</span>
        </div>
      )}

      {/* ---- query bar ---- */}
      <div className="flex flex-wrap items-end gap-3 p-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg">
        <div className="relative">
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-0.5">Symbol</label>
          <div className="relative">
            <Search size={14} className="absolute left-2 top-2.5 text-gray-400" />
            <input
              value={query}
              onChange={(e) => {
                const v = e.target.value.toUpperCase();
                setQuery(v);
                if (!v.trim()) setMatches([]);
              }}
              placeholder="AAPL"
              className="pl-7 pr-2 py-1.5 w-40 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
          </div>
          {matches.length > 0 && (
            <ul className="absolute z-10 mt-1 w-40 max-h-60 overflow-auto bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded shadow-lg">
              {matches.map((m) => (
                <li key={m.symbol}>
                  <button
                    onClick={() => pickSymbol(m.symbol)}
                    className="w-full text-left px-2 py-1 text-sm hover:bg-blue-50 dark:hover:bg-gray-600 text-gray-900 dark:text-gray-100"
                  >
                    <span className="font-mono">{m.symbol}</span>
                    <span className="ml-2 text-[10px] text-gray-500 dark:text-gray-400">
                      {m.stores.length} store{m.stores.length === 1 ? '' : 's'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-0.5">
            As-of date (only dates with data)
          </label>
          <select
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            disabled={!options.length}
            className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 disabled:opacity-50"
          >
            <option value="">
              {symbol
                ? options.length
                  ? `${options.length} cached date${options.length === 1 ? '' : 's'}…`
                  : 'no cached dates for this symbol in this store'
                : 'pick a symbol first'}
            </option>
            {options.map((d) => (
              <option key={d.as_of} value={d.as_of}>
                {d.as_of} ({d.rows} rows)
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-0.5"
            title="The caches store no underlying price. Greeks cannot be computed without one."
          >
            Spot (for computed greeks)
          </label>
          <input
            value={spot}
            onChange={(e) => setSpot(e.target.value)}
            placeholder={store?.has_iv ? 'e.g. 199.00' : 'no IV in this store'}
            disabled={!store?.has_iv}
            className="px-2 py-1.5 w-32 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 disabled:opacity-50"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-0.5">Rate</label>
          <input
            value={rate}
            onChange={(e) => setRate(e.target.value)}
            disabled={!store?.has_iv}
            className="px-2 py-1.5 w-20 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 disabled:opacity-50"
          />
        </div>

        <button
          onClick={loadChain}
          disabled={!symbol || !asOf || loading}
          className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2 text-sm"
        >
          {loading ? <Loader size={14} className="animate-spin" /> : <Search size={14} />}
          Load chain
        </button>
      </div>

      {error && (
        <div className="p-3 rounded-md bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 text-sm flex items-start gap-2">
          <XCircle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* ---- the chain ---- */}
      {chain && (
        <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
          <div className="px-4 py-2 border-b border-gray-200 dark:border-gray-700 flex flex-wrap items-center gap-3">
            <span className="font-semibold text-gray-900 dark:text-gray-100">
              {chain.symbol} @ {chain.as_of}
            </span>
            <span className="text-xs px-2 py-0.5 rounded bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
              {chain.store_label}
            </span>
            <span className="text-xs text-gray-500 dark:text-gray-400">{chain.contracts} contracts</span>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              Spot:{' '}
              <span title={chain.spot.reason || ''} className={chain.spot.value === null ? 'italic' : ''}>
                {chain.spot.value === null ? NA : chain.spot.value}
              </span>
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-gray-50 dark:bg-gray-900/40">
                <tr>
                  <th
                    colSpan={sideCols.length}
                    className="px-2 py-1 text-center text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 border-r border-gray-200 dark:border-gray-700"
                  >
                    Calls
                  </th>
                  <th className="px-2 py-1 text-center text-[11px] uppercase tracking-wide text-gray-700 dark:text-gray-200">
                    Strike
                  </th>
                  <th
                    colSpan={sideCols.length}
                    className="px-2 py-1 text-center text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 border-l border-gray-200 dark:border-gray-700"
                  >
                    Puts
                  </th>
                </tr>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  {sideCols.map((f) => legHeader(f, 'call'))}
                  <th className="px-2 py-1 border-x border-gray-200 dark:border-gray-700" />
                  {[...sideCols].reverse().map((f) => legHeader(f, 'put'))}
                </tr>
              </thead>
              <tbody>
                {chain.expiries.map((g) => {
                  const isOpen = !collapsed[g.expiry];
                  const ivm = renderCell(g.iv_median, 4);
                  return (
                    <React.Fragment key={g.expiry}>
                      <tr
                        className="bg-gray-100 dark:bg-gray-700/60 cursor-pointer"
                        onClick={() => setCollapsed((c) => ({ ...c, [g.expiry]: isOpen }))}
                      >
                        <td colSpan={sideCols.length * 2 + 1} className="px-3 py-1.5">
                          <span className="inline-flex items-center gap-2 font-semibold text-gray-800 dark:text-gray-100">
                            {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                            {g.expiry}
                            <span className="font-normal text-gray-600 dark:text-gray-400">
                              {g.dte} DTE
                            </span>
                            <span
                              className="font-normal text-gray-600 dark:text-gray-400"
                              title={ivm.title}
                            >
                              IV (median of cached rows):{' '}
                              <span className={TONE_CLASS[ivm.tone]}>{ivm.text}</span>
                            </span>
                            <span className="font-normal text-gray-500 dark:text-gray-500">
                              {g.rows.length} strikes
                            </span>
                          </span>
                        </td>
                      </tr>
                      {isOpen &&
                        g.rows.map((row) => (
                          <tr
                            key={`${g.expiry}-${row.strike}`}
                            className="border-b border-gray-100 dark:border-gray-700/50 hover:bg-blue-50/50 dark:hover:bg-gray-700/30"
                          >
                            {legCells(row.call, 'call')}
                            <td className="px-3 py-0.5 text-center font-mono font-semibold text-gray-900 dark:text-gray-100 bg-gray-50 dark:bg-gray-900/40 border-x border-gray-200 dark:border-gray-700">
                              {row.strike}
                            </td>
                            {legCells(row.put, 'put')}
                          </tr>
                        ))}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="px-4 py-3 border-t border-gray-200 dark:border-gray-700 space-y-1">
            {legendLines(chain).map((line, i) => (
              <p key={i} className="text-[11px] text-gray-600 dark:text-gray-400 flex items-start gap-1.5">
                <Info size={12} className="mt-0.5 shrink-0" />
                <span>{line}</span>
              </p>
            ))}
            <p className="text-[11px] text-gray-500 dark:text-gray-500 flex items-start gap-1.5">
              <Info size={12} className="mt-0.5 shrink-0" />
              <span>
                <span className="italic">{NA}</span> means the field is absent from this store, not
                zero — hover any cell for the reason. {COMPUTED_MARKER} marks a number this viewer
                computed.
              </span>
            </p>
          </div>
        </div>
      )}
    </div>
  );
};

export default OptionChainViewer;
