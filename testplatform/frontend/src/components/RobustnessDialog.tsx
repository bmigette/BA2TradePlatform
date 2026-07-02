import React, { useState } from 'react';
import { X, Shield, Loader2 } from 'lucide-react';
import { launchRobustness } from '../lib/btApi';
import type { RobustnessRequestBody, RobustnessLaunchRun } from '../lib/btApi';

// Robustness launch dialog (Task 6). Mirrors the POST /api/backtests/robustness body 1:1:
//   { backtest_ids, monte_carlo:{enabled,n_paths,seed,methods,drop_k,jitter_bp},
//     schedule:{enabled,day_variants,time_variants} }.
// Submit -> POST -> notice with the created run ids -> onLaunched(bids) so the page can reveal the
// results panels + start polling. Styling reuses the ConfirmDialog primitives (fixed overlay,
// dark-aware card) already used across the page.

const inputClass =
  'px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500';

const MC_METHODS: Array<{ value: string; label: string }> = [
  { value: 'bootstrap', label: 'Bootstrap (resample w/ replacement)' },
  { value: 'shuffle', label: 'Shuffle (permute order — DD path)' },
  { value: 'jitter', label: 'Slippage jitter (± bp noise)' },
];

// Default entry-time shifts (matches the plan's schedule.time_variants example).
const DEFAULT_TIME_VARIANTS = ['10:30', '12:30', '15:00'];

interface Props {
  isOpen: boolean;
  backtestIds: number[];
  backtestNames?: Record<number, string>;
  onClose: () => void;
  // Called after a successful launch with the backtest ids that now have runs — the page reveals
  // their results panels and starts polling.
  onLaunched: (backtestIds: number[]) => void;
}

const RobustnessDialog: React.FC<Props> = ({ isOpen, backtestIds, backtestNames, onClose, onLaunched }) => {
  // Monte-Carlo config
  const [mcEnabled, setMcEnabled] = useState(true);
  const [methods, setMethods] = useState<Set<string>>(new Set(['bootstrap', 'shuffle']));
  const [nPaths, setNPaths] = useState(1000);
  const [seed, setSeed] = useState(42);
  const [dropKText, setDropKText] = useState('1,2,3');
  const [jitterBp, setJitterBp] = useState(0);

  // Schedule config
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [dayVariants, setDayVariants] = useState(true);
  const [timeVariants, setTimeVariants] = useState<Set<string>>(new Set());

  const [launching, setLaunching] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  if (!isOpen) return null;

  const toggleMethod = (m: string) =>
    setMethods(prev => {
      const next = new Set(prev);
      if (next.has(m)) next.delete(m); else next.add(m);
      return next;
    });

  const toggleTime = (t: string) =>
    setTimeVariants(prev => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t); else next.add(t);
      return next;
    });

  const parsedDropK = dropKText
    .split(',')
    .map(s => parseInt(s.trim(), 10))
    .filter(n => Number.isFinite(n) && n > 0);

  const canSubmit =
    backtestIds.length > 0 &&
    (mcEnabled || scheduleEnabled) &&
    !(mcEnabled && methods.size === 0 && parsedDropK.length === 0) &&
    !(scheduleEnabled && !dayVariants && timeVariants.size === 0);

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setLaunching(true);
    setNotice(null);
    const body: RobustnessRequestBody = {
      backtest_ids: backtestIds,
      monte_carlo: {
        enabled: mcEnabled,
        n_paths: nPaths,
        seed,
        methods: [...methods],
        drop_k: parsedDropK,
        jitter_bp: jitterBp,
      },
      schedule: {
        enabled: scheduleEnabled,
        day_variants: dayVariants,
        time_variants: [...timeVariants],
      },
    };
    try {
      const res = await launchRobustness(body);
      const runs: RobustnessLaunchRun[] = res.runs ?? [];
      const mcRuns = runs.filter(r => r.kind === 'monte_carlo').length;
      const schedRuns = runs.filter(r => r.kind === 'schedule').length;
      setNotice({
        kind: 'ok',
        text: `Launched ${runs.length} run(s): ${mcRuns} Monte-Carlo, ${schedRuns} schedule (ids: ${runs.map(r => r.robustness_run_id).join(', ')}).`,
      });
      onLaunched([...new Set(runs.map(r => r.backtest_id))]);
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Failed to launch robustness runs.' });
    } finally {
      setLaunching(false);
    }
  };

  const label = 'text-xs font-medium text-gray-600 dark:text-gray-300';
  const sectionCls = 'border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-3';

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="fixed inset-0 bg-black bg-opacity-50 transition-opacity" onClick={onClose} />
      <div className="flex min-h-full items-center justify-center p-4">
        <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full p-6 max-h-[90vh] overflow-y-auto">
          <button
            onClick={onClose}
            className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <X size={20} />
          </button>

          <div className="mb-4 flex items-center gap-2">
            <Shield size={18} className="text-blue-600 dark:text-blue-400" />
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Robustness stress-test</h3>
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">
            {backtestIds.length} backtest{backtestIds.length === 1 ? '' : 's'} selected
            {backtestNames && backtestIds.length <= 4
              ? `: ${backtestIds.map(id => backtestNames[id] ?? `#${id}`).join(', ')}`
              : ''}
            . Monte-Carlo runs inline (sub-second); schedule variants queue full re-runs (minutes each).
          </p>

          {/* Monte-Carlo */}
          <div className={sectionCls}>
            <label className="flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-200">
              <input type="checkbox" checked={mcEnabled} onChange={e => setMcEnabled(e.target.checked)} />
              Monte-Carlo (over persisted trades)
            </label>
            {mcEnabled && (
              <div className="space-y-3 pl-1">
                <div>
                  <div className={label}>Methods</div>
                  <div className="flex flex-col gap-1 mt-1">
                    {MC_METHODS.map(m => (
                      <label key={m.value} className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                        <input type="checkbox" checked={methods.has(m.value)} onChange={() => toggleMethod(m.value)} />
                        {m.label}
                      </label>
                    ))}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <label className="flex flex-col gap-1">
                    <span className={label}>Paths</span>
                    <input type="number" min={1} step={100} value={nPaths}
                      onChange={e => setNPaths(Math.max(1, parseInt(e.target.value, 10) || 0))} className={inputClass} />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className={label}>Seed</span>
                    <input type="number" value={seed}
                      onChange={e => setSeed(parseInt(e.target.value, 10) || 0)} className={inputClass} />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className={label}>Drop-K best (comma list)</span>
                    <input type="text" value={dropKText} placeholder="1,2,3"
                      onChange={e => setDropKText(e.target.value)} className={inputClass} />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className={label}>Jitter σ (bp)</span>
                    <input type="number" min={0} step={1} value={jitterBp}
                      onChange={e => setJitterBp(Math.max(0, parseFloat(e.target.value) || 0))} className={inputClass} />
                  </label>
                </div>
                <p className="text-[11px] text-gray-500 dark:text-gray-400">
                  Jitter is only applied when "Slippage jitter" is checked. Probabilities use the backend
                  defaults P(ann ≥ 30%) / P(dd ≤ −20%).
                </p>
              </div>
            )}
          </div>

          {/* Schedule */}
          <div className={`${sectionCls} mt-3`}>
            <label className="flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-200">
              <input type="checkbox" checked={scheduleEnabled} onChange={e => setScheduleEnabled(e.target.checked)} />
              Schedule perturbation (daily_expert only — full re-runs)
            </label>
            {scheduleEnabled && (
              <div className="space-y-3 pl-1">
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <input type="checkbox" checked={dayVariants} onChange={e => setDayVariants(e.target.checked)} />
                  Weekly entry-day sweep (Mon…Fri)
                </label>
                <div>
                  <div className={label}>Entry-time variants</div>
                  <div className="flex flex-wrap gap-2 mt-1">
                    {DEFAULT_TIME_VARIANTS.map(t => {
                      const on = timeVariants.has(t);
                      return (
                        <button key={t} type="button" onClick={() => toggleTime(t)}
                          className={`px-2 py-1 text-xs rounded border ${on
                            ? 'bg-blue-500 text-white border-blue-500'
                            : 'bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-300 border-gray-300 dark:border-gray-600'}`}>
                          {t}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}
          </div>

          {notice && (
            <div className={`mt-3 text-sm rounded p-2 ${notice.kind === 'ok'
              ? 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
              : 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'}`}>
              {notice.text}
            </div>
          )}

          <div className="flex justify-end space-x-3 mt-5">
            <button onClick={onClose}
              className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600">
              Close
            </button>
            <button onClick={handleSubmit} disabled={!canSubmit || launching}
              className="px-4 py-2 rounded-md bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2">
              {launching && <Loader2 size={16} className="animate-spin" />}
              Launch
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default RobustnessDialog;
