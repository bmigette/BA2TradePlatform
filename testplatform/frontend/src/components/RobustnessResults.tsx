import React, { useEffect, useState, useCallback } from 'react';
import { ChevronDown, ChevronUp, Loader2, Shield } from 'lucide-react';
import {
  ResponsiveContainer,
  ComposedChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  Cell,
} from 'recharts';
import { listRobustnessRuns } from '../lib/btApi';
import type { RobustnessRun, McMethodSummary, McDropKRow, ScheduleVariantRow } from '../lib/btApi';

// Per-backtest robustness results panel (Task 6), collapsible. Polls
//   GET /api/backtests/robustness?backtest_id=<id>
// while any of THIS backtest's runs is still pending/running (schedule variants are minutes-long),
// then renders:
//   * MC: percentile bands (p5/p50/p95 for annualized_return / max_drawdown / calmar),
//     P(ann ≥ target) + P(dd ≤ −limit) badges, drop-K table, ann-return percentile bar chart.
//   * Schedule: variant table (ann/calmar/dd), parent row highlighted, spread badge.
// Field names bound EXACTLY to the backend (_robustness_run_out snake_case + monte_carlo output +
// collect_schedule_results schedule_summary).

const POLL_MS = 4000;

const fmtPct = (v: number | null | undefined, dp = 1): string =>
  v == null || !Number.isFinite(Number(v)) ? '—' : `${Number(v).toFixed(dp)}%`;
const fmtNum = (v: number | null | undefined, dp = 2): string =>
  v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(dp);
const fmtProb = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(Number(v)) ? '—' : `${(Number(v) * 100).toFixed(0)}%`;

// Badge: solid -600 bg + text-white so it renders in both light + dark (test-platform pill contract).
function Badge({ text, tone }: { text: string; tone: 'green' | 'red' | 'blue' | 'amber' | 'gray' }) {
  const tones: Record<string, string> = {
    green: 'bg-green-600 text-white',
    red: 'bg-red-600 text-white',
    blue: 'bg-blue-600 text-white',
    amber: 'bg-amber-600 text-white',
    gray: 'bg-gray-600 text-white',
  };
  return <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${tones[tone]}`}>{text}</span>;
}

function StatusBadge({ status }: { status: string }) {
  const tone = status === 'completed' ? 'green' : status === 'failed' ? 'red' : 'amber';
  return <Badge text={status} tone={tone} />;
}

// ---------------------------------------------------------------------------
// Monte-Carlo section
// ---------------------------------------------------------------------------
function McMethodTable({ name, summary }: { name: string; summary: McMethodSummary }) {
  // Percentile-band bar chart for annualized_return (the plan's ann-return distribution view).
  // MC results carry ONLY the percentile bands (not raw paths), so we chart the p5..p95 bars.
  const bandData = [
    { pct: 'p5', value: summary.annualized_return.p5 },
    { pct: 'p25', value: summary.annualized_return.p25 },
    { pct: 'p50', value: summary.annualized_return.p50 },
    { pct: 'p75', value: summary.annualized_return.p75 },
    { pct: 'p95', value: summary.annualized_return.p95 },
  ];
  const barColor = (v: number) => (v >= 0 ? '#16a34a' : '#dc2626');

  const rows: Array<{ label: string; band: McMethodSummary['annualized_return']; pct?: boolean }> = [
    { label: 'Annualized return', band: summary.annualized_return, pct: true },
    { label: 'Max drawdown', band: summary.max_drawdown, pct: true },
    { label: 'Calmar', band: summary.calmar },
  ];

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-3">
      <div className="flex items-center justify-between">
        <h5 className="text-sm font-semibold text-gray-800 dark:text-gray-200 capitalize">{name}</h5>
        <span className="text-xs text-gray-500 dark:text-gray-400">{summary.n_paths} paths</span>
      </div>

      <div className="flex flex-wrap gap-2">
        <Badge text={`P(ann ≥ 30%): ${fmtProb(summary.prob_target_annual)}`} tone="blue" />
        <Badge text={`P(dd ≤ −20%): ${fmtProb(summary.prob_dd_breach)}`} tone="amber" />
        {summary.consistency != null && (
          <Badge text={`consistency: ${fmtNum(summary.consistency)}`} tone="gray" />
        )}
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 dark:text-gray-400 text-right">
            <th className="text-left py-1">metric</th>
            <th className="py-1">p5</th>
            <th className="py-1">p25</th>
            <th className="py-1">p50</th>
            <th className="py-1">p75</th>
            <th className="py-1">p95</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.label} className="border-t border-gray-100 dark:border-gray-700/50 text-right">
              <td className="text-left py-1 text-gray-700 dark:text-gray-300">{r.label}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{r.pct ? fmtPct(r.band.p5) : fmtNum(r.band.p5)}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{r.pct ? fmtPct(r.band.p25) : fmtNum(r.band.p25)}</td>
              <td className="py-1 font-semibold text-gray-900 dark:text-gray-100">{r.pct ? fmtPct(r.band.p50) : fmtNum(r.band.p50)}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{r.pct ? fmtPct(r.band.p75) : fmtNum(r.band.p75)}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{r.pct ? fmtPct(r.band.p95) : fmtNum(r.band.p95)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div>
        <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">Annualized-return percentile bands (%/yr)</div>
        <ResponsiveContainer width="100%" height={140}>
          <ComposedChart data={bandData} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#8884" />
            <XAxis dataKey="pct" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <RechartsTooltip formatter={(v) => `${Number(v).toFixed(2)}%`} />
            <Bar dataKey="value" name="ann return">
              {bandData.map((d, i) => <Cell key={i} fill={barColor(d.value)} />)}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function DropKTable({ rows }: { rows: McDropKRow[] }) {
  if (!rows.length) return null;
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3">
      <h5 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-2">
        Drop-K best (“was it luck?”)
      </h5>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 dark:text-gray-400 text-right">
            <th className="text-left py-1">without best</th>
            <th className="py-1">ann return</th>
            <th className="py-1">max DD</th>
            <th className="py-1">calmar</th>
            <th className="py-1">dropped pnl%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.k} className="border-t border-gray-100 dark:border-gray-700/50 text-right">
              <td className="text-left py-1 text-gray-700 dark:text-gray-300">best {r.k} trade{r.k === 1 ? '' : 's'}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{fmtPct(r.annualized_return)}</td>
              <td className="py-1 text-red-600 dark:text-red-400">{fmtPct(r.max_drawdown)}</td>
              <td className="py-1 text-gray-900 dark:text-gray-100">{fmtNum(r.calmar)}</td>
              <td className="py-1 text-gray-500 dark:text-gray-400 text-[11px]">
                {r.dropped.map(d => `${d.toFixed(1)}%`).join(', ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MonteCarloResults({ run }: { run: RobustnessRun }) {
  if (run.status === 'failed') {
    return <p className="text-sm text-red-600 dark:text-red-400">Monte-Carlo failed: {run.error_message || 'unknown error'}</p>;
  }
  const res = run.results;
  if (!res || !res.methods) {
    return <p className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> computing…</p>;
  }
  const methodNames = Object.keys(res.methods);
  return (
    <div className="space-y-3">
      <div className="text-xs text-gray-500 dark:text-gray-400">
        {res.n_trades} trades · {res.years?.toFixed?.(2)} yrs
      </div>
      {methodNames.map(name => (
        <McMethodTable key={name} name={name} summary={res.methods[name]} />
      ))}
      <DropKTable rows={res.drop_k || []} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Schedule section
// ---------------------------------------------------------------------------
function ScheduleResultsView({ run, parentBacktestId }: { run: RobustnessRun; parentBacktestId: number }) {
  if (run.status === 'failed') {
    return <p className="text-sm text-red-600 dark:text-red-400">Schedule variants failed: {run.error_message || 'unknown error'}</p>;
  }
  const summary: ScheduleVariantRow[] = run.results?.schedule_summary || [];
  const spread = run.results?.ann_return_spread;
  const nVariants = run.variant_backtest_ids?.length ?? 0;

  if (!summary.length) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2">
        <Loader2 size={14} className="animate-spin" />
        {nVariants ? `running ${nVariants} variant re-run(s)…` : 'launching variants…'}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {spread != null && <Badge text={`ann-return spread: ${fmtPct(spread)}`} tone="blue" />}
        <span className="text-xs text-gray-500 dark:text-gray-400">{summary.length} variants</span>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 dark:text-gray-400 text-right">
            <th className="text-left py-1">variant</th>
            <th className="py-1">status</th>
            <th className="py-1">ann return</th>
            <th className="py-1">calmar</th>
            <th className="py-1">max DD</th>
            <th className="py-1">trades</th>
          </tr>
        </thead>
        <tbody>
          {summary.map(v => {
            const isParent = v.backtest_id === parentBacktestId;
            return (
              <tr key={v.backtest_id}
                className={`border-t border-gray-100 dark:border-gray-700/50 text-right ${isParent
                  ? 'bg-blue-50 dark:bg-blue-900/20 font-semibold'
                  : ''}`}>
                <td className="text-left py-1 text-gray-700 dark:text-gray-300">
                  <div className="max-w-[14rem] truncate" title={v.name}>{v.name}{isParent ? ' (parent)' : ''}</div>
                </td>
                <td className="py-1"><StatusBadge status={v.status} /></td>
                <td className="py-1 text-gray-900 dark:text-gray-100">{fmtPct(v.annualized_return)}</td>
                <td className="py-1 text-gray-900 dark:text-gray-100">{fmtNum(v.calmar)}</td>
                <td className="py-1 text-red-600 dark:text-red-400">{fmtPct(v.max_drawdown)}</td>
                <td className="py-1 text-gray-900 dark:text-gray-100">{v.total_trades ?? '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------
interface Props {
  backtestId: number;
  backtestName?: string;
  // Bumped by the page after a launch so the panel refetches immediately (rather than waiting for
  // the next poll tick).
  refreshKey?: number;
}

const RobustnessResults: React.FC<Props> = ({ backtestId, backtestName, refreshKey }) => {
  const [open, setOpen] = useState(true);
  const [runs, setRuns] = useState<RobustnessRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await listRobustnessRuns(backtestId);
      setRuns(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'failed to load robustness runs');
    } finally {
      setLoading(false);
    }
  }, [backtestId]);

  // Poll while any run is still pending/running (schedule variants take minutes). Reuses the page's
  // setInterval polling idiom (alive guard + clearInterval on cleanup). The status-join dep restarts
  // the interval when the set of runs / their statuses change so it stops once everything is terminal.
  const statusKey = runs.map(r => `${r.robustness_run_id}:${r.status}`).join(',');
  useEffect(() => {
    let alive = true;
    load();
    const anyPending = runs.some(r => r.status !== 'completed' && r.status !== 'failed');
    if (!anyPending && runs.length > 0) return () => { alive = false; };
    const id = setInterval(() => { if (alive) load(); }, POLL_MS);
    return () => { alive = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backtestId, load, refreshKey, statusKey]);

  const mcRuns = runs.filter(r => r.kind === 'monte_carlo');
  const schedRuns = runs.filter(r => r.kind === 'schedule');
  const pendingCount = runs.filter(r => r.status !== 'completed' && r.status !== 'failed').length;

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700/50"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-200">
          <Shield size={15} className="text-blue-600 dark:text-blue-400" />
          Robustness {backtestName ? `— ${backtestName}` : `#${backtestId}`}
          {pendingCount > 0 && <Loader2 size={13} className="animate-spin text-amber-500" />}
        </span>
        <span className="flex items-center gap-2">
          <span className="text-xs text-gray-500 dark:text-gray-400">{runs.length} run{runs.length === 1 ? '' : 's'}</span>
          {open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </span>
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-4">
          {loading && runs.length === 0 && (
            <p className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2">
              <Loader2 size={14} className="animate-spin" /> loading…
            </p>
          )}
          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          {!loading && runs.length === 0 && !error && (
            <p className="text-sm text-gray-500 dark:text-gray-400">No robustness runs yet.</p>
          )}

          {mcRuns.map(run => (
            <div key={run.robustness_run_id} className="space-y-2">
              <div className="flex items-center gap-2">
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Monte-Carlo</h4>
                <StatusBadge status={run.status} />
              </div>
              <MonteCarloResults run={run} />
            </div>
          ))}

          {schedRuns.map(run => (
            <div key={run.robustness_run_id} className="space-y-2">
              <div className="flex items-center gap-2">
                <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Schedule perturbation</h4>
                <StatusBadge status={run.status} />
              </div>
              <ScheduleResultsView run={run} parentBacktestId={backtestId} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default RobustnessResults;
