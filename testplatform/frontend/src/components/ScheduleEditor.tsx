// frontend/src/components/ScheduleEditor.tsx
// Edits an execution-schedule object: { days: {monday..sunday: bool}, times: ["HH:MM", ...] }.
// Mirrors the trade-platform schedule controls. The daily engine's _schedule_allows_entry
// honours `days` on every clock and `times` only on an intraday clock.

export interface Schedule { days: Record<string, boolean>; times: string[]; }

const DAYS: { key: string; label: string }[] = [
  { key: 'monday', label: 'Mon' }, { key: 'tuesday', label: 'Tue' },
  { key: 'wednesday', label: 'Wed' }, { key: 'thursday', label: 'Thu' },
  { key: 'friday', label: 'Fri' }, { key: 'saturday', label: 'Sat' },
  { key: 'sunday', label: 'Sun' },
];

const DEFAULT_SCHEDULE: Schedule = {
  days: { monday: true, tuesday: true, wednesday: true, thursday: true, friday: true, saturday: false, sunday: false },
  times: ['09:30'],
};

export function ScheduleEditor({ value, onChange }:
  { value: unknown; onChange: (v: Schedule) => void; }) {
  const sched: Schedule = (value && typeof value === 'object' && 'days' in (value as Record<string, unknown>))
    ? (value as Schedule)
    : DEFAULT_SCHEDULE;
  const days = sched.days || {};
  const times = sched.times || [];

  const setDay = (k: string, on: boolean) => onChange({ ...sched, days: { ...days, [k]: on } });
  const setTime = (i: number, t: string) => onChange({ ...sched, times: times.map((x, j) => (j === i ? t : x)) });
  const addTime = () => onChange({ ...sched, times: [...times, '09:30'] });
  const rmTime = (i: number) => onChange({ ...sched, times: times.filter((_, j) => j !== i) });

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {DAYS.map((d) => (
          <label key={d.key} className="flex items-center gap-1 text-xs text-gray-700 dark:text-gray-300">
            <input type="checkbox" className="rounded" checked={!!days[d.key]} onChange={(e) => setDay(d.key, e.target.checked)} />
            {d.label}
          </label>
        ))}
      </div>
      <div className="space-y-1">
        <div className="text-xs text-gray-500 dark:text-gray-400">Run times (HH:MM — applied only on intraday intervals)</div>
        {times.map((t, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              type="time"
              value={t}
              onChange={(e) => setTime(i, e.target.value)}
              className="px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
            <button
              type="button"
              onClick={() => rmTime(i)}
              className="px-2 py-1 text-xs border border-red-300 dark:border-red-700 rounded text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
            >
              remove
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={addTime}
          className="px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-50 dark:hover:bg-gray-700"
        >
          + add time
        </button>
      </div>
    </div>
  );
}
