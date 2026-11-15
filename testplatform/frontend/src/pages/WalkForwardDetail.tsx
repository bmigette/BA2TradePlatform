import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { AlertCircle, AlertTriangle, ArrowLeft, Loader2, Play, RefreshCw, Square, X } from 'lucide-react';
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip as RechartsTooltip, XAxis, YAxis } from 'recharts';
import { cancelWfRun, getWfEquity, getWfRun, resumeWfRun } from '../lib/wfApi';
import type { WfEquity, WfFold, WfRunDetail, WfStitch } from '../lib/wfApi';
import { datePos, fmtDd, fmtNum, fmtPct } from '../lib/walkForward';
import { WfStatusBadge } from '../components/WfStatusBadge';

const card = "bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4";
const h2 = "text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3";

const FOLD_STATE_STYLE: Record<string, string> = {
  planned: 'text-gray-500 border-gray-300',
  running: 'text-blue-600 border-blue-400',
  done: 'text-emerald-700 border-emerald-500',
  deferred: 'text-purple-600 border-purple-400',
  eliminated: 'text-gray-400 border-gray-300 line-through',
};

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'pos' | 'neg' }) {
  return (
    <div className="bg-gray-50 dark:bg-gray-900/40 rounded-lg p-3 text-center">
      <p className="text-xs text-gray-500 dark:text-gray-400">{label}</p>
      <p className={`text-lg font-bold ${tone === 'pos' ? 'text-emerald-600' : tone === 'neg' ? 'text-red-600' : 'text-gray-900 dark:text-gray-100'}`}>{value}</p>
    </div>
  );
}

/** Link to the job / backtest when that row is in this DB, plain text otherwise (imported runs). */
function MaybeLink({ to, children }: { to: string | null; children: React.ReactNode }) {
  return to ? <Link to={to} className="text-blue-600 dark:text-blue-400 hover:underline">{children}</Link> : <span>{children}</span>;
}

function FoldTimeline({ folds }: { folds: WfFold[] }) {
  const known = folds.filter(f => f.train_start && f.test_end);
  if (!known.length) return <p className="text-sm text-gray-500">No fold windows recorded.</p>;
  const lo = known.map(f => f.train_start!).sort()[0];
  const hi = known.map(f => f.test_end!).sort().slice(-1)[0];
  const years: number[] = [];
  for (let y = Number(lo.slice(0, 4)) + 1; y <= Number(hi.slice(0, 4)); y++) years.push(y);
  return (
    <div>
      <div className="relative h-4 mb-1 ml-16 text-[10px] text-gray-400">
        {years.map(y => (
          <span key={y} className="absolute -translate-x-1/2" style={{ left: `${datePos(`${y}-01-01`, lo, hi) * 100}%` }}>{y}</span>
        ))}
      </div>
      {folds.map(f => {
        const has = f.train_start && f.train_end && f.test_start && f.test_end;
        return (
          <div key={f.index} className="flex items-center gap-2 mb-1.5">
            <span className={`w-14 text-xs font-medium border rounded px-1 text-center ${FOLD_STATE_STYLE[f.state] ?? ''}`} title={f.state}>fold {f.index}</span>
            <div className="relative flex-1 h-5 bg-gray-100 dark:bg-gray-700/50 rounded">
              {has && (<>
                <div className="absolute h-full rounded-l bg-blue-500/70" title={`train ${f.train_start} → ${f.train_end}`}
                  style={{ left: `${datePos(f.train_start!, lo, hi) * 100}%`, width: `${(datePos(f.train_end!, lo, hi) - datePos(f.train_start!, lo, hi)) * 100}%` }} />
                <div className={`absolute h-full rounded-r ${f.state === 'done' ? (f.winner_passed ? 'bg-emerald-500' : 'bg-red-500') : f.state === 'deferred' ? 'bg-purple-400/70' : 'bg-gray-400/70'}`}
                  title={`test ${f.test_start} → ${f.test_end}`}
                  style={{ left: `${datePos(f.test_start!, lo, hi) * 100}%`, width: `${Math.max(0.5, (datePos(f.test_end!, lo, hi) - datePos(f.test_start!, lo, hi)) * 100)}%` }} />
              </>)}
              {!has && <span className="absolute inset-0 text-[11px] text-gray-400 flex items-center justify-center">window not recorded ({f.state})</span>}
            </div>
            <span className="w-24 text-xs text-gray-500 dark:text-gray-400">{f.state}{f.state === 'done' ? (f.winner_passed ? ' · pass' : ' · fail') : ''}</span>
          </div>
        );
      })}
      <p className="text-[11px] text-gray-400 mt-2 ml-16">
        <span className="inline-block w-3 h-2 bg-blue-500/70 mr-1" />train
        <span className="inline-block w-3 h-2 bg-emerald-500 ml-3 mr-1" />test, winner passed
        <span className="inline-block w-3 h-2 bg-red-500 ml-3 mr-1" />test, winner failed
        <span className="inline-block w-3 h-2 bg-purple-400/70 ml-3 mr-1" />test deferred
        <span className="inline-block w-3 h-2 bg-gray-400/70 ml-3 mr-1" />not tested yet
      </p>
    </div>
  );
}

function StitchCards({ s, partial }: { s: WfStitch; partial: boolean }) {
  return (
    <>
      {partial && <p className="text-xs text-amber-600 dark:text-amber-400 mb-2">Partial: the folds done so far (folds {s.folds.join(', ')}).</p>}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        <Stat label="Compounded OOS return" value={fmtPct(s.total_return)} tone={s.total_return >= 0 ? 'pos' : 'neg'} />
        <Stat label="OOS CAR" value={fmtPct(s.oos_car)} tone={s.oos_car >= 0 ? 'pos' : 'neg'} />
        <Stat label="Worst single-fold DD" value={fmtDd(s.worst_fold_dd)} />
        <Stat label="OOS trades" value={String(s.trades)} />
        <Stat label="IS CAR (mean)" value={fmtPct(s.is_car_mean)} />
        <Stat label="Efficiency (OOS/IS CAR)" value={fmtNum(s.efficiency)} />
      </div>
      <p className="text-[11px] text-gray-400 mt-2">
        Fold winners' OOS returns compounded over {fmtNum(s.years)} test years. The worst DD is the worst single fold's, not a drawdown chained across folds.
      </p>
    </>
  );
}

function EquityChart({ eq }: { eq: WfEquity | null }) {
  if (!eq) return <p className="text-sm text-gray-500">Loading…</p>;
  if (!eq.available) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">
        No stitched equity curve: {eq.missing.length ? `the out-of-sample backtest of ${eq.missing.join(', ')} is not in this database` : 'no fold has been tested yet'}
        {' '}(an imported run's backtests stay on the host that ran it).
      </p>
    );
  }
  const boundaries = eq.folds.slice(1).map(f => eq.points.find(p => p.fold === f.fold)?.date).filter(Boolean) as string[];
  return (
    <div className="h-72">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={eq.points}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} interval="preserveStartEnd"
            tickFormatter={(d: string) => String(d).slice(0, 7)} />
          <YAxis domain={['auto', 'auto']} width={65} tick={{ fontSize: 11 }} tickFormatter={(v: number) => `$${(v / 1000).toFixed(1)}k`} />
          <RechartsTooltip formatter={(v) => [`$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`, 'Equity']} />
          {boundaries.map(d => <ReferenceLine key={d} x={d} stroke="#888" strokeDasharray="3 3" />)}
          <Area type="monotone" dataKey="equity" stroke="#22c55e" fill="#22c55e" fillOpacity={0.2} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function FoldTable({ f, run }: { f: WfFold; run: WfRunDetail }) {
  const job = run.jobs[f.train_job];
  return (
    <div className={card}>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 mb-2">
        <h3 className="font-semibold text-gray-900 dark:text-gray-100">Fold {f.index}</h3>
        <span className="text-sm text-gray-600 dark:text-gray-300">train <span className="font-mono">{f.train_start ?? '?'} → {f.train_end ?? '?'}</span></span>
        <span className="text-sm text-gray-600 dark:text-gray-300">test <span className="font-mono">{f.test_start ?? '?'} → {f.test_end ?? '?'}</span></span>
        <span className={`text-xs border rounded px-1.5 ${FOLD_STATE_STYLE[f.state] ?? ''}`}>{f.state}</span>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
        Train job{' '}
        <MaybeLink to={job ? `/backtesting?optimization=${job.id}` : null}>
          <span className="font-mono">{f.train_job}</span>{job ? ` (#${job.id}, ${job.status})` : f.train_optimization_id ? ` (#${f.train_optimization_id} on the host that ran it)` : ''}
        </MaybeLink>
        {f.generations != null && <> · {f.generations} generations</>}
        {f.seeded_from && <> · seeded from <span className="font-mono">{f.seeded_from}</span></>}
      </p>
      {f.ranks.length === 0 ? (
        <p className="text-sm text-gray-500">{f.state === 'deferred' ? 'Trained; the test waits for the data.' : f.state === 'eliminated' ? 'Not run: the chain could no longer pass.' : 'Not tested yet.'}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                <th className="py-1 pr-2">Rank</th>
                <th className="py-1 pr-2 text-right">IS fitness</th>
                <th className="py-1 pr-2 text-right">IS return</th>
                <th className="py-1 pr-2 text-right">IS max DD</th>
                <th className="py-1 pr-2 text-right">IS trades</th>
                <th className="py-1 pr-2 text-right">OOS return</th>
                <th className="py-1 pr-2 text-right">OOS max DD</th>
                <th className="py-1 pr-2 text-right">OOS trades</th>
                <th className="py-1 pr-2 text-right">Efficiency</th>
                <th className="py-1 pr-2">Verdict</th>
                <th className="py-1 pr-2">OOS backtest</th>
              </tr>
            </thead>
            <tbody>
              {f.ranks.map(g => {
                const linked = g.oos_backtest_id != null && run.backtests[String(g.oos_backtest_id)];
                return (
                  <tr key={g.rank} className="border-b border-gray-100 dark:border-gray-700/50 text-gray-800 dark:text-gray-200">
                    <td className="py-1 pr-2">{g.rank}</td>
                    <td className="py-1 pr-2 text-right">{fmtNum(g.is_fitness, 4)}</td>
                    <td className="py-1 pr-2 text-right">{fmtPct(g.is_return)}</td>
                    <td className="py-1 pr-2 text-right">{fmtDd(g.is_max_dd)}</td>
                    <td className="py-1 pr-2 text-right">{g.is_trades}</td>
                    <td className={`py-1 pr-2 text-right font-medium ${g.oos_return >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>{fmtPct(g.oos_return)}</td>
                    <td className="py-1 pr-2 text-right">{fmtDd(g.oos_max_dd)}</td>
                    <td className="py-1 pr-2 text-right">{g.oos_trades}</td>
                    <td className="py-1 pr-2 text-right">{fmtNum(g.efficiency)}</td>
                    <td className="py-1 pr-2">
                      {g.passed ? <span className="text-emerald-600 font-semibold">PASS</span>
                        : <span className="text-red-600"><span className="font-semibold">FAIL</span>: {g.fail_reasons.join('; ')}</span>}
                    </td>
                    <td className="py-1 pr-2">
                      {g.oos_backtest_id == null ? '—' : <MaybeLink to={linked ? `/backtesting?backtest=${g.oos_backtest_id}` : null}>#{g.oos_backtest_id}</MaybeLink>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function WalkForwardDetail() {
  const { id } = useParams();
  const runId = Number(id);
  const [run, setRun] = useState<WfRunDetail | null>(null);
  const [eq, setEq] = useState<WfEquity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dataEnd, setDataEnd] = useState('');

  const load = useCallback(() => {
    getWfRun(runId).then(r => { setRun(r); setError(null); })
      .catch(e => setError(e instanceof Error ? e.message : String(e)));
  }, [runId]);
  useEffect(() => { load(); }, [load]);

  const live = run?.status === 'running' || run?.status === 'pending';
  useEffect(() => {
    if (!live) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [live, load]);

  const foldsDone = run?.foldsDone ?? 0;
  useEffect(() => {
    if (!foldsDone) return;
    getWfEquity(runId).then(setEq).catch(e => setError(e instanceof Error ? e.message : String(e)));
  }, [runId, foldsDone]);

  const act = async (fn: () => Promise<WfRunDetail>) => {
    setBusy(true);
    try { setRun(await fn()); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  if (!run) {
    return (
      <div className="p-6">
        {error ? <div className="p-3 bg-red-100 text-red-700 rounded-lg">{error}</div>
          : <div className="flex justify-center py-8"><Loader2 className="w-6 h-6 animate-spin text-blue-500" /></div>}
      </div>
    );
  }
  const rep = run.report;
  const spec = (run.spec ?? {}) as Record<string, unknown>;
  const optArgs = spec.optimize_args as string[] | null | undefined;

  return (
    <div className="p-6 space-y-4">
      <div>
        <Link to="/walk-forward" className="text-sm text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1">
          <ArrowLeft className="w-4 h-4" /> Walk-forward runs
        </Link>
        <div className="flex flex-wrap items-center justify-between gap-3 mt-2">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-3 break-all">
              {run.name} <WfStatusBadge status={run.status} />
            </h1>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
              {run.expert ?? '—'} / {run.strategy ?? '—'}{run.band ? ` · ${run.band}` : ''} · {run.foldsDone ?? 0}/{run.plannedFolds ?? '?'} folds ·
              source {run.source}{run.source === 'import' ? ' (view only)' : run.source === 'cli' ? ' (grid / CLI driver)' : ''}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={load} title="Refresh" className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"><RefreshCw className="w-4 h-4" /></button>
            {run.canCancel && (
              <button disabled={busy} onClick={() => { if (window.confirm(`Cancel ${run.name}? The running train job is killed and stays resumable.`)) void act(() => cancelWfRun(run.id)); }}
                className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-red-300 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30 disabled:opacity-40">
                <Square className="w-4 h-4" /> Cancel
              </button>
            )}
            {run.canResume && run.status !== 'deferred-pending' && (
              <button disabled={busy} onClick={() => void act(() => resumeWfRun(run.id))}
                className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40">
                <Play className="w-4 h-4" /> Resume
              </button>
            )}
          </div>
        </div>
      </div>

      {error && (
        <div className="p-3 bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-200 rounded-lg flex items-start gap-2 whitespace-pre-wrap">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" /> <span>{error}</span>
          <button onClick={() => setError(null)} className="ml-auto"><X className="w-4 h-4" /></button>
        </div>
      )}

      {run.canResume && run.status === 'deferred-pending' && (
        <div className={`${card} border-purple-300`}>
          <p className="text-sm text-gray-700 dark:text-gray-200 mb-2">
            Fold {rep.deferred?.fold} is trained; its test {rep.deferred?.test?.join(' → ')} waits for the data (stored data end {String(spec.data_end ?? '—')}).
            Once the data exists, resume with the new data end (empty = no clamp) to test it, decide the run and refit.
          </p>
          <div className="flex items-center gap-2">
            <input type="date" value={dataEnd} onChange={e => setDataEnd(e.target.value)}
              className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
            <button disabled={busy} onClick={() => void act(() => resumeWfRun(run.id, dataEnd || null))}
              className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-40">
              <Play className="w-4 h-4" /> Test the deferred fold
            </button>
          </div>
        </div>
      )}

      {(run.status === 'running' || run.status === 'pending') && (
        <div className={`${card} border-blue-300`}>
          <h2 className={h2}>Progress</h2>
          {run.interrupted && (
            <p className="mb-2 text-sm text-amber-700 dark:text-amber-400 flex items-center gap-1">
              <AlertTriangle className="w-4 h-4" /> Its driver is gone and its task no longer runs: resume it to continue.
            </p>
          )}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm text-gray-700 dark:text-gray-200">
            <div><span className="text-xs text-gray-500 block">Step</span>{run.progressMessage ?? '—'}</div>
            <div><span className="text-xs text-gray-500 block">Current fold</span>{run.live?.fold ?? (run.live?.job ? 'refit' : '—')}{run.live?.phase ? ` (${run.live.phase})` : ''}</div>
            <div><span className="text-xs text-gray-500 block">Generation</span>
              {run.live?.generation != null ? `${run.live.generation} / ${run.live.generations}` : '—'}
              {run.live?.optimization?.progress != null && <div className="mt-1 h-1.5 bg-gray-200 dark:bg-gray-700 rounded"><div className="h-1.5 bg-blue-500 rounded" style={{ width: `${Math.min(100, run.live.optimization.progress)}%` }} /></div>}
            </div>
            <div><span className="text-xs text-gray-500 block">Train job</span>
              {run.live?.job ? <MaybeLink to={run.live.optimization ? `/backtesting?optimization=${run.live.optimization.id}` : null}><span className="font-mono">{run.live.job}</span></MaybeLink> : '—'}
              {run.live?.optimization?.bestFitness != null && <span className="text-xs text-gray-500"> · best {fmtNum(run.live.optimization.bestFitness, 4)}</span>}
            </div>
          </div>
          <p className="text-[11px] text-gray-400 mt-2">task {run.taskStatus ?? '—'} · driver {run.driverState ?? '—'}{run.driverPid ? ` (pid ${run.driverPid} on ${run.driverHost})` : ''}</p>
        </div>
      )}

      {run.errorMessage && (
        <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-200 rounded-lg text-sm whitespace-pre-wrap">
          <span className="font-semibold">Error:</span> {run.errorMessage}
        </div>
      )}

      <div className={card}>
        <h2 className={h2}>Folds</h2>
        <FoldTimeline folds={rep.folds} />
      </div>

      <div className={card}>
        <h2 className={h2}>
          Stitched out-of-sample (fold winners)
          {rep.verdict && <span className={`ml-3 text-sm font-semibold ${rep.verdict === 'PASS' ? 'text-emerald-600' : (rep.verdict === 'FAIL' || rep.verdict === 'ERROR') ? 'text-red-600' : 'text-purple-600'}`}>verdict {rep.verdict}</span>}
          {rep.winners_passed != null && <span className="ml-3 text-sm font-normal text-gray-500">{rep.winners_passed} fold winner(s) passed</span>}
        </h2>
        {rep.winner_stitch ? <StitchCards s={rep.winner_stitch} partial={!!rep.stitch_partial} /> : <p className="text-sm text-gray-500">No fold has been tested yet.</p>}
        {(rep.eliminated || rep.required_failed?.length || rep.refit || rep.refit_running || rep.notes?.length) ? (
          <ul className="mt-3 text-sm text-gray-700 dark:text-gray-300 list-disc ml-5 space-y-0.5">
            {rep.eliminated && <li>Stopped before fold {rep.eliminated.before_fold}: {rep.eliminated.reason}</li>}
            {rep.required_failed && rep.required_failed.length > 0 && <li>Required test year(s) failed: {rep.required_failed.join(', ')}</li>}
            {rep.refit && <li>Refit: <MaybeLink to={run.jobs[rep.refit.job] ? `/backtesting?optimization=${run.jobs[rep.refit.job].id}` : null}><span className="font-mono">{rep.refit.job}</span></MaybeLink> trained on {rep.refit.train.join(' → ')}</li>}
            {rep.refit_running && <li>Refit <span className="font-mono">{rep.refit_running}</span> is training</li>}
            {rep.notes?.map(n => <li key={n}>{n}</li>)}
          </ul>
        ) : null}
      </div>

      <div className={card}>
        <h2 className={h2}>Stitched out-of-sample equity</h2>
        {foldsDone ? <EquityChart eq={eq} /> : <p className="text-sm text-gray-500">No fold has been tested yet.</p>}
      </div>

      {rep.folds.map(f => <FoldTable key={f.index} f={f} run={run} />)}

      <div className={card}>
        <h2 className={h2}>Spec</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm text-gray-700 dark:text-gray-300">
          <div>
            <p className="text-xs text-gray-500 mb-1">Thresholds</p>
            <pre className="text-[11px] bg-gray-50 dark:bg-gray-900 rounded p-2 overflow-x-auto">{JSON.stringify(rep.thresholds ?? spec.thresholds ?? null, null, 2)}</pre>
            <p className="text-xs text-gray-500 mt-2 mb-1">Selection (top {rep.top_n ?? String(spec.top_n ?? '?')})</p>
            <pre className="text-[11px] bg-gray-50 dark:bg-gray-900 rounded p-2 overflow-x-auto">{JSON.stringify(rep.selection ?? {}, null, 2)}</pre>
          </div>
          <div>
            <p className="text-xs text-gray-500 mb-1">Walk-forward</p>
            <pre className="text-[11px] bg-gray-50 dark:bg-gray-900 rounded p-2 overflow-x-auto">{JSON.stringify(Object.fromEntries(
              ['train_start', 'test_years', 'data_end', 'first_generations', 'next_generations', 'defer_last_test', 'run_all_folds', 'refit', 'test_parallel', 'floor']
                .filter(k => k in spec).map(k => [k, spec[k]])), null, 2)}</pre>
            <p className="text-xs text-gray-500 mt-2 mb-1">Optimize args</p>
            <pre className="text-[11px] bg-gray-50 dark:bg-gray-900 rounded p-2 overflow-x-auto whitespace-pre-wrap">{optArgs ? optArgs.join(' ') : 'not recorded (imported run)'}</pre>
            {run.reportDir && <p className="text-xs text-gray-500 mt-2">Report files: <span className="font-mono">{run.reportDir}</span></p>}
          </div>
        </div>
      </div>

      {run.logTail && run.logTail.length > 0 && (
        <details className={card} open={run.status === 'error'}>
          <summary className="text-sm font-semibold text-gray-900 dark:text-gray-100 cursor-pointer">Driver log (last {run.logTail.length} lines)</summary>
          <pre className="mt-2 text-[11px] bg-gray-50 dark:bg-gray-900 rounded p-2 overflow-x-auto max-h-96 text-gray-700 dark:text-gray-300">{run.logTail.join('\n')}</pre>
        </details>
      )}
    </div>
  );
}
