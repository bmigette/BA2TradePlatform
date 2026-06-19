import React, { useState } from 'react';
import { RefreshCw, X } from 'lucide-react';

export interface RegenOptions {
  regenerate_ohlcv: boolean;
  regenerate_technical: boolean;
  regenerate_sentiment: boolean;
  regenerate_fundamentals: boolean;
  regenerate_macro: boolean;
}

const DEFAULT_OPTIONS: RegenOptions = {
  regenerate_ohlcv: true,
  regenerate_technical: true,
  regenerate_sentiment: true,
  regenerate_fundamentals: true,
  regenerate_macro: true,
};

interface RegenerateDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: (options: RegenOptions) => void;
  title?: string;
  description?: string;
  confirmLabel?: string;
}

const RegenerateDialog: React.FC<RegenerateDialogProps> = ({
  isOpen,
  onClose,
  onConfirm,
  title = 'Regenerate Dataset',
  description,
  confirmLabel = 'Regenerate',
}) => {
  const [options, setOptions] = useState<RegenOptions>(DEFAULT_OPTIONS);

  if (!isOpen) return null;

  const setAll = (value: boolean) =>
    setOptions({
      regenerate_ohlcv: value,
      regenerate_technical: value,
      regenerate_sentiment: value,
      regenerate_fundamentals: value,
      regenerate_macro: value,
    });

  const noneSelected = !Object.values(options).some(Boolean);

  const handleConfirm = () => {
    onConfirm(options);
    setOptions(DEFAULT_OPTIONS);
  };

  const handleClose = () => {
    onClose();
    setOptions(DEFAULT_OPTIONS);
  };

  const toggle = (key: keyof RegenOptions) =>
    setOptions((prev) => ({ ...prev, [key]: !prev[key] }));

  const ITEMS: { key: keyof RegenOptions; label: string; sub: string }[] = [
    { key: 'regenerate_ohlcv', label: 'OHLCV Data', sub: 'Re-fetch price data from provider' },
    { key: 'regenerate_technical', label: 'Technical Indicators', sub: 'Recalculate SMA, RSI, MACD, etc.' },
    { key: 'regenerate_sentiment', label: 'News & Sentiment', sub: 'Re-fetch and analyze news articles' },
    { key: 'regenerate_fundamentals', label: 'Fundamentals', sub: 'Re-fetch financial statements & earnings' },
    { key: 'regenerate_macro', label: 'Macro Economic Data', sub: 'Re-fetch GDP, CPI, interest rates, etc.' },
  ];

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md m-4">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-xl font-bold flex items-center gap-2">
            <RefreshCw size={20} className="text-orange-500" />
            {title}
          </h2>
          <button
            onClick={handleClose}
            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-full"
          >
            <X size={20} className="text-gray-500" />
          </button>
        </div>

        {/* Content */}
        <div className="p-4">
          <p className="text-sm text-gray-600 dark:text-gray-400 mb-1">
            Select which components to regenerate. Unselected components will be preserved from the existing dataset.
          </p>
          {description && (
            <p className="text-xs text-orange-600 dark:text-orange-400 font-medium mb-3">{description}</p>
          )}

          <div className="space-y-3 mt-3">
            {ITEMS.map(({ key, label, sub }) => (
              <label
                key={key}
                className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-700 rounded-lg cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-600"
              >
                <input
                  type="checkbox"
                  checked={options[key]}
                  onChange={() => toggle(key)}
                  className="w-4 h-4 text-orange-600 rounded focus:ring-orange-500"
                />
                <div>
                  <span className="font-medium text-gray-900 dark:text-gray-100">{label}</span>
                  <p className="text-xs text-gray-500">{sub}</p>
                </div>
              </label>
            ))}
          </div>

          {/* Quick actions */}
          <div className="flex gap-2 mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
            <button
              onClick={() => setAll(true)}
              className="text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400"
            >
              Select All
            </button>
            <span className="text-gray-300 dark:text-gray-600">|</span>
            <button
              onClick={() =>
                setOptions({
                  regenerate_ohlcv: false,
                  regenerate_technical: true,
                  regenerate_sentiment: false,
                  regenerate_fundamentals: false,
                  regenerate_macro: true,
                })
              }
              className="text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400"
            >
              TA & Macro Only
            </button>
            <span className="text-gray-300 dark:text-gray-600">|</span>
            <button
              onClick={() => setAll(false)}
              className="text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400"
            >
              Clear All
            </button>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 p-4 border-t border-gray-200 dark:border-gray-700">
          <button
            onClick={handleClose}
            className="px-4 py-2 text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={noneSelected}
            className="px-4 py-2 bg-orange-600 text-white rounded-md hover:bg-orange-700 disabled:opacity-50 flex items-center gap-2"
          >
            <RefreshCw size={16} />
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
};

export default RegenerateDialog;
