import { useEffect, useMemo, useState } from 'react';
import { listBacktests, saveBacktest, fetchBacktestExport, deleteBacktest } from '../lib/btApi';
import type { ExportKind } from '../lib/btApi';
import { ExportDialog } from './ExportDialog';
import { usePersistentState } from '../lib/usePersistentState';

// Trigger a browser download of a JSON object via a Blob + temporary <a download>.
// No server filesystem write — the bytes are produced entirely client-side.
function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const inputClass = "px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500";

// Max drawdown is stored as a signed percent (e.g. -11.8). Render like the summary card: red, 1dp.
function fmtDrawdown(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? `${n.toFixed(1)}%` : '—';
}

export function RunHistoryTable({ savedOnly, onSelect, onLoad, selectedId, selectable, selectedIds, onToggleSelect, isSelectable }:
  { savedOnly: boolean; onSelect: (id: number) => void; onLoad?: (id: number) => void; selectedId?: number | null;
    // Robustness multi-select (Task 6). When `selectable` is on, a leading checkbox column appears;
    // `isSelectable(row)` gates which rows can be checked (e.g. saved+completed daily_expert only).
    selectable?: boolean; selectedIds?: Set<number>; onToggleSelect?: (row: any) => void;
    isSelectable?: (row: any) => boolean; }) {
  const [rows, setRows] = useState<any[]>([]);
  // Filters / search / sort persist for the session (sessionStorage) — survive reloads + tab
  // switches. The Saved tab and the BT-History tab are separate instances; key by `savedOnly` so
  // each keeps its own sticky filters.
  const ns = `bt:runhist:${savedOnly ? 'saved' : 'all'}:`;
  const [expert, setExpert] = usePersistentState(ns + 'expert', '');
  const [optId, setOptId] = usePersistentState(ns + 'optId', '');
  const [q, setQ] = usePersistentState(ns + 'q', '');
  // Bumped after a save/delete to re-run the fetch effect below.
  const [refresh, setRefresh] = useState(0);
  // The run whose Export dialog is open (null = closed).
  const [exportRow, setExportRow] = useState<any | null>(null);
  // Collapsible filter menu + numeric thresholds (client-side; expert/optId stay server-side).
  const [showFilters, setShowFilters] = usePersistentState(ns + 'showFilters', false);
  const [minSharpe, setMinSharpe] = usePersistentState(ns + 'minSharpe', '');
  const [minTrades, setMinTrades] = usePersistentState(ns + 'minTrades', '');
  const [minRet, setMinRet] = usePersistentState(ns + 'minRet', '');
  const [maxDD, setMaxDD] = usePersistentState(ns + 'maxDD', '');
  const [minWin, setMinWin] = usePersistentState(ns + 'minWin', '');
  // Sort state: column key + direction. Default newest-first (id desc).
  const [sortKey, setSortKey] = usePersistentState<string>(ns + 'sortKey', 'id');
  const [sortDir, setSortDir] = usePersistentState<'asc' | 'desc'>(ns + 'sortDir', 'desc');

  useEffect(() => {
    listBacktests({
      saved: savedOnly ? true : undefined,
      // BT History (savedOnly=false) lists STANDALONE backtests only — the optimization-derived
      // TOP-N rows live under the Opt History tab. Saved tab is unfiltered (shows all saved).
      single: savedOnly ? undefined : true,
      expert: expert || undefined,
      optimization_id: optId ? Number(optId) : undefined,
    })
      .then(setRows)
      .catch(() => setRows([]));
  }, [savedOnly, expert, optId, refresh]);

  const handleSave = async (r: any) => {
    const name = window.prompt('Save backtest as:', r.name || '');
    if (name == null) return;  // cancelled
    try {
      await saveBacktest(r.id, name);
      setRefresh(n => n + 1);
    } catch (e) {
      alert(`Save failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Called by the ExportDialog with the chosen kind: fetch the read-only payload and trigger a
  // browser download (no server-side file write). Closes the dialog regardless of outcome.
  const handleExport = async (kind: ExportKind) => {
    const r = exportRow;
    setExportRow(null);
    if (!r) return;
    try {
      const payload = await fetchBacktestExport(r.id, kind);
      const suffix = kind === 'expert_settings' ? 'expert-settings' : 'ruleset';
      downloadJson(`backtest-${r.id}-${suffix}.json`, payload);
    } catch (e) {
      alert(`Export failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const handleDelete = async (r: any) => {
    if (!window.confirm(`Delete backtest #${r.id} (${r.name || 'unnamed'})?`)) return;
    try {
      await deleteBacktest(r.id);
      setRefresh(n => n + 1);
    } catch (e) {
      alert(`Delete failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Field names confirmed against backend/app/api/backtests.py list endpoint
  // (camelCase: expertName / optimizationId / totalReturn / sharpeRatio / isSaved).
  // snake_case fallbacks kept for robustness per the plan.
  const experts = useMemo(
    () => Array.from(new Set(rows.map(r => r.expertName ?? r.expert_name).filter(Boolean))),
    [rows],
  );
  const optIds = useMemo(
    () => Array.from(new Set(rows.map(r => r.optimizationId ?? r.optimization_id).filter((x: any) => x != null))),
    [rows],
  );

  // Tolerant numeric coercion (camelCase + snake_case fields). Returns NaN when absent.
  const num = (v: unknown): number => { const n = typeof v === 'number' ? v : Number(v); return Number.isFinite(n) ? n : NaN; };
  // Column accessors keyed by header — used for both sorting and (numeric) display ordering.
  const accessors: Record<string, (r: any) => number | string> = {
    id: r => num(r.id),
    expert: r => String((r.expertName ?? r.expert_name) ?? (r.modelName ?? r.model_name) ?? r.engineType ?? '').toLowerCase(),
    opt: r => num(r.optimizationId ?? r.optimization_id),
    ret: r => num(r.totalReturn ?? r.total_return),
    sharpe: r => num(r.sharpeRatio ?? r.sharpe_ratio),
    trades: r => num(r.totalTrades ?? r.total_trades),
    dd: r => num(r.maxDrawdown ?? r.max_drawdown),
    win: r => num(r.winRate ?? r.win_rate),
    saved: r => ((r.isSaved ?? r.is_saved) ? 1 : 0),
    name: r => String(r.name ?? '').toLowerCase(),
  };

  // Number of active filter-menu thresholds (drives the "Filters (N)" badge + Clear button).
  const activeFilters = [expert, optId, minSharpe, minTrades, minRet, maxDD, minWin].filter(v => v !== '').length;
  const clearFilters = () => { setExpert(''); setOptId(''); setMinSharpe(''); setMinTrades(''); setMinRet(''); setMaxDD(''); setMinWin(''); };

  const filtered = rows.filter(r => {
    if (q && !(r.name || '').toLowerCase().includes(q.toLowerCase())) return false;
    if (minSharpe !== '' && !(num(r.sharpeRatio ?? r.sharpe_ratio) >= Number(minSharpe))) return false;
    if (minTrades !== '' && !(num(r.totalTrades ?? r.total_trades) >= Number(minTrades))) return false;
    if (minRet !== '' && !(num(r.totalReturn ?? r.total_return) >= Number(minRet))) return false;
    // DD is stored signed-negative; "max drawdown %" caps the magnitude.
    if (maxDD !== '' && !(Math.abs(num(r.maxDrawdown ?? r.max_drawdown)) <= Number(maxDD))) return false;
    if (minWin !== '' && !(num(r.winRate ?? r.win_rate) >= Number(minWin))) return false;
    return true;
  });

  const sorted = [...filtered].sort((a, b) => {
    const av = accessors[sortKey](a), bv = accessors[sortKey](b);
    let cmp: number;
    if (typeof av === 'string' || typeof bv === 'string') {
      cmp = String(av).localeCompare(String(bv));
    } else {
      const aN = Number.isFinite(av as number), bN = Number.isFinite(bv as number);
      if (!aN && !bN) return 0;       // both missing
      if (!aN) return 1;              // missing values always sink to the bottom
      if (!bN) return -1;
      cmp = (av as number) - (bv as number);
    }
    return sortDir === 'asc' ? cmp : -cmp;
  });

  const toggleSort = (key: string) => {
    if (sortKey === key) { setSortDir(d => (d === 'asc' ? 'desc' : 'asc')); }
    else { setSortKey(key); setSortDir(key === 'name' || key === 'expert' ? 'asc' : 'desc'); }  // numbers high-first, text A-Z
  };

  const columns: { key: string; label: string }[] = [
    { key: 'id', label: 'id' }, { key: 'expert', label: 'expert' }, { key: 'opt', label: 'opt#' },
    { key: 'ret', label: 'ret%' }, { key: 'sharpe', label: 'sharpe' }, { key: 'trades', label: 'trades' },
    { key: 'dd', label: 'DD%' }, { key: 'win', label: 'win%' }, { key: 'saved', label: 'saved' }, { key: 'name', label: 'name' },
  ];

  return (
    <>
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 p-3 bg-gray-50 dark:bg-gray-700/50 border-b border-gray-200 dark:border-gray-700">
        <input placeholder="search name" value={q} onChange={(e) => setQ(e.target.value)} className={inputClass} />
        <button
          type="button"
          onClick={() => setShowFilters(s => !s)}
          className={`px-2 py-1.5 text-sm border rounded ${activeFilters > 0
            ? 'border-blue-400 dark:border-blue-600 text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/20'
            : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-700'} hover:bg-gray-50 dark:hover:bg-gray-600`}
        >
          {showFilters ? '▾' : '▸'} Filters{activeFilters > 0 ? ` (${activeFilters})` : ''}
        </button>
        {activeFilters > 0 && (
          <button type="button" onClick={clearFilters}
            className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-600">
            Clear
          </button>
        )}
        <span className="ml-auto text-xs text-gray-500 dark:text-gray-400 self-center">{sorted.length} run{sorted.length === 1 ? '' : 's'}</span>
      </div>
      {showFilters && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 p-3 bg-gray-50 dark:bg-gray-800/40 border-b border-gray-200 dark:border-gray-700">
          <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-300">expert
            <select value={expert} onChange={(e) => setExpert(e.target.value)} className={inputClass}>
              <option value="">All experts</option>
              {experts.map(x => <option key={x}>{x}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-300">opt job
            <select value={optId} onChange={(e) => setOptId(e.target.value)} className={inputClass}>
              <option value="">All opt jobs</option>
              {optIds.map(x => <option key={x} value={x}>#{x}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-300">min sharpe
            <input type="number" step="0.1" placeholder="any" value={minSharpe} onChange={(e) => setMinSharpe(e.target.value)} className={inputClass} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-300">min trades
            <input type="number" step="1" placeholder="any" value={minTrades} onChange={(e) => setMinTrades(e.target.value)} className={inputClass} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-300">min ret %
            <input type="number" step="1" placeholder="any" value={minRet} onChange={(e) => setMinRet(e.target.value)} className={inputClass} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-300">max drawdown %
            <input type="number" step="1" placeholder="any" value={maxDD} onChange={(e) => setMaxDD(e.target.value)} className={inputClass} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-300">min win %
            <input type="number" step="1" placeholder="any" value={minWin} onChange={(e) => setMinWin(e.target.value)} className={inputClass} />
          </label>
        </div>
      )}
      <table className="w-full text-sm">
        <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
          <tr>
            {selectable && <th className="px-2 py-1 w-6" />}
            {columns.map(c => (
              <th key={c.key} onClick={() => toggleSort(c.key)}
                className="px-2 py-1 text-left text-xs font-medium text-gray-700 dark:text-gray-300 cursor-pointer select-none hover:text-gray-900 dark:hover:text-gray-100 whitespace-nowrap">
                {c.label}<span className="text-blue-500">{sortKey === c.key ? (sortDir === 'asc' ? ' ▲' : ' ▼') : ''}</span>
              </th>
            ))}
            <th className="px-2 py-1 text-left text-xs font-medium text-gray-700 dark:text-gray-300">Actions</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map(r => (
            <tr key={r.id} onClick={() => onSelect(r.id)}
              className={`border-b border-gray-200 dark:border-gray-600 cursor-pointer transition-colors ${
                r.id === selectedId
                  ? 'bg-blue-50 dark:bg-blue-900/30 ring-1 ring-inset ring-blue-400 dark:ring-blue-600'
                  : 'hover:bg-gray-50 dark:hover:bg-gray-700'}`}>
              {selectable && (
                <td className="px-2 py-1 w-6" onClick={(e) => e.stopPropagation()}>
                  {(!isSelectable || isSelectable(r)) ? (
                    <input
                      type="checkbox"
                      checked={selectedIds?.has(r.id) ?? false}
                      onChange={() => onToggleSelect?.(r)}
                      title="Select for robustness stress-test"
                    />
                  ) : (
                    <input type="checkbox" disabled title="Only saved/completed daily_expert runs are selectable" />
                  )}
                </td>
              )}
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{r.id}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{(r.expertName ?? r.expert_name) ?? (r.modelName ?? r.model_name) ?? r.engineType ?? '—'}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{(r.optimizationId ?? r.optimization_id) ?? '—'}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{(r.totalReturn ?? r.total_return) ?? '—'}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{(r.sharpeRatio ?? r.sharpe_ratio) ?? '—'}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{(r.totalTrades ?? r.total_trades) ?? '—'}</td>
              <td className="px-2 py-1 text-sm text-red-600 dark:text-red-400">{fmtDrawdown(r.maxDrawdown ?? r.max_drawdown)}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{(() => { const w = r.winRate ?? r.win_rate; return w != null ? `${Number(w).toFixed(1)}%` : '—'; })()}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">{(r.isSaved ?? r.is_saved) ? '★' : ''}</td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100"><div className="max-w-[9rem] truncate" title={r.name}>{r.name}</div></td>
              <td className="px-2 py-1 text-sm text-gray-900 dark:text-gray-100">
                <div className="flex gap-0.5" onClick={(e) => e.stopPropagation()}>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleSave(r); }}
                    title={(r.isSaved ?? r.is_saved) ? 'Saved — rename / re-save' : 'Save this run'}
                    className={`px-1.5 py-0.5 text-xs border rounded whitespace-nowrap hover:bg-gray-50 dark:hover:bg-gray-700 ${(r.isSaved ?? r.is_saved) ? 'border-amber-400 dark:border-amber-600 text-amber-500' : 'border-gray-300 dark:border-gray-600'}`}
                  >
                    ★
                  </button>
                  {onLoad && (
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); onLoad(r.id); }}
                      title="Load this run's settings into a New Backtest"
                      className="px-1.5 py-0.5 text-xs border border-blue-300 dark:border-blue-700 rounded text-blue-600 dark:text-blue-400 whitespace-nowrap hover:bg-blue-50 dark:hover:bg-blue-900/20"
                    >
                      Load
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); setExportRow(r); }}
                    title="Export this run"
                    className="px-1.5 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded whitespace-nowrap hover:bg-gray-50 dark:hover:bg-gray-700"
                  >
                    Export
                  </button>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleDelete(r); }}
                    title="Delete this run"
                    className="px-1.5 py-0.5 text-xs border border-red-300 dark:border-red-700 rounded text-red-600 dark:text-red-400 whitespace-nowrap hover:bg-red-50 dark:hover:bg-red-900/20"
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
    <ExportDialog
      isOpen={exportRow != null}
      backtestId={exportRow?.id ?? 0}
      backtestName={exportRow?.name}
      onExport={handleExport}
      onClose={() => setExportRow(null)}
    />
    </>
  );
}
