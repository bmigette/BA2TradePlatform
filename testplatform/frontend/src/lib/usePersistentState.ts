import { useEffect, useState } from 'react';

/**
 * useState whose value persists for the browser SESSION (sessionStorage) — survives page
 * reloads and in-app tab switches, resets when the tab is closed. Used to keep table filters,
 * search text and sort order sticky across reloads (see RunHistoryTable / OptimizationJobsTable /
 * Models). Key must be unique per logical control; corrupt/missing entries fall back to `initial`.
 */
export function usePersistentState<T>(
  key: string,
  initial: T,
): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(key);
      return raw != null ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });

  useEffect(() => {
    try {
      sessionStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* private mode / quota — persistence is best-effort */
    }
  }, [key, value]);

  return [value, setValue];
}
