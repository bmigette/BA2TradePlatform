import React, { useEffect, useRef, useState } from 'react';
import {
  createChart,
  ColorType,
  CandlestickSeries,
  createSeriesMarkers,
} from 'lightweight-charts';
import type {
  IChartApi,
  CandlestickData,
  Time,
  SeriesMarker,
} from 'lightweight-charts';

export interface PredictionPoint {
  date: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close: number | null;
  probability: number;
  predictedClass: number;
  actual: number | null;
  correct: boolean | null;
}

export interface PredictionsChartProps {
  predictions: PredictionPoint[];
  height?: number;
  showOnlyTransitions?: boolean; // Only show markers where prediction changes or at signal points
  showOnlyClass?: number | null; // Only show markers for this predicted class (null = show all)
  minProbability?: number; // Minimum probability to show marker (0-1)
  showActualTargets?: boolean; // Show markers for actual target values (ground truth from dataset)
  showAllActualTargets?: boolean; // Show all actual targets vs only transitions (default: true = show all)
}

const PredictionsChart: React.FC<PredictionsChartProps> = ({
  predictions,
  height = 500,
  showOnlyTransitions = true,
  showOnlyClass = 1, // Default to only showing "up" predictions (class 1)
  minProbability = 0, // Default: show all predictions
  showActualTargets = false, // Default: only show model predictions
  showAllActualTargets = true, // Default: show all actual targets (not just transitions)
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
    if (!chartContainerRef.current || predictions.length === 0) return;

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
          scaleMargins: { top: 0.05, bottom: 0.05 },
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

      // Filter predictions with valid close values and set candlestick data
      const validPredictions = predictions.filter((p) => p.close !== null);
      const candlestickData: CandlestickData[] = validPredictions.map((p) => ({
        time: toTime(p.date),
        open: p.open ?? p.close!,
        high: p.high ?? p.close!,
        low: p.low ?? p.close!,
        close: p.close!,
      }));
      candlestickSeries.setData(candlestickData);

      // Create a Set of valid chart dates
      const validChartDates = new Set(candlestickData.map(d => d.time as number));

      // Build prediction markers
      const markers: SeriesMarker<Time>[] = [];

      predictions.forEach((p, idx) => {
        const time = toTime(p.date);
        if (!validChartDates.has(time as number)) return;

        // === ACTUAL TARGET MARKERS (Ground truth from dataset) ===
        if (showActualTargets && p.actual === 1) {
          // Check if we should show all targets or only transitions
          const prevActual = idx > 0 ? predictions[idx - 1].actual : 0;
          const isTransition = prevActual !== 1;

          if (showAllActualTargets || isTransition) {
            // Show a marker where actual target was 1 (the event occurred)
            markers.push({
              time,
              position: 'aboveBar',
              color: '#3B82F6', // Blue for actual targets
              shape: 'circle',
              text: isTransition ? 'T' : '', // Only show 'T' text on transitions to reduce clutter
            });
          }
        }

        // === MODEL PREDICTION MARKERS ===
        // Skip predictions with null correctness (actual not known yet)
        if (p.correct === null) return;

        // Filter by minimum probability
        if (p.probability < minProbability) return;

        // For transitions mode, only show when prediction changes
        if (showOnlyTransitions && idx > 0) {
          const prevPredicted = predictions[idx - 1].predictedClass;
          if (p.predictedClass === prevPredicted) return;
        }

        // Filter by class if specified
        if (showOnlyClass !== null && p.predictedClass !== showOnlyClass) return;

        // Determine marker properties based on prediction correctness
        const isUpPrediction = p.predictedClass === 1;
        const isCorrect = p.correct;

        // Color: Green for correct, Red for incorrect
        const color = isCorrect ? '#10B981' : '#EF4444';

        // Shape: Arrow up for up prediction, arrow down for down prediction
        const shape = isUpPrediction ? 'arrowUp' : 'arrowDown';
        const position = isUpPrediction ? 'belowBar' : 'aboveBar';

        // Text shows probability
        const probText = `${(p.probability * 100).toFixed(0)}%`;

        markers.push({
          time,
          position,
          color,
          shape,
          text: probText,
        });
      });

      // Sort markers by time and add to chart
      if (markers.length > 0) {
        markers.sort((a, b) => (a.time as number) - (b.time as number));
        createSeriesMarkers(candlestickSeries, markers);
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
      console.error('PredictionsChart error:', err);
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [predictions, height, showOnlyTransitions, showOnlyClass, minProbability, showActualTargets, showAllActualTargets]);

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

  if (predictions.length === 0) {
    return (
      <div
        className="bg-gray-800 flex items-center justify-center text-gray-500"
        style={{ width: '100%', height: `${height}px` }}
      >
        No prediction data available
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

export default PredictionsChart;
