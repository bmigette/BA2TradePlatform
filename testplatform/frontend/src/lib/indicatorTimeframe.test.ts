import { describe, it, expect } from 'vitest';
import { onDatasetTimeframe } from './indicatorTimeframe';

/**
 * A per-indicator timeframe only renamed the column ("RSI 14 @ 4H"): the backend computes every
 * indicator on the dataset's own bars. Indicators that arrive from a saved collection or an
 * existing dataset must therefore carry the dataset timeframe, or the name lies about the maths.
 */
describe('onDatasetTimeframe', () => {
  it('replaces a collection indicator\'s own timeframe with the dataset timeframe', () => {
    const rsi = { id: 'a', type: 'rsi', name: 'RSI', period: 14, timeframe: '4h' };
    expect(onDatasetTimeframe(rsi, '1h')).toEqual({ ...rsi, timeframe: '1h' });
  });

  it('fills a missing timeframe', () => {
    expect(onDatasetTimeframe({ id: 'b', type: 'sma' } as { id: string; type: string; timeframe?: string }, '1d').timeframe).toBe('1d');
  });

  it('keeps every other field and does not mutate the input', () => {
    const macd = { id: 'c', type: 'macd', fast: 12, slow: 26, signal: 9, timeframe: '1w' };
    const out = onDatasetTimeframe(macd, '1d');
    expect(out).toEqual({ ...macd, timeframe: '1d' });
    expect(macd.timeframe).toBe('1w');
  });
});
