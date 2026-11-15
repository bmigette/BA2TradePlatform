import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { AlertCircle, GitBranch, Loader2, Plus, RefreshCw, X } from 'lucide-react';
import { listWfRuns } from '../lib/wfApi';
import type { WfRunLight } from '../lib/wfApi';
import { fmtDd, fmtPct } from '../lib/walkForward';
import { WfStatusBadge } from '../components/WfStatusBadge';

function fmtWhen(iso: string | null): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  return Number.isFinite(t) ? new Date(t).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
}

/** Walk-forward runs: every chain in this DB (created here, run by the goal2027 grid on this host, or
 * imported from a report directory with tools/import_wf_reports.py). */
export default function WalkForward() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<WfRunLight[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');

  const load = useCallback(() => {
    listWfRuns().then(r => { setRuns(r); setError(null); })
      .catch(e => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => { load(); }, [load]);
  const live = (runs ?? []).some(r => r.status === 'running' || r.status === 'pending');
  useEffect(() => {
    if (!live) return;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [live, load]);

  const q = filter.trim().toLowerCase();
  const shown = (runs ?? []).filter(r => !q || [r.name, r.expert, r.strategy, r.band, r.status]
    .some(v => (v ?? '').toLowerCase().includes(q)));

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Walk-forward</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-1">
            Anchored walk-forward chains: train on an expanding window, test the top-N out of sample per fold.
          </p>
        </div>
        <button onClick={() => navigate('/walk-forward/new')}
          className="flex items-center gap-2 px-4 py-2 text-sm rounded-lg bg-blue-600 text-white hover:bg-blue-700">
          <Plus className="w-4 h-4" /> New walk-forward
        </button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-100 text-red-700 rounded-lg flex items-center gap-2">
          <AlertCircle className="w-4 h-4" /> {error}
          <button onClick={() => setError(null)} className="ml-auto"><X className="w-4 h-4" /></button>
        </div>
      )}

      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <GitBranch className="w-5 h-5 text-blue-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Runs</h2>
            <span className="text-sm text-gray-500 dark:text-gray-400">({shown.length})</span>
          </div>
          <div className="flex items-center gap-2">
            <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter runs..."
              className="px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 w-56" />
            <button onClick={load} title="Refresh" className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg">
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>
        </div>
        <div className="p-4 overflow-x-auto">
          {runs === null ? (
            <div className="flex justify-center py-8"><Loader2 className="w-6 h-6 animate-spin text-blue-500" /></div>
          ) : shown.length === 0 ? (
            <p className="text-center text-gray-500 dark:text-gray-400 py-8">
              {runs.length ? 'No run matches the filter' : 'No walk-forward runs yet. Create one, or import a report directory with tools/import_wf_reports.py.'}
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                  <th className="py-2 pr-3">Name</th>
                  <th className="py-2 pr-3">Expert / strategy</th>
                  <th className="py-2 pr-3">Folds</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3 text-right">Stitched OOS return</th>
                  <th className="py-2 pr-3 text-right">Worst OOS DD</th>
                  <th className="py-2 pr-3 text-right">OOS trades</th>
                  <th className="py-2 pr-3">Source</th>
                  <th className="py-2 pr-3">Updated</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(r => (
                  <tr key={r.id} className="border-b border-gray-100 dark:border-gray-700/50 hover:bg-gray-50 dark:hover:bg-gray-700/40 text-gray-800 dark:text-gray-200">
                    <td className="py-2 pr-3"><Link to={`/walk-forward/${r.id}`} className="text-blue-600 dark:text-blue-400 hover:underline font-medium break-all">{r.name}</Link></td>
                    <td className="py-2 pr-3">{r.expert ?? '—'} / {r.strategy ?? '—'}{r.band ? <span className="text-gray-400"> · {r.band}</span> : null}</td>
                    <td className="py-2 pr-3">{r.foldsDone ?? 0}/{r.plannedFolds ?? '?'}{r.status === 'running' && r.currentFold ? <span className="text-xs text-blue-500"> (fold {r.currentFold})</span> : null}</td>
                    <td className="py-2 pr-3"><WfStatusBadge status={r.status} /></td>
                    <td className={`py-2 pr-3 text-right ${r.stitchedReturn == null ? '' : r.stitchedReturn >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>{fmtPct(r.stitchedReturn)}</td>
                    <td className="py-2 pr-3 text-right">{fmtDd(r.worstOosDd)}</td>
                    <td className="py-2 pr-3 text-right">{r.stitchedTrades ?? '—'}</td>
                    <td className="py-2 pr-3 text-xs text-gray-500 dark:text-gray-400">{r.source}</td>
                    <td className="py-2 pr-3 text-xs text-gray-500 dark:text-gray-400">{fmtWhen(r.updatedAt ?? r.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
