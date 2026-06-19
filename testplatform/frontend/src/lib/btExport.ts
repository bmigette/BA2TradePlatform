// Shared export/import schema for optimization-job settings and individual params.
//
// The schema is defined ONCE here and used on both ends:
//   - Part 4 (export): build a self-describing JSON blob from the loaded optimization detail /
//     job settings (opt-job export) or from one individual's concrete gene dict (individual export).
//   - Part 5 (import): the New-Backtest form reads either blob and pre-fills its fields.
//
// All downloads are client-side (Blob + <a download>); no server filesystem write.

import type { OptimizationJob, OptJobSettings, OptIndividual } from './btApi';

// Version the on-disk shape so the importer can detect/upgrade older files.
export const BT_EXPORT_VERSION = 1 as const;

/** Universe block shared by both export shapes. */
export type ExportUniverse =
  | { mode: 'static'; symbols: string[] }
  | { mode: 'screener'; screener_settings: Record<string, number | string>; group?: string; cache_db?: string }
  | { mode: string | null };

/** Fields shared by BOTH export shapes — used by the importer to read common backtest context
 *  without an `OptSettingsExport & IndividualExport` intersection (which collapses to never on
 *  the conflicting `schema` literal). */
export interface BtExportCommon {
  engine?: string | null;
  startDate?: string | null;
  endDate?: string | null;
  executionInterval?: string | null;
  initialCapital?: number | null;
  universe?: ExportUniverse;
}

/** A self-describing export of a whole optimization JOB's settings. */
export interface OptSettingsExport {
  schema: 'ba2.opt-settings';
  version: number;
  exportedAt: string;
  optimizationId: number;
  name: string | null;
  fitnessMetric: string | null;
  optimizationType?: string | null;
  /** Genetic-algorithm config (populationSize, generations, crossover/mutation prob, etc.). */
  ga: OptJobSettings['ga'];
  /** Backtest window + engine. */
  engine: string | null;
  startDate: string | null;
  endDate: string | null;
  executionInterval?: string | null;
  initialCapital?: number | null;
  /** Universe: static symbols or screener settings. */
  universe: ExportUniverse;
  /** Optimized expert/RM parameter RANGES ({name: {min,max,step,type}}). */
  expertRanges: OptJobSettings['expertRanges'];
}

/** A self-describing export of ONE individual's concrete (resolved) params. */
export interface IndividualExport {
  schema: 'ba2.opt-individual';
  version: number;
  exportedAt: string;
  optimizationId: number;
  optimizationName: string | null;
  fitnessMetric: string | null;
  rank: number;
  fitness?: number | null;
  nTrades?: number | null;
  /** Backtest window + engine + universe carried from the parent job so an individual
   *  export round-trips into a runnable New-Backtest config on its own. */
  engine?: string | null;
  startDate?: string | null;
  endDate?: string | null;
  executionInterval?: string | null;
  initialCapital?: number | null;
  universe?: ExportUniverse;
  /** Concrete gene dict: tp/sl + model:* / cond:* / exit:* / screener:* (whatever the GA tuned). */
  params: Record<string, unknown>;
}

/** Trigger a browser download of a JSON object (no server filesystem write). */
export function downloadJson(filename: string, data: unknown): void {
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

/** Pull the backtest{} sub-block of an optimization config (engine/universe/window/capital).
 *  The full config is only present on the detail endpoint; pass it explicitly when available. */
function backtestBlock(cfg?: Record<string, unknown> | null): Record<string, unknown> {
  const bt = (cfg && typeof cfg === 'object' ? (cfg as any).backtest : undefined) as
    | Record<string, unknown>
    | undefined;
  return bt && typeof bt === 'object' ? bt : {};
}

/** Build the universe block for an export, preferring the full config's universe, else the summary. */
function universeFor(
  cfg: Record<string, unknown> | null | undefined,
  s: OptJobSettings,
): OptSettingsExport['universe'] {
  const bt = backtestBlock(cfg);
  const u = (bt.universe as any) ?? null;
  if (u && typeof u === 'object') {
    if (u.mode === 'static' && Array.isArray(u.symbols)) {
      return { mode: 'static', symbols: u.symbols.map(String) };
    }
    if (u.mode === 'screener') {
      return {
        mode: 'screener',
        screener_settings: (u.screener_settings as Record<string, number | string>) ?? s.screener?.screener_settings ?? {},
        ...(u.group ? { group: String(u.group) } : {}),
        ...(u.cache_db ? { cache_db: String(u.cache_db) } : {}),
      };
    }
  }
  // Fall back to the compact summary (no static symbol list available there).
  if (s.universeMode === 'screener') {
    return {
      mode: 'screener',
      screener_settings: s.screener?.screener_settings ?? {},
      ...(s.screener?.group ? { group: s.screener.group } : {}),
      ...(s.screener?.cache_db ? { cache_db: s.screener.cache_db } : {}),
    };
  }
  return { mode: s.universeMode ?? null };
}

/** Build the opt-job settings export blob (client-side, from the already-loaded job + optional
 *  full config from the detail endpoint, which carries the static universe symbol list). */
export function buildOptSettingsExport(
  job: OptimizationJob,
  cfg?: Record<string, unknown> | null,
): OptSettingsExport {
  const s = job.settings;
  const bt = backtestBlock(cfg);
  return {
    schema: 'ba2.opt-settings',
    version: BT_EXPORT_VERSION,
    exportedAt: new Date().toISOString(),
    optimizationId: job.id,
    name: job.name ?? null,
    fitnessMetric: job.fitnessMetric ?? s.fitnessMetric ?? null,
    optimizationType: job.optimizationType ?? null,
    ga: s.ga ?? {},
    engine: s.engine ?? (bt.engine as string) ?? null,
    startDate: s.startDate ?? (bt.start_date as string) ?? null,
    endDate: s.endDate ?? (bt.end_date as string) ?? null,
    executionInterval: (bt.execution_interval as string) ?? null,
    initialCapital: (bt.initial_capital as number) ?? null,
    universe: universeFor(cfg, s),
    expertRanges: s.expertRanges ?? {},
  };
}

/** Build a single-individual export blob (concrete params + parent backtest context). */
export function buildIndividualExport(
  job: OptimizationJob,
  ind: OptIndividual,
  cfg?: Record<string, unknown> | null,
): IndividualExport {
  const s = job.settings;
  const bt = backtestBlock(cfg);
  return {
    schema: 'ba2.opt-individual',
    version: BT_EXPORT_VERSION,
    exportedAt: new Date().toISOString(),
    optimizationId: job.id,
    optimizationName: job.name ?? null,
    fitnessMetric: job.fitnessMetric ?? s.fitnessMetric ?? null,
    rank: ind.rank,
    fitness: ind.fitness ?? null,
    nTrades: ind.nTrades ?? null,
    engine: s.engine ?? (bt.engine as string) ?? null,
    startDate: s.startDate ?? (bt.start_date as string) ?? null,
    endDate: s.endDate ?? (bt.end_date as string) ?? null,
    executionInterval: (bt.execution_interval as string) ?? null,
    initialCapital: (bt.initial_capital as number) ?? null,
    universe: universeFor(cfg, s),
    params: ind.params ?? {},
  };
}

export type ImportedExport =
  | { kind: 'opt'; data: OptSettingsExport }
  | { kind: 'individual'; data: IndividualExport };

/** Parse an imported JSON blob, detecting which of the two schemas it is. Throws on a bad shape. */
export function parseExport(raw: string): ImportedExport {
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    throw new Error('Not valid JSON.');
  }
  if (!obj || typeof obj !== 'object') throw new Error('Empty or non-object JSON.');
  const o = obj as Record<string, unknown>;
  if (o.schema === 'ba2.opt-individual') return { kind: 'individual', data: o as unknown as IndividualExport };
  if (o.schema === 'ba2.opt-settings') return { kind: 'opt', data: o as unknown as OptSettingsExport };
  // Tolerate schema-less blobs: an `params` object -> individual; `expertRanges`/`ga` -> opt-settings.
  if (o.params && typeof o.params === 'object') return { kind: 'individual', data: o as unknown as IndividualExport };
  if (o.ga || o.expertRanges) return { kind: 'opt', data: o as unknown as OptSettingsExport };
  throw new Error('Unrecognized export: expected an opt-settings or opt-individual JSON.');
}
