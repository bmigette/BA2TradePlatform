import { useEffect, useState } from 'react';
import { summariseRun, runLabels, cardPosition } from '../lib/runSummary';
import type { RunRow } from '../lib/runSummary';

/** Card size used for edge-flipping. A max-height keeps a long card on screen; see cardPosition. */
const CARD_W = 340;
const CARD_H = 300;

/**
 * The run's full name and everything the table has no column for, INSTANTLY on hover.
 *
 * WHY NOT ``title=""``. The name cell carried a native tooltip, which the browser withholds for
 * about a second and then renders as unstyled plain text -- long enough that reading a truncated
 * name meant waiting, per run, down a table of hundreds. This opens on ``mouseenter`` with no
 * timer at all.
 *
 * NO NETWORK, by construction: it renders the row object the table already holds. Everything it
 * shows came down with the list response, so hovering a hundred rows costs a hundred re-renders
 * and zero requests.
 *
 * ``pointer-events-none`` is load-bearing -- the card follows the cursor across a row whose
 * buttons (Load / Export / Delete) must stay clickable, and a card that could swallow a click on
 * Delete would be worse than no card.
 */
export function RunHoverCard({ row, x, y }: { row: RunRow | null; x: number; y: number }) {
  const [viewport, setViewport] = useState({ w: 1280, h: 800 });

  useEffect(() => {
    const read = () => setViewport({ w: window.innerWidth, h: window.innerHeight });
    read();
    window.addEventListener('resize', read);
    return () => window.removeEventListener('resize', read);
  }, []);

  if (!row) return null;
  const fields = summariseRun(row);
  const labels = runLabels(row);
  const { left, top } = cardPosition(x, y, CARD_W, CARD_H, viewport.w, viewport.h);
  const name = row.name ?? '';
  const description = row.description ?? '';

  return (
    <div
      data-testid="run-hover-card"
      className="fixed z-50 pointer-events-none rounded-lg shadow-xl border
                 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-3"
      style={{ left, top, width: CARD_W, maxHeight: CARD_H, overflow: 'hidden' }}
    >
      <div className="text-sm font-medium text-gray-900 dark:text-gray-100 break-words">
        {name || `Run #${row.id}`}
      </div>
      {description ? (
        <div className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 break-words line-clamp-2">
          {description}
        </div>
      ) : null}

      {labels.length ? (
        <div className="mt-1.5 flex flex-wrap gap-0.5">
          {labels.map(l => (
            <span
              key={l}
              className="px-1.5 py-0.5 text-[10px] rounded-full border border-blue-300
                         dark:border-blue-700 bg-blue-50 dark:bg-blue-900/20 text-blue-700
                         dark:text-blue-300 whitespace-nowrap"
            >
              {l}
            </span>
          ))}
        </div>
      ) : null}

      {fields.length ? (
        <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs">
          {fields.map(f => (
            <div key={f.label} className="contents">
              <dt className="text-gray-500 dark:text-gray-400 truncate">{f.label}</dt>
              <dd className="text-gray-900 dark:text-gray-100 text-right truncate">{f.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
