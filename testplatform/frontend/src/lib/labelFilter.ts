/**
 * Label matching for the run-history filters.
 *
 * A run's `labels` array is set by the grid drivers (e.g. tools/run_screener_capband_matrix.py)
 * and mixes two kinds of tag: a batch id like "goal2020-notional" and a strategy tag like "S4".
 * Those two readings want opposite operators, which is why the mode is a user toggle rather
 * than a fixed rule:
 *
 *   ALL ("goal2020-notional" + "S4")  -> exactly that job's S4 cells, across every cap band.
 *   ANY ("S1" + "S2" + "S3")          -> every run from any of those strategies.
 *
 * ALL stays the default: it is the narrowing operator, and a filter that silently widens the
 * result set is the one that misleads.
 */
export type LabelMatchMode = 'all' | 'any';

export function matchesLabels(
  rowLabels: unknown,
  selected: readonly string[],
  mode: LabelMatchMode,
): boolean {
  // No selection is not "match nothing" -- it means the label filter is off entirely.
  if (selected.length === 0) return true;
  const labels = Array.isArray(rowLabels) ? rowLabels : [];
  return mode === 'any'
    ? selected.some(l => labels.includes(l))
    : selected.every(l => labels.includes(l));
}
