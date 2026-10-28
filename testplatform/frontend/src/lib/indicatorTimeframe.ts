/**
 * An indicator as the dataset wizard computes it: on the dataset's own bars.
 *
 * A per-indicator timeframe only renamed the column; the backend never resampled. Indicators that
 * arrive with their own timeframe (saved collections, an existing dataset being edited or
 * duplicated) are pinned to the dataset timeframe so the column name matches the computation. For
 * a real higher-timeframe indicator see services/indicators.py calculate_indicators_multi_timeframe.
 */
export function onDatasetTimeframe<T extends { timeframe?: string }>(indicator: T, datasetTimeframe: string): T {
  return { ...indicator, timeframe: datasetTimeframe };
}
