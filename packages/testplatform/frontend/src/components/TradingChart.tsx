import React, { useEffect, useRef, useCallback, useState } from 'react';
import {
  createChart,
  ColorType,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  createSeriesMarkers,
} from 'lightweight-charts';
import type {
  IChartApi,
  ISeriesApi,
  CandlestickData,
  LineData,
  HistogramData,
  Time,
  SeriesMarker,
} from 'lightweight-charts';
import type { CalculatedTarget } from '../types/targets';

export interface OHLCData {
  Date: string;
  Open: number;
  High: number;
  Low: number;
  Close: number;
  Volume?: number;
  [key: string]: any;
}

export interface NewsFrequency {
  date: string;
  count: number;
  positiveCount?: number;
  negativeCount?: number;
  neutralCount?: number;
  dominantSentiment: 'positive' | 'negative' | 'neutral';
}

export interface TrendPoint {
  date: string;
  trend: string;
}

export interface PredictionTarget {
  Date: string;
  [key: string]: any;
}

export interface IndicatorData {
  rsi?: { date: string; value: number | null }[];
  macd?: { date: string; macd: number | null; signal: number | null; histogram: number | null }[];
  sar?: { date: string; value: number | null }[];
  zigzag?: { date: string; value: number | null }[];
}

export interface TradingChartProps {
  data: OHLCData[];
  indicators: {
    showSMA20: boolean;
    showSMA50: boolean;
    showBollinger: boolean;
    showVolume: boolean;
    showSentiment: boolean;
    showTargets: boolean;
    showTrends: boolean;
  };
  newsFrequencyByDate?: NewsFrequency[];
  trendData?: TrendPoint[];
  predictionPreview?: {
    target_columns: string[];
    target_data: PredictionTarget[];
  } | null;
  calculatedTargets?: CalculatedTarget[];
  indicatorData?: IndicatorData;
  enabledIndicators?: Set<string>;
  height?: number;
  showAllTargets?: boolean; // Show all target markers vs only transitions (default: false = transitions only)
}

const DYNAMIC_INDICATOR_COLORS = [
  '#3B82F6', '#F97316', '#10B981', '#8B5CF6', '#EC4899',
  '#06B6D4', '#F59E0B', '#6366F1', '#84CC16', '#EF4444',
];

const TradingChart: React.FC<TradingChartProps> = ({
  data,
  indicators,
  newsFrequencyByDate = [],
  trendData = [],
  calculatedTargets = [],
  indicatorData,
  enabledIndicators,
  height = 500,
  showAllTargets = false, // Default: show only transitions (existing behavior)
}) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlestickSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Convert date string to Time format
  const toTime = useCallback((dateStr: string): Time => {
    const date = new Date(dateStr);
    return Math.floor(date.getTime() / 1000) as Time;
  }, []);

  // Calculate SMA
  const calculateSMA = useCallback((sourceData: OHLCData[], period: number): LineData[] => {
    const result: LineData[] = [];
    for (let i = period - 1; i < sourceData.length; i++) {
      let sum = 0;
      for (let j = 0; j < period; j++) {
        sum += sourceData[i - j].Close;
      }
      result.push({
        time: toTime(sourceData[i].Date),
        value: sum / period,
      });
    }
    return result;
  }, [toTime]);

  // Calculate Bollinger Bands
  const calculateBollinger = useCallback((sourceData: OHLCData[], period: number = 20, stdDev: number = 2): {
    upper: LineData[];
    middle: LineData[];
    lower: LineData[];
  } => {
    const upper: LineData[] = [];
    const middle: LineData[] = [];
    const lower: LineData[] = [];

    for (let i = period - 1; i < sourceData.length; i++) {
      let sum = 0;
      for (let j = 0; j < period; j++) {
        sum += sourceData[i - j].Close;
      }
      const sma = sum / period;

      let squaredSum = 0;
      for (let j = 0; j < period; j++) {
        squaredSum += Math.pow(sourceData[i - j].Close - sma, 2);
      }
      const std = Math.sqrt(squaredSum / period);

      const time = toTime(sourceData[i].Date);
      middle.push({ time, value: sma });
      upper.push({ time, value: sma + stdDev * std });
      lower.push({ time, value: sma - stdDev * std });
    }

    return { upper, middle, lower };
  }, [toTime]);

  // Initialize and update chart
  useEffect(() => {
    if (!chartContainerRef.current || data.length === 0) return;

    try {
      setError(null);

      // Clean up previous chart
      if (chartRef.current) {
        chartRef.current.remove();
        chartRef.current = null;
      }

      // Calculate dynamic pane layout based on which indicators are enabled
      // Each pane gets a percentage of chart height
      const VOLUME_HEIGHT = 0.08;  // 8% for volume
      const RSI_HEIGHT = indicatorData?.rsi && indicatorData.rsi.length > 0 ? 0.12 : 0;
      const MACD_HEIGHT = indicatorData?.macd && indicatorData.macd.length > 0 ? 0.12 : 0;
      const volatilityTargets = calculatedTargets.filter(
        t => t.visible && t.config.category === 'regression' && t.data && t.data.length > 0
      );
      const VOLATILITY_HEIGHT = volatilityTargets.length > 0 ? 0.12 : 0;

      // Calculate positions from bottom up
      const volumeBottom = 0;
      const volumeTop = 1 - VOLUME_HEIGHT;

      const volatilityBottom = VOLUME_HEIGHT;
      const volatilityTop = 1 - VOLATILITY_HEIGHT - VOLUME_HEIGHT;

      const macdBottom = VOLUME_HEIGHT + VOLATILITY_HEIGHT;
      const macdTop = 1 - MACD_HEIGHT - VOLATILITY_HEIGHT - VOLUME_HEIGHT;

      const rsiBottom = VOLUME_HEIGHT + VOLATILITY_HEIGHT + MACD_HEIGHT;
      const rsiTop = 1 - RSI_HEIGHT - MACD_HEIGHT - VOLATILITY_HEIGHT - VOLUME_HEIGHT;

      // Main chart gets the remaining space at top
      const mainChartBottom = RSI_HEIGHT + MACD_HEIGHT + VOLATILITY_HEIGHT + VOLUME_HEIGHT;

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
          scaleMargins: { top: 0.02, bottom: mainChartBottom + 0.02 },
        },
        timeScale: {
          borderColor: '#374151',
          timeVisible: true,
          secondsVisible: false,
        },
      });

    chartRef.current = chart;

    // Create candlestick series using v5 API
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
    candlestickSeriesRef.current = candlestickSeries as unknown as ISeriesApi<'Candlestick'>;

    // Set candlestick data
    const candlestickData: CandlestickData[] = data.map((d) => ({
      time: toTime(d.Date),
      open: d.Open,
      high: d.High,
      low: d.Low,
      close: d.Close,
    }));
    candlestickSeries.setData(candlestickData);

    // Create a Set of valid chart dates (as timestamps) for filtering target data
    // This ensures markers only appear at dates that exist in the sampled chart data
    const validChartDates = new Set(candlestickData.map(d => d.time as number));

    // Add markers for calculated targets - only at TRANSITION points (when value changes)
    // and only for dates that exist in the chart data
    const markers: SeriesMarker<Time>[] = [];
    calculatedTargets.forEach((target) => {
      if (!target.visible || !target.data || target.data.length === 0) return;

      const config = target.config;

      // Filter target data to only include points with dates in the chart
      const filteredData = target.data.filter(point => {
        const time = toTime(point.date);
        return validChartDates.has(time as number);
      });

      // For binary classification, show markers at transitions (0→1) or all if showAllTargets is true
      if (config.category === 'binary_classification') {
        const direction = (config as { direction?: string }).direction;
        const isUp = direction === 'up' || direction === 'bullish';

        filteredData.forEach((point, idx) => {
          if (point.value === null || point.value === undefined) return;
          if (point.value !== 1) return; // Only show markers where target is 1

          // Check if this is a transition (0→1)
          const prevValue = idx > 0 ? filteredData[idx - 1].value : 0;
          const isTransition = prevValue !== 1;

          // Show marker if: showAllTargets is true OR this is a transition
          if (showAllTargets || isTransition) {
            const time = toTime(point.date);
            markers.push({
              time,
              position: isUp ? 'aboveBar' : 'belowBar',
              color: target.color,
              shape: isUp ? 'arrowUp' : 'arrowDown',
              text: '', // Keep empty to avoid clutter
            });
          }
        });
      } else if (config.category === 'multiclass_classification') {
        // Triple barrier: 0=stop, 1=profit, 2=timeout - show transitions
        filteredData.forEach((point, idx) => {
          if (point.value === null || point.value === undefined) return;

          const prevValue = idx > 0 ? filteredData[idx - 1].value : null;
          if (prevValue === point.value) return; // Skip if same as previous

          const time = toTime(point.date);
          if (point.value === 1) {
            markers.push({
              time,
              position: 'aboveBar',
              color: '#10B981', // green
              shape: 'arrowUp',
              text: '',
            });
          } else if (point.value === 0) {
            markers.push({
              time,
              position: 'belowBar',
              color: '#EF4444', // red
              shape: 'arrowDown',
              text: '',
            });
          } else if (point.value === 2) {
            markers.push({
              time,
              position: 'inBar',
              color: '#F59E0B', // yellow
              shape: 'circle',
              text: '',
            });
          }
        });
      }
      // Regression targets don't show markers
    });

    // Add trend markers if showTrends is enabled
    // Filter to only include dates that exist in the chart data
    if (indicators.showTrends && trendData.length > 0) {
      const filteredTrendData = trendData.filter(point => {
        const time = toTime(point.date);
        return validChartDates.has(time as number);
      });

      filteredTrendData.forEach((point, index) => {
        // Only show markers at trend changes
        const prevTrend = index > 0 ? filteredTrendData[index - 1].trend : null;
        if (point.trend !== prevTrend) {
          const time = toTime(point.date);
          if (point.trend === 'uptrend') {
            markers.push({
              time,
              position: 'belowBar',
              color: '#22C55E',
              shape: 'arrowUp',
              text: 'Up',
            });
          } else if (point.trend === 'downtrend') {
            markers.push({
              time,
              position: 'aboveBar',
              color: '#EF4444',
              shape: 'arrowDown',
              text: 'Down',
            });
          } else if (point.trend === 'sideways') {
            markers.push({
              time,
              position: 'inBar',
              color: '#F59E0B',
              shape: 'circle',
              text: 'Side',
            });
          }
        }
      });
    }

    // Sort markers by time and set them on the candlestick series
    if (markers.length > 0) {
      markers.sort((a, b) => (a.time as number) - (b.time as number));
      createSeriesMarkers(candlestickSeries, markers);
    }

    // News sentiment - render as circles above bars with size based on count
    // Group news by sentiment and size bucket, create separate series for each
    if (indicators.showSentiment && newsFrequencyByDate.length > 0) {
      // Create a lookup map for OHLC data by date (try multiple date formats)
      const ohlcByDate = new Map<string, OHLCData>();
      data.forEach(d => {
        // Store by full date string and also by date-only part
        ohlcByDate.set(d.Date, d);
        const dateOnly = d.Date.split('T')[0];
        if (!ohlcByDate.has(dateOnly)) {
          ohlcByDate.set(dateOnly, d);
        }
      });

      // Find max count for normalization
      const maxCount = Math.max(...newsFrequencyByDate.map(n => n.count), 1);

      // Group by sentiment and size bucket
      type SentimentBucket = { data: LineData[]; color: string; radius: number };
      const sentimentBuckets: SentimentBucket[] = [];

      // Define size buckets (small, medium, large, xlarge)
      const getSizeBucket = (count: number): number => {
        const ratio = count / maxCount;
        if (ratio <= 0.25) return 0;  // small
        if (ratio <= 0.5) return 1;   // medium
        if (ratio <= 0.75) return 2;  // large
        return 3;                      // xlarge
      };

      const radiusByBucket = [5, 8, 12, 16];
      const colorBySentiment: Record<string, string> = {
        positive: '#22C55E99',  // 60% opacity
        negative: '#EF444499',
        neutral: '#F59E0B99',
      };

      // Create 12 buckets (3 sentiments x 4 sizes)
      const sentiments = ['positive', 'negative', 'neutral'] as const;
      sentiments.forEach(sentiment => {
        radiusByBucket.forEach((radius) => {
          sentimentBuckets.push({
            data: [],
            color: colorBySentiment[sentiment],
            radius,
          });
        });
      });

      // Distribute news into buckets
      newsFrequencyByDate.forEach((news) => {
        // Try multiple date key formats
        const dateKey = news.date.split('T')[0];
        let ohlc = ohlcByDate.get(dateKey) || ohlcByDate.get(news.date);

        if (!ohlc) {
          // Try to find by converting to same format as OHLC dates
          for (const [key, val] of ohlcByDate.entries()) {
            if (key.startsWith(dateKey)) {
              ohlc = val;
              break;
            }
          }
        }

        if (!ohlc) return;

        const time = toTime(ohlc.Date);  // Use OHLC date for consistency
        const sizeBucket = getSizeBucket(news.count);
        const sentimentIdx = sentiments.indexOf(news.dominantSentiment);
        if (sentimentIdx === -1) return;

        const bucketIdx = sentimentIdx * 4 + sizeBucket;
        // Position circle above the high price with some offset
        const offset = ohlc.High * 0.03;  // 3% above high
        sentimentBuckets[bucketIdx].data.push({
          time,
          value: ohlc.High + offset,
        });
      });

      // Create a line series for each non-empty bucket
      sentimentBuckets.forEach((bucket) => {
        if (bucket.data.length === 0) return;

        // Sort data by time to avoid rendering issues
        bucket.data.sort((a, b) => (a.time as number) - (b.time as number));

        const series = chart.addSeries(LineSeries, {
          color: 'transparent',
          lineWidth: 1,
          lineVisible: false,
          pointMarkersVisible: true,
          pointMarkersRadius: bucket.radius,
          lastValueVisible: false,
          priceLineVisible: false,
        });

        // Apply marker color via series options
        series.applyOptions({
          color: bucket.color,
        });

        series.setData(bucket.data);
      });
    }

    // Volume series - at the bottom
    if (indicators.showVolume) {
      const volumeSeries = chart.addSeries(HistogramSeries, {
        color: '#6366F1',
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      chart.priceScale('volume').applyOptions({
        scaleMargins: { top: volumeTop, bottom: volumeBottom },
        borderVisible: false,
      });
      const volumeData: HistogramData[] = data.map((d) => ({
        time: toTime(d.Date),
        value: d.Volume || 0,
        color: d.Close >= d.Open ? '#10B98140' : '#EF444440',
      }));
      volumeSeries.setData(volumeData);
    }

    // SMA 20
    if (indicators.showSMA20) {
      const sma20Series = chart.addSeries(LineSeries, {
        color: '#3B82F6',
        lineWidth: 1,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      sma20Series.setData(calculateSMA(data, 20));
    }

    // SMA 50
    if (indicators.showSMA50) {
      const sma50Series = chart.addSeries(LineSeries, {
        color: '#F97316',
        lineWidth: 1,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      sma50Series.setData(calculateSMA(data, 50));
    }

    // Bollinger Bands
    if (indicators.showBollinger) {
      const bollinger = calculateBollinger(data);

      const bollingerUpper = chart.addSeries(LineSeries, {
        color: '#8B5CF6',
        lineWidth: 1,
        lineStyle: 2,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      bollingerUpper.setData(bollinger.upper);

      const bollingerMiddle = chart.addSeries(LineSeries, {
        color: '#8B5CF6',
        lineWidth: 1,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      bollingerMiddle.setData(bollinger.middle);

      const bollingerLower = chart.addSeries(LineSeries, {
        color: '#8B5CF6',
        lineWidth: 1,
        lineStyle: 2,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      bollingerLower.setData(bollinger.lower);
    }

    // SAR overlay (on price chart) - visible dots
    // Filter to only include dates that exist in the chart data
    if (indicatorData?.sar && indicatorData.sar.length > 0) {
      const sarSeries = chart.addSeries(LineSeries, {
        color: '#F59E0B', // Yellow/amber color for SAR dots
        lineWidth: 1,
        lineVisible: false, // No line connecting dots
        pointMarkersVisible: true,
        pointMarkersRadius: 4, // Larger dots for visibility
        lastValueVisible: false,
        priceLineVisible: false,
      });
      const sarData: LineData[] = indicatorData.sar
        .filter(d => d.value !== null && validChartDates.has(toTime(d.date) as number))
        .map(d => ({
          time: toTime(d.date),
          value: d.value as number,
        }));
      sarSeries.setData(sarData);
    }

    // ZigZag overlay (on price chart) - connects pivot points
    // Filter to only include dates that exist in the chart data
    if (indicatorData?.zigzag && indicatorData.zigzag.length > 0) {
      const zigzagSeries = chart.addSeries(LineSeries, {
        color: '#EC4899', // Pink/magenta for visibility
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      });
      // ZigZag only has values at pivot points (nulls between), so filter to non-null values
      // and to dates that exist in the chart data
      const zigzagData: LineData[] = indicatorData.zigzag
        .filter(d => d.value !== null && validChartDates.has(toTime(d.date) as number))
        .map(d => ({
          time: toTime(d.date),
          value: d.value as number,
        }));
      if (zigzagData.length > 0) {
        zigzagSeries.setData(zigzagData);
      }
    }

    // RSI pane (separate scale) - above MACD
    if (indicatorData?.rsi && indicatorData.rsi.length > 0) {
      const rsiSeries = chart.addSeries(LineSeries, {
        color: '#8B5CF6',
        lineWidth: 2,
        priceScaleId: 'rsi',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      chart.priceScale('rsi').applyOptions({
        scaleMargins: { top: rsiTop, bottom: rsiBottom },
        borderVisible: false,
      });
      const rsiData: LineData[] = indicatorData.rsi
        .filter(d => d.value !== null)
        .map(d => ({
          time: toTime(d.date),
          value: d.value as number,
        }));
      rsiSeries.setData(rsiData);

      // Add overbought/oversold lines
      const rsiUpperSeries = chart.addSeries(LineSeries, {
        color: '#EF444480',
        lineWidth: 1,
        lineStyle: 2,
        priceScaleId: 'rsi',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      const rsiLowerSeries = chart.addSeries(LineSeries, {
        color: '#10B98180',
        lineWidth: 1,
        lineStyle: 2,
        priceScaleId: 'rsi',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      // Create constant lines at 70 and 30
      const rsiDates = indicatorData.rsi.filter(d => d.value !== null);
      if (rsiDates.length >= 2) {
        rsiUpperSeries.setData([
          { time: toTime(rsiDates[0].date), value: 70 },
          { time: toTime(rsiDates[rsiDates.length - 1].date), value: 70 },
        ]);
        rsiLowerSeries.setData([
          { time: toTime(rsiDates[0].date), value: 30 },
          { time: toTime(rsiDates[rsiDates.length - 1].date), value: 30 },
        ]);
      }
    }

    // MACD pane (separate scale) - above volatility
    if (indicatorData?.macd && indicatorData.macd.length > 0) {
      // MACD histogram
      const macdHistSeries = chart.addSeries(HistogramSeries, {
        color: '#6366F1',
        priceScaleId: 'macd',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      chart.priceScale('macd').applyOptions({
        scaleMargins: { top: macdTop, bottom: macdBottom },
        borderVisible: false,
      });
      const histData: HistogramData[] = indicatorData.macd
        .filter(d => d.histogram !== null)
        .map(d => ({
          time: toTime(d.date),
          value: d.histogram as number,
          color: (d.histogram as number) >= 0 ? '#10B98180' : '#EF444480',
        }));
      macdHistSeries.setData(histData);

      // MACD line
      const macdLineSeries = chart.addSeries(LineSeries, {
        color: '#3B82F6',
        lineWidth: 2,
        priceScaleId: 'macd',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      const macdLineData: LineData[] = indicatorData.macd
        .filter(d => d.macd !== null)
        .map(d => ({
          time: toTime(d.date),
          value: d.macd as number,
        }));
      macdLineSeries.setData(macdLineData);

      // Signal line
      const signalLineSeries = chart.addSeries(LineSeries, {
        color: '#F97316',
        lineWidth: 2,
        priceScaleId: 'macd',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      const signalLineData: LineData[] = indicatorData.macd
        .filter(d => d.signal !== null)
        .map(d => ({
          time: toTime(d.date),
          value: d.signal as number,
        }));
      signalLineSeries.setData(signalLineData);
    }

    // Volatility targets (regression) - render as line series in separate pane above sentiment
    if (volatilityTargets.length > 0) {
      volatilityTargets.forEach((target, idx) => {
        const volSeries = chart.addSeries(LineSeries, {
          color: target.color || ['#F59E0B', '#8B5CF6', '#EC4899'][idx % 3],
          lineWidth: 2,
          priceScaleId: 'volatility',
          lastValueVisible: false,
          priceLineVisible: false,
        });

        const volData: LineData[] = target.data
          .filter(d => d.value !== null && d.value !== undefined)
          .map(d => ({
            time: toTime(d.date),
            value: d.value as number,
          }));
        volSeries.setData(volData);
      });

      // Configure volatility pane - above sentiment/volume
      chart.priceScale('volatility').applyOptions({
        scaleMargins: { top: volatilityTop, bottom: volatilityBottom },
        borderVisible: false,
      });
    }

    // Dynamic indicator overlays from dataset columns
    if (enabledIndicators && enabledIndicators.size > 0) {
      let colorIdx = 0;
      enabledIndicators.forEach(colName => {
        const seriesData: LineData[] = data
          .filter(d => d[colName] !== null && d[colName] !== undefined && !isNaN(Number(d[colName])))
          .map(d => ({
            time: toTime(d.Date),
            value: d[colName] as number,
          }));
        if (seriesData.length === 0) return;
        const dynSeries = chart.addSeries(LineSeries, {
          color: DYNAMIC_INDICATOR_COLORS[colorIdx % DYNAMIC_INDICATOR_COLORS.length],
          lineWidth: 1,
          lastValueVisible: false,
          priceLineVisible: false,
          title: colName,
        });
        dynSeries.setData(seriesData);
        colorIdx++;
      });
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
      console.error('TradingChart error:', err);
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [data, indicators, height, toTime, calculateSMA, calculateBollinger, newsFrequencyByDate, trendData, calculatedTargets, indicatorData, enabledIndicators, showAllTargets]);

  if (error) {
    return (
      <div
        className="bg-red-900/20 border border-red-500 rounded p-4 text-red-400"
        style={{ width: '100%', height: `${height}px` }}
      >
        <p className="font-bold">Chart Error:</p>
        <p className="font-mono text-sm">{error}</p>
        <p className="mt-2 text-gray-400">Data points: {data.length}</p>
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

export default TradingChart;
