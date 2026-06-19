// frontend/src/components/ExpertSettingsForm.tsx
import { useEffect, useState } from 'react';
import { getExpertSettings } from '../lib/btApi';
import type { SettingDef } from '../lib/btApi';
import { ScheduleEditor } from './ScheduleEditor';

// `optimize` is optional/additive: existing expert-setting genes omit it (treated as
// on when present), RM genes set it explicitly to true so the optimizer merges them
// into expert_params via the model:* namespace keyed by the real ba2 setting name.
export interface OptRange { min: number; max: number; step: number; optimize?: boolean; }
export interface ExpertSettingsValue {
  settings: Record<string, unknown>;            // chosen values (fixed)
  expert_params: Record<string, OptRange & { type: string }>; // Opt-on numeric settings
}

// Risk-Management genes (CLI _RM_OPT), keyed by the REAL ba2 setting names. Each shows
// a current-value input (-> settings[name]) and an Opt checkbox + min/max/step
// (-> expert_params[name] = {type:'float', min, max, step, optimize:true}).
const RM_FIELDS: { key: string; label: string; def: { min: number; max: number; step: number } }[] = [
  { key: 'risk_per_trade_pct', label: 'Risk per trade %', def: { min: 0.5, max: 5.0, step: 0.5 } },
  { key: 'atr_multiplier', label: 'ATR multiplier', def: { min: 1.5, max: 4.0, step: 0.5 } },
  { key: 'min_stop_loss_pct', label: 'Min stop-loss %', def: { min: 3.0, max: 10.0, step: 1.0 } },
  { key: 'max_virtual_equity_per_instrument_percent', label: 'Max equity / instrument %', def: { min: 5.0, max: 30.0, step: 5.0 } },
];
const isNumeric = (t: string) => t === 'float' || t === 'int';
// enabled_instruments is the universe — owned by the UniversePicker, so keep it out of this list.
const HIDDEN_KEYS = new Set(['enabled_instruments']);
// Schedule objects get a dedicated editor (days + times) instead of a raw text input.
const SCHEDULE_KEYS = new Set(['execution_schedule_enter_market', 'execution_schedule_open_positions']);
// Render any value safely — objects/arrays as JSON instead of "[object Object]".
const displayVal = (v: unknown) =>
  v != null && typeof v === 'object' ? JSON.stringify(v) : String(v ?? '');

const inputClass = "px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500 focus:border-blue-500";
const rangeInputClass = "w-16 px-1.5 py-0.5 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100";

export function ExpertSettingsForm({ expertClass, value, onChange, usesRiskManager = true }:
  { expertClass: string; value: ExpertSettingsValue; onChange: (v: ExpertSettingsValue) => void;
    // BYPASS experts (e.g. FactorRanker) ignore classic RM — pass false to hide the RM section.
    usesRiskManager?: boolean; }) {
  const [defs, setDefs] = useState<Record<string, SettingDef>>({});
  useEffect(() => {
    if (!expertClass) return;
    getExpertSettings(expertClass).then((d) => {
      setDefs(d);
      // seed defaults for any setting not yet set
      const settings = { ...value.settings };
      for (const [k, def] of Object.entries(d)) if (!HIDDEN_KEYS.has(k) && !(k in settings) && def.default !== undefined) settings[k] = def.default;
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
  // RM genes use the SAME mechanism as expert params but stamp optimize:true (the
  // optimizer merges them via the model:* namespace keyed by the real setting name).
  const setRmOpt = (k: string, on: boolean, range?: Partial<OptRange>, def?: { min: number; max: number; step: number }) => {
    const ep = { ...value.expert_params };
    if (on) {
      const base = ep[k] ?? { min: def?.min ?? 0, max: def?.max ?? 0, step: def?.step ?? 0 };
      ep[k] = {
        type: 'float',
        min: range?.min ?? base.min,
        max: range?.max ?? base.max,
        step: range?.step ?? base.step,
        optimize: true,
      };
    } else {
      delete ep[k];
    }
    onChange({ ...value, expert_params: ep });
  };

  return (
    <div className="space-y-2">
      {Object.entries(defs).filter(([k]) => !HIDDEN_KEYS.has(k)).map(([k, def]) => {
        if (SCHEDULE_KEYS.has(k)) {
          return (
            <div
              key={k}
              title={def.tooltip || def.description || ''}
              className="p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600"
            >
              <div className="text-sm text-gray-700 dark:text-gray-300 mb-2">{k}</div>
              <ScheduleEditor value={value.settings[k]} onChange={(v) => setVal(k, v)} />
            </div>
          );
        }
        const choices = (def.choices ?? def.valid_values) as unknown[] | undefined;
        const opt = value.expert_params[k];
        return (
          <div
            key={k}
            title={def.tooltip || def.description || ''}
            className="flex items-center justify-between gap-3 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600"
          >
            <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">{k}</span>
            <div>
              {def.type === 'bool' ? (
                <input type="checkbox" className="rounded" checked={!!value.settings[k]} onChange={(e) => setVal(k, e.target.checked)} />
              ) : choices ? (
                <select className={inputClass} value={String(value.settings[k] ?? '')} onChange={(e) => setVal(k, e.target.value)}>
                  {choices.map((c) => <option key={String(c)} value={String(c)}>{String(c)}</option>)}
                </select>
              ) : (
                <input className={inputClass} type={isNumeric(def.type) ? 'number' : 'text'}
                  value={displayVal(value.settings[k])}
                  onChange={(e) => setVal(k, isNumeric(def.type) ? Number(e.target.value) : e.target.value)} />
              )}
            </div>
            <div>
              {isNumeric(def.type) && (
                <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400">
                  <input type="checkbox" className="rounded" checked={!!opt}
                    onChange={(e) => setOpt(k, def.type, e.target.checked)} /> Opt
                  {opt && (<>
                    <input type="number" placeholder="min" value={opt.min} className={rangeInputClass}
                      onChange={(e) => setOpt(k, def.type, true, { ...opt, min: Number(e.target.value) })} />
                    <input type="number" placeholder="max" value={opt.max} className={rangeInputClass}
                      onChange={(e) => setOpt(k, def.type, true, { ...opt, max: Number(e.target.value) })} />
                    <input type="number" placeholder="step" value={opt.step} className={rangeInputClass}
                      onChange={(e) => setOpt(k, def.type, true, { ...opt, step: Number(e.target.value) })} />
                  </>)}
                </label>
              )}
            </div>
          </div>
        );
      })}

      {usesRiskManager && (
        <div className="pt-2 space-y-1">
          <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Risk Management</div>
          {RM_FIELDS.map(({ key, label, def }) => {
            const opt = value.expert_params[key];
            return (
              <div
                key={key}
                className="flex items-center justify-between gap-3 p-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600"
              >
                <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">{label}</span>
                <div>
                  <input className={inputClass} type="number"
                    value={displayVal(value.settings[key])}
                    onChange={(e) => setVal(key, Number(e.target.value))} />
                </div>
                <div>
                  <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400">
                    <input type="checkbox" className="rounded" checked={!!opt}
                      onChange={(e) => setRmOpt(key, e.target.checked, undefined, def)} /> Opt
                    {opt && (<>
                      <input type="number" placeholder="min" value={opt.min} className={rangeInputClass}
                        onChange={(e) => setRmOpt(key, true, { ...opt, min: Number(e.target.value) })} />
                      <input type="number" placeholder="max" value={opt.max} className={rangeInputClass}
                        onChange={(e) => setRmOpt(key, true, { ...opt, max: Number(e.target.value) })} />
                      <input type="number" placeholder="step" value={opt.step} className={rangeInputClass}
                        onChange={(e) => setRmOpt(key, true, { ...opt, step: Number(e.target.value) })} />
                    </>)}
                  </label>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
