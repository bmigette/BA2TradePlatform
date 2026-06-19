import { useState, type ReactNode } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';

/**
 * A foldable section: a clickable header (chevron + uppercase title, optional icon + right-aligned
 * slot) over collapsible content. Used to tame the long New-Backtest form (Expert Settings,
 * Universe, Strategy Conditions, Execution, …). Local open/closed state; defaults to open so the
 * form looks unchanged until the user folds a section.
 */
export function CollapsibleSection({
  title,
  icon,
  right,
  defaultOpen = true,
  children,
}: {
  title: string;
  icon?: ReactNode;
  right?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left bg-gray-50 dark:bg-gray-800/50 hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors"
      >
        {open ? (
          <ChevronDown className="w-4 h-4 text-gray-500 flex-shrink-0" />
        ) : (
          <ChevronRight className="w-4 h-4 text-gray-500 flex-shrink-0" />
        )}
        {icon}
        <span className="flex-1 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
          {title}
        </span>
        {right && <span onClick={(e) => e.stopPropagation()}>{right}</span>}
      </button>
      {open && <div className="p-3">{children}</div>}
    </div>
  );
}
