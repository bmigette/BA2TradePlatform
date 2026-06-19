import { API_BASE } from '../lib/config';
import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Calendar, TrendingUp, Database, Download, MessageSquare, Target, X, RefreshCw, AlertCircle, CheckCircle, Loader, Settings, ChevronDown, ChevronUp, Eye, EyeOff } from 'lucide-react';
import TradingChart from '../components/TradingChart';
import type { IndicatorData, NewsFrequency } from '../components/TradingChart';
import PredictionTargetsPanel from '../components/PredictionTargetsPanel';
import TargetSetModal from '../components/TargetSetModal';
import type { CalculatedTarget, TargetConfig, TrendReversalTarget } from '../types/targets';
import RegenerateDialog from '../components/RegenerateDialog';
import type { RegenOptions } from '../components/RegenerateDialog';

interface Dataset {
  id: number;
  name: string;
  ticker: string;
  timeframe: string;
  start_date: string;
  end_date: string;
  rows_count: number;
  status: 'pending' | 'building' | 'ready' | 'error';
  error_message: string | null;
  technical_indicators: any;
  fundamentals_config: any;
  sentiment_config: any;
  file_path: string;
  created_at: string;
}

interface OHLCData {
  Date: string;
  Open: number;
  High: number;
  Low: number;
  Close: number;
  Volume: number;
  // Technical indicators
  SMA_20?: number;
  SMA_50?: number;
  EMA_12?: number;
  EMA_26?: number;
  MACD?: number;
  MACD_signal?: number;
  RSI?: number;
  BB_upper?: number;
  BB_lower?: number;
  BB_middle?: number;
}

// NewsFrequency is imported from TradingChart

interface PredictionTarget {
  profitPct: number;
  maxDd: number;
  days: number;
}

interface PredictionPreview {
  target_columns: string[];
  statistics: Record<string, {
    positive_count: number;
    negative_count: number;
    positive_pct: number;
    negative_pct: number;
    total_valid: number;
  }>;
  target_data: any[];  // All rows with Date + target columns
  total_rows: number;
}

interface TrendDataPoint {
  date: string;
  trend: 'uptrend' | 'downtrend' | 'sideways' | 'breakout_up' | 'breakout_down';
  strength: number;
  close: number;
}

interface TrendStatistics {
  total_rows: number;
  trends: Record<string, { count: number; percentage: number }>;
  strength: { mean: number; min: number; max: number };
}

interface TrendConfig {
  method: 'moving_average' | 'linear_regression' | 'adx' | 'pivot_points' | 'donchian';
  lookback_period: number;
  prediction_horizon: number;
  fast_period: number;
  slow_period: number;
}

interface IndicatorVisibility {
  sma20: boolean;
  sma50: boolean;
  ema12: boolean;
  ema26: boolean;
  bollingerBands: boolean;
  volume: boolean;
  showMacd: boolean;
  showRsi: boolean;
  showSentiment: boolean;
  showTargets: boolean;
  showTrends: boolean;
}

interface ColumnInfo {
  name: string;
  dtype: string;
  category: string;
}

interface DatasetColumns {
  dataset_id: number;
  total_columns: number;
  category_counts: Record<string, number>;
  columns: Record<string, ColumnInfo[]>;
  all_columns: string[];
}

// Colors for dynamic indicators
const INDICATOR_COLORS = [
  '#3B82F6', // blue
  '#F97316', // orange
  '#10B981', // green
  '#8B5CF6', // purple
  '#EC4899', // pink
  '#06B6D4', // cyan
  '#F59E0B', // amber
  '#6366F1', // indigo
  '#84CC16', // lime
  '#EF4444', // red
];

const DatasetDetails: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [chartData, setChartData] = useState<OHLCData[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isRegenerating, setIsRegenerating] = useState(false);
  const [showRegenerateModal, setShowRegenerateModal] = useState(false);

  // Prediction targets state (legacy - kept for backwards compatibility)
  const [predictionTargets, setPredictionTargets] = useState<PredictionTarget[]>([]);
  const [newTarget, setNewTarget] = useState<PredictionTarget>({ profitPct: 10, maxDd: 5, days: 14 });
  const [predictionPreview, setPredictionPreview] = useState<PredictionPreview | null>(null);
  const [_previewLoading, setPreviewLoading] = useState(false);
  const [_generateLoading, setGenerateLoading] = useState(false);
  const [_generatedFiles, setGeneratedFiles] = useState<{ training: string; normalization: string } | null>(null);
  // Suppress unused variable warnings
  void _previewLoading; void _generateLoading; void _generatedFiles;

  // New prediction targets system
  const [calculatedTargets, setCalculatedTargets] = useState<CalculatedTarget[]>([]);
  const [indicatorData, setIndicatorData] = useState<IndicatorData | undefined>(undefined);
  const [targetSetModalOpen, setTargetSetModalOpen] = useState(false);
  const [targetSetModalMode, setTargetSetModalMode] = useState<'save' | 'load'>('save');
  const [currentTargetConfigs, setCurrentTargetConfigs] = useState<TargetConfig[]>([]);
  const [showAllTargets, setShowAllTargets] = useState(false); // Show all targets vs transitions only
  const [indicators, setIndicators] = useState<IndicatorVisibility>({
    sma20: true,
    sma50: false,
    ema12: false,
    ema26: false,
    bollingerBands: false,
    volume: true,
    showMacd: false,
    showRsi: false,
    showSentiment: true,
    showTargets: true,
    showTrends: false,
  });

  // Trend analysis state
  const [trendData, setTrendData] = useState<TrendDataPoint[]>([]);
  const [trendStats, setTrendStats] = useState<TrendStatistics | null>(null);
  const [trendLoading, setTrendLoading] = useState(false);
  const [trendConfig, setTrendConfig] = useState<TrendConfig>({
    method: 'moving_average',
    lookback_period: 20,
    prediction_horizon: 5,
    fast_period: 10,
    slow_period: 30,
  });
  const [showTrendConfig, setShowTrendConfig] = useState(false);

  // Dynamic indicators from dataset columns
  const [datasetColumns, setDatasetColumns] = useState<DatasetColumns | null>(null);
  const [columnsLoading, setColumnsLoading] = useState(false);
  const [showIndicatorPopup, setShowIndicatorPopup] = useState(false);
  const [enabledIndicators, setEnabledIndicators] = useState<Set<string>>(new Set());
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set(['technical']));


  // Fetch dataset columns for indicator selection
  const fetchDatasetColumns = async (datasetId: number) => {
    setColumnsLoading(true);
    try {
      const response = await fetch(`${API_BASE}/datasets/${datasetId}/columns`);
      if (response.ok) {
        const data = await response.json();
        setDatasetColumns(data);
      } else {
        console.error('Failed to fetch dataset columns');
      }
    } catch (err) {
      console.error('Error fetching columns:', err);
    } finally {
      setColumnsLoading(false);
    }
  };

  // Fetch trend analysis data
  const fetchTrendData = async (datasetId: number, config: TrendConfig) => {
    setTrendLoading(true);
    try {
      const params = new URLSearchParams({
        method: config.method,
        lookback_period: config.lookback_period.toString(),
        prediction_horizon: config.prediction_horizon.toString(),
        fast_period: config.fast_period.toString(),
        slow_period: config.slow_period.toString(),
      });
      const response = await fetch(`${API_BASE}/datasets/${datasetId}/trends?${params}`);
      if (response.ok) {
        const data = await response.json();
        setTrendData(data.trends || []);
        setTrendStats(data.statistics || null);
      } else {
        console.error('Failed to fetch trend data');
      }
    } catch (err) {
      console.error('Error fetching trends:', err);
    } finally {
      setTrendLoading(false);
    }
  };

  // Toggle indicator visibility
  const toggleDynamicIndicator = (columnName: string) => {
    setEnabledIndicators(prev => {
      const next = new Set(prev);
      if (next.has(columnName)) {
        next.delete(columnName);
      } else {
        next.add(columnName);
      }
      return next;
    });
  };

  // Toggle category expansion in popup
  const toggleCategory = (category: string) => {
    setExpandedCategories(prev => {
      const next = new Set(prev);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return next;
    });
  };

  // Get color for an indicator based on its index
  const getIndicatorColor = (columnName: string): string => {
    const index = Array.from(enabledIndicators).indexOf(columnName);
    return INDICATOR_COLORS[index % INDICATOR_COLORS.length];
  };

  // Handle dataset regeneration
  const handleRegenerate = async (regenOptions: RegenOptions) => {
    if (!dataset) return;

    setIsRegenerating(true);
    setShowRegenerateModal(false);
    setError(null);

    try {
      const response = await fetch(`${API_BASE}/datasets/${dataset.id}/regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(regenOptions),
      });

      if (response.ok) {
        const updatedDataset = await response.json();
        setDataset(updatedDataset);

        // Refetch chart data if successful - load all rows (max_rows=0 means no limit)
        if (updatedDataset.status === 'ready') {
          const csvResponse = await fetch(`${API_BASE}/datasets/${dataset.id}/preview?max_rows=0`);
          if (csvResponse.ok) {
            const csvData = await csvResponse.json();
            console.log(`Preview refresh: ${csvData.returned_rows} rows of ${csvData.total_rows} total`);
            const rawData = csvData.data || [];
            const enrichedData = addIndicatorsToData(rawData);
            setChartData(enrichedData);
          }
        }
      } else {
        const errorData = await response.json();
        setError(errorData.detail || 'Failed to regenerate dataset');
        // Refetch dataset to get updated status
        const refreshResponse = await fetch(`${API_BASE}/datasets/${dataset.id}`);
        if (refreshResponse.ok) {
          setDataset(await refreshResponse.json());
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setIsRegenerating(false);
    }
  };

  // Prediction target functions (legacy - kept for backwards compatibility)
  const _addPredictionTarget = () => {
    if (newTarget.profitPct > 0 && newTarget.maxDd > 0 && newTarget.days > 0) {
      setPredictionTargets([...predictionTargets, { ...newTarget }]);
      setNewTarget({ profitPct: 10, maxDd: 5, days: 14 });
    }
  };

  const _removePredictionTarget = (index: number) => {
    setPredictionTargets(predictionTargets.filter((_, i) => i !== index));
  };

  const _previewPredictionTargets = async () => {
    if (!dataset || predictionTargets.length === 0) return;

    setPreviewLoading(true);
    setPredictionPreview(null);

    try {
      // Convert targets to API format (both up and down directions)
      const apiTargets = predictionTargets.flatMap(t => [
        { profit_pct: t.profitPct, max_dd: t.maxDd, days: t.days, direction: 'up' },
        { profit_pct: t.profitPct, max_dd: t.maxDd, days: t.days, direction: 'down' }
      ]);

      const response = await fetch(
        `${API_BASE}/ml/datasets/${dataset.id}/preview-targets`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(apiTargets)
        }
      );

      if (response.ok) {
        const data = await response.json();
        setPredictionPreview(data);
      } else {
        console.error('Preview failed');
      }
    } catch (err) {
      console.error('Preview error:', err);
    } finally {
      setPreviewLoading(false);
    }
  };

  const _generateTrainingData = async () => {
    if (!dataset || predictionTargets.length === 0) return;

    setGenerateLoading(true);
    setGeneratedFiles(null);

    try {
      const apiTargets = predictionTargets.flatMap(t => [
        { profit_pct: t.profitPct, max_dd: t.maxDd, days: t.days, direction: 'up' },
        { profit_pct: t.profitPct, max_dd: t.maxDd, days: t.days, direction: 'down' }
      ]);

      const response = await fetch(
        `${API_BASE}/ml/datasets/${dataset.id}/generate-training-data`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ targets: apiTargets, normalize: true })
        }
      );

      if (response.ok) {
        const data = await response.json();
        setGeneratedFiles({
          training: data.training_file,
          normalization: data.normalization_file
        });
      } else {
        console.error('Generate failed');
      }
    } catch (err) {
      console.error('Generate error:', err);
    } finally {
      setGenerateLoading(false);
    }
  };
  // Suppress unused function warnings
  void _addPredictionTarget; void _removePredictionTarget; void _previewPredictionTargets; void _generateTrainingData;

  // New prediction targets panel callbacks
  const handleTargetsCalculated = useCallback(async (targets: CalculatedTarget[]) => {
    setCalculatedTargets(targets);

    // Check if any targets are trend_reversal type and need indicator display
    const trendReversalTargets = targets.filter(t => t.config.type === 'trend_reversal');
    if (trendReversalTargets.length > 0 && dataset) {
      try {
        // Collect unique indicators to fetch
        const indicatorsToFetch: Array<{ type: string; [key: string]: unknown }> = [];
        const seenIndicators = new Set<string>();

        trendReversalTargets.forEach(t => {
          const config = t.config as TrendReversalTarget;
          const key = `${config.indicator}_${JSON.stringify(config.indicatorParams)}`;
          if (!seenIndicators.has(key)) {
            seenIndicators.add(key);
            if (config.indicator === 'rsi') {
              indicatorsToFetch.push({ type: 'rsi', period: (config.indicatorParams as { period?: number }).period || 14 });
            } else if (config.indicator === 'macd') {
              const p = config.indicatorParams as { fast?: number; slow?: number; signal?: number };
              indicatorsToFetch.push({ type: 'macd', fast: p.fast || 12, slow: p.slow || 26, signal: p.signal || 9 });
            } else if (config.indicator === 'sar') {
              const p = config.indicatorParams as { afStart?: number; afMax?: number };
              indicatorsToFetch.push({ type: 'sar', af_start: p.afStart || 0.02, af_max: p.afMax || 0.2 });
            } else if (config.indicator === 'zigzag') {
              indicatorsToFetch.push({ type: 'zigzag', deviation_pct: (config.indicatorParams as { deviationPct?: number }).deviationPct || 5 });
            } else if (config.indicator === 'stochastic') {
              const p = config.indicatorParams as { kPeriod?: number; dPeriod?: number };
              indicatorsToFetch.push({ type: 'stochastic', k_period: p.kPeriod || 14, d_period: p.dPeriod || 3 });
            } else if (config.indicator === 'adx') {
              indicatorsToFetch.push({ type: 'adx', period: (config.indicatorParams as { period?: number }).period || 14 });
            } else if (config.indicator === 'donchian') {
              indicatorsToFetch.push({ type: 'donchian', period: (config.indicatorParams as { period?: number }).period || 20 });
            }
          }
        });

        if (indicatorsToFetch.length > 0) {
          const response = await fetch(`${API_BASE}/datasets/${dataset.id}/calculate-indicators`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ indicators: indicatorsToFetch }),
          });

          if (response.ok) {
            const rawData = await response.json();
            // Transform flat data to structured format expected by TradingChart
            // Backend returns keys with params: { data: [{date, rsi_14, sar_0.02_0.2, zigzag_5.0, macd_12_26_9, ...}, ...] }
            // Frontend expects: { rsi: [{date, value}], sar: [{date, value}], macd: [{date, macd, signal, histogram}], zigzag: [{date, value}] }
            const transformed: IndicatorData = {};

            if (rawData.data && rawData.data.length > 0) {
              const firstRow = rawData.data[0];
              const keys = Object.keys(firstRow);

              // Find keys by prefix (e.g., rsi_14 -> rsi)
              const rsiKey = keys.find(k => k.startsWith('rsi_'));
              const sarKey = keys.find(k => k.startsWith('sar_'));
              const zigzagKey = keys.find(k => k.startsWith('zigzag_'));
              const macdKey = keys.find(k => k.startsWith('macd_') && !k.includes('signal') && !k.includes('hist'));
              const signalKey = keys.find(k => k.startsWith('macd_signal_'));
              const histKey = keys.find(k => k.startsWith('macd_hist_'));

              // RSI
              if (rsiKey) {
                transformed.rsi = rawData.data.map((d: Record<string, unknown>) => ({
                  date: d.date as string,
                  value: d[rsiKey] as number | null,
                }));
              }

              // SAR
              if (sarKey) {
                transformed.sar = rawData.data.map((d: Record<string, unknown>) => ({
                  date: d.date as string,
                  value: d[sarKey] as number | null,
                }));
              }

              // ZigZag
              if (zigzagKey) {
                transformed.zigzag = rawData.data.map((d: Record<string, unknown>) => ({
                  date: d.date as string,
                  value: d[zigzagKey] as number | null,
                }));
              }

              // MACD (has 3 values)
              if (macdKey || signalKey || histKey) {
                transformed.macd = rawData.data.map((d: Record<string, unknown>) => ({
                  date: d.date as string,
                  macd: macdKey ? d[macdKey] as number | null : null,
                  signal: signalKey ? d[signalKey] as number | null : null,
                  histogram: histKey ? d[histKey] as number | null : null,
                }));
              }
            }

            setIndicatorData(transformed);
          }
        }
      } catch (error) {
        console.error('Error fetching indicator data:', error);
      }
    } else if (trendReversalTargets.length === 0) {
      // Clear indicator data if no trend reversal targets
      setIndicatorData(undefined);
    }
  }, [dataset]);

  const handleSaveTargetSet = useCallback((targets: TargetConfig[]) => {
    setCurrentTargetConfigs(targets);
    setTargetSetModalMode('save');
    setTargetSetModalOpen(true);
  }, []);

  const handleLoadTargetSet = useCallback(() => {
    setTargetSetModalMode('load');
    setTargetSetModalOpen(true);
  }, []);

  const handleTargetSetSaved = useCallback(() => {
    // Could show a toast notification here
    console.log('Target set saved');
  }, []);

  // State for loaded target configs to pass to PredictionTargetsPanel
  const [loadedTargetConfigs, setLoadedTargetConfigs] = useState<TargetConfig[] | undefined>(undefined);

  const handleTargetSetLoaded = useCallback((targets: TargetConfig[]) => {
    // Pass loaded targets to PredictionTargetsPanel
    setLoadedTargetConfigs(targets);
    // Clear after a short delay to allow the panel to pick them up
    setTimeout(() => setLoadedTargetConfigs(undefined), 100);
  }, []);

  // Calculate simple moving average (utility function for future use)
  const calculateSMA = (data: OHLCData[], period: number): OHLCData[] => {
    return data.map((d, i) => {
      if (i < period - 1) return d;
      const sum = data.slice(i - period + 1, i + 1).reduce((acc, curr) => acc + curr.Close, 0);
      return {
        ...d,
        [`SMA_${period}`]: sum / period,
      };
    });
  };
  void calculateSMA; // Reserved for dynamic period calculations

  // Add indicators to chart data - only calculate if not already present
  const addIndicatorsToData = (data: OHLCData[]): OHLCData[] => {
    if (data.length === 0) return data;

    // Check if indicators already exist in the data (from backend)
    const firstRow = data[0] as unknown as Record<string, unknown>;
    const hasSMA20 = 'SMA_20' in firstRow || 'sma_20' in firstRow;
    const hasSMA50 = 'SMA_50' in firstRow || 'sma_50' in firstRow;
    const hasBB = 'BB_upper' in firstRow || 'bb_upper' in firstRow;

    // If all indicators exist, just return data as-is (already calculated on backend)
    if (hasSMA20 && hasSMA50 && hasBB) {
      console.log('Indicators already present in data, skipping frontend calculation');
      return data;
    }

    console.log('Calculating indicators on frontend (consider adding them to dataset)');

    // Only calculate missing indicators in a single pass for performance
    const n = data.length;
    const enrichedData: OHLCData[] = new Array(n);

    // Pre-compute cumulative sums for O(n) calculation instead of O(n^2)
    let sumSMA20 = 0;
    let sumSMA50 = 0;

    for (let i = 0; i < n; i++) {
      const d = data[i];
      const closePrice = d.Close;

      // Update running sums
      sumSMA20 += closePrice;
      sumSMA50 += closePrice;

      // Subtract values falling out of window
      if (i >= 20) sumSMA20 -= data[i - 20].Close;
      if (i >= 50) sumSMA50 -= data[i - 50].Close;

      enrichedData[i] = { ...d };

      // Calculate SMA 20 (need at least 20 data points)
      if (!hasSMA20 && i >= 19) {
        enrichedData[i].SMA_20 = sumSMA20 / 20;
      }

      // Calculate SMA 50 (need at least 50 data points)
      if (!hasSMA50 && i >= 49) {
        enrichedData[i].SMA_50 = sumSMA50 / 50;
      }

      // Calculate Bollinger Bands (20-period, 2 std dev)
      if (!hasBB && i >= 19) {
        const mean = sumSMA20 / 20;
        let variance = 0;
        for (let j = i - 19; j <= i; j++) {
          variance += Math.pow(data[j].Close - mean, 2);
        }
        variance /= 20;
        const stdDev = Math.sqrt(variance);
        enrichedData[i].BB_middle = mean;
        enrichedData[i].BB_upper = mean + 2 * stdDev;
        enrichedData[i].BB_lower = mean - 2 * stdDev;
      }
    }

    return enrichedData;
  };

  const toggleIndicator = (key: keyof IndicatorVisibility) => {
    setIndicators(prev => ({ ...prev, [key]: !prev[key] }));
  };

  useEffect(() => {
    const fetchDataset = async () => {
      setIsLoading(true);
      setError(null);
      try {
        // Fetch dataset metadata
        const response = await fetch(`${API_BASE}/datasets/${id}`);
        if (!response.ok) {
          throw new Error('Failed to fetch dataset details');
        }
        const data = await response.json();
        setDataset(data);

        // Fetch dataset CSV data for charting - load all rows (max_rows=0 means no limit)
        // Note: Preview endpoint requires backend restart to be available
        try {
          console.time('fetchPreview');
          const csvResponse = await fetch(`${API_BASE}/datasets/${id}/preview?max_rows=0`);
          console.timeEnd('fetchPreview');
          if (csvResponse.ok) {
            console.time('parsePreview');
            const csvData = await csvResponse.json();
            console.timeEnd('parsePreview');
            console.log(`Preview data: ${csvData.returned_rows} rows of ${csvData.total_rows} total, ${csvData.columns?.length} columns`);
            const rawData = csvData.data || [];
            console.time('enrichData');
            const enrichedData = addIndicatorsToData(rawData);
            console.timeEnd('enrichData');
            setChartData(enrichedData);
          }
        } catch (err) {
          console.log('Preview endpoint not available yet:', err);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'An error occurred');
      } finally {
        setIsLoading(false);
      }
    };

    if (id) {
      fetchDataset();
    }
  }, [id]);

  // Sentiment data now comes from chartData columns (news_1d_positive, etc.)
  // No API call needed - newsFrequencyByDate is computed from chartData directly

  // Fetch dataset columns for indicator popup
  useEffect(() => {
    if (dataset) {
      fetchDatasetColumns(dataset.id);
    }
  }, [dataset?.id]);

  // Fetch trend data when trends are enabled
  useEffect(() => {
    if (dataset && indicators.showTrends && trendData.length === 0) {
      fetchTrendData(dataset.id, trendConfig);
    }
  }, [dataset?.id, indicators.showTrends]);

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString();
  };

  const formatNumber = (num: number) => {
    return new Intl.NumberFormat('en-US').format(num);
  };

  // Aggregate news by date for frequency-based visualization
  // Uses existing dataset columns (news_1d_positive, news_1d_neutral, news_1d_negative) instead of API
  const newsFrequencyByDate = useMemo<NewsFrequency[]>(() => {
    if (!chartData || chartData.length === 0) return [];

    // Check if dataset has news sentiment columns
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const firstRow = chartData[0] as any;
    const hasNewsColumns = 'news_1d_positive' in firstRow || 'news_count' in firstRow;
    if (!hasNewsColumns) return [];

    const frequencies: NewsFrequency[] = [];

    chartData.forEach((row) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const dataRow = row as any;
      // Use news_1d columns (1-day lookback) for the chart
      const positiveCount = Number(dataRow['news_1d_positive'] || 0);
      const negativeCount = Number(dataRow['news_1d_negative'] || 0);
      const neutralCount = Number(dataRow['news_1d_neutral'] || 0);
      const totalCount = positiveCount + negativeCount + neutralCount;

      // Only add if there's any news for this date
      if (totalCount > 0) {
        let dominantSentiment: 'positive' | 'neutral' | 'negative' = 'neutral';
        if (positiveCount > negativeCount && positiveCount > neutralCount) {
          dominantSentiment = 'positive';
        } else if (negativeCount > positiveCount && negativeCount > neutralCount) {
          dominantSentiment = 'negative';
        }

        frequencies.push({
          date: row.Date,
          count: totalCount,
          positiveCount,
          negativeCount,
          neutralCount,
          dominantSentiment
        });
      }
    });

    return frequencies;
  }, [chartData]);

  // Get color for trend type
  const getTrendColor = (trend: string): string => {
    switch (trend) {
      case 'uptrend':
      case 'breakout_up':
        return '#10B981'; // green
      case 'downtrend':
      case 'breakout_down':
        return '#EF4444'; // red
      case 'sideways':
      default:
        return '#F59E0B'; // amber
    }
  };

  const handleExport = async () => {
    try {
      const response = await fetch(`${API_BASE}/datasets/${id}/export`);
      if (!response.ok) {
        throw new Error('Failed to export dataset');
      }

      // Create a blob from the response
      const blob = await response.blob();

      // Create a temporary download link
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${dataset?.name || 'dataset'}.csv`;
      document.body.appendChild(a);
      a.click();

      // Cleanup
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error('Export error:', err);
      alert('Failed to export dataset');
    }
  };

  const handleExportParquet = async () => {
    try {
      const response = await fetch(`${API_BASE}/datasets/${id}/export/parquet`);
      if (!response.ok) {
        throw new Error('Failed to export dataset to Parquet');
      }

      // Create a blob from the response
      const blob = await response.blob();

      // Create a temporary download link
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${dataset?.name || 'dataset'}.parquet`;
      document.body.appendChild(a);
      a.click();

      // Cleanup
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error('Export Parquet error:', err);
      alert('Failed to export dataset to Parquet');
    }
  };

  if (isLoading) {
    return (
      <div className="p-6">
        <div className="text-center py-12">
          <p className="text-gray-600 dark:text-gray-400">Loading dataset...</p>
        </div>
      </div>
    );
  }

  if (error || !dataset) {
    return (
      <div className="p-6">
        <div className="mb-4 p-4 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-200 rounded-md">
          {error || 'Dataset not found'}
        </div>
        <button
          onClick={() => navigate('/datasets')}
          className="px-4 py-2 bg-gray-500 text-white rounded-md hover:bg-gray-600 flex items-center space-x-2"
        >
          <ArrowLeft size={16} />
          <span>Back to Datasets</span>
        </button>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-full overflow-x-hidden">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center justify-between mb-4">
          <button
            onClick={() => navigate('/datasets')}
            className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 flex items-center space-x-2"
          >
            <ArrowLeft size={16} />
            <span>Back to Datasets</span>
          </button>
          <div className="flex space-x-2">
            <button
              onClick={() => setShowRegenerateModal(true)}
              disabled={isRegenerating}
              className="px-4 py-2 bg-orange-600 text-white rounded-md hover:bg-orange-700 disabled:opacity-50 flex items-center space-x-2"
            >
              <RefreshCw size={16} className={isRegenerating ? 'animate-spin' : ''} />
              <span>{isRegenerating ? 'Regenerating...' : 'Regenerate'}</span>
            </button>
            <button
              onClick={handleExport}
              disabled={dataset.status !== 'ready'}
              className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center space-x-2"
            >
              <Download size={16} />
              <span>Export CSV</span>
            </button>
            <button
              onClick={handleExportParquet}
              disabled={dataset.status !== 'ready'}
              className="px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 flex items-center space-x-2"
            >
              <Download size={16} />
              <span>Export Parquet</span>
            </button>
          </div>
        </div>
        <div className="flex items-center gap-3 mb-2">
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">{dataset.name}</h1>
          {/* Status Badge */}
          {dataset.status === 'ready' && (
            <span className="px-2.5 py-1 text-xs font-medium bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300 rounded-full flex items-center gap-1">
              <CheckCircle size={12} />
              Ready
            </span>
          )}
          {dataset.status === 'building' && (
            <span className="px-2.5 py-1 text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300 rounded-full flex items-center gap-1">
              <Loader size={12} className="animate-spin" />
              Building
            </span>
          )}
          {dataset.status === 'pending' && (
            <span className="px-2.5 py-1 text-xs font-medium bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300 rounded-full flex items-center gap-1">
              <Loader size={12} />
              Pending
            </span>
          )}
          {dataset.status === 'error' && (
            <span className="px-2.5 py-1 text-xs font-medium bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300 rounded-full flex items-center gap-1">
              <AlertCircle size={12} />
              Error
            </span>
          )}
        </div>
        <p className="text-gray-600 dark:text-gray-400">
          {dataset.ticker} • {dataset.timeframe}
        </p>
        {/* Error Message Banner */}
        {dataset.status === 'error' && dataset.error_message && (
          <div className="mt-3 p-3 bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-lg">
            <div className="flex items-start gap-2">
              <AlertCircle size={18} className="text-red-500 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-red-700 dark:text-red-300">Dataset Generation Failed</p>
                <p className="text-sm text-red-600 dark:text-red-400 mt-1">{dataset.error_message}</p>
                <button
                  onClick={() => setShowRegenerateModal(true)}
                  disabled={isRegenerating}
                  className="mt-2 px-3 py-1.5 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50 flex items-center gap-1"
                >
                  <RefreshCw size={14} className={isRegenerating ? 'animate-spin' : ''} />
                  {isRegenerating ? 'Retrying...' : 'Retry Generation'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow">
          <div className="flex items-center space-x-2 mb-2">
            <Database size={20} className="text-blue-500" />
            <h3 className="text-sm font-medium text-gray-600 dark:text-gray-400">
              Data Points
            </h3>
          </div>
          <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {formatNumber(dataset.rows_count)}
          </p>
        </div>

        <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow">
          <div className="flex items-center space-x-2 mb-2">
            <Calendar size={20} className="text-green-500" />
            <h3 className="text-sm font-medium text-gray-600 dark:text-gray-400">
              Start Date
            </h3>
          </div>
          <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {formatDate(dataset.start_date)}
          </p>
        </div>

        <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow">
          <div className="flex items-center space-x-2 mb-2">
            <Calendar size={20} className="text-orange-500" />
            <h3 className="text-sm font-medium text-gray-600 dark:text-gray-400">
              End Date
            </h3>
          </div>
          <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {formatDate(dataset.end_date)}
          </p>
        </div>

        <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow">
          <div className="flex items-center space-x-2 mb-2">
            <TrendingUp size={20} className="text-purple-500" />
            <h3 className="text-sm font-medium text-gray-600 dark:text-gray-400">
              Timeframe
            </h3>
          </div>
          <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {dataset.timeframe}
          </p>
        </div>
      </div>

      {/* Price Chart - Candlestick */}
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow mb-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-bold">Price Chart (Candlestick)</h2>
          <span className="text-sm text-gray-500 dark:text-gray-400">Scroll to zoom, drag to pan</span>
        </div>

        {/* Indicator Toggle Controls */}
        <div className="flex flex-wrap gap-3 mb-4 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
          <span className="text-sm font-medium text-gray-600 dark:text-gray-400 self-center">Overlays:</span>
          <button
            onClick={() => toggleIndicator('sma20')}
            className={`px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors ${
              indicators.sma20
                ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
            }`}
          >
            {indicators.sma20 ? <Eye size={14} /> : <EyeOff size={14} />}
            SMA 20
          </button>
          <button
            onClick={() => toggleIndicator('sma50')}
            className={`px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors ${
              indicators.sma50
                ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
            }`}
          >
            {indicators.sma50 ? <Eye size={14} /> : <EyeOff size={14} />}
            SMA 50
          </button>
          <button
            onClick={() => toggleIndicator('bollingerBands')}
            className={`px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors ${
              indicators.bollingerBands
                ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
            }`}
          >
            {indicators.bollingerBands ? <Eye size={14} /> : <EyeOff size={14} />}
            Bollinger Bands
          </button>
          <button
            onClick={() => toggleIndicator('volume')}
            className={`px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors ${
              indicators.volume
                ? 'bg-violet-100 text-violet-700 dark:bg-violet-900/50 dark:text-violet-300'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
            }`}
          >
            {indicators.volume ? <Eye size={14} /> : <EyeOff size={14} />}
            Volume
          </button>
          <button
            onClick={() => setShowIndicatorPopup(true)}
            className="px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300 hover:bg-indigo-200 dark:hover:bg-indigo-800/50"
          >
            <Settings size={14} />
            More Indicators
            {enabledIndicators.size > 0 && (
              <span className="ml-1 px-1.5 py-0.5 text-xs bg-indigo-500 text-white rounded-full">
                {enabledIndicators.size}
              </span>
            )}
          </button>
          <div className="border-l border-gray-300 dark:border-gray-600 mx-2"></div>
          <button
            onClick={() => toggleIndicator('showSentiment')}
            className={`px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors ${
              indicators.showSentiment
                ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
            }`}
          >
            <MessageSquare size={14} />
            News Sentiment
          </button>
          <button
            onClick={() => toggleIndicator('showTargets')}
            className={`px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors ${
              indicators.showTargets
                ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
            }`}
          >
            <Target size={14} />
            Prediction Targets
          </button>
          <button
            onClick={() => toggleIndicator('showTrends')}
            className={`px-3 py-1.5 text-sm rounded-md flex items-center gap-1.5 transition-colors ${
              indicators.showTrends
                ? 'bg-teal-100 text-teal-700 dark:bg-teal-900/50 dark:text-teal-300'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-600 dark:text-gray-400'
            }`}
          >
            <TrendingUp size={14} />
            Trend Analysis
            {trendLoading && <Loader size={12} className="animate-spin" />}
          </button>
          {/* Target Legend and Show All Toggle */}
          {indicators.showTargets && (predictionPreview || calculatedTargets.length > 0) && (
            <div className="flex items-center gap-3 ml-2 text-xs">
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={showAllTargets}
                  onChange={(e) => setShowAllTargets(e.target.checked)}
                  className="w-3 h-3 text-purple-600 rounded"
                />
                <span className="text-gray-500 dark:text-gray-400">Show All</span>
              </label>
              <div className="flex items-center gap-1">
                <div className="w-2.5 h-2.5 rotate-45 bg-green-500 border border-green-700"></div>
                <span className="text-gray-500 dark:text-gray-400">Up Target</span>
              </div>
              <div className="flex items-center gap-1">
                <div className="w-2.5 h-2.5 rotate-45 bg-red-500 border border-red-700"></div>
                <span className="text-gray-500 dark:text-gray-400">Down Target</span>
              </div>
            </div>
          )}
          {/* Sentiment Legend */}
          {indicators.showSentiment && (
            <div className="flex items-center gap-3 ml-3 text-xs">
              {newsFrequencyByDate.length === 0 ? (
                <span className="text-gray-400">No sentiment data in dataset</span>
              ) : (
                <>
                  <div className="flex items-center gap-1">
                    <div className="w-3 h-3 rounded-full bg-green-500"></div>
                    <span className="text-gray-500 dark:text-gray-400">Positive</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <div className="w-3 h-3 rounded-full bg-orange-500"></div>
                    <span className="text-gray-500 dark:text-gray-400">Neutral</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <div className="w-3 h-3 rounded-full bg-red-500"></div>
                    <span className="text-gray-500 dark:text-gray-400">Negative</span>
                  </div>
                  <span className="text-gray-400 dark:text-gray-500 mx-1">|</span>
                  <span className="text-gray-500 dark:text-gray-400">Size = count</span>
                  <span className="text-gray-400 text-xs">({newsFrequencyByDate.length} days with news)</span>
                </>
              )}
            </div>
          )}
          {/* Trend Legend - always show Config button when trends enabled */}
          {indicators.showTrends && (
            <div className="flex items-center gap-3 ml-3 text-xs">
              {trendData.length > 0 ? (
                <>
                  <div className="flex items-center gap-1">
                    <div className="w-3 h-3 rounded bg-green-500"></div>
                    <span className="text-gray-500 dark:text-gray-400">Uptrend</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <div className="w-3 h-3 rounded bg-red-500"></div>
                    <span className="text-gray-500 dark:text-gray-400">Downtrend</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <div className="w-3 h-3 rounded bg-amber-500"></div>
                    <span className="text-gray-500 dark:text-gray-400">Sideways</span>
                  </div>
                  <span className="text-gray-400">({trendData.length} points)</span>
                </>
              ) : trendLoading ? (
                <span className="text-gray-400 flex items-center gap-1">
                  <Loader size={12} className="animate-spin" />
                  Loading trends...
                </span>
              ) : (
                <span className="text-amber-500">No trend data - click Config to fetch</span>
              )}
              <button
                onClick={() => setShowTrendConfig(!showTrendConfig)}
                className="ml-2 px-2 py-0.5 text-xs bg-teal-100 text-teal-700 dark:bg-teal-900/50 dark:text-teal-300 rounded hover:bg-teal-200 dark:hover:bg-teal-800/50"
              >
                <Settings size={10} className="inline mr-1" />
                Config
              </button>
            </div>
          )}
          {/* Volatility/Regression Targets Legend */}
          {calculatedTargets.filter(t => t.visible && t.config.category === 'regression').length > 0 && (
            <div className="flex items-center gap-3 ml-3 text-xs">
              <span className="text-gray-400 dark:text-gray-500">Volatility:</span>
              {calculatedTargets
                .filter(t => t.visible && t.config.category === 'regression')
                .map((target, idx) => {
                  const config = target.config as { method?: string; horizon?: number };
                  const label = `${config.method?.toUpperCase() || 'Vol'} ${config.horizon || ''}b`;
                  return (
                    <div key={idx} className="flex items-center gap-1">
                      <div
                        className="w-4 h-0.5 rounded"
                        style={{ backgroundColor: target.color }}
                      />
                      <span className="text-gray-600 dark:text-gray-300">{label}</span>
                    </div>
                  );
                })}
            </div>
          )}
        </div>
        {chartData.length > 0 ? (
          <TradingChart
            data={chartData}
            indicators={{
              showSMA20: indicators.sma20,
              showSMA50: indicators.sma50,
              showBollinger: indicators.bollingerBands,
              showVolume: indicators.volume,
              showSentiment: indicators.showSentiment,
              showTargets: indicators.showTargets,
              showTrends: indicators.showTrends,
            }}
            newsFrequencyByDate={newsFrequencyByDate}
            trendData={trendData}
            predictionPreview={predictionPreview}
            calculatedTargets={calculatedTargets}
            indicatorData={indicatorData}
            enabledIndicators={enabledIndicators}
            height={500}
            showAllTargets={showAllTargets}
          />
        ) : (
          <div className="text-center py-12">
            <p className="text-gray-600 dark:text-gray-400">
              No chart data available
            </p>
          </div>
        )}
      </div>

      {/* New Prediction Targets Panel - below chart */}
      {dataset && chartData.length > 0 && (
        <div className="mt-6 mb-6">
          <PredictionTargetsPanel
            datasetId={dataset.id}
            datasetTimeframe={dataset.timeframe}
            onTargetsCalculated={handleTargetsCalculated}
            onSaveSet={handleSaveTargetSet}
            onLoadSet={handleLoadTargetSet}
            loadedTargets={loadedTargetConfigs}
          />
        </div>
      )}

      {/* Non-Chart Data Table (Fundamental, Sentiment, Macro, and other non-chart columns) */}
      {datasetColumns && chartData.length > 0 && (
        (() => {
          // Get columns that are NOT price or technical (include all other categories)
          const chartCategories = ['price', 'technical'];  // These are displayed on the chart
          const nonChartColumns = Object.entries(datasetColumns.columns)
            .filter(([category]) => !chartCategories.includes(category))
            .flatMap(([_, cols]) => cols.map(c => c.name))
            .filter(col => chartData[0] && col in chartData[0]);

          // Limit displayed rows to last 50
          const displayData = chartData.slice(-50);

          return (
            <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow mb-6 overflow-hidden">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100">
                  Non-Chart Data
                </h2>
                {nonChartColumns.length > 0 && (
                  <span className="text-sm text-gray-500 dark:text-gray-400">
                    Showing {displayData.length} of {chartData.length} rows ({nonChartColumns.length} columns)
                  </span>
                )}
              </div>
              <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
                Fundamental, sentiment, and macro data that are not displayed on the chart.
              </p>

              {nonChartColumns.length === 0 ? (
                <div className="text-center py-8 text-gray-500 dark:text-gray-400">
                  <p>No fundamental, sentiment, or macro data in this dataset.</p>
                  <p className="text-sm mt-2">
                    Enable sentiment sources or add fundamental data when creating a dataset to see data here.
                  </p>
                </div>
              ) : (
                <div className="w-full overflow-hidden">
                  <div className="overflow-x-auto max-h-96 overflow-y-auto border border-gray-200 dark:border-gray-700 rounded-lg">
                    <table className="text-sm whitespace-nowrap">
                    <thead className="bg-gray-50 dark:bg-gray-700 sticky top-0">
                      <tr>
                        <th className="px-3 py-2 text-left font-medium text-gray-700 dark:text-gray-300">
                          Date
                        </th>
                        {nonChartColumns.map(col => (
                          <th key={col} className="px-3 py-2 text-left font-medium text-gray-700 dark:text-gray-300 whitespace-nowrap">
                            {col}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                      {displayData.map((row, idx) => (
                        <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                          <td className="px-3 py-2 text-gray-600 dark:text-gray-400 whitespace-nowrap font-mono text-xs">
                            {new Date(row.Date).toLocaleString()}
                          </td>
                          {nonChartColumns.map(col => {
                            const value = (row as any)[col];
                            const formatted = value === null || value === undefined
                              ? '-'
                              : typeof value === 'number'
                                ? Number.isInteger(value) ? value : value.toFixed(4)
                                : String(value);
                            return (
                              <td key={col} className="px-3 py-2 text-gray-900 dark:text-gray-100 whitespace-nowrap font-mono text-xs">
                                {formatted}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          );
        })()
      )}

      {/* Dataset Information */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <h2 className="text-xl font-bold mb-4">Dataset Information</h2>
          <dl className="space-y-3">
            <div>
              <dt className="text-sm font-medium text-gray-600 dark:text-gray-400">
                Dataset ID
              </dt>
              <dd className="text-sm text-gray-900 dark:text-gray-100">
                {dataset.id}
              </dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-600 dark:text-gray-400">
                File Path
              </dt>
              <dd className="text-sm text-gray-900 dark:text-gray-100 font-mono break-all">
                {dataset.file_path}
              </dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-600 dark:text-gray-400">
                Created At
              </dt>
              <dd className="text-sm text-gray-900 dark:text-gray-100">
                {new Date(dataset.created_at).toLocaleString()}
              </dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-600 dark:text-gray-400 flex items-center gap-1">
                <MessageSquare size={14} />
                News Articles
              </dt>
              <dd className="text-sm text-gray-900 dark:text-gray-100">
                {dataset.sentiment_config?.articles_count !== undefined ? (
                  <div>
                    <span className="font-semibold">{dataset.sentiment_config.articles_count}</span> articles used
                    {dataset.sentiment_config.news_sources && (
                      <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Sources: {dataset.sentiment_config.news_sources.map((s: string) => s.replace('_news', '')).join(', ')}
                      </div>
                    )}
                  </div>
                ) : dataset.sentiment_config?.enabled ? (
                  <span className="text-gray-500">Count not available (regenerate dataset)</span>
                ) : (
                  <span className="text-gray-500">Sentiment not enabled</span>
                )}
              </dd>
            </div>
          </dl>
        </div>

        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <h2 className="text-xl font-bold mb-4">Configuration</h2>
          <dl className="space-y-3">
            <div>
              <dt className="text-sm font-medium text-gray-600 dark:text-gray-400">
                Technical Indicators
              </dt>
              <dd className="text-sm text-gray-900 dark:text-gray-100">
                {dataset.technical_indicators
                  ? JSON.stringify(dataset.technical_indicators)
                  : 'Not configured'}
              </dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-600 dark:text-gray-400">
                Fundamentals
              </dt>
              <dd className="text-sm text-gray-900 dark:text-gray-100">
                {dataset.fundamentals_config
                  ? JSON.stringify(dataset.fundamentals_config)
                  : 'Not configured'}
              </dd>
            </div>
            <div>
              <dt className="text-sm font-medium text-gray-600 dark:text-gray-400">
                Sentiment Analysis
              </dt>
              <dd className="text-sm text-gray-900 dark:text-gray-100">
                {dataset.sentiment_config
                  ? JSON.stringify(dataset.sentiment_config)
                  : 'Not configured'}
              </dd>
            </div>
          </dl>
        </div>
      </div>

      {/* Trend Analysis Configuration Modal */}
      {showTrendConfig && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-3xl max-h-[80vh] flex flex-col m-4">
            {/* Header */}
            <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
              <h2 className="text-xl font-bold flex items-center gap-2">
                <TrendingUp size={20} className="text-teal-500" />
                Trend Analysis Configuration
              </h2>
              <button
                onClick={() => setShowTrendConfig(false)}
                className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-full"
              >
                <X size={20} className="text-gray-500" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-4">
              <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
                Configure trend detection parameters. Trend-based targets can provide more balanced class distributions compared to price percentage targets.
              </p>

              <div className="grid grid-cols-2 md:grid-cols-3 gap-4 mb-4 p-4 bg-gray-50 dark:bg-gray-700 rounded-lg">
                <div>
                  <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                    Detection Method
                  </label>
                  <select
                    value={trendConfig.method}
                    onChange={(e) => setTrendConfig({ ...trendConfig, method: e.target.value as TrendConfig['method'] })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-800"
                  >
                    <option value="moving_average">Moving Average</option>
                    <option value="linear_regression">Linear Regression</option>
                    <option value="adx">ADX (Strength)</option>
                    <option value="pivot_points">Pivot Points</option>
                    <option value="donchian">Donchian Channels</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                    Lookback Period
                  </label>
                  <input
                    type="number"
                    min="5"
                    max="100"
                    value={trendConfig.lookback_period}
                    onChange={(e) => setTrendConfig({ ...trendConfig, lookback_period: parseInt(e.target.value) || 20 })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-800"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                    Prediction Horizon
                  </label>
                  <input
                    type="number"
                    min="0"
                    max="30"
                    value={trendConfig.prediction_horizon}
                    onChange={(e) => setTrendConfig({ ...trendConfig, prediction_horizon: parseInt(e.target.value) || 5 })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-800"
                  />
                </div>
                {trendConfig.method === 'moving_average' && (
                  <>
                    <div>
                      <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                        Fast MA Period
                      </label>
                      <input
                        type="number"
                        min="3"
                        max="50"
                        value={trendConfig.fast_period}
                        onChange={(e) => setTrendConfig({ ...trendConfig, fast_period: parseInt(e.target.value) || 10 })}
                        className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-800"
                      />
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                        Slow MA Period
                      </label>
                      <input
                        type="number"
                        min="10"
                        max="200"
                        value={trendConfig.slow_period}
                        onChange={(e) => setTrendConfig({ ...trendConfig, slow_period: parseInt(e.target.value) || 30 })}
                        className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm bg-white dark:bg-gray-800"
                      />
                    </div>
                  </>
                )}
              </div>

              <div className="flex gap-3 mb-4">
                <button
                  onClick={() => dataset && fetchTrendData(dataset.id, trendConfig)}
                  disabled={trendLoading}
                  className="px-4 py-2 bg-teal-600 text-white rounded-md hover:bg-teal-700 disabled:opacity-50 flex items-center gap-2"
                >
                  {trendLoading ? <Loader size={16} className="animate-spin" /> : <RefreshCw size={16} />}
                  {trendLoading ? 'Analyzing...' : 'Recalculate Trends'}
                </button>
              </div>

              {/* Trend Statistics */}
              {trendStats && (
                <div className="mt-4">
                  <h4 className="text-sm font-medium mb-2">Trend Distribution:</h4>
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                    {Object.entries(trendStats.trends).map(([trend, data]) => {
                      if (data.count === 0) return null;
                      const color = getTrendColor(trend);
                      return (
                        <div key={trend} className="p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
                          <div className="flex items-center gap-2 mb-2">
                            <div className="w-3 h-3 rounded" style={{ backgroundColor: color }}></div>
                            <span className="text-sm font-medium text-gray-700 dark:text-gray-300 capitalize">
                              {trend.replace('_', ' ')}
                            </span>
                          </div>
                          <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                            {data.percentage.toFixed(1)}%
                          </div>
                          <div className="text-xs text-gray-500">{data.count} periods</div>
                        </div>
                      );
                    })}
                  </div>
                  {trendStats.strength && (
                    <div className="mt-3 text-xs text-gray-500">
                      Trend strength: mean={trendStats.strength.mean.toFixed(2)}, min={trendStats.strength.min.toFixed(2)}, max={trendStats.strength.max.toFixed(2)}
                    </div>
                  )}
                </div>
              )}

              <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg text-sm text-blue-800 dark:text-blue-200">
                <strong>Tip:</strong> Trend-based targets often provide more balanced class distributions than price percentage targets.
                Use these for ML training by enabling trend targets in job configuration.
              </div>
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end p-4 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => setShowTrendConfig(false)}
                className="px-4 py-2 bg-teal-600 text-white rounded-md hover:bg-teal-700"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Indicator Selection Popup */}
      {showIndicatorPopup && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-2xl max-h-[80vh] flex flex-col">
            {/* Header */}
            <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
              <div>
                <h3 className="text-lg font-bold text-gray-900 dark:text-gray-100">
                  Dataset Indicators
                </h3>
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  Select indicators to display on the chart (loaded from dataset)
                </p>
              </div>
              <button
                onClick={() => setShowIndicatorPopup(false)}
                className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-full"
              >
                <X size={20} className="text-gray-500" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-4">
              {columnsLoading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader size={24} className="animate-spin text-gray-500" />
                  <span className="ml-2 text-gray-500">Loading columns...</span>
                </div>
              ) : datasetColumns ? (
                <div className="space-y-4">
                  {/* Category sections */}
                  {['technical', 'fundamental', 'sentiment', 'macro', 'other'].map(category => {
                    const columns = datasetColumns.columns[category] || [];
                    if (columns.length === 0) return null;

                    const isExpanded = expandedCategories.has(category);
                    const categoryLabel = category.charAt(0).toUpperCase() + category.slice(1);
                    const enabledCount = columns.filter(c => enabledIndicators.has(c.name)).length;

                    return (
                      <div key={category} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                        <button
                          onClick={() => toggleCategory(category)}
                          className="w-full flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700/50 hover:bg-gray-100 dark:hover:bg-gray-700"
                        >
                          <div className="flex items-center gap-2">
                            {isExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                            <span className="font-medium text-gray-900 dark:text-gray-100">
                              {categoryLabel}
                            </span>
                            <span className="text-sm text-gray-500">
                              ({columns.length} columns)
                            </span>
                            {enabledCount > 0 && (
                              <span className="px-2 py-0.5 text-xs bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300 rounded-full">
                                {enabledCount} selected
                              </span>
                            )}
                          </div>
                        </button>
                        {isExpanded && (
                          <div className="p-3 grid grid-cols-2 md:grid-cols-3 gap-2">
                            {columns.map(col => {
                              // Skip non-numeric columns
                              if (!col.dtype.includes('float') && !col.dtype.includes('int')) {
                                return null;
                              }
                              const isEnabled = enabledIndicators.has(col.name);
                              const color = isEnabled ? getIndicatorColor(col.name) : undefined;

                              return (
                                <button
                                  key={col.name}
                                  onClick={() => toggleDynamicIndicator(col.name)}
                                  className={`px-3 py-2 text-sm rounded-md flex items-center gap-2 transition-colors text-left ${
                                    isEnabled
                                      ? 'bg-indigo-100 dark:bg-indigo-900/50 text-indigo-700 dark:text-indigo-300'
                                      : 'bg-gray-100 dark:bg-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-500'
                                  }`}
                                >
                                  {isEnabled && (
                                    <div
                                      className="w-3 h-3 rounded-full flex-shrink-0"
                                      style={{ backgroundColor: color }}
                                    />
                                  )}
                                  <span className="truncate">{col.name}</span>
                                </button>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    );
                  })}

                  {/* Currently selected indicators */}
                  {enabledIndicators.size > 0 && (
                    <div className="mt-4 p-3 bg-indigo-50 dark:bg-indigo-900/20 rounded-lg">
                      <h4 className="text-sm font-medium text-indigo-700 dark:text-indigo-300 mb-2">
                        Selected Indicators ({enabledIndicators.size})
                      </h4>
                      <div className="flex flex-wrap gap-2">
                        {Array.from(enabledIndicators).map(name => (
                          <span
                            key={name}
                            className="px-2 py-1 text-xs rounded-full flex items-center gap-1.5"
                            style={{
                              backgroundColor: `${getIndicatorColor(name)}20`,
                              color: getIndicatorColor(name),
                              border: `1px solid ${getIndicatorColor(name)}`
                            }}
                          >
                            <div
                              className="w-2 h-2 rounded-full"
                              style={{ backgroundColor: getIndicatorColor(name) }}
                            />
                            {name}
                            <button
                              onClick={() => toggleDynamicIndicator(name)}
                              className="ml-1 hover:opacity-70"
                            >
                              <X size={12} />
                            </button>
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-center py-8 text-gray-500">
                  No column data available
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between p-4 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => setEnabledIndicators(new Set())}
                className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
              >
                Clear All
              </button>
              <button
                onClick={() => setShowIndicatorPopup(false)}
                className="px-4 py-2 bg-indigo-600 text-white rounded-md hover:bg-indigo-700"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Target Set Save/Load Modal */}
      <TargetSetModal
        isOpen={targetSetModalOpen}
        mode={targetSetModalMode}
        currentTargets={currentTargetConfigs}
        onClose={() => setTargetSetModalOpen(false)}
        onSave={handleTargetSetSaved}
        onLoad={handleTargetSetLoaded}
      />

      {/* Regenerate Dataset Modal */}
      <RegenerateDialog
        isOpen={showRegenerateModal}
        onClose={() => setShowRegenerateModal(false)}
        onConfirm={handleRegenerate}
      />
    </div>
  );
};

export default DatasetDetails;
