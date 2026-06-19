import { useEffect, useState } from 'react';
import { getExitPresets } from '../lib/btApi';
import type { ExitPreset } from '../lib/btApi';

export function ExitPresetPicker({ onAdd }: { onAdd: (p: ExitPreset) => void }) {
  const [presets, setPresets] = useState<ExitPreset[]>([]);
  useEffect(() => {
    getExitPresets().then(setPresets).catch(() => setPresets([]));
  }, []);
  if (!presets.length) return null;
  return (
    <select
      className="px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
      value=""
      onChange={(e) => {
        const p = presets.find((x) => x.key === e.target.value);
        if (p) onAdd(p);
        e.currentTarget.value = '';
      }}
    >
      <option value="">+ Add preset…</option>
      {presets.map((p) => (
        <option key={p.key} value={p.key}>
          {p.label}
        </option>
      ))}
    </select>
  );
}
