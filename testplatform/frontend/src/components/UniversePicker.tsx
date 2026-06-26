// frontend/src/components/UniversePicker.tsx
import { useEffect, useRef, useState } from 'react';
import { parseSymbols } from '../lib/symbols';

// Per-field optimization range for a screener metric the GA can tune. Keyed (in
// screener_param_ranges) by the UNPREFIXED metric-store name (e.g. market_cap_min).
export interface ScreenerOptRange { min: number; max: number; step: number; optimize: boolean; }

export type UniverseValue =
  | { mode: 'static'; symbols: string[] }
  | {
      mode: 'screener';
      // Path to the prebuilt metric_store parquet dir (built by `ba2-test build-screener-metrics`).
      // The candidate universe = the store's symbol union; the engine gates entries PER BAR
      // (point-in-time) from it. Required to run a screener backtest.
      screener_store: string;
      screener_settings: Record<string, number | string>;
      // Optional rebuild cadence for the per-bar screen (days); backend defaults to 7 (weekly).
      screener_cadence_days?: number;
      // Optional GA ranges for the optimizer; absent => behaves as a plain backtest.
      screener_param_ranges?: Record<string, ScreenerOptRange>;
    };

// All screener settings stored under screener_settings with the screener_ prefix.
// Backend screen_universe_for_day supports every field below.
const SCREENER_NUMBER_FIELDS: [string, string][] = [
  ['screener_market_cap_min', 'Market cap min'], ['screener_market_cap_max', 'Market cap max'],
  ['screener_volume_min', 'Volume min'], ['screener_volume_max', 'Volume max'],
  ['screener_float_min', 'Free float min'], ['screener_float_max', 'Free float max'],
  ['screener_price_min', 'Price min'], ['screener_price_max', 'Price max'],
  ['screener_relative_volume_min', 'RVOL min'],
  ['screener_price_drop_pct', 'Price drop % (dip min)'],
  ['screener_price_drop_days', 'Price drop lookback (days)'],
  ['screener_max_stocks', 'Max stocks'],
];

const SORT_METRIC_CHOICES: string[] = ['market_cap', 'relative_volume', 'price_drop_pct', 'float_shares'];

// Fields the GA can tune (mirrors the CLI _SCREENER_OPT). Each entry: unprefixed
// metric-store key (the screener_param_ranges key), a label, and a default range.
const SCREENER_OPT_FIELDS: { key: string; label: string; def: ScreenerOptRange }[] = [
  { key: 'market_cap_min', label: 'Market cap min', def: { min: 1e9, max: 1e11, step: 1e10, optimize: true } },
  { key: 'float_min', label: 'Free float min', def: { min: 1e7, max: 5e8, step: 5e7, optimize: true } },
  { key: 'relative_volume_min', label: 'RVOL min', def: { min: 1.0, max: 5.0, step: 0.5, optimize: true } },
  { key: 'price_drop_pct', label: 'Price drop %', def: { min: 1.0, max: 15.0, step: 1.0, optimize: true } },
  { key: 'price_drop_days', label: 'Price drop lookback (days)', def: { min: 2, max: 30, step: 1, optimize: true } },
  { key: 'max_stocks', label: 'Max stocks', def: { min: 5, max: 50, step: 5, optimize: true } },
  { key: 'weinstein_stage2_only', label: 'Weinstein stage-2 only', def: { min: 0, max: 1, step: 1, optimize: true } },
];

const inputClass = "px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500";
const rangeInputClass = "w-20 px-1.5 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100";

export function UniversePicker({ value, onChange }: { value: UniverseValue; onChange: (v: UniverseValue) => void; }) {
  const fileRef = useRef<HTMLInputElement>(null);
  // Raw text the user is editing. Kept SEPARATE from value.symbols.join() so typing a
  // separator (',', space, newline) or a symbol char ('.', '/' as in BRK.B / BRK/B) is not
  // clobbered by re-deriving the textarea from the parsed array on every keystroke (which
  // dropped the trailing separator, making it impossible to start a second symbol).
  const [symbolsText, setSymbolsText] = useState(
    value.mode === 'static' ? value.symbols.join(', ') : '',
  );
  // Re-sync from props ONLY on external changes (import / Quick Load / clear). During typing
  // our own onChange already set symbols = parseSymbols(text), so the parsed-vs-props check
  // matches and we leave the user's raw text (and trailing separators) intact.
  useEffect(() => {
    if (value.mode !== 'static') return;
    if (parseSymbols(symbolsText).join(',') !== value.symbols.join(',')) {
      setSymbolsText(value.symbols.join(', '));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const onFile = (f: File | undefined) => {
    if (!f) return;
    f.text().then((t) => onChange({ mode: 'static', symbols: parseSymbols(t) }));
  };

  // Snapshot the screener variant for the helpers below. Each helper rebuilds the
  // discriminated-union value so existing callers keep getting a typed UniverseValue.
  const screenerStore = value.mode === 'screener' ? (value.screener_store ?? '') : '';
  const screenerCadence = value.mode === 'screener' ? value.screener_cadence_days : undefined;
  const screenerSettings = value.mode === 'screener' ? value.screener_settings : {};
  const screenerRanges = value.mode === 'screener' ? (value.screener_param_ranges ?? {}) : {};

  // Rebuild the screener value, preserving store/cadence/settings/ranges, applying overrides.
  const emitScreener = (over: Partial<Extract<UniverseValue, { mode: 'screener' }>> = {}) =>
    onChange({
      mode: 'screener',
      screener_store: screenerStore,
      screener_settings: screenerSettings,
      ...(screenerCadence != null ? { screener_cadence_days: screenerCadence } : {}),
      ...(Object.keys(screenerRanges).length ? { screener_param_ranges: screenerRanges } : {}),
      ...over,
    });

  const setSetting = (k: string, v: number | string) =>
    emitScreener({ screener_settings: { ...screenerSettings, [k]: v } });

  const setRange = (key: string, on: boolean, range?: Partial<ScreenerOptRange>, def?: ScreenerOptRange) => {
    const ranges = { ...screenerRanges };
    if (on) {
      const base = ranges[key] ?? def ?? { min: 0, max: 0, step: 0, optimize: true };
      ranges[key] = {
        min: range?.min ?? base.min,
        max: range?.max ?? base.max,
        step: range?.step ?? base.step,
        optimize: true,
      };
    } else {
      delete ranges[key];
    }
    emitScreener({ screener_param_ranges: Object.keys(ranges).length ? ranges : undefined });
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-4">
        <label className="flex items-center gap-1 text-sm text-gray-700 dark:text-gray-300">
          <input type="radio" checked={value.mode === 'static'} onChange={() => onChange({ mode: 'static', symbols: value.mode === 'static' ? value.symbols : [] })} /> Static list
        </label>
        <label className="flex items-center gap-1 text-sm text-gray-700 dark:text-gray-300">
          <input type="radio" checked={value.mode === 'screener'} onChange={() => onChange({ mode: 'screener', screener_store: screenerStore, screener_settings: value.mode === 'screener' ? value.screener_settings : {} })} /> Screener
        </label>
      </div>

      {value.mode === 'static' ? (
        <div className="space-y-2">
          <textarea className={`${inputClass} w-full`} rows={3} placeholder="AAPL, MSFT, BRK.B …"
            value={symbolsText}
            onChange={(e) => { setSymbolsText(e.target.value); onChange({ mode: 'static', symbols: parseSymbols(e.target.value) }); }} />
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => fileRef.current?.click()}
              className="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-1">
              ⬆ Import from .txt
            </button>
            <input ref={fileRef} type="file" accept=".txt,text/plain" className="hidden"
              onChange={(e) => onFile(e.target.files?.[0])} />
            <span className="text-xs text-gray-500 dark:text-gray-400">{value.symbols.length} symbols</span>
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {/* metric_store dir: the candidate universe + per-bar (point-in-time) screen source. */}
          <div className="flex items-center justify-between gap-3 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
            <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">
              Metric store dir
              <span className="block text-xs text-gray-500 dark:text-gray-400">built by <code>ba2-test build-screener-metrics</code></span>
            </span>
            <input type="text" className={`${inputClass} w-64`} placeholder="/path/to/metric_store"
              value={screenerStore}
              onChange={(e) => emitScreener({ screener_store: e.target.value })} />
          </div>
          <div className="flex items-center justify-between gap-3 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
            <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">Screen cadence (days)</span>
            <input type="number" className={`${inputClass} w-24`} placeholder="7"
              value={screenerCadence ?? ''}
              onChange={(e) => emitScreener({ screener_cadence_days: e.target.value === '' ? undefined : Number(e.target.value) })} />
          </div>

          {SCREENER_NUMBER_FIELDS.map(([k, label]) => (
            <div key={k} className="flex items-center justify-between gap-3 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
              <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">{label}</span>
              <input type="number" className={`${inputClass} w-24`} value={Number(screenerSettings[k] ?? 0)}
                onChange={(e) => setSetting(k, Number(e.target.value))} />
            </div>
          ))}

          {/* Weinstein stage-2 only (checkbox -> 0/1) */}
          <div className="flex items-center justify-between gap-3 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
            <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">Weinstein stage-2 only</span>
            <input type="checkbox" className="rounded"
              checked={Number(screenerSettings['screener_weinstein_stage2_only'] ?? 0) === 1}
              onChange={(e) => setSetting('screener_weinstein_stage2_only', e.target.checked ? 1 : 0)} />
          </div>

          {/* Sort metric (select) */}
          <div className="flex items-center justify-between gap-3 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
            <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">Sort metric</span>
            <select className={inputClass}
              value={String(screenerSettings['screener_sort_metric'] ?? 'market_cap')}
              onChange={(e) => setSetting('screener_sort_metric', e.target.value)}>
              {SORT_METRIC_CHOICES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>

          {/* Optimize ranges (GA-tunable screener metrics) */}
          <div className="pt-1 space-y-1">
            <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Optimize ranges</div>
            {SCREENER_OPT_FIELDS.map(({ key, label, def }) => {
              const r = screenerRanges[key];
              return (
                <div key={key} className="flex items-center justify-between gap-2 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
                  <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">{label}</span>
                  <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400">
                    <input type="checkbox" className="rounded" checked={!!r}
                      onChange={(e) => setRange(key, e.target.checked, undefined, def)} /> Opt
                    {r && (<>
                      <input type="number" placeholder="min" value={r.min} className={rangeInputClass}
                        onChange={(e) => setRange(key, true, { ...r, min: Number(e.target.value) })} />
                      <input type="number" placeholder="max" value={r.max} className={rangeInputClass}
                        onChange={(e) => setRange(key, true, { ...r, max: Number(e.target.value) })} />
                      <input type="number" placeholder="step" value={r.step} className={rangeInputClass}
                        onChange={(e) => setRange(key, true, { ...r, step: Number(e.target.value) })} />
                    </>)}
                  </label>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
