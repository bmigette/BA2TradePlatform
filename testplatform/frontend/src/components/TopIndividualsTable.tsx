import type { OptIndividual } from '../lib/btApi';

/** Format a fitness/number to a fixed number of decimals, with an em-dash fallback. */
function fmt(v?: number | null, n = 3): string {
  return typeof v === 'number' && isFinite(v) ? v.toFixed(n) : '–';
}

/** Render a single gene value compactly (round floats; pass through ints/strings/bools). */
function fmtVal(v: unknown): string {
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
  }
  if (typeof v === 'boolean') return v ? 'on' : 'off';
  return String(v);
}

/**
 * Compact view of the genes that distinguish individuals: the strategy-level tp/sl plus a
 * couple of expert `model:*` genes (the RM/decision settings). Other namespaces (cond:*,
 * exit:*) are skipped to keep the cell readable. Returns '' when there's nothing to show.
 */
function paramsPreview(params?: Record<string, unknown>): string {
  if (!params) return '';
  const bits: string[] = [];
  if ('tp' in params) bits.push(`tp ${fmtVal(params.tp)}`);
  if ('sl' in params) bits.push(`sl ${fmtVal(params.sl)}`);
  const modelGenes = Object.keys(params)
    .filter(k => k.startsWith('model:'))
    .slice(0, 2);
  for (const k of modelGenes) {
    bits.push(`${k.slice('model:'.length)} ${fmtVal(params[k])}`);
  }
  return bits.join(' · ');
}

/**
 * Shared top-individuals table used by both the running-jobs panel (live) and the Opt-History
 * panel (completed jobs). Rows are already ranked best-first by the backend (`_top_individuals`).
 *
 * Styled to match RunHistoryTable: a header row with a tinted background + bottom border, row
 * dividers, a visible row hover, and right-aligned numeric columns. Columns: # | <metric> |
 * trades | params.
 *
 * Interactivity is opt-in (the running panel renders it read-only):
 *  - `onSelect(ind)` makes rows clickable (used in Opt-History to load an individual's backtest);
 *  - `selectedRank` highlights the active row;
 *  - `onExport(ind)` renders a per-row "Export" button (downloads that individual's params).
 */
export function TopIndividualsTable({
  individuals,
  fitnessMetric,
  note,
  onSelect,
  selectedRank,
  onExport,
}: {
  individuals?: OptIndividual[];
  fitnessMetric?: string;
  note?: string;
  onSelect?: (ind: OptIndividual) => void;
  selectedRank?: number | null;
  onExport?: (ind: OptIndividual) => void;
}) {
  const top = individuals ?? [];
  if (top.length === 0) {
    return (
      <div className="text-xs text-gray-500 dark:text-gray-400">
        No individuals evaluated yet.
      </div>
    );
  }
  const clickable = !!onSelect;
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
            <tr>
              <th className="px-3 py-2 text-left font-semibold text-gray-700 dark:text-gray-300">#</th>
              <th className="px-3 py-2 text-right font-semibold text-gray-700 dark:text-gray-300">{fitnessMetric ?? 'fitness'}</th>
              <th className="px-3 py-2 text-right font-semibold text-gray-700 dark:text-gray-300">trades</th>
              <th className="px-3 py-2 text-left font-semibold text-gray-700 dark:text-gray-300">params</th>
              {onExport && <th className="px-3 py-2 text-right font-semibold text-gray-700 dark:text-gray-300"></th>}
            </tr>
          </thead>
          <tbody>
            {top.map(ind => {
              const preview = paramsPreview(ind.params);
              const isSelected = selectedRank != null && ind.rank === selectedRank;
              return (
                <tr
                  key={ind.rank}
                  onClick={clickable ? () => onSelect!(ind) : undefined}
                  className={`border-b border-gray-200 dark:border-gray-700 transition-colors ${
                    isSelected
                      ? 'bg-blue-50 dark:bg-blue-900/20'
                      : clickable ? 'hover:bg-gray-50 dark:hover:bg-gray-700/50' : ''
                  } ${clickable ? 'cursor-pointer' : ''}`}
                >
                  <td className="px-3 py-2 text-gray-700 dark:text-gray-300">{ind.rank}</td>
                  <td className="px-3 py-2 text-right font-semibold text-gray-900 dark:text-gray-100">{fmt(ind.fitness)}</td>
                  <td className="px-3 py-2 text-right text-gray-800 dark:text-gray-200">{ind.nTrades ?? '–'}</td>
                  <td className="px-3 py-2 font-mono text-gray-700 dark:text-gray-300">
                    {preview || <span className="text-gray-400 dark:text-gray-500">–</span>}
                  </td>
                  {onExport && (
                    <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); onExport(ind); }}
                        title="Export this individual's params as JSON"
                        className="px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
                      >
                        Export
                      </button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {note && (
        <div className="px-3 py-2 text-[11px] text-gray-500 dark:text-gray-400 border-t border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-700/30">{note}</div>
      )}
    </div>
  );
}
