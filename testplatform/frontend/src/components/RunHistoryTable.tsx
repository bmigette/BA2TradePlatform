import { useEffect, useMemo, useState } from 'react';
import { listBacktests, saveBacktest, fetchBacktestExport, deleteBacktest } from '../lib/btApi';
import type { ExportKind } from '../lib/btApi';
import { ExportDialog } from './ExportDialog';

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

export function RunHistoryTable({ savedOnly, onSelect, onLoad }:
  { savedOnly: boolean; onSelect: (id: number) => void; onLoad?: (id: number) => void; }) {
  const [rows, setRows] = useState<any[]>([]);
  const [expert, setExpert] = useState('');
  const [optId, setOptId] = useState('');
  const [q, setQ] = useState('');
  // Bumped after a save/delete to re-run the fetch effect below.
  const [refresh, setRefresh] = useState(0);
  // The run whose Export dialog is open (null = closed).
  const [exportRow, setExportRow] = useState<any | null>(null);

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
  const filtered = rows.filter(r => !q || (r.name || '').toLowerCase().includes(q.toLowerCase()));

  return (
    <>
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <div className="flex gap-2 p-3 bg-gray-50 dark:bg-gray-700/50 border-b border-gray-200 dark:border-gray-700">
        <select value={expert} onChange={(e) => setExpert(e.target.value)} className={inputClass}>
          <option value="">All experts</option>
          {experts.map(x => <option key={x}>{x}</option>)}
        </select>
        <select value={optId} onChange={(e) => setOptId(e.target.value)} className={inputClass}>
          <option value="">All opt jobs</option>
          {optIds.map(x => <option key={x} value={x}>#{x}</option>)}
        </select>
        <input placeholder="search name" value={q} onChange={(e) => setQ(e.target.value)} className={inputClass} />
      </div>
      <table className="w-full text-sm">
        <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
          <tr>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">id</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">expert</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">opt#</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">ret%</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">sharpe</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">trades</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">DD%</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">win%</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">saved</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">name</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">Actions</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map(r => (
            <tr key={r.id} onClick={() => onSelect(r.id)}
              className="border-b border-gray-200 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer transition-colors">
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{r.id}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.expertName ?? r.expert_name) ?? (r.modelName ?? r.model_name) ?? r.engineType ?? '—'}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.optimizationId ?? r.optimization_id) ?? '—'}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.totalReturn ?? r.total_return) ?? '—'}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.sharpeRatio ?? r.sharpe_ratio) ?? '—'}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.totalTrades ?? r.total_trades) ?? '—'}</td>
              <td className="px-3 py-2 text-sm text-red-600 dark:text-red-400">{fmtDrawdown(r.maxDrawdown ?? r.max_drawdown)}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(() => { const w = r.winRate ?? r.win_rate; return w != null ? `${Number(w).toFixed(1)}%` : '—'; })()}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.isSaved ?? r.is_saved) ? '★' : ''}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{r.name}</td>
              <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">
                <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleSave(r); }}
                    title={(r.isSaved ?? r.is_saved) ? 'Saved — rename / re-save' : 'Save this run'}
                    className="px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700"
                  >
                    {(r.isSaved ?? r.is_saved) ? '★ Saved' : '★ Save'}
                  </button>
                  {onLoad && (
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); onLoad(r.id); }}
                      title="Load this run's settings into a New Backtest"
                      className="px-2 py-1 text-xs border border-blue-300 dark:border-blue-700 rounded text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20"
                    >
                      Load
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); setExportRow(r); }}
                    title="Export this run"
                    className="px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700"
                  >
                    Export
                  </button>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleDelete(r); }}
                    title="Delete this run"
                    className="px-2 py-1 text-xs border border-red-300 dark:border-red-700 rounded text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
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
