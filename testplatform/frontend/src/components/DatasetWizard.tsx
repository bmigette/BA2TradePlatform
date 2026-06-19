import { API_BASE } from '../lib/config';
import React, { useState, useEffect } from 'react';
import { X, MessageSquare, TrendingUp, BarChart3, FileText, Settings, CheckCircle, Plus, Trash2, Save, ChevronDown, ChevronRight, FolderOpen, Upload } from 'lucide-react';

type WizardMode = 'create' | 'duplicate' | 'edit';

interface InitialDataset {
  id: number;
  name: string;
  ticker: string;
  timeframe: string;
  start_date: string;
  end_date: string;
  normalization_buffer_pct?: number;
  technical_indicators?: any;
  generation_config?: any;
  sentiment_config?: any;
  fundamentals_config?: any;
}

interface DatasetWizardProps {
  isOpen: boolean;
  onClose: () => void;
  onComplete: () => void;
  mode?: WizardMode;
  initialData?: InitialDataset | null;
}

// Updated indicator config with individual timeframe
interface IndicatorConfig {
  id: string;  // Unique ID for React keys
  name: string;
  type: string;
  timeframe: string;
  period?: number;
  fast?: number;
  slow?: number;
  signal?: number;
  std_dev?: number;
  k_period?: number;
  d_period?: number;
  smooth_k?: number;
  // SAR parameters
  af_start?: number;
  af_max?: number;
  // ZigZag parameters
  deviation_pct?: number;
  // Pivot Points parameters
  method?: string;
}

interface IndicatorCollection {
  id: number;
  name: string;
  description: string;
  is_default: boolean;
  indicators: IndicatorConfig[];
}

interface SentimentConfig {
  enabled: boolean;
  newsSources: string[];
  lookbackPeriods: string[];
  sentimentCategories: string[];
  impactTimeframes: string[];
  useCachedNews: boolean;
}

interface FundamentalsConfig {
  enabled: boolean;
  statementTypes: string[];  // balance_sheet, income_statement, cash_flow, earnings
  lookbackStatements: number;  // Number of historical statement periods to include (e.g., 2 means last 2 quarters)
  macroIndicators: string[];
  fundamentalsProviders: string[];  // Priority-ordered list of providers
  macroProvider: string;
}

interface WizardData {
  name: string;  // Custom dataset name/title
  ticker: string;
  timeframe: string;
  startDate: string;
  endDate: string;
  dataProvider: string;
  indicators: IndicatorConfig[];
  sentiment: SentimentConfig;
  fundamentals: FundamentalsConfig;
}

// Timeframe ordering for validation
const TIMEFRAME_ORDER = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w', '1mo'];

const TIMEFRAME_LABELS: Record<string, string> = {
  '1m': '1 Minute',
  '5m': '5 Minutes',
  '15m': '15 Minutes',
  '30m': '30 Minutes',
  '1h': '1 Hour',
  '4h': '4 Hours',
  '1d': '1 Day',
  '1w': '1 Week',
  '1mo': '1 Month'
};

// Available indicator types
const INDICATOR_TYPES = [
  { type: 'sma', name: 'SMA (Simple Moving Average)', hasPeriod: true, defaultPeriod: 20 },
  { type: 'ema', name: 'EMA (Exponential Moving Average)', hasPeriod: true, defaultPeriod: 20 },
  { type: 'rsi', name: 'RSI (Relative Strength Index)', hasPeriod: true, defaultPeriod: 14 },
  { type: 'macd', name: 'MACD', hasPeriod: false },
  { type: 'bbands', name: 'Bollinger Bands', hasPeriod: true, defaultPeriod: 20 },
  { type: 'atr', name: 'ATR (Average True Range)', hasPeriod: true, defaultPeriod: 14 },
  { type: 'stochastic', name: 'Stochastic Oscillator', hasPeriod: false },
  { type: 'adx', name: 'ADX (Average Directional Index)', hasPeriod: true, defaultPeriod: 14 },
  { type: 'sar', name: 'Parabolic SAR', hasPeriod: false },
  { type: 'zigzag', name: 'ZigZag', hasPeriod: false },
  { type: 'donchian', name: 'Donchian Channels', hasPeriod: true, defaultPeriod: 20 },
  { type: 'obv', name: 'OBV (On-Balance Volume)', hasPeriod: false },
  { type: 'pivot_points', name: 'Pivot Points', hasPeriod: false },
];

const getDefaultWizardData = (): WizardData => ({
  name: '',  // Will auto-generate from ticker if empty
  ticker: '',
  timeframe: '1d',
  startDate: '',
  endDate: '',
  dataProvider: 'yfinance',
  indicators: [],
  sentiment: {
    enabled: false,
    newsSources: ['fmp_news'],
    lookbackPeriods: ['1d', '1w', '1m', '6m'],
    sentimentCategories: ['positive', 'neutral', 'negative'],
    impactTimeframes: ['short', 'medium', 'long'],
    useCachedNews: false
  },
  fundamentals: {
    enabled: false,
    statementTypes: ['balance_sheet', 'income_statement', 'cash_flow'],
    lookbackStatements: 2,  // Include last 2 statement periods
    macroIndicators: ['interest_rate', 'gdp', 'inflation', 'unemployment'],
    fundamentalsProviders: ['yfinance', 'fmp', 'alphavantage'],  // Priority order
    macroProvider: 'fred'
  }
});

const DatasetWizard: React.FC<DatasetWizardProps> = ({ isOpen, onClose, onComplete, mode = 'create', initialData = null }) => {
  const [currentStep, setCurrentStep] = useState(1);
  const [wizardData, setWizardData] = useState<WizardData>(getDefaultWizardData());

  // Initialize from initialData when mode changes
  useEffect(() => {
    if (isOpen && initialData && (mode === 'duplicate' || mode === 'edit')) {
      // Parse dates - use actual data dates for editing (what the dataset contains)
      // For duplicate, also use actual dates as the starting point
      const startDate = initialData.start_date ? initialData.start_date.split('T')[0] : '';
      const endDate = initialData.end_date ? initialData.end_date.split('T')[0] : '';

      // Parse indicators
      let indicators: IndicatorConfig[] = [];
      if (initialData.technical_indicators && Array.isArray(initialData.technical_indicators)) {
        indicators = initialData.technical_indicators.map((ind: any) => ({
          id: `ind_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
          ...ind
        }));
      }

      // Parse sentiment config from saved data
      const savedSentiment = initialData.sentiment_config || {};
      const sentimentConfig: SentimentConfig = {
        enabled: savedSentiment.enabled || false,
        newsSources: savedSentiment.newsSources || savedSentiment.news_sources || ['fmp_news'],
        lookbackPeriods: savedSentiment.lookbackPeriods || savedSentiment.lookback_periods || ['1d', '1w', '1m', '6m'],
        sentimentCategories: savedSentiment.sentimentCategories || savedSentiment.sentiment_categories || ['positive', 'neutral', 'negative'],
        impactTimeframes: savedSentiment.impactTimeframes || savedSentiment.impact_timeframes || ['short', 'medium', 'long'],
        useCachedNews: savedSentiment.useCachedNews || savedSentiment.use_cached_news || false
      };

      // Parse fundamentals config from saved data
      const savedFundamentals = initialData.fundamentals_config || {};
      const fundamentalsConfig: FundamentalsConfig = {
        enabled: savedFundamentals.enabled || false,
        statementTypes: savedFundamentals.statementTypes || savedFundamentals.statement_types || savedFundamentals.metrics || ['balance_sheet', 'income_statement', 'cash_flow'],
        lookbackStatements: savedFundamentals.lookbackStatements || savedFundamentals.lookback_statements || 2,
        macroIndicators: savedFundamentals.macroIndicators || savedFundamentals.macro_indicators || ['interest_rate', 'gdp', 'inflation', 'unemployment'],
        fundamentalsProviders: savedFundamentals.fundamentalsProviders || savedFundamentals.fundamentals_providers ||
          (savedFundamentals.fundamentals_provider ? [savedFundamentals.fundamentals_provider] : ['yfinance', 'fmp', 'alphavantage']),
        macroProvider: savedFundamentals.macroProvider || savedFundamentals.macro_provider || 'fred'
      };

      setWizardData({
        name: mode === 'duplicate' ? '' : (initialData.name || ''),  // Clear name for duplicate
        ticker: mode === 'duplicate' ? '' : initialData.ticker,  // Clear ticker for duplicate
        timeframe: initialData.timeframe,
        startDate,
        endDate,
        dataProvider: initialData.generation_config?.data_provider || 'yfinance',
        indicators,
        sentiment: sentimentConfig,
        fundamentals: fundamentalsConfig
      });
    } else if (isOpen && mode === 'create') {
      setWizardData(getDefaultWizardData());
    }
  }, [isOpen, mode, initialData]);
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Batch mode state
  const [batchMode, setBatchMode] = useState(false);
  const [batchSymbolsInput, setBatchSymbolsInput] = useState('');
  const [batchSymbols, setBatchSymbols] = useState<string[]>([]);

  // Labels state
  const [labels, setLabels] = useState<string[]>([]);
  const [labelInput, setLabelInput] = useState('');

  const parseBatchSymbols = (text: string): string[] => {
    return text
      .split(/[\n,;]+/)
      .map(s => s.trim().toUpperCase())
      .filter(s => s.length > 0 && /^[A-Z]{1,5}(\.[A-Z]{1,2})?$/.test(s));
  };

  const handleBatchSymbolsChange = (text: string) => {
    setBatchSymbolsInput(text);
    setBatchSymbols(parseBatchSymbols(text));
  };

  const handleBatchFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (event) => {
      const text = event.target?.result as string;
      setBatchSymbolsInput(text);
      setBatchSymbols(parseBatchSymbols(text));
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  const addLabel = (label: string) => {
    const trimmed = label.trim();
    if (trimmed && !labels.includes(trimmed)) {
      setLabels([...labels, trimmed]);
    }
    setLabelInput('');
  };

  const removeLabel = (label: string) => {
    setLabels(labels.filter(l => l !== label));
  };

  const handleLabelKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addLabel(labelInput);
    }
  };

  // Indicator add form state
  const [newIndicatorType, setNewIndicatorType] = useState('sma');
  const [newIndicatorTimeframe, setNewIndicatorTimeframe] = useState('1d');
  const [newIndicatorPeriod, setNewIndicatorPeriod] = useState(20);
  // SAR parameters
  const [sarAfStart, setSarAfStart] = useState(0.02);
  const [sarAfMax, setSarAfMax] = useState(0.2);
  // ZigZag parameters
  const [zigzagDeviation, setZigzagDeviation] = useState(5.0);
  // Pivot Points parameters
  const [pivotMethod, setPivotMethod] = useState('standard');

  // Collections state
  const [collections, setCollections] = useState<IndicatorCollection[]>([]);
  const [_selectedCollectionId, setSelectedCollectionId] = useState<number | null>(null);
  const [saveCollectionName, setSaveCollectionName] = useState('');
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [showCollectionPicker, setShowCollectionPicker] = useState(false);
  const [expandedCollections, setExpandedCollections] = useState<Set<number>>(new Set());
  const [selectedIndicatorIds, setSelectedIndicatorIds] = useState<Set<string>>(new Set());

  // Fetch collections on mount
  useEffect(() => {
    if (isOpen) {
      fetchCollections();
    }
  }, [isOpen]);

  // Update indicator timeframe dropdown when dataset timeframe changes
  useEffect(() => {
    const validTimeframes = getAvailableTimeframes();
    if (!validTimeframes.includes(newIndicatorTimeframe)) {
      setNewIndicatorTimeframe(wizardData.timeframe);
    }
  }, [wizardData.timeframe]);

  const fetchCollections = async () => {
    try {
      const response = await fetch(`${API_BASE}/indicator-collections`);
      if (response.ok) {
        const data = await response.json();
        setCollections(data.collections);
      }
    } catch (err) {
      console.error('Failed to fetch collections:', err);
    }
  };

  const getAvailableTimeframes = () => {
    const datasetIdx = TIMEFRAME_ORDER.indexOf(wizardData.timeframe);
    if (datasetIdx === -1) return TIMEFRAME_ORDER;
    return TIMEFRAME_ORDER.slice(datasetIdx);
  };

  const generateIndicatorId = () => {
    return `ind_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  };

  const getIndicatorDisplayName = (indicator: IndicatorConfig) => {
    const typeInfo = INDICATOR_TYPES.find(t => t.type === indicator.type);
    let name = typeInfo?.name.split(' ')[0] || indicator.type.toUpperCase();

    if (indicator.period) {
      name += ` ${indicator.period}`;
    }

    return `${name} @ ${indicator.timeframe.toUpperCase()}`;
  };

  const addIndicator = () => {
    const typeInfo = INDICATOR_TYPES.find(t => t.type === newIndicatorType);
    if (!typeInfo) return;

    const newIndicator: IndicatorConfig = {
      id: generateIndicatorId(),
      type: newIndicatorType,
      name: typeInfo.name,
      timeframe: newIndicatorTimeframe,
    };

    // Add type-specific parameters
    if (typeInfo.hasPeriod) {
      newIndicator.period = newIndicatorPeriod;
    }

    if (newIndicatorType === 'macd') {
      newIndicator.fast = 12;
      newIndicator.slow = 26;
      newIndicator.signal = 9;
    } else if (newIndicatorType === 'bbands') {
      newIndicator.std_dev = 2.0;
    } else if (newIndicatorType === 'stochastic') {
      newIndicator.k_period = 14;
      newIndicator.d_period = 3;
      newIndicator.smooth_k = 3;
    } else if (newIndicatorType === 'sar') {
      newIndicator.af_start = sarAfStart;
      newIndicator.af_max = sarAfMax;
    } else if (newIndicatorType === 'zigzag') {
      newIndicator.deviation_pct = zigzagDeviation;
    } else if (newIndicatorType === 'pivot_points') {
      newIndicator.method = pivotMethod;
    }

    setWizardData({
      ...wizardData,
      indicators: [...wizardData.indicators, newIndicator]
    });
  };

  const removeIndicator = (id: string) => {
    setWizardData({
      ...wizardData,
      indicators: wizardData.indicators.filter(i => i.id !== id)
    });
  };

  const _loadCollection = (collectionId: number) => {
    const collection = collections.find(c => c.id === collectionId);
    if (!collection) return;

    // Filter indicators to only include those with valid timeframes
    const validTimeframes = getAvailableTimeframes();
    const validIndicators = collection.indicators
      .filter(ind => validTimeframes.includes(ind.timeframe))
      .map(ind => ({
        ...ind,
        id: generateIndicatorId()
      }));

    const invalidCount = collection.indicators.length - validIndicators.length;

    setWizardData({
      ...wizardData,
      indicators: validIndicators
    });

    setSelectedCollectionId(collectionId);

    if (invalidCount > 0) {
      setError(`${invalidCount} indicators were skipped because their timeframe is smaller than the dataset timeframe (${wizardData.timeframe})`);
    }
  };
  // Suppress unused warning - function reserved for future use
  void _loadCollection;

  const toggleCollectionExpanded = (collectionId: number) => {
    setExpandedCollections(prev => {
      const next = new Set(prev);
      if (next.has(collectionId)) {
        next.delete(collectionId);
      } else {
        next.add(collectionId);
      }
      return next;
    });
  };

  const getIndicatorUniqueKey = (collectionId: number, indicatorIndex: number) => {
    return `${collectionId}-${indicatorIndex}`;
  };

  const toggleIndicatorSelection = (collectionId: number, indicatorIndex: number) => {
    const key = getIndicatorUniqueKey(collectionId, indicatorIndex);
    setSelectedIndicatorIds(prev => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const toggleAllCollectionIndicators = (collection: IndicatorCollection, select: boolean) => {
    const validTimeframes = getAvailableTimeframes();
    setSelectedIndicatorIds(prev => {
      const next = new Set(prev);
      collection.indicators.forEach((ind, idx) => {
        const key = getIndicatorUniqueKey(collection.id, idx);
        if (select && validTimeframes.includes(ind.timeframe)) {
          next.add(key);
        } else {
          next.delete(key);
        }
      });
      return next;
    });
  };

  const addSelectedIndicatorsFromPicker = () => {
    const validTimeframes = getAvailableTimeframes();
    const indicatorsToAdd: IndicatorConfig[] = [];

    collections.forEach(collection => {
      collection.indicators.forEach((ind, idx) => {
        const key = getIndicatorUniqueKey(collection.id, idx);
        if (selectedIndicatorIds.has(key) && validTimeframes.includes(ind.timeframe)) {
          indicatorsToAdd.push({
            ...ind,
            id: generateIndicatorId()
          });
        }
      });
    });

    if (indicatorsToAdd.length > 0) {
      setWizardData({
        ...wizardData,
        indicators: [...wizardData.indicators, ...indicatorsToAdd]
      });
    }

    setShowCollectionPicker(false);
    setSelectedIndicatorIds(new Set());
    setExpandedCollections(new Set());
  };

  const getCollectionSelectedCount = (collection: IndicatorCollection) => {
    let count = 0;
    collection.indicators.forEach((_, idx) => {
      if (selectedIndicatorIds.has(getIndicatorUniqueKey(collection.id, idx))) {
        count++;
      }
    });
    return count;
  };

  const getIndicatorParamsString = (indicator: IndicatorConfig) => {
    const params: string[] = [];
    if (indicator.period) params.push(`period=${indicator.period}`);
    if (indicator.fast) params.push(`fast=${indicator.fast}`);
    if (indicator.slow) params.push(`slow=${indicator.slow}`);
    if (indicator.signal) params.push(`signal=${indicator.signal}`);
    if (indicator.k_period) params.push(`k=${indicator.k_period}`);
    if (indicator.d_period) params.push(`d=${indicator.d_period}`);
    if (indicator.af_start) params.push(`af=${indicator.af_start}-${indicator.af_max}`);
    if (indicator.deviation_pct) params.push(`dev=${indicator.deviation_pct}%`);
    if (indicator.std_dev) params.push(`std=${indicator.std_dev}`);
    if (indicator.method) params.push(`method=${indicator.method}`);
    return params.join(', ') || '-';
  };

  const saveCollection = async () => {
    if (!saveCollectionName.trim()) {
      setError('Please enter a collection name');
      return;
    }

    try {
      const response = await fetch(`${API_BASE}/indicator-collections`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: saveCollectionName,
          description: `Custom collection with ${wizardData.indicators.length} indicators`,
          indicators: wizardData.indicators.map(({ id, ...rest }) => rest)
        })
      });

      if (response.ok) {
        setShowSaveDialog(false);
        setSaveCollectionName('');
        fetchCollections();
        setError(null);
      } else {
        const data = await response.json();
        setError(data.detail || 'Failed to save collection');
      }
    } catch (err) {
      setError('Failed to save collection');
    }
  };

  if (!isOpen) return null;

  const validateTicker = (ticker: string): boolean => {
    const tickerRegex = /^[A-Z]{1,5}(\.[A-Z]{1,2})?$/;
    return tickerRegex.test(ticker);
  };

  const handleNext = () => {
    if (currentStep === 1) {
      if (batchMode) {
        if (batchSymbols.length === 0) {
          setError('Enter at least one valid symbol for batch creation');
          return;
        }
      } else {
        if (!wizardData.ticker) {
          setError('Ticker is required');
          return;
        }
        if (!validateTicker(wizardData.ticker)) {
          setError('Invalid ticker format. Use 1-5 uppercase letters (e.g., AAPL, MSFT)');
          return;
        }
      }
      if (wizardData.startDate && wizardData.endDate) {
        const start = new Date(wizardData.startDate);
        const end = new Date(wizardData.endDate);
        if (start >= end) {
          setError('Start date must be before end date');
          return;
        }
      }
    }

    if (currentStep === 2) {
      if (!wizardData.dataProvider) {
        setError('Please select a data provider');
        return;
      }
    }

    setError(null);
    setCurrentStep(currentStep + 1);
  };

  const handleBack = () => {
    setError(null);
    setCurrentStep(currentStep - 1);
  };

  const handleCreate = async () => {
    setIsCreating(true);
    setError(null);

    try {
      // Convert indicators to API format
      const technicalIndicators = wizardData.indicators.map(({ id, name, ...rest }) => rest);

      let response: Response;

      // Prepare sentiment and fundamentals configs
      const sentimentConfig = wizardData.sentiment.enabled ? {
        enabled: true,
        news_sources: wizardData.sentiment.newsSources,
        lookback_periods: wizardData.sentiment.lookbackPeriods,
        sentiment_categories: wizardData.sentiment.sentimentCategories,
        impact_timeframes: wizardData.sentiment.impactTimeframes,
        use_cached_news: wizardData.sentiment.useCachedNews
      } : { enabled: false };

      const fundamentalsConfig = wizardData.fundamentals.enabled ? {
        enabled: true,
        statement_types: wizardData.fundamentals.statementTypes,
        lookback_statements: wizardData.fundamentals.lookbackStatements,
        macro_indicators: wizardData.fundamentals.macroIndicators,
        fundamentals_providers: wizardData.fundamentals.fundamentalsProviders,
        macro_provider: wizardData.fundamentals.macroProvider
      } : { enabled: false };

      if (mode === 'duplicate' && initialData) {
        // Duplicate: POST to /{id}/duplicate
        response = await fetch(`${API_BASE}/datasets/${initialData.id}/duplicate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            new_ticker: wizardData.ticker || undefined,
            new_name: undefined  // Let backend generate name
          }),
        });
      } else if (mode === 'edit' && initialData) {
        // Edit: PUT to /{id}
        response = await fetch(`${API_BASE}/datasets/${initialData.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: wizardData.name || undefined,  // Custom name, or undefined to auto-generate
            ticker: wizardData.ticker,
            timeframe: wizardData.timeframe,
            start_date: wizardData.startDate || undefined,
            end_date: wizardData.endDate || undefined,
            data_provider: wizardData.dataProvider,
            technical_indicators: technicalIndicators,
            sentiment_config: sentimentConfig,
            fundamentals_config: fundamentalsConfig
          }),
        });
      } else if (batchMode && batchSymbols.length > 0) {
        // Batch Create: POST to /batch
        response = await fetch(`${API_BASE}/datasets/batch`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            symbols: batchSymbols,
            name: wizardData.name || undefined,
            timeframe: wizardData.timeframe,
            start_date: wizardData.startDate || undefined,
            end_date: wizardData.endDate || undefined,
            data_provider: wizardData.dataProvider,
            technical_indicators: technicalIndicators,
            sentiment_config: sentimentConfig,
            fundamentals_config: fundamentalsConfig,
            labels: labels.length > 0 ? labels : undefined
          }),
        });
      } else {
        // Single Create: POST to /
        response = await fetch(`${API_BASE}/datasets`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: wizardData.name || undefined,  // Custom name, or undefined to auto-generate
            ticker: wizardData.ticker,
            timeframe: wizardData.timeframe,
            start_date: wizardData.startDate || undefined,
            end_date: wizardData.endDate || undefined,
            data_provider: wizardData.dataProvider,
            technical_indicators: technicalIndicators,
            sentiment_config: sentimentConfig,
            fundamentals_config: fundamentalsConfig,
            labels: labels.length > 0 ? labels : undefined
          }),
        });
      }

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Failed to ${mode} dataset`);
      }

      onComplete();
      onClose();

      // Reset wizard
      setCurrentStep(1);
      setWizardData(getDefaultWizardData());
      setBatchMode(false);
      setBatchSymbolsInput('');
      setBatchSymbols([]);
      setLabels([]);
      setLabelInput('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setIsCreating(false);
    }
  };

  const getActionButtonText = () => {
    if (isCreating) {
      switch (mode) {
        case 'duplicate': return 'Duplicating...';
        case 'edit': return 'Updating...';
        default: return batchMode ? `Creating ${batchSymbols.length} datasets...` : 'Creating...';
      }
    }
    switch (mode) {
      case 'duplicate': return 'Duplicate Dataset';
      case 'edit': return 'Update Dataset';
      default: return batchMode ? `Create ${batchSymbols.length} Datasets` : 'Create Dataset';
    }
  };

  const getModalTitle = () => {
    switch (mode) {
      case 'duplicate': return 'Duplicate Dataset';
      case 'edit': return 'Edit Dataset';
      default: return 'Create New Dataset';
    }
  };

  const getTickerError = (): string | null => {
    if (!wizardData.ticker) return null;
    if (!validateTicker(wizardData.ticker)) {
      return 'Invalid ticker format';
    }
    return null;
  };

  const getDateError = (): string | null => {
    if (wizardData.startDate && wizardData.endDate) {
      const start = new Date(wizardData.startDate);
      const end = new Date(wizardData.endDate);
      if (start >= end) {
        return 'Start date must be before end date';
      }
    }
    return null;
  };

  const tickerError = getTickerError();
  const dateError = getDateError();

  const renderStep1 = () => (
    <div className="space-y-4">
      {/* Batch mode toggle - only in create mode */}
      {mode === 'create' && (
        <div className="flex items-center gap-2 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
          <input
            type="checkbox"
            id="batchMode"
            checked={batchMode}
            onChange={(e) => setBatchMode(e.target.checked)}
            className="rounded border-gray-300 dark:border-gray-600"
          />
          <label htmlFor="batchMode" className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Create multiple datasets (batch mode)
          </label>
        </div>
      )}

      {/* Single ticker input */}
      {!batchMode && (
        <div>
          <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">
            Ticker Symbol <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            value={wizardData.ticker}
            onChange={(e) => setWizardData({ ...wizardData, ticker: e.target.value.toUpperCase() })}
            placeholder="e.g., AAPL, MSFT, GOOGL"
            className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-gray-100 ${
              tickerError
                ? 'border-red-500 focus:ring-red-500'
                : 'border-gray-300 dark:border-gray-600'
            }`}
          />
          {tickerError && (
            <p className="text-xs text-red-500 mt-1">{tickerError}</p>
          )}
        </div>
      )}

      {/* Batch symbols input */}
      {batchMode && (
        <div>
          <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">
            Symbols ({batchSymbols.length} parsed) <span className="text-red-500">*</span>
          </label>
          <div className="flex gap-2">
            <textarea
              value={batchSymbolsInput}
              onChange={(e) => handleBatchSymbolsChange(e.target.value)}
              placeholder="Enter symbols, one per line or comma-separated&#10;e.g.:&#10;AAPL&#10;MSFT&#10;GOOGL"
              rows={5}
              className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <div className="flex flex-col gap-2">
              <label className="px-3 py-2 bg-gray-100 dark:bg-gray-600 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-500 cursor-pointer flex items-center gap-1 text-sm">
                <Upload size={14} />
                Upload .txt
                <input
                  type="file"
                  accept=".txt"
                  onChange={handleBatchFileUpload}
                  className="hidden"
                />
              </label>
            </div>
          </div>
          {batchSymbols.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {batchSymbols.slice(0, 20).map(s => (
                <span key={s} className="px-2 py-0.5 text-xs bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 rounded-full">
                  {s}
                </span>
              ))}
              {batchSymbols.length > 20 && (
                <span className="px-2 py-0.5 text-xs text-gray-500">+{batchSymbols.length - 20} more</span>
              )}
            </div>
          )}
        </div>
      )}

      <div>
        <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">
          {batchMode ? 'Batch Name' : 'Dataset Name'}
        </label>
        <input
          type="text"
          value={wizardData.name}
          onChange={(e) => setWizardData({ ...wizardData, name: e.target.value })}
          placeholder={batchMode ? 'Optional batch name (used in batch label)' : (wizardData.ticker ? `${wizardData.ticker}_${wizardData.timeframe}` : 'Auto-generated from ticker')}
          className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:border-gray-600 dark:text-gray-100"
        />
        <p className="text-xs text-gray-400 dark:text-gray-300 mt-1">
          {batchMode ? 'Used as batch label prefix (e.g., batch-SP500)' : 'Leave empty to auto-generate from ticker and timeframe'}
        </p>
      </div>

      <div>
        <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">Timeframe</label>
        <select
          value={wizardData.timeframe}
          onChange={(e) => setWizardData({ ...wizardData, timeframe: e.target.value })}
          className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:border-gray-600 dark:text-gray-100"
        >
          {TIMEFRAME_ORDER.map(tf => (
            <option key={tf} value={tf}>{TIMEFRAME_LABELS[tf]}</option>
          ))}
        </select>
      </div>

      <div>
        <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">Start Date (optional)</label>
        <input
          type="date"
          value={wizardData.startDate}
          onChange={(e) => setWizardData({ ...wizardData, startDate: e.target.value })}
          className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-gray-100 ${
            dateError
              ? 'border-red-500 focus:ring-red-500'
              : 'border-gray-300 dark:border-gray-600'
          }`}
        />
        <p className="text-xs text-gray-400 dark:text-gray-300 mt-1">Leave empty for 1 year of data</p>
      </div>

      <div>
        <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">End Date (optional)</label>
        <input
          type="date"
          value={wizardData.endDate}
          onChange={(e) => setWizardData({ ...wizardData, endDate: e.target.value })}
          className={`w-full px-3 py-2 border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-gray-100 ${
            dateError
              ? 'border-red-500 focus:ring-red-500'
              : 'border-gray-300 dark:border-gray-600'
          }`}
        />
        {dateError ? (
          <p className="text-xs text-red-500 mt-1">{dateError}</p>
        ) : (
          <p className="text-xs text-gray-400 dark:text-gray-300 mt-1">Leave empty for today</p>
        )}
      </div>

      {/* Labels input */}
      <div>
        <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">Labels</label>
        <div className="flex gap-2">
          <input
            type="text"
            value={labelInput}
            onChange={(e) => setLabelInput(e.target.value)}
            onKeyDown={handleLabelKeyDown}
            onBlur={() => { if (labelInput.trim()) addLabel(labelInput); }}
            placeholder="Type a label and press Enter"
            className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        {labels.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {labels.map(label => (
              <span
                key={label}
                className="inline-flex items-center gap-1 bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 rounded-full px-2 py-0.5 text-xs"
              >
                {label}
                <button
                  onClick={() => removeLabel(label)}
                  className="hover:text-blue-900 dark:hover:text-blue-100"
                >
                  <X size={10} />
                </button>
              </span>
            ))}
          </div>
        )}
        <p className="text-xs text-gray-400 dark:text-gray-300 mt-1">
          Optional tags for organizing datasets. Press Enter or comma to add.
          {batchMode && ' A batch label is auto-added.'}
        </p>
      </div>
    </div>
  );

  const renderStep2 = () => (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground mb-4">
        Select a data provider to fetch historical market data:
      </p>

      <div className="space-y-3 max-h-96 overflow-y-auto">
        {[
          { id: 'yfinance', name: 'Yahoo Finance', desc: 'Free, reliable market data. No API key required.', tags: ['Free', 'No API Key'], recommended: true },
          { id: 'alphavantage', name: 'Alpha Vantage', desc: 'Professional-grade financial data with fundamentals.', tags: ['API Key Required', '500/day'] },
          { id: 'fmp', name: 'Financial Modeling Prep', desc: 'Comprehensive financial data with earnings data.', tags: ['API Key Required', '250/day'] },
          { id: 'alpaca', name: 'Alpaca Markets', desc: 'Real-time and historical market data for trading.', tags: ['API Key Required'] },
        ].map(provider => {
          const isSelected = wizardData.dataProvider === provider.id;
          return (
          <label key={provider.id} className={`block p-4 border-2 rounded-lg cursor-pointer transition-colors ${
            isSelected
              ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30'
              : 'border-gray-200 dark:border-gray-700 hover:border-blue-300'
          }`}>
            <input
              type="radio"
              name="dataProvider"
              value={provider.id}
              checked={isSelected}
              onChange={(e) => setWizardData({ ...wizardData, dataProvider: e.target.value })}
              className="sr-only"
            />
            <div className="font-semibold text-lg mb-1 text-foreground">{provider.name}</div>
            <p className="text-sm text-muted-foreground">{provider.desc}</p>
            <div className="mt-2 flex items-center space-x-2 text-xs">
              {provider.tags.map(tag => (
                <span key={tag} className={`px-2 py-1 rounded ${tag.includes('Free') ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' : 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200'}`}>
                  {tag}
                </span>
              ))}
              {provider.recommended && <span className="text-muted-foreground">Recommended</span>}
            </div>
          </label>
        );})}
      </div>

    </div>
  );

  const renderStep3 = () => {
    const availableTimeframes = getAvailableTimeframes();
    const selectedTypeInfo = INDICATOR_TYPES.find(t => t.type === newIndicatorType);

    return (
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground mb-2">
          Add technical indicators with individual timeframes. Indicator timeframe must be equal or greater than the dataset timeframe ({wizardData.timeframe}).
        </p>

        {/* Collection load/save controls */}
        <div className="flex items-center gap-2 pb-2 border-b border-gray-200 dark:border-gray-700">
          <button
            onClick={() => {
              setShowCollectionPicker(true);
              setSelectedIndicatorIds(new Set());
              setExpandedCollections(new Set());
            }}
            className="flex-1 flex items-center justify-center gap-2 px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200"
          >
            <FolderOpen className="w-4 h-4" />
            Load from Collection
          </button>
          <button
            onClick={() => setShowSaveDialog(true)}
            disabled={wizardData.indicators.length === 0}
            className="flex items-center gap-1 px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed text-gray-700 dark:text-gray-200"
          >
            <Save className="w-4 h-4" />
            Save
          </button>
        </div>

        {/* Save collection dialog */}
        {showSaveDialog && (
          <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg space-y-2">
            <input
              type="text"
              value={saveCollectionName}
              onChange={(e) => setSaveCollectionName(e.target.value)}
              placeholder="Collection name..."
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm dark:bg-gray-700 dark:text-gray-100"
            />
            <div className="flex gap-2">
              <button
                onClick={saveCollection}
                className="px-3 py-1 bg-blue-500 text-white text-sm rounded-md hover:bg-blue-600"
              >
                Save Collection
              </button>
              <button
                onClick={() => setShowSaveDialog(false)}
                className="px-3 py-1 text-gray-600 dark:text-gray-300 text-sm hover:bg-gray-200 dark:hover:bg-gray-600 rounded-md"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Collection Picker Modal */}
        {showCollectionPicker && (
          <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-3xl mx-4 max-h-[80vh] flex flex-col">
              <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
                <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                  Select Indicators from Collections
                </h3>
                <button
                  onClick={() => setShowCollectionPicker(false)}
                  className="text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="flex-1 overflow-y-auto p-4 space-y-2">
                {collections.length === 0 ? (
                  <p className="text-center text-gray-500 dark:text-gray-400 py-8">No collections available</p>
                ) : (
                  collections.map(collection => {
                    const isExpanded = expandedCollections.has(collection.id);
                    const selectedCount = getCollectionSelectedCount(collection);
                    const validIndicatorsCount = collection.indicators.filter(
                      ind => availableTimeframes.includes(ind.timeframe)
                    ).length;

                    return (
                      <div
                        key={collection.id}
                        className={`border rounded-lg ${
                          collection.is_default
                            ? 'border-purple-200 dark:border-purple-800'
                            : 'border-gray-200 dark:border-gray-700'
                        }`}
                      >
                        {/* Collection Header */}
                        <div
                          className={`p-3 flex items-center justify-between cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50 rounded-t-lg ${
                            isExpanded ? '' : 'rounded-b-lg'
                          }`}
                          onClick={() => toggleCollectionExpanded(collection.id)}
                        >
                          <div className="flex items-center gap-3">
                            {isExpanded ? (
                              <ChevronDown className="w-4 h-4 text-gray-500" />
                            ) : (
                              <ChevronRight className="w-4 h-4 text-gray-500" />
                            )}
                            <div>
                              <div className="flex items-center gap-2">
                                <span className="font-medium text-gray-900 dark:text-gray-100">
                                  {collection.name}
                                </span>
                                {collection.is_default && (
                                  <span className="px-1.5 py-0.5 text-xs bg-purple-100 dark:bg-purple-900/50 text-purple-700 dark:text-purple-300 rounded">
                                    Default
                                  </span>
                                )}
                              </div>
                              <p className="text-xs text-gray-500 dark:text-gray-400">
                                {validIndicatorsCount} indicators available
                                {validIndicatorsCount < collection.indicators.length && (
                                  <span className="text-yellow-600 dark:text-yellow-400 ml-1">
                                    ({collection.indicators.length - validIndicatorsCount} incompatible)
                                  </span>
                                )}
                              </p>
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            {selectedCount > 0 && (
                              <span className="px-2 py-0.5 text-xs bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 rounded">
                                {selectedCount} selected
                              </span>
                            )}
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                const allSelected = selectedCount === validIndicatorsCount;
                                toggleAllCollectionIndicators(collection, !allSelected);
                              }}
                              className="px-2 py-1 text-xs text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded"
                            >
                              {selectedCount === validIndicatorsCount ? 'Deselect All' : 'Select All'}
                            </button>
                          </div>
                        </div>

                        {/* Expanded Indicators List */}
                        {isExpanded && (
                          <div className="border-t border-gray-200 dark:border-gray-700 p-3 bg-gray-50 dark:bg-gray-700/30 rounded-b-lg">
                            <div className="max-h-48 overflow-y-auto space-y-1">
                              {collection.indicators.map((indicator, idx) => {
                                const key = getIndicatorUniqueKey(collection.id, idx);
                                const isSelected = selectedIndicatorIds.has(key);
                                const isValidTimeframe = availableTimeframes.includes(indicator.timeframe);

                                return (
                                  <label
                                    key={idx}
                                    className={`flex items-center gap-3 p-2 rounded cursor-pointer ${
                                      !isValidTimeframe
                                        ? 'opacity-40 cursor-not-allowed'
                                        : isSelected
                                        ? 'bg-blue-50 dark:bg-blue-900/30'
                                        : 'hover:bg-gray-100 dark:hover:bg-gray-600/50'
                                    }`}
                                  >
                                    <input
                                      type="checkbox"
                                      checked={isSelected}
                                      disabled={!isValidTimeframe}
                                      onChange={() => toggleIndicatorSelection(collection.id, idx)}
                                      className="w-4 h-4 rounded border-gray-300 dark:border-gray-600 text-blue-500 focus:ring-blue-500"
                                    />
                                    <div className="flex-1 flex items-center justify-between">
                                      <div className="flex items-center gap-2">
                                        <span className="font-mono text-xs px-1.5 py-0.5 bg-gray-200 dark:bg-gray-600 rounded text-gray-700 dark:text-gray-300">
                                          {indicator.type}
                                        </span>
                                        <span className="text-sm text-gray-900 dark:text-gray-100">
                                          {indicator.name}
                                        </span>
                                      </div>
                                      <div className="flex items-center gap-2 text-xs">
                                        <span className={`px-1.5 py-0.5 rounded ${
                                          isValidTimeframe
                                            ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300'
                                            : 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
                                        }`}>
                                          {indicator.timeframe?.toUpperCase() || 'N/A'}
                                        </span>
                                        <span className="text-gray-500 dark:text-gray-400 font-mono">
                                          {getIndicatorParamsString(indicator)}
                                        </span>
                                      </div>
                                    </div>
                                  </label>
                                );
                              })}
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>

              <div className="p-4 border-t border-gray-200 dark:border-gray-700 flex justify-between items-center">
                <span className="text-sm text-gray-600 dark:text-gray-400">
                  {selectedIndicatorIds.size} indicators selected
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setShowCollectionPicker(false)}
                    className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={addSelectedIndicatorsFromPicker}
                    disabled={selectedIndicatorIds.size === 0}
                    className="px-4 py-2 bg-blue-500 text-white rounded-md hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Add Selected
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Add indicator form */}
        <div className="p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg space-y-3">
          <div className="font-medium text-sm text-gray-700 dark:text-gray-200">Add Indicator</div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">Type</label>
              <select
                value={newIndicatorType}
                onChange={(e) => {
                  setNewIndicatorType(e.target.value);
                  const typeInfo = INDICATOR_TYPES.find(t => t.type === e.target.value);
                  if (typeInfo?.defaultPeriod) {
                    setNewIndicatorPeriod(typeInfo.defaultPeriod);
                  }
                }}
                className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded text-sm dark:bg-gray-700 dark:text-gray-100"
              >
                {INDICATOR_TYPES.map(type => (
                  <option key={type.type} value={type.type}>{type.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">Timeframe</label>
              <select
                value={newIndicatorTimeframe}
                onChange={(e) => setNewIndicatorTimeframe(e.target.value)}
                className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded text-sm dark:bg-gray-700 dark:text-gray-100"
              >
                {availableTimeframes.map(tf => (
                  <option key={tf} value={tf}>{TIMEFRAME_LABELS[tf]}</option>
                ))}
              </select>
            </div>
            <div>
              {selectedTypeInfo?.hasPeriod && (
                <>
                  <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">Period</label>
                  <input
                    type="number"
                    min="1"
                    max="500"
                    value={newIndicatorPeriod}
                    onChange={(e) => setNewIndicatorPeriod(parseInt(e.target.value) || 1)}
                    className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded text-sm dark:bg-gray-700 dark:text-gray-100"
                  />
                </>
              )}
            </div>
          </div>

          {/* SAR parameters */}
          {newIndicatorType === 'sar' && (
            <div className="grid grid-cols-2 gap-3 mt-2">
              <div>
                <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">AF Start</label>
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  max="0.5"
                  value={sarAfStart}
                  onChange={(e) => setSarAfStart(parseFloat(e.target.value) || 0.02)}
                  className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded text-sm dark:bg-gray-700 dark:text-gray-100"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">AF Max</label>
                <input
                  type="number"
                  step="0.01"
                  min="0.1"
                  max="1.0"
                  value={sarAfMax}
                  onChange={(e) => setSarAfMax(parseFloat(e.target.value) || 0.2)}
                  className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded text-sm dark:bg-gray-700 dark:text-gray-100"
                />
              </div>
            </div>
          )}

          {/* ZigZag parameters */}
          {newIndicatorType === 'zigzag' && (
            <div className="mt-2">
              <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">Deviation %</label>
              <input
                type="number"
                step="0.5"
                min="0.5"
                max="20"
                value={zigzagDeviation}
                onChange={(e) => setZigzagDeviation(parseFloat(e.target.value) || 5.0)}
                className="w-32 px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded text-sm dark:bg-gray-700 dark:text-gray-100"
              />
            </div>
          )}

          {/* Pivot Points parameters */}
          {newIndicatorType === 'pivot_points' && (
            <div className="mt-2">
              <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">Method</label>
              <select
                value={pivotMethod}
                onChange={(e) => setPivotMethod(e.target.value)}
                className="w-full px-2 py-1.5 border border-gray-300 dark:border-gray-600 rounded text-sm dark:bg-gray-700 dark:text-gray-100"
              >
                <option value="standard">Standard</option>
                <option value="fibonacci">Fibonacci</option>
                <option value="woodie">Woodie</option>
                <option value="camarilla">Camarilla</option>
              </select>
            </div>
          )}

          <button
            onClick={addIndicator}
            className="flex items-center gap-1 px-3 py-1.5 bg-blue-500 text-white text-sm rounded-md hover:bg-blue-600"
          >
            <Plus className="w-4 h-4" />
            Add Indicator
          </button>
        </div>

        {/* List of added indicators */}
        <div className="space-y-2 max-h-48 overflow-y-auto">
          {wizardData.indicators.length === 0 ? (
            <div className="text-center py-6 text-gray-400 dark:text-gray-500 text-sm">
              No indicators added yet. Use the form above to add indicators.
            </div>
          ) : (
            wizardData.indicators.map((indicator) => (
              <div
                key={indicator.id}
                className="flex items-center justify-between p-3 border border-gray-200 dark:border-gray-700 rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <TrendingUp className="w-4 h-4 text-blue-500" />
                  <div>
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                      {getIndicatorDisplayName(indicator)}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => removeIndicator(indicator.id)}
                  className="p-1 text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="bg-blue-50 dark:bg-gray-700 p-3 rounded-md">
          <p className="text-sm text-blue-800 dark:text-gray-100">
            Added: <strong>{wizardData.indicators.length}</strong> indicators
          </p>
        </div>
      </div>
    );
  };

  const renderStep4 = () => (
    <div className="space-y-4">
      <div className="flex items-center justify-between p-4 border border-gray-200 dark:border-gray-700 rounded-lg">
        <div className="flex items-center gap-3">
          <MessageSquare className="w-6 h-6 text-purple-500" />
          <div>
            <h3 className="font-semibold text-gray-900 dark:text-gray-100">Enable Sentiment Analysis</h3>
            <p className="text-sm text-gray-500 dark:text-gray-300">Analyze news sentiment for the ticker</p>
          </div>
        </div>
        <label className="relative inline-flex items-center cursor-pointer">
          <input
            type="checkbox"
            checked={wizardData.sentiment.enabled}
            onChange={(e) => setWizardData({
              ...wizardData,
              sentiment: { ...wizardData.sentiment, enabled: e.target.checked }
            })}
            className="sr-only peer"
          />
          <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-700 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
        </label>
      </div>

      {wizardData.sentiment.enabled && (
        <div className="space-y-4 p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
          {/* Use Cached News toggle */}
          <div className="flex items-center justify-between p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-200 dark:border-blue-800">
            <div>
              <h4 className="text-sm font-medium text-blue-800 dark:text-blue-200">Use Cached News</h4>
              <p className="text-xs text-blue-600 dark:text-blue-400">
                Use pre-fetched news from the cache (Tools &gt; News Batch Fetch) instead of calling the API.
                Faster and doesn't consume API credits.
              </p>
            </div>
            <label className="relative inline-flex items-center cursor-pointer ml-4 flex-shrink-0">
              <input
                type="checkbox"
                checked={wizardData.sentiment.useCachedNews}
                onChange={(e) => setWizardData({
                  ...wizardData,
                  sentiment: { ...wizardData.sentiment, useCachedNews: e.target.checked }
                })}
                className="sr-only peer"
              />
              <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-700 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
            </label>
          </div>

          {/* News Provider Selection (always shown) */}
          <div>
            <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">
              {wizardData.sentiment.useCachedNews ? 'Cached News Providers' : 'API News Sources'}
            </label>
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
              {wizardData.sentiment.useCachedNews
                ? 'Select which providers to load cached articles from'
                : 'Fetch live news from providers'}
            </p>
            <div className="flex flex-wrap gap-2">
              {[
                { id: 'fmp_news', label: 'FMP News' },
                { id: 'alpaca_news', label: 'Alpaca News' },
                { id: 'finnhub_news', label: 'Finnhub News' },
                { id: 'alphavantage_news', label: 'AlphaVantage News' }
              ].map(source => (
                <label key={source.id} className={`px-3 py-2 rounded-lg cursor-pointer border text-sm ${
                  wizardData.sentiment.newsSources.includes(source.id)
                    ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/30 text-purple-800 dark:text-purple-200'
                    : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300'
                }`}>
                  <input
                    type="checkbox"
                    checked={wizardData.sentiment.newsSources.includes(source.id)}
                    onChange={(e) => {
                      const newSources = e.target.checked
                        ? [...wizardData.sentiment.newsSources, source.id]
                        : wizardData.sentiment.newsSources.filter(s => s !== source.id);
                      setWizardData({
                        ...wizardData,
                        sentiment: { ...wizardData.sentiment, newsSources: newSources }
                      });
                    }}
                    className="sr-only"
                  />
                  <span>{source.label}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Local Files News Sources (hidden when using cached news) */}
          {!wizardData.sentiment.useCachedNews && (
          <div className="pt-4 border-t border-gray-200 dark:border-gray-600">
            <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">Local Files (Cached News)</label>
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">Import previously exported news from local JSON files</p>
            <div className="flex flex-wrap gap-2">
              <label className={`px-3 py-2 rounded-lg cursor-pointer border text-sm ${
                wizardData.sentiment.newsSources.includes('localfiles_company')
                  ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/30 text-purple-800 dark:text-purple-200'
                  : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300'
              }`}>
                <input
                  type="checkbox"
                  checked={wizardData.sentiment.newsSources.includes('localfiles_company')}
                  onChange={(e) => {
                    const newSources = e.target.checked
                      ? [...wizardData.sentiment.newsSources, 'localfiles_company']
                      : wizardData.sentiment.newsSources.filter(s => s !== 'localfiles_company');
                    setWizardData({
                      ...wizardData,
                      sentiment: { ...wizardData.sentiment, newsSources: newSources }
                    });
                  }}
                  className="sr-only"
                />
                <span>Company News (Local)</span>
              </label>
              <label className={`px-3 py-2 rounded-lg cursor-pointer border text-sm ${
                wizardData.sentiment.newsSources.includes('localfiles_global')
                  ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/30 text-purple-800 dark:text-purple-200'
                  : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300'
              }`}>
                <input
                  type="checkbox"
                  checked={wizardData.sentiment.newsSources.includes('localfiles_global')}
                  onChange={(e) => {
                    const newSources = e.target.checked
                      ? [...wizardData.sentiment.newsSources, 'localfiles_global']
                      : wizardData.sentiment.newsSources.filter(s => s !== 'localfiles_global');
                    setWizardData({
                      ...wizardData,
                      sentiment: { ...wizardData.sentiment, newsSources: newSources }
                    });
                  }}
                  className="sr-only"
                />
                <span>Global News (Local)</span>
              </label>
            </div>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
              Export news from the Tools page first, then import here
            </p>
          </div>
          )}

          <div>
            <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">Lookback Periods</label>
            <div className="flex flex-wrap gap-2">
              {['1d', '1w', '1m', '6m'].map(period => (
                <label key={period} className={`px-3 py-2 rounded-lg cursor-pointer border text-sm ${
                  wizardData.sentiment.lookbackPeriods.includes(period)
                    ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/30 text-purple-800 dark:text-purple-200'
                    : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300'
                }`}>
                  <input
                    type="checkbox"
                    checked={wizardData.sentiment.lookbackPeriods.includes(period)}
                    onChange={(e) => {
                      const newPeriods = e.target.checked
                        ? [...wizardData.sentiment.lookbackPeriods, period]
                        : wizardData.sentiment.lookbackPeriods.filter(p => p !== period);
                      setWizardData({
                        ...wizardData,
                        sentiment: { ...wizardData.sentiment, lookbackPeriods: newPeriods }
                      });
                    }}
                    className="sr-only"
                  />
                  <span>{period}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="bg-purple-50 dark:bg-gray-700 p-3 rounded-md">
            <p className="text-sm text-purple-800 dark:text-gray-100">
              Sentiment features like <code className="bg-purple-100 dark:bg-gray-600 px-1 rounded">news_1d_positive_short</code>,
              <code className="bg-purple-100 dark:bg-gray-600 px-1 rounded ml-1">news_1w_negative_long</code> will be generated.
            </p>
          </div>
        </div>
      )}
    </div>
  );

  const renderStep5 = () => (
    <div className="space-y-4">
      <div className="flex items-center justify-between p-4 border border-gray-200 dark:border-gray-700 rounded-lg">
        <div className="flex items-center gap-3">
          <BarChart3 className="w-6 h-6 text-green-500" />
          <div>
            <h3 className="font-semibold text-gray-900 dark:text-gray-100">Enable Fundamentals & Macro Data</h3>
            <p className="text-sm text-gray-500 dark:text-gray-300">Add company fundamentals and macroeconomic indicators</p>
          </div>
        </div>
        <label className="relative inline-flex items-center cursor-pointer">
          <input
            type="checkbox"
            checked={wizardData.fundamentals.enabled}
            onChange={(e) => setWizardData({
              ...wizardData,
              fundamentals: { ...wizardData.fundamentals, enabled: e.target.checked }
            })}
            className="sr-only peer"
          />
          <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 rounded-full peer dark:bg-gray-700 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all dark:border-gray-600 peer-checked:bg-blue-600"></div>
        </label>
      </div>

      {wizardData.fundamentals.enabled && (
        <div className="space-y-4 p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
          <div>
            <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">Financial Statements</label>
            <div className="grid grid-cols-2 gap-2">
              {[
                { id: 'balance_sheet', label: 'Balance Sheet', desc: 'Assets, liabilities, equity' },
                { id: 'income_statement', label: 'Income Statement', desc: 'Revenue, expenses, profit' },
                { id: 'cash_flow', label: 'Cash Flow Statement', desc: 'Operating, investing, financing' },
                { id: 'earnings', label: 'Earnings History', desc: 'EPS, estimates, surprises' }
              ].map(stmt => (
                <label key={stmt.id} className={`p-3 rounded-lg cursor-pointer border text-sm ${
                  wizardData.fundamentals.statementTypes.includes(stmt.id)
                    ? 'border-green-500 bg-green-50 dark:bg-green-900/30 text-green-800 dark:text-green-200'
                    : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300'
                }`}>
                  <input
                    type="checkbox"
                    checked={wizardData.fundamentals.statementTypes.includes(stmt.id)}
                    onChange={(e) => {
                      const newTypes = e.target.checked
                        ? [...wizardData.fundamentals.statementTypes, stmt.id]
                        : wizardData.fundamentals.statementTypes.filter(t => t !== stmt.id);
                      setWizardData({
                        ...wizardData,
                        fundamentals: { ...wizardData.fundamentals, statementTypes: newTypes }
                      });
                    }}
                    className="sr-only"
                  />
                  <div className="font-medium">{stmt.label}</div>
                  <div className="text-xs opacity-70">{stmt.desc}</div>
                </label>
              ))}
            </div>
          </div>

          {/* Lookback Statements */}
          <div>
            <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">
              Historical Statement Periods
            </label>
            <div className="flex items-center gap-3">
              <input
                type="number"
                min={1}
                max={8}
                value={wizardData.fundamentals.lookbackStatements}
                onChange={(e) => setWizardData({
                  ...wizardData,
                  fundamentals: { ...wizardData.fundamentals, lookbackStatements: parseInt(e.target.value) || 2 }
                })}
                className="w-20 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
              <span className="text-sm text-gray-600 dark:text-gray-400">
                Include the last {wizardData.fundamentals.lookbackStatements} {wizardData.fundamentals.lookbackStatements === 1 ? 'period' : 'periods'} of each statement type in each data point
              </span>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              For quarterly statements, 2 periods = last 2 quarters. Useful for showing trends.
              Data is fetched before the dataset start date to ensure first bars have lookback data.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">Macro Economic Indicators</label>
            <div className="grid grid-cols-2 gap-2">
              {[
                { id: 'interest_rate', label: 'Interest Rates' },
                { id: 'gdp', label: 'GDP Growth' },
                { id: 'inflation', label: 'Inflation Rate (CPI)' },
                { id: 'unemployment', label: 'Unemployment Rate' }
              ].map(indicator => (
                <label key={indicator.id} className={`p-3 rounded-lg cursor-pointer border text-sm ${
                  wizardData.fundamentals.macroIndicators.includes(indicator.id)
                    ? 'border-green-500 bg-green-50 dark:bg-green-900/30 text-green-800 dark:text-green-200'
                    : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300'
                }`}>
                  <input
                    type="checkbox"
                    checked={wizardData.fundamentals.macroIndicators.includes(indicator.id)}
                    onChange={(e) => {
                      const newIndicators = e.target.checked
                        ? [...wizardData.fundamentals.macroIndicators, indicator.id]
                        : wizardData.fundamentals.macroIndicators.filter(i => i !== indicator.id);
                      setWizardData({
                        ...wizardData,
                        fundamentals: { ...wizardData.fundamentals, macroIndicators: newIndicators }
                      });
                    }}
                    className="sr-only"
                  />
                  <span>{indicator.label}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Data Provider Priority Selection */}
          <div className="pt-4 border-t border-gray-200 dark:border-gray-600">
            <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-200">
              Fundamentals Providers (Priority Order)
            </label>
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
              Click to toggle. First enabled provider has highest priority for overlapping data.
            </p>
            <div className="space-y-2">
              {[
                { id: 'yfinance', name: 'Yahoo Finance', desc: 'Free, no API key required' },
                { id: 'fmp', name: 'Financial Modeling Prep', desc: 'Comprehensive data, API key required' },
                { id: 'alphavantage', name: 'Alpha Vantage', desc: 'Professional grade, API key required' }
              ].map((provider) => {
                const isEnabled = wizardData.fundamentals.fundamentalsProviders.includes(provider.id);
                const priorityIndex = wizardData.fundamentals.fundamentalsProviders.indexOf(provider.id);
                return (
                  <div
                    key={provider.id}
                    className={`flex items-center justify-between p-3 rounded-lg border cursor-pointer ${
                      isEnabled
                        ? 'border-green-500 bg-green-50 dark:bg-green-900/30'
                        : 'border-gray-300 dark:border-gray-600 opacity-50'
                    }`}
                    onClick={() => {
                      const newProviders = isEnabled
                        ? wizardData.fundamentals.fundamentalsProviders.filter(p => p !== provider.id)
                        : [...wizardData.fundamentals.fundamentalsProviders, provider.id];
                      setWizardData({
                        ...wizardData,
                        fundamentals: { ...wizardData.fundamentals, fundamentalsProviders: newProviders }
                      });
                    }}
                  >
                    <div className="flex items-center gap-3">
                      {isEnabled && (
                        <span className="flex items-center justify-center w-6 h-6 rounded-full bg-green-500 text-white text-xs font-bold">
                          {priorityIndex + 1}
                        </span>
                      )}
                      <div>
                        <div className={`font-medium ${isEnabled ? 'text-green-800 dark:text-green-200' : 'text-gray-600 dark:text-gray-400'}`}>
                          {provider.name}
                        </div>
                        <div className="text-xs opacity-70">{provider.desc}</div>
                      </div>
                    </div>
                    <div className={`w-4 h-4 rounded border-2 ${isEnabled ? 'bg-green-500 border-green-500' : 'border-gray-400'}`}>
                      {isEnabled && (
                        <svg className="w-3 h-3 text-white" fill="currentColor" viewBox="0 0 20 20">
                          <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                        </svg>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="mt-4">
              <label className="block text-xs text-gray-500 dark:text-gray-300 mb-1">Macro Data Provider</label>
              <select
                value={wizardData.fundamentals.macroProvider}
                onChange={(e) => setWizardData({
                  ...wizardData,
                  fundamentals: { ...wizardData.fundamentals, macroProvider: e.target.value }
                })}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm dark:bg-gray-700 dark:text-gray-100"
              >
                <option value="fred">FRED (Federal Reserve)</option>
                <option value="alphavantage">Alpha Vantage (API Key)</option>
              </select>
            </div>
          </div>

          <div className="bg-green-50 dark:bg-gray-700 p-3 rounded-md">
            <p className="text-sm text-green-800 dark:text-gray-100">
              Financial statement data will be fetched from providers in priority order.
              If a provider fails or lacks data, the next provider will be used.
            </p>
          </div>
        </div>
      )}
    </div>
  );

  const renderStep6 = () => (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground mb-4">
        Review your dataset configuration:
      </p>
      <div className="bg-gray-50 dark:bg-gray-700 p-4 rounded-md space-y-3 max-h-96 overflow-y-auto">
        <div className="flex justify-between py-2 border-b border-gray-200 dark:border-gray-600">
          <span className="font-medium text-gray-700 dark:text-gray-200">Ticker:</span>
          <span className="font-mono text-gray-900 dark:text-gray-100">{wizardData.ticker}</span>
        </div>
        <div className="flex justify-between py-2 border-b border-gray-200 dark:border-gray-600">
          <span className="font-medium text-gray-700 dark:text-gray-200">Timeframe:</span>
          <span className="text-gray-900 dark:text-gray-100">{TIMEFRAME_LABELS[wizardData.timeframe]}</span>
        </div>
        <div className="flex justify-between py-2 border-b border-gray-200 dark:border-gray-600">
          <span className="font-medium text-gray-700 dark:text-gray-200">Data Provider:</span>
          <span className="capitalize text-gray-900 dark:text-gray-100">{wizardData.dataProvider}</span>
        </div>
        <div className="flex justify-between py-2 border-b border-gray-200 dark:border-gray-600">
          <span className="font-medium text-gray-700 dark:text-gray-200">Date Range:</span>
          <span className="text-gray-900 dark:text-gray-100">{wizardData.startDate || '1 year ago'} - {wizardData.endDate || 'Today'}</span>
        </div>

        <div className="py-2 border-b border-gray-200 dark:border-gray-600">
          <div className="flex justify-between items-center">
            <span className="font-medium flex items-center gap-2 text-gray-700 dark:text-gray-200">
              <TrendingUp className="w-4 h-4 text-blue-500" />
              Technical Indicators:
            </span>
            <span className="text-gray-900 dark:text-gray-100">{wizardData.indicators.length} selected</span>
          </div>
          {wizardData.indicators.length > 0 && (
            <div className="mt-2 text-sm text-gray-600 dark:text-gray-300 space-y-1">
              {wizardData.indicators.map(indicator => (
                <div key={indicator.id} className="pl-6">
                  {getIndicatorDisplayName(indicator)}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="py-2 border-b border-gray-200 dark:border-gray-600">
          <div className="flex justify-between items-center">
            <span className="font-medium flex items-center gap-2 text-gray-700 dark:text-gray-200">
              <MessageSquare className="w-4 h-4 text-purple-500" />
              Sentiment Analysis:
            </span>
            <span className={wizardData.sentiment.enabled ? 'text-green-600 dark:text-green-400' : 'text-gray-500 dark:text-gray-300'}>
              {wizardData.sentiment.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          {wizardData.sentiment.enabled && (
            <div className="mt-2 text-sm text-gray-600 dark:text-gray-300 pl-6">
              <div>Sources: {wizardData.sentiment.newsSources.join(', ')}</div>
              <div>Periods: {wizardData.sentiment.lookbackPeriods.join(', ')}</div>
            </div>
          )}
        </div>

        <div className="py-2">
          <div className="flex justify-between items-center">
            <span className="font-medium flex items-center gap-2 text-gray-700 dark:text-gray-200">
              <BarChart3 className="w-4 h-4 text-green-500" />
              Fundamentals & Macro:
            </span>
            <span className={wizardData.fundamentals.enabled ? 'text-green-600 dark:text-green-400' : 'text-gray-500 dark:text-gray-300'}>
              {wizardData.fundamentals.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          {wizardData.fundamentals.enabled && (
            <div className="mt-2 text-sm text-gray-600 dark:text-gray-300 pl-6">
              <div>Statements: {wizardData.fundamentals.statementTypes.join(', ')}</div>
              <div>Lookback: {wizardData.fundamentals.lookbackStatements} period{wizardData.fundamentals.lookbackStatements !== 1 ? 's' : ''}</div>
              <div>Providers: {wizardData.fundamentals.fundamentalsProviders.join(' → ')}</div>
              <div>Macro: {wizardData.fundamentals.macroIndicators.join(', ')}</div>
            </div>
          )}
        </div>
      </div>

      <div className="bg-blue-50 dark:bg-gray-700 p-3 rounded-md">
        <p className="text-sm text-blue-800 dark:text-gray-100">
          Click "Create Dataset" to fetch data and build your dataset with all configured features.
        </p>
      </div>
    </div>
  );

  const steps = [
    { num: 1, label: 'Ticker', icon: FileText },
    { num: 2, label: 'Provider', icon: Settings },
    { num: 3, label: 'Indicators', icon: TrendingUp },
    { num: 4, label: 'Sentiment', icon: MessageSquare },
    { num: 5, label: 'Fundamentals', icon: BarChart3 },
    { num: 6, label: 'Review', icon: CheckCircle }
  ];

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-2xl mx-4 max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">{getModalTitle()}</h2>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
          >
            <X size={24} />
          </button>
        </div>

        {/* Steps indicator */}
        <div className="px-6 pt-4 pb-2 overflow-x-auto">
          <div className="flex items-center justify-between min-w-max">
            {steps.map((step, index) => (
              <React.Fragment key={step.num}>
                <div className="flex flex-col items-center">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center ${
                    currentStep >= step.num ? 'bg-blue-500 text-white' : 'bg-gray-300 dark:bg-gray-600 text-gray-600 dark:text-gray-300'
                  }`}>
                    <step.icon className="w-4 h-4" />
                  </div>
                  <span className={`text-xs mt-1 ${currentStep >= step.num ? 'font-medium text-gray-900 dark:text-gray-100' : 'text-gray-500 dark:text-gray-300'}`}>
                    {step.label}
                  </span>
                </div>
                {index < steps.length - 1 && (
                  <div className={`flex-1 h-0.5 mx-2 ${currentStep > step.num ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-600'}`}></div>
                )}
              </React.Fragment>
            ))}
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {error && (
            <div className="mb-4 p-3 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-200 rounded-md">
              {error}
            </div>
          )}

          {currentStep === 1 && renderStep1()}
          {currentStep === 2 && renderStep2()}
          {currentStep === 3 && renderStep3()}
          {currentStep === 4 && renderStep4()}
          {currentStep === 5 && renderStep5()}
          {currentStep === 6 && renderStep6()}
        </div>

        {/* Footer */}
        <div className="flex justify-between p-6 border-t border-gray-200 dark:border-gray-700">
          <button
            onClick={currentStep === 1 ? onClose : handleBack}
            className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md"
            disabled={isCreating}
          >
            {currentStep === 1 ? 'Cancel' : 'Back'}
          </button>
          <button
            onClick={currentStep === 6 ? handleCreate : handleNext}
            className="px-4 py-2 bg-blue-500 text-white rounded-md hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={isCreating}
          >
            {currentStep === 6 ? getActionButtonText() : 'Next'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default DatasetWizard;
