/**
 * Type definitions for prediction targets system
 */

// Target categories for metric selection
export type TargetCategory = 'binary_classification' | 'multiclass_classification' | 'regression';

// Base target configuration
export interface BaseTargetConfig {
  type: string;
  category: TargetCategory;
  enabled?: boolean;
  color?: string;
  // Target feature options - additional features derived from target for training
  includeValues?: boolean;      // Include indicator values as features (for indicator-based targets)
  valueLookback?: number;       // How many bars of lagged values to include (default 5)
  includeBarsSince?: boolean;   // Include bars-since-last-signal counter feature
  // Timeframe-aware targets - use a different timeframe than the dataset's base timeframe
  timeframe?: string;           // Optional: target calculation timeframe (e.g., "1h" for 1h indicator on 30m dataset)
}

// Price-based target (existing)
export interface PriceBasedTarget extends BaseTargetConfig {
  type: 'price_based';
  category: 'binary_classification';
  direction: 'up' | 'down';
  profitPct: number;
  maxDrawdownPct: number;
  timeBars: number;
  timeBarsUnit?: HorizonUnit; // 'bars' (default) or 'days'
}

// Horizon unit type - bars or days
export type HorizonUnit = 'bars' | 'days';

// Directional movement target
export interface DirectionalTarget extends BaseTargetConfig {
  type: 'directional';
  category: 'binary_classification';
  direction: 'up' | 'down';
  horizon: number; // value ahead
  horizonUnit?: HorizonUnit; // 'bars' (default) or 'days'
}

// Triple-barrier target
export interface TripleBarrierTarget extends BaseTargetConfig {
  type: 'triple_barrier';
  category: 'multiclass_classification';
  profitPct: number;
  stopPct: number;
  maxBars: number;
  maxBarsUnit?: HorizonUnit; // 'bars' (default) or 'days'
}

// Trend reversal target (binary - single direction)
export interface TrendReversalTarget extends BaseTargetConfig {
  type: 'trend_reversal';
  category: 'binary_classification';
  indicator: 'rsi' | 'macd' | 'sar' | 'zigzag' | 'donchian' | 'adx' | 'stochastic';
  indicatorParams: IndicatorParams;
  threshold: number;
  direction: 'bullish' | 'bearish';
}

// Unified 3-class trend target (bearish/neutral/bullish)
export interface UnifiedTrendTarget extends BaseTargetConfig {
  type: 'unified_trend';
  category: 'multiclass_classification';
  indicator: 'zigzag' | 'macd' | 'rsi' | 'adx';
  indicatorParams: IndicatorParams;
  // Classes: 0=bearish, 1=neutral, 2=bullish
  classes: ['bearish', 'neutral', 'bullish'];
}

// Volatility target
export interface VolatilityTarget extends BaseTargetConfig {
  type: 'volatility';
  category: 'regression';
  horizon: number; // value ahead
  horizonUnit?: HorizonUnit; // 'bars' (default) or 'days'
  method: 'std' | 'range' | 'atr';
}

// Union type for all target configs
export type TargetConfig =
  | PriceBasedTarget
  | DirectionalTarget
  | TripleBarrierTarget
  | TrendReversalTarget
  | UnifiedTrendTarget
  | VolatilityTarget;

// Indicator parameters
export interface RSIParams {
  period: number;
}

export interface MACDParams {
  fast: number;
  slow: number;
  signal: number;
}

export interface SARParams {
  afStart: number;
  afMax: number;
}

export interface ZigZagParams {
  deviationPct: number;
}

export interface DonchianParams {
  period: number;
}

export interface ADXParams {
  period: number;
}

export interface StochasticParams {
  kPeriod: number;
  dPeriod: number;
}

export type IndicatorParams = RSIParams | MACDParams | SARParams | ZigZagParams | DonchianParams | ADXParams | StochasticParams;

// Indicator configuration for API
export interface IndicatorConfig {
  type: 'rsi' | 'macd' | 'sar' | 'zigzag' | 'donchian' | 'adx' | 'stochastic' | 'atr' | 'pivot_points' | 'obv';
  period?: number;
  fast?: number;
  slow?: number;
  signal?: number;
  af_start?: number;
  af_max?: number;
  deviation_pct?: number;
  k_period?: number;
  d_period?: number;
  method?: string;
}

// Calculated target with data and stats
export interface CalculatedTarget {
  config: TargetConfig;
  columnName: string;
  data: TargetDataPoint[];
  stats: TargetStats;
  color: string;
  visible: boolean;
}

export interface TargetDataPoint {
  date: string;
  value: number | null;
}

export interface TargetStats {
  totalRows: number;
  validRows: number;
  // For classification
  positiveCount?: number;
  negativeCount?: number;
  positivePct?: number;
  negativePct?: number;
  // For multiclass (triple barrier)
  profitHitCount?: number;
  stopHitCount?: number;
  timeoutCount?: number;
  // For regression
  mean?: number;
  std?: number;
  min?: number;
  max?: number;
}

// Saved target set
export interface TargetSet {
  id: number;
  name: string;
  description?: string;
  targets: TargetConfig[];
  createdAt: string;
  updatedAt: string;
}

// API request/response types
export interface CalculateIndicatorsRequest {
  indicators: IndicatorConfig[];
}

export interface CalculateIndicatorsResponse {
  data: Record<string, number | null>[];
}

export interface CalculateTargetsRequest {
  targets: TargetConfig[];
}

export interface CalculateTargetsResponse {
  targets: CalculatedTarget[];
}

// Chart marker types
export type MarkerShape = 'arrowUp' | 'arrowDown' | 'circle' | 'square';
export type MarkerPosition = 'aboveBar' | 'belowBar' | 'inBar';

export interface ChartMarker {
  time: number;
  position: MarkerPosition;
  shape: MarkerShape;
  color: string;
  text?: string;
  size?: number;
}

// Color palette for targets
export const TARGET_COLORS = {
  priceBased: {
    up: '#10B981',    // green
    down: '#EF4444',  // red
  },
  directional: {
    up: '#3B82F6',    // blue
    down: '#8B5CF6',  // purple
  },
  tripleBarrier: {
    profit: '#10B981',  // green
    stop: '#EF4444',    // red
    timeout: '#F59E0B', // yellow/amber
  },
  trendReversal: {
    bullish: '#A855F7', // purple
    bearish: '#EC4899', // pink
  },
  volatility: '#6366F1', // indigo
} as const;

// Default indicator parameters
export const DEFAULT_INDICATOR_PARAMS = {
  rsi: { period: 14 },
  macd: { fast: 12, slow: 26, signal: 9 },
  sar: { afStart: 0.02, afMax: 0.2 },
  zigzag: { deviationPct: 5.0 },
  donchian: { period: 20 },
  adx: { period: 14 },
  stochastic: { kPeriod: 14, dPeriod: 3 },
} as const;

// Supported timeframes for multi-timeframe targets
export const SUPPORTED_TIMEFRAMES = [
  { value: '15m', label: '15 Minutes' },
  { value: '30m', label: '30 Minutes' },
  { value: '1h', label: '1 Hour' },
  { value: '4h', label: '4 Hours' },
  { value: '1d', label: '1 Day' },
] as const;

// Bars per day for each timeframe (trading hours ~6.5h/day for stocks, 24h for crypto)
// Using approximate values for stock market (6.5h trading day)
export const BARS_PER_DAY: Record<string, number> = {
  '1m': 390,    // 6.5h * 60
  '5m': 78,     // 6.5h * 12
  '15m': 26,    // 6.5h * 4
  '30m': 13,    // 6.5h * 2
  '1h': 7,      // ~6.5h (rounded)
  '2h': 3,      // ~3
  '4h': 2,      // ~2 (might span multiple days)
  '1d': 1,
  'D1': 1,
  '1w': 0.2,    // 1/5 (5 trading days per week)
  'W1': 0.2,
};

// Convert days to bars based on timeframe
export function daysToBar(days: number, timeframe: string): number {
  const barsPerDay = BARS_PER_DAY[timeframe] || 1;
  return Math.round(days * barsPerDay);
}

// Metric options by category
export const METRICS_BY_CATEGORY = {
  binary_classification: [
    { value: 'accuracy', label: 'Accuracy' },
    { value: 'f1', label: 'F1 Score' },
    { value: 'precision', label: 'Precision' },
    { value: 'recall', label: 'Recall' },
    { value: 'auc_roc', label: 'AUC-ROC' },
  ],
  multiclass_classification: [
    { value: 'accuracy', label: 'Accuracy' },
    { value: 'macro_f1', label: 'Macro F1' },
    { value: 'weighted_f1', label: 'Weighted F1' },
  ],
  regression: [
    { value: 'mse', label: 'MSE' },
    { value: 'rmse', label: 'RMSE' },
    { value: 'mae', label: 'MAE' },
    { value: 'r2', label: 'R²' },
    { value: 'mape', label: 'MAPE' },
  ],
} as const;
