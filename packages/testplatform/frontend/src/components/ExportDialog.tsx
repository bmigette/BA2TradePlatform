import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import type { ExportKind } from '../lib/btApi';

/**
 * Modal that asks WHAT to export from a backtest (expert settings vs. conditions ruleset)
 * via radio buttons, then hands the chosen ExportKind back to the caller (which performs the
 * read-only fetch + browser download). Styled like the app's ConfirmDialog: full-screen
 * overlay (bg-black/50) + a centered dark-mode card (bg-white dark:bg-gray-800 rounded-lg
 * shadow-xl). Closes on Export, Cancel, overlay click, or Esc.
 */
export function ExportDialog({
  isOpen,
  backtestId,
  backtestName,
  onExport,
  onClose,
}: {
  isOpen: boolean;
  backtestId: number;
  backtestName?: string | null;
  onExport: (kind: ExportKind) => void;
  onClose: () => void;
}) {
  const [kind, setKind] = useState<ExportKind>('expert_settings');

  // Reset to the default choice each time the dialog opens for a (possibly different) run.
  useEffect(() => {
    if (isOpen) setKind('expert_settings');
  }, [isOpen, backtestId]);

  // Esc closes the dialog (matches overlay-click / Cancel behavior).
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const options: { value: ExportKind; label: string }[] = [
    { value: 'expert_settings', label: 'Expert settings' },
    { value: 'ruleset', label: 'Conditions ruleset' },
  ];

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/50 transition-opacity"
        onClick={onClose}
      />

      {/* Dialog */}
      <div className="flex min-h-full items-center justify-center p-4">
        <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
          {/* Close button */}
          <button
            type="button"
            onClick={onClose}
            className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <X size={20} />
          </button>

          {/* Content */}
          <div className="mb-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3">
              Export backtest #{backtestId} ({backtestName || 'unnamed'})
            </h3>
            <div className="space-y-2">
              {options.map(opt => (
                <label
                  key={opt.value}
                  className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer"
                >
                  <input
                    type="radio"
                    name="export-kind"
                    value={opt.value}
                    checked={kind === opt.value}
                    onChange={() => setKind(opt.value)}
                    className="text-blue-600 focus:ring-blue-500"
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </div>

          {/* Actions */}
          <div className="flex justify-end space-x-3">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => onExport(kind)}
              className="px-4 py-2 rounded-md bg-blue-600 hover:bg-blue-700 text-white"
            >
              Export
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default ExportDialog;
