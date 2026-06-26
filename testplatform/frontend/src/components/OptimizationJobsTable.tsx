import { useEffect, useMemo, useState } from 'react';
import { listOptimizationJobs, getOptimization, listBacktests } from '../lib/btApi';
import type { OptimizationJob, OptJobSettings, OptimizationDetail } from '../lib/btApi';
import { usePersistentState } from '../lib/usePersistentState';

const inputClass = "px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500";

/** Human-readable elapsed time between two ISO timestamps (e.g. "3h 12m", "45s"). */
function humanDuration(startIso?: string | null, endIso?: string | null): string {
  if (!startIso || !endIso) return '—';
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (!isFinite(start) || !isFinite(end) || end < start) return '—';
  let secs = Math.round((end - start) / 1000);
  if (secs < 1) return '<1s';
  const d = Math.floor(secs / 86400); secs -= d * 86400;
  const h = Math.floor(secs / 3600); secs -= h * 3600;
  const m = Math.floor(secs / 60); secs -= m * 60;
  const parts: string[] = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  if (!d && !h && (secs || !m)) parts.push(`${secs}s`);
  return parts.slice(0, 2).join(' ');
}

/** Local-time, compact date for the "Date" column (run date = created_at). */
function fmtDate(iso?: string | null): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!isFinite(t)) return '—';
  return new Date(t).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function fmtFitness(v?: number | null): string {
  return typeof v === 'number' && isFinite(v) ? v.toFixed(4) : '—';
}

// Status pills: SOLID mid-tone background + white text. Readable in BOTH themes and immune to
// this app's dark-mode quirks — the native `dark:` variant doesn't fire here (the app toggles a
// `.dark` class while the OS is light) AND a global `.dark .font-semibold` rule force-lightens
// pill text; light `bg-*-100` pills therefore rendered light-text-on-light-bg. White-on-*-600 is
// high-contrast regardless. completed=green · running=blue · cancelled/stopped=gray · failed=red · pending=amber.
const STATUS_STYLES: Record<string, string> = {
  completed: 'bg-emerald-600 text-white border-emerald-600',
  running: 'bg-blue-600 text-white border-blue-600',
  pending: 'bg-amber-600 text-white border-amber-600',
  cancelled: 'bg-slate-500 text-white border-slate-500',
  stopped: 'bg-slate-500 text-white border-slate-500',
  failed: 'bg-red-600 text-white border-red-600',
};

function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_STYLES[status] ?? 'bg-slate-500 text-white border-slate-500';
  return (
    <span className={`inline-block px-2 py-0.5 text-xs font-semibold rounded-full border ${cls}`}>
      {status}
    </span>
  );
}

/** Condensed one-line preview of the optimization settings for the row cell. */
function settingsPreview(s: OptJobSettings): string {
  const bits: string[] = [];
  if (s.ga.populationSize != null && s.ga.generations != null) {
    bits.push(`pop ${s.ga.populationSize} × ${s.ga.generations} gen`);
  }
  const nRanges = Object.keys(s.expertRanges || {}).length;
  if (nRanges) bits.push(`${nRanges} param${nRanges === 1 ? '' : 's'}`);
  if (s.universeMode) bits.push(s.universeMode === 'screener' ? 'screener' : s.universeMode);
  return bits.length ? bits.join(' · ') : 'no settings';
}

/**
 * Expanded settings: GA config, optimized expert/RM param ranges, and screener settings.
 * Exported so the Opt-History RIGHT panel can render the selected job's full settings.
 */
export function OptJobSettingsDetail({ s }: { s: OptJobSettings }) {
  const gaEntries = Object.entries(s.ga ?? {});
  const rangeEntries = Object.entries(s.expertRanges ?? {});
  const screenerEntries = Object.entries(s.screener?.screener_settings ?? {});
  return (
    <div className="mt-2 space-y-3 text-xs">
      {/* Genetic config */}
      {gaEntries.length > 0 && (
        <div>
          <div className="font-semibold text-gray-900 dark:text-gray-100 mb-1">Genetic config</div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-gray-800 dark:text-gray-200">
            {gaEntries.map(([k, v]) => (
              <span key={k}><span className="text-gray-500 dark:text-gray-400">{k}:</span> {String(v)}</span>
            ))}
          </div>
        </div>
      )}

      {/* Backtest window / engine */}
      {(s.engine || s.startDate || s.endDate) && (
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-gray-800 dark:text-gray-200">
          {s.engine && <span><span className="text-gray-500 dark:text-gray-400">engine:</span> {s.engine}</span>}
          {(s.startDate || s.endDate) && (
            <span><span className="text-gray-500 dark:text-gray-400">window:</span> {s.startDate ?? '?'} → {s.endDate ?? '?'}</span>
          )}
        </div>
      )}

      {/* Optimized expert / RM param ranges */}
      <div>
        <div className="font-semibold text-gray-900 dark:text-gray-100 mb-1">
          Optimized params {rangeEntries.length ? `(${rangeEntries.length})` : ''}
        </div>
        {rangeEntries.length === 0 ? (
          <div className="text-gray-600 dark:text-gray-300">None (expert frozen)</div>
        ) : (
          <table className="text-xs">
            <tbody>
              {rangeEntries.map(([name, r]) => (
                <tr key={name}>
                  <td className="pr-3 py-0.5 font-mono text-gray-900 dark:text-gray-100">{name}</td>
                  <td className="py-0.5 text-gray-700 dark:text-gray-300">
                    [{r.min ?? '?'} … {r.max ?? '?'}]
                    {r.step != null ? ` step ${r.step}` : ''}
                    {r.type ? ` · ${r.type}` : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Screener settings (only when universe is screener-mode) */}
      {s.universeMode === 'screener' && (
        <div>
          <div className="font-semibold text-gray-900 dark:text-gray-100 mb-1">Screener</div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-gray-800 dark:text-gray-200">
            {s.screener?.screener_store && <span className="truncate max-w-xs"><span className="text-gray-500 dark:text-gray-400">store:</span> {s.screener.screener_store}</span>}
            {s.screener?.screener_cadence_days != null && <span><span className="text-gray-500 dark:text-gray-400">cadence:</span> {s.screener.screener_cadence_days}d</span>}
          </div>
          {screenerEntries.length > 0 && (
            <div className="flex flex-wrap gap-x-4 gap-y-0.5 mt-0.5 text-gray-800 dark:text-gray-200">
              {screenerEntries.map(([k, v]) => (
                <span key={k}><span className="text-gray-500 dark:text-gray-400">{k}:</span> {String(v)}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function fmtPct(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? `${n.toFixed(1)}%` : '—';
}
function fmtNum(v: unknown, dp = 2): string {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n.toFixed(dp) : '—';
}

/**
 * The bottom area of the Opt-History tab: the selected job's persisted TOP-N backtest rows.
 * Each row is clickable and loads that backtest's full result into the RIGHT panel via the
 * shared viewBacktest flow. A job with no persisted backtests (e.g. a serve-run job that did
 * not persist its top-N) shows an explanatory note — its top-individuals summary is still
 * available in the right panel.
 */
function JobBacktestsTable({
  jobId,
  jobName,
  onSelectBacktest,
}: {
  jobId: number;
  jobName?: string | null;
  onSelectBacktest: (id: number) => void;
}) {
  const [rows, setRows] = useState<any[] | null>(null);

  useEffect(() => {
    let alive = true;
    setRows(null);
    listBacktests({ optimization_id: jobId })
      .then(r => { if (alive) setRows(r); })
      .catch(() => { if (alive) setRows([]); });
    return () => { alive = false; };
  }, [jobId]);

  return (
    <div className="mt-4 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <div className="px-3 py-2 bg-gray-50 dark:bg-gray-700/50 border-b border-gray-200 dark:border-gray-700 text-xs font-medium text-gray-600 dark:text-gray-300">
        Saved backtests for job #{jobId}{jobName ? ` · ${jobName}` : ''}
      </div>
      {rows === null ? (
        <div className="px-3 py-4 text-xs text-gray-400 dark:text-gray-500">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="px-3 py-4 text-xs text-gray-500 dark:text-gray-400">
          No saved backtests for this job — only the top individuals summary is available.
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
            <tr>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">rank</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">name</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">ret%</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">sharpe</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">trades</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">DD%</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.id} onClick={() => onSelectBacktest(r.id)}
                className="border-b border-gray-200 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer transition-colors">
                <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{i + 1}</td>
                <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{r.name}</td>
                <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.totalReturn ?? r.total_return) ?? '—'}</td>
                <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{fmtNum(r.sharpeRatio ?? r.sharpe_ratio)}</td>
                <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{(r.totalTrades ?? r.total_trades) ?? '—'}</td>
                <td className="px-3 py-2 text-sm text-red-600 dark:text-red-400">{fmtPct(r.maxDrawdown ?? r.max_drawdown)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/**
 * Opt-History tab (top area): lists genetic StrategyOptimization runs. Clicking a job row:
 *   (a) calls onSelectJob(job, detail) so the page shows the job's settings + top individuals
 *       in the RIGHT results panel (detail is fetched lazily here and cached);
 *   (b) reveals the per-job persisted TOP-N backtests BELOW (JobBacktestsTable), whose rows
 *       call onSelectBacktest(id) to load a full backtest result into the right panel.
 */
export function OptimizationJobsTable({
  onSelectJob,
  onSelectBacktest,
  selectedJobId,
}: {
  onSelectJob: (job: OptimizationJob, detail?: OptimizationDetail) => void;
  onSelectBacktest: (id: number) => void;
  selectedJobId: number | null;
}) {
  const [rows, setRows] = useState<OptimizationJob[]>([]);
  const [loaded, setLoaded] = useState(false);
  // Status filter + search persist for the session (survive reloads + tab switches).
  const [status, setStatus] = usePersistentState('bt:optjobs:status', '');
  const [q, setQ] = usePersistentState('bt:optjobs:q', '');
  // Lazily-fetched per-job detail (top individuals + config), cached by job id.
  const [details, setDetails] = useState<Record<number, OptimizationDetail | 'loading'>>({});

  useEffect(() => {
    listOptimizationJobs()
      .then(r => setRows(r))
      .catch(() => setRows([]))
      .finally(() => setLoaded(true));
  }, []);

  const statuses = useMemo(
    () => Array.from(new Set(rows.map(r => r.status).filter(Boolean))),
    [rows],
  );
  const filtered = rows.filter(r =>
    (!status || r.status === status) &&
    (!q || (r.name || '').toLowerCase().includes(q.toLowerCase())),
  );

  const selectJob = (job: OptimizationJob) => {
    const cached = details[job.id];
    if (cached && cached !== 'loading') {
      onSelectJob(job, cached);
      return;
    }
    // Surface the job immediately (settings render from the row); fetch top individuals async.
    onSelectJob(job, undefined);
    if (!cached) {
      setDetails(prev => ({ ...prev, [job.id]: 'loading' }));
      getOptimization(job.id)
        .then(d => {
          setDetails(p => ({ ...p, [job.id]: d }));
          onSelectJob(job, d);  // re-surface with the loaded top individuals
        })
        .catch(() => setDetails(p => ({ ...p, [job.id]: { id: job.id, status: 'error' } })));
    }
  };

  if (loaded && rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-16 text-gray-400 dark:text-gray-500">
        <p className="text-sm">No optimization jobs yet.</p>
        <p className="text-xs mt-1">Launch a genetic optimization from the New Backtest tab and it will appear here.</p>
      </div>
    );
  }

  const selectedJob = filtered.find(r => r.id === selectedJobId) ?? rows.find(r => r.id === selectedJobId);

  return (
    <div>
      <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
        <div className="flex gap-2 p-3 bg-gray-50 dark:bg-gray-700/50 border-b border-gray-200 dark:border-gray-700">
          <select value={status} onChange={(e) => setStatus(e.target.value)} className={inputClass}>
            <option value="">All statuses</option>
            {statuses.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <input placeholder="search name" value={q} onChange={(e) => setQ(e.target.value)} className={inputClass} />
        </div>
        <table className="w-full text-sm">
          <thead className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
            <tr>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">id</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">name</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">status</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">best fitness</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">date</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">duration</th>
              <th className="px-3 py-2 text-left text-xs font-medium text-gray-700 dark:text-gray-300">settings</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(r => {
              const isSelected = r.id === selectedJobId;
              return (
                <tr key={r.id}
                  onClick={() => selectJob(r)}
                  className={`border-b border-gray-200 dark:border-gray-600 align-top cursor-pointer transition-colors ${
                    isSelected ? 'bg-blue-50 dark:bg-blue-900/20' : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
                  }`}>
                  <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100">{r.id}</td>
                  <td className="px-3 py-2 text-sm text-gray-900 dark:text-gray-100 max-w-xs break-words">
                    {r.name || <span className="text-gray-500 dark:text-gray-400">unnamed</span>}
                    {r.fitnessMetric && (
                      <span className="block text-xs text-gray-600 dark:text-gray-400">{r.fitnessMetric}</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-sm">
                    <StatusBadge status={r.status} />
                    {r.status === 'failed' && r.errorMessage && (
                      <span className="block text-xs text-red-500 dark:text-red-400 mt-0.5 max-w-xs truncate" title={r.errorMessage}>
                        {r.errorMessage}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-sm font-medium text-emerald-600 dark:text-emerald-400">{fmtFitness(r.bestFitness)}</td>
                  <td className="px-3 py-2 text-sm text-gray-600 dark:text-gray-400 whitespace-nowrap">{fmtDate(r.createdAt)}</td>
                  <td className="px-3 py-2 text-sm text-gray-600 dark:text-gray-400 whitespace-nowrap">{humanDuration(r.createdAt, r.completedAt)}</td>
                  <td className="px-3 py-2 text-sm text-gray-800 dark:text-gray-200">
                    <span className="text-xs">{settingsPreview(r.settings)}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Second area: the selected job's persisted TOP-N backtests. */}
      {selectedJobId != null && (
        <JobBacktestsTable
          jobId={selectedJobId}
          jobName={selectedJob?.name}
          onSelectBacktest={onSelectBacktest}
        />
      )}
    </div>
  );
}
