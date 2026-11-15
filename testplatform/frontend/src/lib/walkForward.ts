// Pure helpers of the Walk-forward pages: the create form -> the API request (the exact
// `ba2-test optimize` tokens + the walk-forward spec), and display formatting.
import type { WfOptions, WfRequest, WfStatus } from './wfApi';

export interface WfForm {
  name: string;
  expert: string;
  strategy: string;
  universeMode: 'static' | 'screener';
  universe: string;
  screenerStore: string;
  capBand: string;
  fitness: string;
  population: string;
  parallel: string;
  interval: string;
  estimatesSource: string;
  historySource: string;
  earlyStop: string;
  earlyStopMinRel: string;
  labels: string;
  workers: string;
  extraArgs: string;
  trainStart: string;
  testYears: string;
  dataEnd: string;
  firstGenerations: string;
  nextGenerations: string;
  topN: string;
  testParallel: string;
  minOosReturn: string;
  exclusiveReturn: boolean;
  maxDdMult: string;
  maxOosDd: string;
  minFolds: string;
  requireTestYears: string;
  deferLastTest: boolean;
  runAllFolds: boolean;
  refit: boolean;
}

/** The create form prefilled from the options (goal2027 grid values; no estimates source). */
export function initialForm(o: WfOptions): WfForm {
  const p = o.prefill;
  return {
    name: '', expert: o.experts.includes('FMPRating') ? 'FMPRating' : (o.experts[0] ?? ''),
    strategy: o.strategies.includes('S2') ? 'S2' : (o.strategies[0] ?? ''),
    universeMode: 'static', universe: '', screenerStore: '', capBand: 'large',
    fitness: p.fitness, population: String(p.population), parallel: String(p.parallel),
    interval: p.interval, estimatesSource: '', historySource: o.defaultHistorySource,
    earlyStop: String(p.early_stop), earlyStopMinRel: String(p.early_stop_min_rel), labels: '',
    workers: '', extraArgs: '', trainStart: p.train_start, testYears: p.test_years.join(', '),
    dataEnd: '', firstGenerations: String(p.first_generations),
    nextGenerations: String(p.next_generations), topN: String(p.top_n),
    testParallel: String(p.test_parallel), minOosReturn: String(p.min_oos_return),
    exclusiveReturn: p.exclusive_return, maxDdMult: String(p.max_dd_mult),
    maxOosDd: p.max_oos_dd == null ? '' : String(p.max_oos_dd), minFolds: String(p.min_folds),
    requireTestYears: p.require_test_years.join(', '), deferLastTest: p.defer_last_test,
    runAllFolds: p.run_all_folds, refit: p.refit,
  };
}

/** Split a command-line fragment into tokens: whitespace separates, '...' and "..." group. */
export function splitArgs(text: string): string[] {
  const out: string[] = [];
  let cur = '';
  let quote: string | null = null;
  let has = false;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null; else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch; has = true;
    } else if (/\s/.test(ch)) {
      if (has || cur) out.push(cur);
      cur = ''; has = false;
    } else {
      cur += ch; has = true;
    }
  }
  if (quote) throw new Error(`unclosed ${quote} quote in the extra optimize args`);
  if (has || cur) out.push(cur);
  return out;
}

/** "2024, 2025 2026" -> [2024, 2025, 2026]; refuses anything that is not a 4-digit year. */
export function parseYears(text: string, what: string): number[] {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const bad = parts.filter(p => !/^\d{4}$/.test(p));
  if (bad.length) throw new Error(`${what}: ${bad.join(', ')} is not a year`);
  return parts.map(Number);
}

function num(v: string, what: string, opts: { int?: boolean; optional?: boolean } = {}): number | null {
  if (v.trim() === '') {
    if (opts.optional) return null;
    throw new Error(`${what} is required`);
  }
  const n = Number(v);
  if (!Number.isFinite(n) || (opts.int && !Number.isInteger(n))) {
    throw new Error(`${what}: ${v} is not ${opts.int ? 'a whole number' : 'a number'}`);
  }
  return n;
}

/** The `ba2-test optimize` tokens of the form (what follows `--` on the CLI). The chain adds
 * --start/--end/--name/--generations per job. Empty optional fields are not sent (the launcher's
 * own rule then applies, as on the CLI). */
export function buildOptimizeArgs(f: WfForm): string[] {
  const req = (v: string, what: string) => {
    if (!v.trim()) throw new Error(`${what} is required`);
    return v.trim();
  };
  const args = ['--expert', req(f.expert, 'Expert'), '--strategy', req(f.strategy, 'Strategy'),
    '--universe', req(f.universe, f.universeMode === 'static' ? 'Universe' : 'Placeholder universe')];
  if (f.universeMode === 'screener') {
    args.push('--screener');
    if (f.screenerStore.trim()) args.push('--screener-store', f.screenerStore.trim());
    args.push('--screener-cap-band', req(f.capBand, 'Cap band'));
  }
  args.push('--fitness', req(f.fitness, 'Fitness'));
  args.push('--population', String(num(f.population, 'Population', { int: true })));
  args.push('--parallel', String(num(f.parallel, 'Parallel', { int: true })));
  args.push('--interval', req(f.interval, 'Interval'));
  if (f.estimatesSource) args.push('--estimates-source', f.estimatesSource);
  if (f.historySource) args.push('--ohlcv-history-source', f.historySource);
  if (f.earlyStop.trim()) args.push('--early-stop', String(num(f.earlyStop, 'Early stop', { int: true })));
  if (f.earlyStopMinRel.trim()) args.push('--early-stop-min-rel', String(num(f.earlyStopMinRel, 'Early-stop min rel')));
  if (f.labels.trim()) args.push('--labels', f.labels.trim());
  if (f.workers.trim()) args.push('--workers', f.workers.trim());
  return [...args, ...splitArgs(f.extraArgs)];
}

/** The create / preview request of the form; throws with a readable message on a bad field. */
export function buildRequest(f: WfForm): WfRequest {
  return {
    name: f.name.trim(),
    optimize_args: buildOptimizeArgs(f),
    train_start: f.trainStart.trim(),
    test_years: parseYears(f.testYears, 'Test years'),
    data_end: f.dataEnd.trim() || null,
    first_generations: num(f.firstGenerations, 'Generations (first fold)', { int: true })!,
    next_generations: num(f.nextGenerations, 'Generations (later folds)', { int: true })!,
    top_n: num(f.topN, 'Top-N', { int: true })!,
    test_parallel: num(f.testParallel, 'Test parallel', { int: true })!,
    min_oos_return: num(f.minOosReturn, 'Min OOS return')!,
    exclusive_return: f.exclusiveReturn,
    max_dd_mult: num(f.maxDdMult, 'Max DD multiple')!,
    max_oos_dd: num(f.maxOosDd, 'Max OOS drawdown', { optional: true }),
    min_folds: num(f.minFolds, 'Min passing folds', { int: true })!,
    require_test_years: parseYears(f.requireTestYears, 'Required test years'),
    defer_last_test: f.deferLastTest,
    run_all_folds: f.runAllFolds,
    refit: f.refit,
  };
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`;
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—';
  return v.toFixed(digits);
}

/** |drawdown| as a negative percent (drawdown sign is not trusted, as in the engine). */
export function fmtDd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—';
  return `-${Math.abs(v).toFixed(2)}%`;
}

// Solid pills (white text): readable in both themes, as OptimizationJobsTable does.
export const WF_STATUS_STYLES: Record<WfStatus, string> = {
  pending: 'bg-amber-600 text-white border-amber-600',
  running: 'bg-blue-600 text-white border-blue-600',
  'deferred-pending': 'bg-purple-600 text-white border-purple-600',
  pass: 'bg-emerald-600 text-white border-emerald-600',
  fail: 'bg-red-600 text-white border-red-600',
  error: 'bg-rose-800 text-white border-rose-800',
  cancelled: 'bg-slate-500 text-white border-slate-500',
  incomplete: 'bg-yellow-700 text-white border-yellow-700',
};

/** Fraction (0..1) of `iso` between `lo` and `hi` (ISO dates), for the fold timeline. */
export function datePos(iso: string, lo: string, hi: string): number {
  const a = Date.parse(lo), b = Date.parse(hi), x = Date.parse(iso);
  if (!(b > a)) return 0;
  return Math.min(1, Math.max(0, (x - a) / (b - a)));
}
