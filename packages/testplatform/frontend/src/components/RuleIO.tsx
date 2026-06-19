import { useRef } from 'react';
import { importRules } from '../lib/btApi';
import type { ConditionTree } from './ConditionBuilder';

const buttonClass = "px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700 flex items-center gap-1";

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
    // Export uses the in-memory tree. When a strategy is saved, prefer the server export
    // endpoint (exportRulesUrl) for canonical v1.1 output; this direct-blob path covers the
    // unsaved-tree case (the server normalises on import).
    const blob = new Blob(
      [JSON.stringify({ export_version: '1.1', export_type: 'condition_tree', which, tree }, null, 2)],
      { type: 'application/json' },
    );
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `rules-${which}.json`;
    a.click();
  };
  return (
    <div className="flex gap-2">
      <button type="button" onClick={() => fileRef.current?.click()} className={buttonClass}>Import JSON</button>
      <button type="button" onClick={doExport} className={buttonClass}>Export JSON</button>
      <input ref={fileRef} type="file" accept=".json,application/json" className="hidden"
        onChange={(e) => doImport(e.target.files?.[0])} />
    </div>
  );
}
