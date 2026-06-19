import React, { useEffect, useRef, useState } from 'react';
import { createChart, ColorType, CandlestickSeries, createSeriesMarkers } from 'lightweight-charts';
import type { IChartApi, CandlestickData, Time, SeriesMarker } from 'lightweight-charts';
import { getOhlcvBars } from '../lib/btApi';

/** The subset of a backtest Trade this modal needs. */
export interface TradeLike {
  symbol?: string;
  entryDate: string;
  exitDate: string;
  entryPrice: number;
  exitPrice: number;
  direction: 'long' | 'short';
  pnl: number;
  pnlPercent: number;
  exitReason: string;
}

const dayOf = (iso?: string) => (iso || '').slice(0, 10); // ISO datetime -> 'YYYY-MM-DD'
const addDays = (iso: string, n: number) => {
  const d = new Date(dayOf(iso) + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};

/**
 * Click-through chart for a single backtest trade: daily candles around the trade window
 * with entry/exit markers. Uses the same lightweight-charts lib as the other chart pages.
 */
const TradeChartModal: React.FC<{ trade: TradeLike | null; onClose: () => void }> = ({ trade, onClose }) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bars, setBars] = useState<CandlestickData[]>([]);

  // Fetch daily bars (padded ~20 calendar days each side of the trade) when a trade is opened.
  useEffect(() => {
    if (!trade || !trade.symbol) { setBars([]); return; }
    let alive = true;
    setLoading(true); setError(null); setBars([]);
    const start = addDays(trade.entryDate, -20);
    const end = addDays(trade.exitDate || trade.entryDate, 20);
    getOhlcvBars(trade.symbol, start, end, '1d')
      .then(res => {
        if (!alive) return;
        setBars((res.bars || []).map(b => ({
          time: dayOf(b.Date) as Time, open: b.Open, high: b.High, low: b.Low, close: b.Close,
        })));
      })
      .catch(e => { if (alive) setError(String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [trade]);

  // Build/destroy the chart whenever bars change.
  useEffect(() => {
    if (!trade || !containerRef.current || bars.length === 0) return;
    const isDark = document.documentElement.classList.contains('dark');
    const chart: IChartApi = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 380,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: isDark ? '#cbd5e1' : '#334155' },
      grid: { vertLines: { color: isDark ? '#1f2937' : '#eef2f7' }, horzLines: { color: isDark ? '#1f2937' : '#eef2f7' } },
      timeScale: { borderColor: isDark ? '#334155' : '#cbd5e1' },
      rightPriceScale: { borderColor: isDark ? '#334155' : '#cbd5e1' },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#16a34a', downColor: '#dc2626', borderVisible: false,
      wickUpColor: '#16a34a', wickDownColor: '#dc2626',
    });
    series.setData(bars);
    const markers: SeriesMarker<Time>[] = [
      { time: dayOf(trade.entryDate) as Time, position: 'belowBar', color: '#2563eb',
        shape: 'arrowUp', text: `Entry $${trade.entryPrice.toFixed(2)}` },
    ];
    if (trade.exitDate) {
      markers.push({ time: dayOf(trade.exitDate) as Time, position: 'aboveBar',
        color: trade.pnl >= 0 ? '#16a34a' : '#dc2626', shape: 'arrowDown',
        text: `Exit $${trade.exitPrice.toFixed(2)}` });
    }
    createSeriesMarkers(series, markers);
    chart.timeScale().fitContent();
    const onResize = () => containerRef.current && chart.applyOptions({ width: containerRef.current.clientWidth });
    window.addEventListener('resize', onResize);
    return () => { window.removeEventListener('resize', onResize); chart.remove(); };
  }, [trade, bars]);

  if (!trade) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-4xl p-4" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-lg font-semibold text-gray-900 dark:text-gray-100">{trade.symbol || '—'} · daily</div>
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {trade.direction} · {dayOf(trade.entryDate)} → {dayOf(trade.exitDate)} ·{' '}
              <span className={trade.pnl >= 0 ? 'text-green-600' : 'text-red-600'}>
                {trade.pnl >= 0 ? '+' : ''}{trade.pnlPercent.toFixed(2)}%
              </span> · {trade.exitReason}
            </div>
          </div>
          <button onClick={onClose}
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-2xl leading-none px-2">×</button>
        </div>
        {!trade.symbol
          ? <div className="h-[380px] flex items-center justify-center text-sm text-gray-500">No symbol recorded on this trade (re-run a backtest to populate symbols).</div>
          : loading
            ? <div className="h-[380px] flex items-center justify-center text-sm text-gray-500">Loading daily bars…</div>
            : error
              ? <div className="h-[380px] flex items-center justify-center text-sm text-red-500">Could not load bars: {error}</div>
              : bars.length === 0
                ? <div className="h-[380px] flex items-center justify-center text-sm text-gray-500">No bars in range.</div>
                : <div ref={containerRef} />}
      </div>
    </div>
  );
};

export default TradeChartModal;
