import React, { useEffect, useRef, useState } from 'react';
import {
  createChart,
  ColorType,
  CandlestickSeries,
  LineSeries,
  createSeriesMarkers,
} from 'lightweight-charts';
import type {
  IChartApi,
  CandlestickData,
  Time,
  SeriesMarker,
} from 'lightweight-charts';

export interface OHLCData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  signal?: number;
}

export interface Trade {
  id: string | number;
  entryDate: string;
  exitDate: string;
  entryPrice: number;
  exitPrice: number;
  direction: 'long' | 'short';
  size: number;
  pnl: number;
  pnlPercent: number;
  duration: number;
  exitReason: string;
}

export interface BacktestChartProps {
  priceData: OHLCData[];
  trades: Trade[];
  height?: number;
  showEquityCurve?: boolean;
  equityData?: { date: string; equity: number }[];
}

const BacktestChart: React.FC<BacktestChartProps> = ({
  priceData,
  trades,
  height = 400,
  showEquityCurve = false,
  equityData = [],
}) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Convert date string to Time format
  const toTime = (dateStr: string): Time => {
    const date = new Date(dateStr);
    return Math.floor(date.getTime() / 1000) as Time;
  };

  useEffect(() => {
    if (!chartContainerRef.current || priceData.length === 0) return;

    try {
      setError(null);

      // Clean up previous chart
      if (chartRef.current) {
        chartRef.current.remove();
        chartRef.current = null;
      }

      const chart = createChart(chartContainerRef.current, {
        layout: {
          background: { type: ColorType.Solid, color: '#1F2937' },
          textColor: '#9CA3AF',
        },
        grid: {
          vertLines: { color: '#374151' },
          horzLines: { color: '#374151' },
        },
        width: chartContainerRef.current.clientWidth,
        height: height,
        crosshair: {
          mode: 1,
        },
        rightPriceScale: {
          borderColor: '#374151',
          scaleMargins: { top: 0.05, bottom: showEquityCurve ? 0.25 : 0.05 },
        },
        timeScale: {
          borderColor: '#374151',
          timeVisible: true,
          secondsVisible: false,
        },
      });

      chartRef.current = chart;

      // Create candlestick series
      const candlestickSeries = chart.addSeries(CandlestickSeries, {
        upColor: '#10B981',
        downColor: '#EF4444',
        borderUpColor: '#10B981',
        borderDownColor: '#EF4444',
        wickUpColor: '#10B981',
        wickDownColor: '#EF4444',
        lastValueVisible: false,
        priceLineVisible: false,
      });

      // Set candlestick data
      const candlestickData: CandlestickData[] = priceData.map((d) => ({
        time: toTime(d.date),
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      }));
      candlestickSeries.setData(candlestickData);

      // Create a Set of valid chart dates for filtering
      const validChartDates = new Set(candlestickData.map(d => d.time as number));

      // Build trade markers
      const markers: SeriesMarker<Time>[] = [];

      trades.forEach((trade) => {
        const entryTime = toTime(trade.entryDate);
        const exitTime = toTime(trade.exitDate);
        const isProfitable = trade.pnl >= 0;
        const isLong = trade.direction === 'long';

        // Entry marker
        if (validChartDates.has(entryTime as number)) {
          markers.push({
            time: entryTime,
            position: isLong ? 'belowBar' : 'aboveBar',
            color: isProfitable ? '#10B981' : '#EF4444', // Green for profitable, red for losing
            shape: isLong ? 'arrowUp' : 'arrowDown',
            text: `${isLong ? 'BUY' : 'SELL'} $${trade.entryPrice.toFixed(2)}`,
          });
        }

        // Exit marker
        if (validChartDates.has(exitTime as number)) {
          markers.push({
            time: exitTime,
            position: isLong ? 'aboveBar' : 'belowBar',
            color: isProfitable ? '#10B981' : '#EF4444',
            shape: 'circle',
            text: `EXIT ${trade.pnl >= 0 ? '+' : ''}$${trade.pnl.toFixed(2)}`,
          });
        }
      });

      // Sort markers by time and add to chart
      if (markers.length > 0) {
        markers.sort((a, b) => (a.time as number) - (b.time as number));
        createSeriesMarkers(candlestickSeries, markers);
      }

      // Add equity curve if enabled
      if (showEquityCurve && equityData.length > 0) {
        const equitySeries = chart.addSeries(LineSeries, {
          color: '#3B82F6',
          lineWidth: 2,
          priceScaleId: 'equity',
          lastValueVisible: true,
          priceLineVisible: false,
        });

        chart.priceScale('equity').applyOptions({
          scaleMargins: { top: 0.8, bottom: 0.02 },
          borderVisible: false,
        });

        const equityLineData = equityData.map(d => ({
          time: toTime(d.date),
          value: d.equity,
        }));
        equitySeries.setData(equityLineData);
      }

      // Fit content
      chart.timeScale().fitContent();

      // Handle resize
      const handleResize = () => {
        if (chartContainerRef.current && chartRef.current) {
          chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth });
        }
      };
      window.addEventListener('resize', handleResize);

      return () => {
        window.removeEventListener('resize', handleResize);
        if (chartRef.current) {
          chartRef.current.remove();
          chartRef.current = null;
        }
      };
    } catch (err) {
      console.error('BacktestChart error:', err);
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [priceData, trades, height, showEquityCurve, equityData]);

  if (error) {
    return (
      <div
        className="bg-red-900/20 border border-red-500 rounded p-4 text-red-400"
        style={{ width: '100%', height: `${height}px` }}
      >
        <p className="font-bold">Chart Error:</p>
        <p className="font-mono text-sm">{error}</p>
      </div>
    );
  }

  if (priceData.length === 0) {
    return (
      <div
        className="bg-gray-800 flex items-center justify-center text-gray-500"
        style={{ width: '100%', height: `${height}px` }}
      >
        No price data available
      </div>
    );
  }

  return (
    <div
      ref={chartContainerRef}
      style={{ width: '100%', height: `${height}px` }}
    />
  );
};

export default BacktestChart;
