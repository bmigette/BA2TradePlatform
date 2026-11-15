import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle, Loader2, Play } from 'lucide-react';
import { createWfRun, previewWfRun } from '../lib/wfApi';
import type { WfOptions, WfPreview, WfRequest, WfRunDetail } from '../lib/wfApi';
import { buildRequest, initialForm } from '../lib/walkForward';
import type { WfForm } from '../lib/walkForward';

const inputClass = "w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500";
const labelClass = "block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1";
const sectionClass = "bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4";

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className={labelClass}>{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">{hint}</span>}
    </label>
  );
}

function Check({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (v: boolean) => void; hint?: string }) {
  return (
    <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-200">
      <input type="checkbox" className="mt-0.5" checked={checked} onChange={e => onChange(e.target.checked)} />
      <span>{label}{hint && <span className="block text-[11px] text-gray-400 dark:text-gray-500">{hint}</span>}</span>
    </label>
  );
}

interface Props {
  options: WfOptions;
  onCreated: (run: WfRunDetail) => void;
}

/** Create a walk-forward run. The optimize fields render the exact `ba2-test optimize` tokens; the
 * fold table below is the BACKEND's preview (same validation as create), refreshed as you type. */
export function WalkForwardForm({ options, onCreated }: Props) {
  const [form, setForm] = useState<WfForm>(() => initialForm(options));
  // The backend's answer for one request (keyed by the request's JSON, so a stale answer is known).
  const [result, setResult] = useState<{ key: string; preview: WfPreview | null; error: string | null } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const set = <K extends keyof WfForm>(k: K, v: WfForm[K]) => setForm(f => ({ ...f, [k]: v }));

  // Client-side shape errors (a missing number, an unclosed quote) never reach the server.
  const built = useMemo<{ req: WfRequest | null; error: string | null }>(() => {
    try {
      return { req: buildRequest(form), error: null };
    } catch (e) {
      return { req: null, error: e instanceof Error ? e.message : String(e) };
    }
  }, [form]);

  const clientError = built.error ?? (built.req && !built.req.name ? 'Name is required' : null);
  const reqKey = clientError || !built.req ? null : JSON.stringify(built.req);

  useEffect(() => {
    if (!reqKey) return;
    let cancelled = false;
    const t = setTimeout(() => {
      previewWfRun(JSON.parse(reqKey) as WfRequest)
        .then(p => { if (!cancelled) setResult({ key: reqKey, preview: p, error: null }); })
        .catch(e => { if (!cancelled) setResult({ key: reqKey, preview: null, error: e instanceof Error ? e.message : String(e) }); });
    }, 500);
    return () => { cancelled = true; clearTimeout(t); };
  }, [reqKey]);

  const current = result && result.key === reqKey ? result : null;
  const previewing = reqKey !== null && current === null;
  const previewError = clientError ?? current?.error ?? null;
  // While a new answer is pending the last one stays on screen (no flicker); submit waits for it.
  const preview = clientError ? null : (current ?? result)?.preview ?? null;
  const canSubmit = !!current?.preview && !current.preview.nameTaken && !submitting;

  const submit = async () => {
    if (!built.req) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      onCreated(await createWfRun(built.req));
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const isOption = options.optionStrategies.includes(form.strategy);

  return (
    <div className="space-y-4">
      <div className={sectionClass}>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3">Run</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <Field label="Name (chain prefix)" hint="Train jobs are <name>-wf1..N, the refit <name>-refit. A completed job of that name is reused.">
            <input className={inputClass} value={form.name} onChange={e => set('name', e.target.value)} placeholder="wf-fmp-s2-large" />
          </Field>
        </div>
      </div>

      <div className={sectionClass}>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Optimize job</h2>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          Every fold runs <code>ba2-test optimize</code> with these arguments; the chain adds{' '}
          <code>{options.chainSetFlags.join(' ')}</code> per job.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <Field label="Expert">
            <select className={inputClass} value={form.expert} onChange={e => set('expert', e.target.value)}>
              {options.experts.map(x => <option key={x} value={x}>{x}</option>)}
            </select>
          </Field>
          <Field label="Strategy" hint={isOption ? 'option strategy: train windows must end before 2026 (holdout rail)' : undefined}>
            <select className={inputClass} value={form.strategy} onChange={e => set('strategy', e.target.value)}>
              {options.strategies.map(x => <option key={x} value={x}>{x}</option>)}
            </select>
          </Field>
          <Field label="Fitness">
            <input className={inputClass} list="wf-fitness" value={form.fitness} onChange={e => set('fitness', e.target.value)} />
            <datalist id="wf-fitness">{options.fitness.map(x => <option key={x} value={x} />)}</datalist>
          </Field>
          <Field label="Interval">
            <input className={inputClass} value={form.interval} onChange={e => set('interval', e.target.value)} />
          </Field>
          <Field label="Universe">
            <select className={inputClass} value={form.universeMode} onChange={e => set('universeMode', e.target.value as WfForm['universeMode'])}>
              <option value="static">Static symbols</option>
              <option value="screener">Screener (cap band)</option>
            </select>
          </Field>
          <div className="md:col-span-3">
            <Field label={form.universeMode === 'static' ? 'Symbols (comma-separated)' : 'Placeholder universe (the screener replaces it; required by the launcher)'}>
              <input className={inputClass} value={form.universe} onChange={e => set('universe', e.target.value)} placeholder="AAPL,MSFT,NVDA" />
            </Field>
          </div>
          {form.universeMode === 'screener' && (<>
            <Field label="Cap band">
              <select className={inputClass} value={form.capBand} onChange={e => set('capBand', e.target.value)}>
                {options.capBands.map(x => <option key={x} value={x}>{x}</option>)}
              </select>
            </Field>
            <div className="md:col-span-3">
              <Field label="Screener store (empty: the launcher's default)">
                <input className={inputClass} value={form.screenerStore} onChange={e => set('screenerStore', e.target.value)} />
              </Field>
            </div>
          </>)}
          <Field label="Population">
            <input className={inputClass} value={form.population} onChange={e => set('population', e.target.value)} />
          </Field>
          <Field label="Parallel trials">
            <input className={inputClass} value={form.parallel} onChange={e => set('parallel', e.target.value)} />
          </Field>
          <Field label="Early stop (generations)">
            <input className={inputClass} value={form.earlyStop} onChange={e => set('earlyStop', e.target.value)} />
          </Field>
          <Field label="Early-stop min relative gain">
            <input className={inputClass} value={form.earlyStopMinRel} onChange={e => set('earlyStopMinRel', e.target.value)} />
          </Field>
          <Field label="Estimates source" hint="Required, no default (launcher rule)">
            <select className={inputClass} value={form.estimatesSource} onChange={e => set('estimatesSource', e.target.value)}>
              <option value="">— choose —</option>
              {options.estimatesSources.map(x => <option key={x} value={x}>{x}</option>)}
            </select>
          </Field>
          <Field label="OHLCV history source">
            <select className={inputClass} value={form.historySource} onChange={e => set('historySource', e.target.value)}>
              {options.historySources.map(x => <option key={x} value={x}>{x}</option>)}
            </select>
          </Field>
          <Field label="Labels (optional)">
            <input className={inputClass} value={form.labels} onChange={e => set('labels', e.target.value)} />
          </Field>
          <Field label="Remote workers (optional)">
            <input className={inputClass} value={form.workers} onChange={e => set('workers', e.target.value)} />
          </Field>
          <div className="md:col-span-4">
            <Field label="Additional optimize args (verbatim)" hint={`Anything else ba2-test optimize takes (e.g. --market-condition-profile ...). Refused: ${Object.keys(options.forbiddenFlags).join(', ')}.`}>
              <input className={`${inputClass} font-mono`} value={form.extraArgs} onChange={e => set('extraArgs', e.target.value)} />
            </Field>
          </div>
        </div>
      </div>

      <div className={sectionClass}>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3">Walk-forward</h2>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <Field label="Training start" hint="Every fold trains from here (anchored, expanding)">
            <input type="date" className={inputClass} value={form.trainStart} onChange={e => set('trainStart', e.target.value)} />
          </Field>
          <Field label="Test years" hint="One fold per year, ascending">
            <input className={inputClass} value={form.testYears} onChange={e => set('testYears', e.target.value)} />
          </Field>
          <Field label="Data end (optional)" hint="Shortens the last test window; with defer, a date before the last year">
            <input type="date" className={inputClass} value={form.dataEnd} onChange={e => set('dataEnd', e.target.value)} />
          </Field>
          <Field label="Top-N tested per fold">
            <input className={inputClass} value={form.topN} onChange={e => set('topN', e.target.value)} />
          </Field>
          <Field label="Generations, first fold">
            <input className={inputClass} value={form.firstGenerations} onChange={e => set('firstGenerations', e.target.value)} />
          </Field>
          <Field label="Generations, later folds" hint="Seeded from the previous fold's final population">
            <input className={inputClass} value={form.nextGenerations} onChange={e => set('nextGenerations', e.target.value)} />
          </Field>
          <Field label="OOS re-runs at a time">
            <input className={inputClass} value={form.testParallel} onChange={e => set('testParallel', e.target.value)} />
          </Field>
        </div>
        <div className="flex flex-wrap gap-6 mt-3">
          <Check label="Defer the last test" hint="Train the last fold now, test it when the data exists (resume with the full data)" checked={form.deferLastTest} onChange={v => set('deferLastTest', v)} />
          <Check label="Run all folds" hint="No early stop when the run can no longer pass" checked={form.runAllFolds} onChange={v => set('runAllFolds', v)} />
          <Check label="Refit on pass" hint="Train <name>-refit on the whole window when the run passes" checked={form.refit} onChange={v => set('refit', v)} />
        </div>
      </div>

      <div className={sectionClass}>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Pass rule</h2>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          The engine has no default thresholds; the values filled in are the goal2027 grid's
          (tools/grid_goal2027.sh). A rank passes a fold when it trades out of sample, its OOS return
          clears the minimum and its OOS drawdown is within both limits.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
          <Field label="Min OOS return per fold (%)" hint="Total over the test window, not annualised">
            <input className={inputClass} value={form.minOosReturn} onChange={e => set('minOosReturn', e.target.value)} />
          </Field>
          <Field label="Max OOS DD, multiple of IS DD">
            <input className={inputClass} value={form.maxDdMult} onChange={e => set('maxDdMult', e.target.value)} />
          </Field>
          <Field label="Max OOS DD, absolute (%)" hint="Empty: no absolute cap">
            <input className={inputClass} value={form.maxOosDd} onChange={e => set('maxOosDd', e.target.value)} />
          </Field>
          <Field label="Fold winners that must pass">
            <input className={inputClass} value={form.minFolds} onChange={e => set('minFolds', e.target.value)} />
          </Field>
          <Field label="Test years that must pass">
            <input className={inputClass} value={form.requireTestYears} onChange={e => set('requireTestYears', e.target.value)} />
          </Field>
        </div>
        <div className="mt-3">
          <Check label="Return must be ABOVE the minimum (strict)" checked={form.exclusiveReturn} onChange={v => set('exclusiveReturn', v)} />
        </div>
      </div>

      <div className={sectionClass}>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            Folds {previewing && <Loader2 className="w-4 h-4 animate-spin text-blue-500" />}
          </h2>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="flex items-center gap-2 px-4 py-2 text-sm rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
            Create and start
          </button>
        </div>
        {previewError && (
          <div className="mb-3 p-3 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-200 rounded-lg flex items-start gap-2 text-sm whitespace-pre-wrap">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" /> <span>{previewError}</span>
          </div>
        )}
        {submitError && (
          <div className="mb-3 p-3 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-200 rounded-lg flex items-start gap-2 text-sm whitespace-pre-wrap">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" /> <span>{submitError}</span>
          </div>
        )}
        {preview && (
          <>
            {preview.nameTaken && (
              <p className="mb-2 text-sm text-red-600">A walk-forward run with this name exists: open it and resume it, or choose another name.</p>
            )}
            {current && !preview.nameTaken && !previewError && (
              <p className="mb-2 text-sm text-emerald-700 dark:text-emerald-400 flex items-center gap-1"><CheckCircle className="w-4 h-4" /> Valid: the backend accepted this run.</p>
            )}
            {preview.existingJobs.length > 0 && (
              <p className="mb-2 text-sm text-amber-700 dark:text-amber-400">
                Existing optimizations with these job names will be reused when completed (else resumed or refused):{' '}
                {preview.existingJobs.map(j => `${j.name} (#${j.id}, ${j.status})`).join(', ')}
              </p>
            )}
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                  <th className="py-1 pr-3">Fold</th><th className="py-1 pr-3">Train</th><th className="py-1 pr-3">Test</th>
                  <th className="py-1 pr-3">Generations</th><th className="py-1 pr-3">Train job</th>
                </tr>
              </thead>
              <tbody>
                {preview.folds.map(f => (
                  <tr key={f.index} className="border-b border-gray-100 dark:border-gray-700/50 text-gray-800 dark:text-gray-200">
                    <td className="py-1 pr-3">{f.index}</td>
                    <td className="py-1 pr-3 font-mono">{f.train_start} → {f.train_end}</td>
                    <td className="py-1 pr-3 font-mono">{f.test_start} → {f.test_end}{f.deferred && <span className="ml-2 text-xs text-purple-600">test deferred</span>}</td>
                    <td className="py-1 pr-3">{f.generations}</td>
                    <td className="py-1 pr-3 font-mono">{f.train_job}</td>
                  </tr>
                ))}
                {preview.refit && (
                  <tr className="text-gray-600 dark:text-gray-400">
                    <td className="py-1 pr-3">refit</td>
                    <td className="py-1 pr-3 font-mono">{preview.refit.train_start} → {preview.refit.train_end}</td>
                    <td className="py-1 pr-3">— (only if the run passes)</td>
                    <td className="py-1 pr-3">{form.nextGenerations}</td>
                    <td className="py-1 pr-3 font-mono">{preview.refit.job}</td>
                  </tr>
                )}
              </tbody>
            </table>
            <details className="mt-3">
              <summary className="text-xs text-gray-500 dark:text-gray-400 cursor-pointer">Exact commands</summary>
              <pre className="mt-2 p-2 text-[11px] bg-gray-50 dark:bg-gray-900 rounded overflow-x-auto text-gray-700 dark:text-gray-300">
                {[...preview.folds.map(f => f.command.join(' ')), ...(preview.refit ? [preview.refit.command.join(' ')] : [])].join('\n\n')}
              </pre>
            </details>
          </>
        )}
      </div>
    </div>
  );
}
