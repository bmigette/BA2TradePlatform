import { useEffect, useState } from 'react';
import { Activity, XCircle } from 'lucide-react';
import { listTasks, cancelTask, listRunningOptimizations } from '../lib/btApi';
import type { TaskInfo, RunningOpt } from '../lib/btApi';
import { TopIndividualsTable } from './TopIndividualsTable';

const BT_TASK_TYPES = new Set(['daily_backtest', 'backtest', 'strategy_optimization']);

/**
 * Parse the optimizer's progress_message into structured bits.
 * Format: "Gen 2/3 · ind 7/12 best=2.8400" (ind segment present once a generation is underway).
 */
function parseProgress(msg?: string): {
  gen?: number; total?: number; ind?: number; indTotal?: number; best?: string;
} {
  if (!msg) return {};
  const g = msg.match(/Gen\s+(\d+)\s*\/\s*(\d+)/i);
  const ind = msg.match(/ind\s+(\d+)\s*\/\s*(\d+)/i);
  const b = msg.match(/best\s*=\s*([-\d.]+)/i);
  return {
    gen: g ? Number(g[1]) : undefined,
    total: g ? Number(g[2]) : undefined,
    ind: ind ? Number(ind[1]) : undefined,
    indTotal: ind ? Number(ind[2]) : undefined,
    best: b ? b[1] : undefined,
  };
}

/**
 * Rich running-jobs view (its own Backtesting tab). For each in-flight backtest /
 * optimization shows the full (untruncated) name, total-generation progress bar, the
 * current generation (Gen X/N), best fitness so far, and a Cancel action. Self-polls
 * every 2s; runs independently of the rest of the page so it never blocks running jobs.
 */
export function RunningJobsPanel() {
  const [jobs, setJobs] = useState<TaskInfo[]>([]);
  const [opts, setOpts] = useState<RunningOpt[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [all, runningOpts] = await Promise.all([
          listTasks('running'),
          listRunningOptimizations().catch(() => [] as RunningOpt[]),
        ]);
        if (alive) {
          setJobs(all.filter(t => !t.task_type || BT_TASK_TYPES.has(t.task_type)));
          setOpts(runningOpts);
        }
      } catch {
        if (alive) { setJobs([]); setOpts([]); }
      } finally {
        if (alive) setLoaded(true);
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  // Match a running optimization to its task by name (task name == optimization name).
  const optByName = new Map(opts.map(o => [o.name, o] as const));
  // Running optimizations launched OUTSIDE the API task queue (e.g. the `ba2-test optimize` CLI)
  // have a StrategyOptimization row but NO task — surface them as their own rows so they aren't
  // invisible here. (API-submitted opts have a task AND a row, matched by name -> not orphaned.)
  const jobNames = new Set(jobs.map(j => j.name).filter(Boolean) as string[]);
  const orphanOpts = opts.filter(o => !(o.name && jobNames.has(o.name)));

  const onCancel = (taskId: string) => {
    cancelTask(taskId)
      .then(() => setJobs(prev => prev.filter(x => x.task_id !== taskId)))
      .catch(() => { /* keep row; next poll reconciles */ });
  };

  if (loaded && !jobs.length && !orphanOpts.length) {
    return (
      <div className="flex flex-col items-center justify-center text-center py-16 text-gray-400 dark:text-gray-500">
        <Activity className="w-10 h-10 mb-3 opacity-50" />
        <p className="text-sm">No running jobs.</p>
        <p className="text-xs mt-1">Submitted backtests &amp; optimizations appear here with live progress.</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
        Running jobs ({jobs.length + orphanOpts.length})
      </div>
      {jobs.map(j => {
        const { gen, total, ind, indTotal, best } = parseProgress(j.progress_message);
        const pct = Math.max(0, Math.min(100, Math.round(j.progress ?? 0)));
        const genPct = ind != null && indTotal ? Math.round((ind / indTotal) * 100) : null;
        return (
          <div key={j.task_id}
            className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            <div className="flex items-start justify-between gap-3 mb-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-gray-900 dark:text-gray-100 break-words">
                  {j.name ?? j.task_id}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  {j.task_type ?? 'job'} · {j.status}
                </div>
              </div>
              <button type="button" onClick={() => onCancel(j.task_id)}
                className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-white bg-red-600 hover:bg-red-700 rounded border border-red-700">
                <XCircle className="w-3.5 h-3.5" /> Cancel
              </button>
            </div>

            {/* Total progress (across all generations) */}
            <div>
              <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
                <span>Total{gen != null && total != null ? ` · generation ${gen} / ${total}` : ''}</span>
                <span className="font-medium text-gray-700 dark:text-gray-300">{pct}%</span>
              </div>
              <div className="bg-gray-200 dark:bg-gray-700 rounded-full h-2.5">
                <div className="bg-blue-500 h-2.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
              </div>
            </div>

            {/* Current-generation progress (individuals evaluated within this generation) */}
            {genPct != null && (
              <div className="mt-2">
                <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
                  <span>Generation {gen ?? ''} · individual {ind} / {indTotal}</span>
                  <span className="font-medium text-gray-700 dark:text-gray-300">{genPct}%</span>
                </div>
                <div className="bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                  <div className="bg-indigo-400 h-1.5 rounded-full transition-all" style={{ width: `${genPct}%` }} />
                </div>
              </div>
            )}

            {/* Detail line */}
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1 mt-2 text-xs text-gray-600 dark:text-gray-400">
              {best != null && (
                <span>Best fitness <span className="font-semibold text-emerald-600 dark:text-emerald-400">{best}</span></span>
              )}
              {gen == null && j.progress_message && (
                <span className="truncate">{j.progress_message}</span>
              )}
            </div>

            {/* Live optimization detail: best metric + top individuals */}
            <OptimizationDetail opt={optByName.get(j.name ?? '')} />
          </div>
        );
      })}

      {/* Running optimizations with no API task (e.g. launched via the `ba2-test optimize` CLI). */}
      {orphanOpts.map(o => {
        const pct = Math.max(0, Math.min(100, Math.round(o.progress ?? 0)));
        return (
          <div key={`opt-${o.id}`}
            className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            <div className="flex items-start justify-between gap-3 mb-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-gray-900 dark:text-gray-100 break-words">
                  {o.name ?? `Optimization #${o.id}`}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  strategy_optimization · {o.status} (CLI)
                </div>
              </div>
            </div>
            <div>
              <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
                <span>Total</span>
                <span className="font-medium text-gray-700 dark:text-gray-300">{pct}%</span>
              </div>
              <div className="bg-gray-200 dark:bg-gray-700 rounded-full h-2.5">
                <div className="bg-blue-500 h-2.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
              </div>
            </div>
            <OptimizationDetail opt={o} />
          </div>
        );
      })}
    </div>
  );
}

function fmt(v?: number, n = 2): string {
  return typeof v === 'number' && isFinite(v) ? v.toFixed(n) : '–';
}

/**
 * Compact live progress for ONE running job, matched by name — rendered in the right-hand detail
 * pane (next to the "Running the backtest…" spinner) so the % bar isn't only on the Running tab.
 * Polls every 2s; renders nothing until a matching running task/optimization with a progress value
 * is found.
 */
export function RunningJobProgress({ name }: { name?: string }) {
  const [job, setJob] = useState<TaskInfo | null>(null);
  const [opt, setOpt] = useState<RunningOpt | null>(null);
  useEffect(() => {
    if (!name) return;
    let alive = true;
    const tick = async () => {
      try {
        const [tasks, opts] = await Promise.all([
          listTasks('running'),
          listRunningOptimizations().catch(() => [] as RunningOpt[]),
        ]);
        if (!alive) return;
        setJob(tasks.find(t => t.name === name) ?? null);
        setOpt(opts.find(o => o.name === name) ?? null);
      } catch { /* keep last known progress */ }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { alive = false; clearInterval(id); };
  }, [name]);

  const rawPct = job?.progress ?? opt?.progress;
  if (rawPct == null) return null;
  const pct = Math.max(0, Math.min(100, Math.round(rawPct)));
  const { gen, total, ind, indTotal, best } = parseProgress(job?.progress_message);
  const genPct = ind != null && indTotal ? Math.round((ind / indTotal) * 100) : null;
  return (
    <div className="w-full max-w-sm mt-2 text-left">
      <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
        <span>Total{gen != null && total != null ? ` · generation ${gen} / ${total}` : ''}</span>
        <span className="font-medium text-gray-700 dark:text-gray-300">{pct}%</span>
      </div>
      <div className="bg-gray-200 dark:bg-gray-700 rounded-full h-2.5">
        <div className="bg-blue-500 h-2.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
      </div>
      {genPct != null && (
        <div className="mt-2">
          <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
            <span>Generation {gen ?? ''} · individual {ind} / {indTotal}</span>
            <span className="font-medium text-gray-700 dark:text-gray-300">{genPct}%</span>
          </div>
          <div className="bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
            <div className="bg-indigo-400 h-1.5 rounded-full transition-all" style={{ width: `${genPct}%` }} />
          </div>
        </div>
      )}
      {(best != null || (gen == null && job?.progress_message)) && (
        <div className="mt-2 text-xs text-gray-600 dark:text-gray-400">
          {best != null
            ? <>Best fitness <span className="font-semibold text-emerald-600 dark:text-emerald-400">{best}</span></>
            : <span className="truncate">{job?.progress_message}</span>}
        </div>
      )}
    </div>
  );
}

/** Live best-metric + top-individuals table for a running optimization (matched to its job). */
function OptimizationDetail({ opt }: { opt?: RunningOpt }) {
  if (!opt) return null;
  const top = opt.topIndividuals ?? [];
  return (
    <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs mb-2">
        <span className="text-gray-500 dark:text-gray-400">
          Best <span className="font-semibold text-emerald-600 dark:text-emerald-400">{fmt(opt.bestFitness)}</span>
          {opt.fitnessMetric ? ` (${opt.fitnessMetric})` : ''}
        </span>
        <span className="text-gray-500 dark:text-gray-400">
          Evaluated <span className="font-medium text-gray-700 dark:text-gray-300">{opt.nEvaluated ?? 0}</span>
        </span>
      </div>
      <div className="text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Top individuals</div>
      <TopIndividualsTable
        individuals={top}
        fitnessMetric={opt.fitnessMetric}
        note={top.length > 0
          ? `Informational only — these running individuals have no saved backtest yet. The top ${Math.min(5, top.length)} are persisted as full backtests when the job completes; select them in the Opt History tab to view their results.`
          : undefined}
      />
    </div>
  );
}
