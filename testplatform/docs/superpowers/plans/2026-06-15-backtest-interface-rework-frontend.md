# Backtest Interface Rework — Frontend Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the expert-centric backtest UI in `BA2TestPlatform/frontend`: a `New / History / Saved` tab layout, a source selector (Expert | ML model), an auto-rendered expert-settings form with per-numeric Opt toggles, a universe picker (static list with .txt import / screener filters), the shared Strategy section (condition trees with value-Opt + on/off + JSON import/export), and History/Saved tables filtered by expert + optimization job. Depends on Plan 1 (backend) endpoints.

**Architecture:** React + TypeScript (Vite), fetch-based API (`API_BASE = 'http://localhost:8000/api'`, no axios). The big `pages/Backtesting.tsx` is restructured: extract focused components (`ExpertPicker`, `ExpertSettingsForm`, `UniversePicker`, `RuleIO`, `RunHistoryTable`) and add the source selector + History tab. Reuse `components/ConditionBuilder.tsx` (extended with an on/off toggle). API calls go through a new typed helper `lib/btApi.ts`.

**Tech Stack:** React 18 + TypeScript, Vite. Verify with `npm run build` (type-check) + the dev server (`ba2-test serve` or `npm run dev`) + manual clicks. No test runner exists; Task 1 optionally adds vitest for pure helpers.

**Spec:** `docs/superpowers/specs/2026-06-15-backtest-interface-rework-design.md`
**Backend plan (prereq):** `docs/superpowers/plans/2026-06-15-backtest-interface-rework-backend.md`

**Conventions:**
- All paths under `BA2TestPlatform/frontend`. Build check: `cd frontend && npm run build`.
- Commit after each task on `dev`.
- The backend (Plan 1) must be running for manual verification: `ba2-test serve` (port 8000).

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `frontend/src/lib/btApi.ts` | typed fetch helpers for the new endpoints | Create |
| `frontend/src/lib/symbols.ts` | pure `parseSymbols(text)` (dedup/uppercase) | Create |
| `frontend/src/components/ConditionBuilder.tsx` | add on/off `toggleOptimize` field + render | Modify |
| `frontend/src/components/ExpertPicker.tsx` | expert dropdown (from `/api/experts`) | Create |
| `frontend/src/components/ExpertSettingsForm.tsx` | dynamic settings form + per-numeric Opt | Create |
| `frontend/src/components/UniversePicker.tsx` | static (textarea + .txt import) / screener filters | Create |
| `frontend/src/components/RuleIO.tsx` | Import/Export JSON buttons per ruleset | Create |
| `frontend/src/components/RunHistoryTable.tsx` | runs table + filter bar (shared History/Saved) | Create |
| `frontend/src/pages/Backtesting.tsx` | source selector + History tab + integrate components | Modify |

---

## Task 1: API helpers + pure symbol parser (+ optional vitest)

**Files:**
- Create: `frontend/src/lib/btApi.ts`
- Create: `frontend/src/lib/symbols.ts`
- Test: `frontend/src/lib/symbols.test.ts` (optional, requires vitest)

- [ ] **Step 1: Write the pure parser**

```ts
// frontend/src/lib/symbols.ts
/** Parse a free-text blob (.txt import / paste) into a clean symbol list:
 *  split on comma/whitespace/newline, uppercase, trim, dedup, drop empties. */
export function parseSymbols(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const s = raw.trim().toUpperCase();
    if (s && !seen.has(s)) { seen.add(s); out.push(s); }
  }
  return out;
}
```

- [ ] **Step 2: Write the API helpers**

```ts
// frontend/src/lib/btApi.ts
const API_BASE = 'http://localhost:8000/api';

export interface ExpertInfo { class: string; label: string; bypasses_classic_rm: boolean; uses_risk_manager: boolean; }
export interface SettingDef { type: string; default?: unknown; choices?: unknown[]; valid_values?: unknown[]; description?: string; tooltip?: string; }

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function jpost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const listExperts = () => jget<{ experts: ExpertInfo[] }>('/experts').then(r => r.experts);
export const getExpertSettings = (cls: string) => jget<{ definitions: Record<string, SettingDef> }>(`/experts/${cls}/settings-definitions`).then(r => r.definitions);
export const importRules = (json: unknown, which: 'enter' | 'exit') => jpost<{ tree: unknown }>('/strategies/import-rules', { json, which }).then(r => r.tree);
export const exportRulesUrl = (strategyId: number, which: 'enter' | 'exit') => `${API_BASE}/strategies/${strategyId}/export-rules?which=${which}`;
export const listBacktests = (q: { expert?: string; optimization_id?: number; saved?: boolean } = {}) => {
  const p = new URLSearchParams();
  if (q.expert) p.set('expert', q.expert);
  if (q.optimization_id != null) p.set('optimization_id', String(q.optimization_id));
  if (q.saved != null) p.set('saved', String(q.saved));
  return jget<{ backtests: any[] }>(`/backtests?${p.toString()}`).then(r => r.backtests);
};
```

- [ ] **Step 3 (optional): add vitest + a parser test**

If you want a test runner: `npm i -D vitest`, add `"test": "vitest run"` to `package.json` scripts, then:
```ts
// frontend/src/lib/symbols.test.ts
import { describe, it, expect } from 'vitest';
import { parseSymbols } from './symbols';
describe('parseSymbols', () => {
  it('uppercases, dedups, splits on commas/space/newlines', () => {
    expect(parseSymbols('aapl, msft\nNVDA aapl')).toEqual(['AAPL', 'MSFT', 'NVDA']);
  });
  it('drops empties', () => { expect(parseSymbols('  ,, \n ')).toEqual([]); });
});
```
Run: `cd frontend && npm run test`. Expected: PASS. (If skipping vitest, verify `parseSymbols` by eye against the cases above.)

- [ ] **Step 4: Type-check**

Run: `cd frontend && npm run build`
Expected: builds without TS errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/btApi.ts frontend/src/lib/symbols.ts frontend/src/lib/symbols.test.ts frontend/package.json
git commit -m "feat(ui): bt API helpers + symbol parser"
```

---

## Task 2: ConditionBuilder — add the on/off (toggle) optimize field

The optimizer's `cond:<id>:enabled` / `exit:<id>:enabled` genes need a UI field. `ConditionNode` already has `optimizeEnabled` (value-opt) + `valueMin/Max/Step`; add `toggleOptimize?: boolean` and render a checkbox.

**Files:**
- Modify: `frontend/src/components/ConditionBuilder.tsx`

- [ ] **Step 1: Extend the `ConditionNode` interface**

In `ConditionBuilder.tsx`, add to `ConditionNode`:
```ts
  toggleOptimize?: boolean; // optimizer may turn this condition on/off (cond:<id>:enabled gene)
```

- [ ] **Step 2: Render the toggle**

In the node row JSX (next to the existing `optimizeEnabled` checkbox), add:
```tsx
<label className="text-xs flex items-center gap-1" title="Let the optimizer enable/disable this condition">
  <input type="checkbox" checked={!!node.toggleOptimize}
    onChange={(e) => onChange({ ...node, toggleOptimize: e.target.checked })} />
  on/off opt
</label>
```
(Match the existing checkbox/handler pattern in the file for `optimizeEnabled`.)

- [ ] **Step 3: Type-check**

Run: `cd frontend && npm run build`
Expected: builds clean.

- [ ] **Step 4: Manual verify**

Start dev server, open a condition row, confirm the "on/off opt" checkbox renders and toggles. (Serialization to `toggle_optimize` happens where the strategy payload is built — handled in Task 7 / the existing save path; confirm the saved JSON carries it.)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ConditionBuilder.tsx
git commit -m "feat(ui): condition on/off optimize toggle"
```

---

## Task 3: ExpertPicker + ExpertSettingsForm

**Files:**
- Create: `frontend/src/components/ExpertPicker.tsx`
- Create: `frontend/src/components/ExpertSettingsForm.tsx`

- [ ] **Step 1: ExpertPicker**

```tsx
// frontend/src/components/ExpertPicker.tsx
import React, { useEffect, useState } from 'react';
import { listExperts, ExpertInfo } from '../lib/btApi';

export function ExpertPicker({ value, onChange }: { value: string; onChange: (cls: string, info: ExpertInfo) => void; }) {
  const [experts, setExperts] = useState<ExpertInfo[]>([]);
  useEffect(() => { listExperts().then(setExperts).catch(() => setExperts([])); }, []);
  return (
    <select value={value} onChange={(e) => {
      const info = experts.find(x => x.class === e.target.value);
      if (info) onChange(info.class, info);
    }}>
      <option value="" disabled>Select expert…</option>
      {experts.map(x => <option key={x.class} value={x.class}>{x.label}{x.bypasses_classic_rm ? ' (bypass RM)' : ''}</option>)}
    </select>
  );
}
```

- [ ] **Step 2: ExpertSettingsForm (dynamic, per-numeric Opt)**

```tsx
// frontend/src/components/ExpertSettingsForm.tsx
import React, { useEffect, useState } from 'react';
import { getExpertSettings, SettingDef } from '../lib/btApi';

export interface OptRange { min: number; max: number; step: number; }
export interface ExpertSettingsValue {
  settings: Record<string, unknown>;            // chosen values (fixed)
  expert_params: Record<string, OptRange & { type: string }>; // Opt-on numeric settings
}
const isNumeric = (t: string) => t === 'float' || t === 'int';

export function ExpertSettingsForm({ expertClass, value, onChange }:
  { expertClass: string; value: ExpertSettingsValue; onChange: (v: ExpertSettingsValue) => void; }) {
  const [defs, setDefs] = useState<Record<string, SettingDef>>({});
  useEffect(() => {
    if (!expertClass) return;
    getExpertSettings(expertClass).then((d) => {
      setDefs(d);
      // seed defaults for any setting not yet set
      const settings = { ...value.settings };
      for (const [k, def] of Object.entries(d)) if (!(k in settings) && def.default !== undefined) settings[k] = def.default;
      onChange({ ...value, settings });
    }).catch(() => setDefs({}));
  }, [expertClass]);

  const setVal = (k: string, v: unknown) => onChange({ ...value, settings: { ...value.settings, [k]: v } });
  const setOpt = (k: string, type: string, on: boolean, range?: Partial<OptRange>) => {
    const ep = { ...value.expert_params };
    if (on) ep[k] = { type, min: range?.min ?? 0, max: range?.max ?? 0, step: range?.step ?? 0 };
    else delete ep[k];
    onChange({ ...value, expert_params: ep });
  };

  return (
    <table className="w-full text-sm">
      <tbody>
        {Object.entries(defs).map(([k, def]) => {
          const choices = (def.choices ?? def.valid_values) as unknown[] | undefined;
          const opt = value.expert_params[k];
          return (
            <tr key={k} title={def.tooltip || def.description || ''}>
              <td className="pr-2">{k}</td>
              <td>
                {def.type === 'bool' ? (
                  <input type="checkbox" checked={!!value.settings[k]} onChange={(e) => setVal(k, e.target.checked)} />
                ) : choices ? (
                  <select value={String(value.settings[k] ?? '')} onChange={(e) => setVal(k, e.target.value)}>
                    {choices.map((c) => <option key={String(c)} value={String(c)}>{String(c)}</option>)}
                  </select>
                ) : (
                  <input type={isNumeric(def.type) ? 'number' : 'text'}
                    value={String(value.settings[k] ?? '')}
                    onChange={(e) => setVal(k, isNumeric(def.type) ? Number(e.target.value) : e.target.value)} />
                )}
              </td>
              <td>
                {isNumeric(def.type) && (
                  <label className="text-xs flex gap-1 items-center">
                    <input type="checkbox" checked={!!opt}
                      onChange={(e) => setOpt(k, def.type, e.target.checked)} /> Opt
                    {opt && (<>
                      <input type="number" placeholder="min" value={opt.min}
                        onChange={(e) => setOpt(k, def.type, true, { ...opt, min: Number(e.target.value) })} style={{ width: 60 }} />
                      <input type="number" placeholder="max" value={opt.max}
                        onChange={(e) => setOpt(k, def.type, true, { ...opt, max: Number(e.target.value) })} style={{ width: 60 }} />
                      <input type="number" placeholder="step" value={opt.step}
                        onChange={(e) => setOpt(k, def.type, true, { ...opt, step: Number(e.target.value) })} style={{ width: 60 }} />
                    </>)}
                  </label>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npm run build`. Expected: clean.

- [ ] **Step 4: Manual verify** — temporarily mount `ExpertSettingsForm` (or wait for Task 7 integration): pick FMPRating, confirm settings render, numeric rows show Opt + min/max/step, the `expert_params` object updates.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ExpertPicker.tsx frontend/src/components/ExpertSettingsForm.tsx
git commit -m "feat(ui): expert picker + dynamic settings form with per-numeric Opt"
```

---

## Task 4: UniversePicker (static + .txt import / screener)

**Files:**
- Create: `frontend/src/components/UniversePicker.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/UniversePicker.tsx
import React, { useRef } from 'react';
import { parseSymbols } from '../lib/symbols';

export type UniverseValue =
  | { mode: 'static'; symbols: string[] }
  | { mode: 'screener'; screener_settings: Record<string, number | string> };

const SCREENER_FIELDS: [string, string][] = [
  ['screener_market_cap_min', 'Market cap min'], ['screener_volume_min', 'Volume min'],
  ['screener_price_min', 'Price min'], ['screener_relative_volume_min', 'RVOL min'],
  ['screener_max_stocks', 'Max stocks'],
];

export function UniversePicker({ value, onChange }: { value: UniverseValue; onChange: (v: UniverseValue) => void; }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const onFile = (f: File | undefined) => {
    if (!f) return;
    f.text().then((t) => onChange({ mode: 'static', symbols: parseSymbols(t) }));
  };
  return (
    <div>
      <label><input type="radio" checked={value.mode === 'static'} onChange={() => onChange({ mode: 'static', symbols: value.mode === 'static' ? value.symbols : [] })} /> Static list</label>
      <label className="ml-3"><input type="radio" checked={value.mode === 'screener'} onChange={() => onChange({ mode: 'screener', screener_settings: value.mode === 'screener' ? value.screener_settings : {} })} /> Screener</label>

      {value.mode === 'static' ? (
        <div>
          <textarea style={{ width: '100%', height: 56 }} placeholder="AAPL, MSFT, NVDA …"
            value={value.symbols.join(', ')}
            onChange={(e) => onChange({ mode: 'static', symbols: parseSymbols(e.target.value) })} />
          <button type="button" onClick={() => fileRef.current?.click()}>⬆ Import from .txt</button>
          <input ref={fileRef} type="file" accept=".txt,text/plain" style={{ display: 'none' }}
            onChange={(e) => onFile(e.target.files?.[0])} />
          <span className="text-xs text-gray-500"> {value.symbols.length} symbols</span>
        </div>
      ) : (
        <table className="text-sm">
          <tbody>
            {SCREENER_FIELDS.map(([k, label]) => (
              <tr key={k}><td>{label}</td><td>
                <input type="number" value={Number(value.screener_settings[k] ?? 0)}
                  onChange={(e) => onChange({ mode: 'screener', screener_settings: { ...value.screener_settings, [k]: Number(e.target.value) } })} />
              </td></tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Type-check** — `cd frontend && npm run build`. Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/UniversePicker.tsx
git commit -m "feat(ui): universe picker (static + .txt import / screener filters)"
```

---

## Task 5: RuleIO (Import/Export JSON per ruleset)

**Files:**
- Create: `frontend/src/components/RuleIO.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/RuleIO.tsx
import React, { useRef } from 'react';
import { importRules } from '../lib/btApi';
import { ConditionTree } from './ConditionBuilder';

export function RuleIO({ which, tree, onImport }:
  { which: 'enter' | 'exit'; tree: ConditionTree; onImport: (tree: ConditionTree) => void; }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const doImport = (f: File | undefined) => {
    if (!f) return;
    f.text().then((t) => importRules(JSON.parse(t), which))
      .then((tr) => onImport(tr as ConditionTree))
      .catch((e) => alert(`Import failed: ${e.message}`));
  };
  const doExport = () => {
    // Export uses the in-memory tree -> server v1.1 JSON via a saved strategy; for an unsaved
    // tree, download it directly as a v1.0-compatible blob (the server normalises on import).
    const blob = new Blob([JSON.stringify({ export_version: '1.1', export_type: 'condition_tree', which, tree }, null, 2)], { type: 'application/json' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `rules-${which}.json`; a.click();
  };
  return (
    <span>
      <button type="button" onClick={() => fileRef.current?.click()}>Import JSON</button>
      <button type="button" onClick={doExport}>Export JSON</button>
      <input ref={fileRef} type="file" accept=".json,application/json" style={{ display: 'none' }}
        onChange={(e) => doImport(e.target.files?.[0])} />
    </span>
  );
}
```

Note: when a strategy is saved, prefer the server export endpoint (`exportRulesUrl(id, which)`) so the exported JSON carries the canonical ruleset shape + optimize ranges. The direct-blob export above covers the unsaved-tree case; align with the backend `tree_to_ruleset_json` shape if you want byte-identical round-trips.

- [ ] **Step 2: Type-check + commit**

Run: `cd frontend && npm run build`. Expected: clean.
```bash
git add frontend/src/components/RuleIO.tsx
git commit -m "feat(ui): per-ruleset JSON import/export buttons"
```

---

## Task 6: RunHistoryTable (shared History/Saved with filters)

**Files:**
- Create: `frontend/src/components/RunHistoryTable.tsx`

- [ ] **Step 1: Component**

```tsx
// frontend/src/components/RunHistoryTable.tsx
import React, { useEffect, useMemo, useState } from 'react';
import { listBacktests } from '../lib/btApi';

export function RunHistoryTable({ savedOnly, onSelect }:
  { savedOnly: boolean; onSelect: (id: number) => void; }) {
  const [rows, setRows] = useState<any[]>([]);
  const [expert, setExpert] = useState('');
  const [optId, setOptId] = useState('');
  const [q, setQ] = useState('');

  useEffect(() => {
    listBacktests({ saved: savedOnly ? true : undefined, expert: expert || undefined, optimization_id: optId ? Number(optId) : undefined })
      .then(setRows).catch(() => setRows([]));
  }, [savedOnly, expert, optId]);

  const experts = useMemo(() => Array.from(new Set(rows.map(r => r.expert_name).filter(Boolean))), [rows]);
  const optIds = useMemo(() => Array.from(new Set(rows.map(r => r.optimization_id).filter((x: any) => x != null))), [rows]);
  const filtered = rows.filter(r => !q || (r.name || '').toLowerCase().includes(q.toLowerCase()));

  return (
    <div>
      <div className="flex gap-2 text-sm mb-2">
        <select value={expert} onChange={(e) => setExpert(e.target.value)}><option value="">All experts</option>{experts.map(x => <option key={x}>{x}</option>)}</select>
        <select value={optId} onChange={(e) => setOptId(e.target.value)}><option value="">All opt jobs</option>{optIds.map(x => <option key={x} value={x}>#{x}</option>)}</select>
        <input placeholder="search name" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <table className="w-full text-sm"><thead><tr><th>id</th><th>expert</th><th>opt#</th><th>ret%</th><th>sharpe</th><th>saved</th><th>name</th></tr></thead>
        <tbody>{filtered.map(r => (
          <tr key={r.id} onClick={() => onSelect(r.id)} style={{ cursor: 'pointer' }}>
            <td>{r.id}</td><td>{r.expert_name ?? '—'}</td><td>{r.optimization_id ?? '—'}</td>
            <td>{r.totalReturn ?? r.total_return ?? '—'}</td><td>{r.sharpeRatio ?? r.sharpe_ratio ?? '—'}</td>
            <td>{(r.isSaved ?? r.is_saved) ? '★' : ''}</td><td>{r.name}</td>
          </tr>))}
        </tbody></table>
    </div>
  );
}
```

Note: confirm the backtest list item field names (camelCase via `to_dict()` vs snake) and use the right ones; the fallbacks above cover both.

- [ ] **Step 2: Type-check + commit**

Run: `cd frontend && npm run build`. Expected: clean.
```bash
git add frontend/src/components/RunHistoryTable.tsx
git commit -m "feat(ui): shared runs table with expert + optimization-job filters"
```

---

## Task 7: Integrate into Backtesting.tsx — History tab + source selector + payloads

**Files:**
- Modify: `frontend/src/pages/Backtesting.tsx`

- [ ] **Step 1: Add the History tab**

Change the tab state type (line ~356) from `useState<'new' | 'saved'>('new')` to `useState<'new' | 'history' | 'saved'>('new')`. In the tab-button row, insert a **History** button between New and Saved. Render `<RunHistoryTable savedOnly={false} onSelect={loadBacktestDetails} />` when `backtestCardTab === 'history'` and `<RunHistoryTable savedOnly={true} .../>` for `'saved'` (replace/augment the current saved list). Use the existing handler that loads a backtest's details by id for `onSelect`.

- [ ] **Step 2: Add source selector + expert/universe state**

Add state:
```tsx
const [source, setSource] = useState<'expert' | 'ml'>('expert');
const [expertClass, setExpertClass] = useState('');
const [expertSettings, setExpertSettings] = useState<ExpertSettingsValue>({ settings: {}, expert_params: {} });
const [universe, setUniverse] = useState<UniverseValue>({ mode: 'static', symbols: [] });
```
In the New-tab JSX, above the existing model/dataset block, render the source toggle. When `source === 'expert'`, render `<ExpertPicker>` + `<ExpertSettingsForm>` + `<UniversePicker>` and hide the ML model/symbol/dataset block; when `source === 'ml'`, keep the existing model/dataset/symbol block. Render `<RuleIO>` next to the enter/exit `<ConditionBuilder>`s.

- [ ] **Step 3: Build the run payload by source**

In the `runBacktest` handler (around line 571 where it POSTs `/backtests`), branch on `source`:
```tsx
const body = source === 'expert'
  ? { engine: 'daily_expert', name, start_date: startDate, end_date: endDate,
      expert: { class: expertClass, settings: expertSettings.settings },
      universe, initial_capital: initialCapital, commission, slippage,
      buy_entry_conditions: buyEntryConditions, sell_entry_conditions: sellEntryConditions,
      exit_conditions: exitConditions, initial_tp_percent: initialTpPercent, initial_sl_percent: initialSlPercent }
  : { /* existing ML body unchanged */ };
const res = await fetch(`${API_BASE}/backtests`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
```

- [ ] **Step 4: Build the optimize payload by source**

In the optimize dialog submit (around line 910 `POST /api/strategies/{id}/optimize`), set `optimization_config.expert_params = expertSettings.expert_params` and `optimization_config.backtest` with `engine: source === 'expert' ? 'daily' : 'ml'`, the `expert`/`universe` (expert source) or model/datasets (ml source), and the account settings. Do NOT send `rm_params`.

- [ ] **Step 5: Type-check**

Run: `cd frontend && npm run build`. Expected: clean (fix import paths for the new components + `ExpertSettingsValue`/`UniverseValue` types).

- [ ] **Step 6: Manual end-to-end verify** (backend running via `ba2-test serve`)

- Tabs show New / History / Saved; History lists all runs, Saved only saved; both filter by expert + opt job.
- Source=Expert → pick FMPRating → settings render with Opt toggles; Universe static accepts typed symbols + .txt import; enter/exit conditions with on/off + value Opt; **Run backtest** creates a daily_expert run that appears in History.
- **Run Joint Optimization** posts expert_params + expert/universe; the run/trials appear under the opt-job filter.
- Source=ML model → the legacy model/dataset/symbol flow still runs (regression).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/Backtesting.tsx
git commit -m "feat(ui): expert-centric New tab + History tab + source-aware run/optimize payloads"
```

---

## Task 8: Running-jobs live-progress strip

Backtests already poll status, but optimizations have no live view. The backend exposes
`GET /api/tasks?status=running` (+ pending/queued) with per-task `progress`/`task_type` and
`POST /api/tasks/{id}/cancel`. Add a strip that lists in-flight backtest/optimization jobs
with a progress bar + cancel, polling while any are active.

**Files:**
- Modify: `frontend/src/lib/btApi.ts` (add `listTasks` + `cancelTask`)
- Create: `frontend/src/components/RunningJobsStrip.tsx`
- Modify: `frontend/src/pages/Backtesting.tsx` (render the strip atop the History tab)

- [ ] **Step 1: API helpers** — append to `btApi.ts`:

```ts
export interface TaskInfo { id: string; status: string; task_type?: string; progress?: number; name?: string; }
export const listTasks = (status = 'running') =>
  jget<{ tasks: TaskInfo[] }>(`/tasks?status=${status}&limit=100`).then(r => r.tasks ?? (r as any));
export const cancelTask = (id: string) => jpost<unknown>(`/tasks/${id}/cancel`, {});
```
Note: confirm the real `GET /api/tasks` response shape (it may return a bare list or `{tasks:[...]}`, and field names may be camel or snake) and the task_type strings for backtest vs optimization (e.g. `daily_backtest`, `strategy_optimization`) by calling it / reading `app/services/task_queue.py::list_tasks`; adjust the helper + the `BT_TASK_TYPES` filter below accordingly.

- [ ] **Step 2: Component**

```tsx
// frontend/src/components/RunningJobsStrip.tsx
import React, { useEffect, useState } from 'react';
import { listTasks, cancelTask, TaskInfo } from '../lib/btApi';

const BT_TASK_TYPES = new Set(['daily_backtest', 'backtest', 'strategy_optimization']);

export function RunningJobsStrip() {
  const [jobs, setJobs] = useState<TaskInfo[]>([]);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const all = await listTasks('running');
        if (alive) setJobs(all.filter(t => !t.task_type || BT_TASK_TYPES.has(t.task_type)));
      } catch { if (alive) setJobs([]); }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  if (!jobs.length) return null;
  return (
    <div style={{ border: '1px solid #cbd5e1', borderRadius: 6, padding: 8, marginBottom: 12 }}>
      <b>Running jobs ({jobs.length})</b>
      {jobs.map(j => (
        <div key={j.id} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
          <span style={{ width: 160 }}>{j.task_type ?? 'job'} · {j.name ?? j.id}</span>
          <progress max={100} value={j.progress ?? 0} style={{ flex: 1 }} />
          <span>{Math.round(j.progress ?? 0)}%</span>
          <button type="button" onClick={() => cancelTask(j.id).then(() => setJobs(p => p.filter(x => x.id !== j.id)))}>Cancel</button>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Render it** at the top of the History tab body in `Backtesting.tsx` (and optionally the New tab): `<RunningJobsStrip />`.

- [ ] **Step 4: Type-check** — `cd frontend && npm run build`. Expected: clean.

- [ ] **Step 5: Manual verify** (backend running): launch an optimization → the strip appears with a live progress bar that advances and a working Cancel; disappears when done.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/btApi.ts frontend/src/components/RunningJobsStrip.tsx frontend/src/pages/Backtesting.tsx
git commit -m "feat(ui): running-jobs strip with live progress + cancel"
```

---

## Final verification

- [ ] `cd frontend && npm run build` clean.
- [ ] With `ba2-test serve` running: full manual pass of Task 7 Step 6 (Expert path single run + optimization; ML path regression; History/Saved filters).
- [ ] Screener path: select Screener universe for a range with a built cache → run; for an unbuilt range → expect the fail-fast message from the backend resolver.

## Self-review notes (author)
- Spec coverage: tabs (T7), source selector + ML coexistence (T7), expert picker/settings + per-numeric Opt (T3), universe static/.txt/screener (T4), condition on/off opt (T2), JSON import/export (T5), History/Saved filters (T6), payloads incl expert_params / drop rm_params (T7). Backend endpoints are Plan 1.
- Open confirmations flagged inline (backtest list-item field casing in T6; ConditionNode→backend snake_case serialization on save in T2/T7; exact line anchors in the large Backtesting.tsx in T7) — resolve against the real file while integrating.
- No test runner today → verification is build + manual; Task 1 optionally adds vitest for the one pure helper.
```
