import { API_BASE } from '../lib/config';
import React, { useState, useEffect } from 'react';
import { Wrench, Newspaper, Search, Loader, CheckCircle, XCircle, AlertCircle, MessageSquare, Download, DollarSign, TrendingUp, Trash2, HardDrive, Database, Upload, ChevronDown, ChevronRight } from 'lucide-react';
import ConfirmDialog from '../components/ConfirmDialog';

interface NewsArticle {
  title: string;
  summary?: string;
  content?: string;
  source?: string;
  url?: string;
  date?: string;
  published_at?: string;
  // Provider-specific sentiment fields (e.g., from Alpha Vantage)
  sentiment?: string;
  sentiment_score?: number;
}

interface SentimentResult {
  sentiment: 'positive' | 'neutral' | 'negative';
  sentiment_score: number;
  confidence: number;
}

interface NewsProvider {
  id: string;
  name: string;
  description: string;
  api_key_configured: boolean;
  has_sentiment?: boolean;
}

const Tools: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'news' | 'fundamentals' | 'macro' | 'maintenance' | 'ohlcv' | 'newsbatch'>('news');

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
          <Wrench size={32} />
          Tools
        </h1>
        <p className="text-gray-600 dark:text-gray-400 mt-1">
          Testing and debugging utilities
        </p>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 dark:border-gray-700 mb-6">
        <nav className="flex space-x-4">
          <button
            onClick={() => setActiveTab('news')}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'news'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            <div className="flex items-center gap-2">
              <Newspaper size={16} />
              News Providers
            </div>
          </button>
          <button
            onClick={() => setActiveTab('fundamentals')}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'fundamentals'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            <div className="flex items-center gap-2">
              <DollarSign size={16} />
              Fundamentals
            </div>
          </button>
          <button
            onClick={() => setActiveTab('macro')}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'macro'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            <div className="flex items-center gap-2">
              <TrendingUp size={16} />
              Macro Indicators
            </div>
          </button>
          <button
            onClick={() => setActiveTab('ohlcv')}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'ohlcv'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            <div className="flex items-center gap-2">
              <Database size={16} />
              OHLCV Data
            </div>
          </button>
          <button
            onClick={() => setActiveTab('newsbatch')}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'newsbatch'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            News Batch Fetch
          </button>
          <button
            onClick={() => setActiveTab('maintenance')}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === 'maintenance'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}
          >
            <div className="flex items-center gap-2">
              <HardDrive size={16} />
              Maintenance
            </div>
          </button>
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'news' && <NewsProviderTester />}
      {activeTab === 'fundamentals' && <FundamentalsTester />}
      {activeTab === 'macro' && <MacroTester />}
      {activeTab === 'ohlcv' && <OHLCVCacheTool />}
      {activeTab === 'newsbatch' && <NewsBatchFetchTool />}
      {activeTab === 'maintenance' && <MaintenancePanel />}
    </div>
  );
};

// OHLCV Cache Tool Component
interface OHLCVProvider {
  id: string;
  name: string;
  description: string;
  available: boolean;
}

interface CacheFile {
  provider?: string;
  symbol: string;
  interval: string;
  file_size_mb: number;
  last_modified: string;
  rows: number;
  date_from?: string;
  date_to?: string;
  filename: string;
}

interface GapInfo {
  gap_start: string;
  gap_end: string;
  gap_days: number;
}

interface GapResult {
  provider: string;
  symbol: string;
  interval: string;
  filename: string;
  rows: number;
  date_from: string | null;
  date_to: string | null;
  gap_count: number;
  gaps: GapInfo[];
  has_gaps: boolean;
}

interface GapReport {
  results: GapResult[];
  total_files: number;
  files_with_gaps: number;
  files_without_gaps: number;
  total_gaps: number;
}

interface FetchTask {
  symbol: string;
  task_id: string;
  status?: string;
  progress?: number;
  progress_message?: string;
}

const OHLCVCacheTool: React.FC = () => {
  const [provider, setProvider] = useState('yfinance');
  const [providers, setProviders] = useState<OHLCVProvider[]>([]);
  const [symbolInput, setSymbolInput] = useState('');
  const [symbols, setSymbols] = useState<string[]>([]);
  const [timeframes, setTimeframes] = useState<string[]>(['1d']);
  const [parallelJobs, setParallelJobs] = useState(3);
  const [executorWorkers, setExecutorWorkers] = useState(5);
  const [fetching, setFetching] = useState(false);
  const [tasks, setTasks] = useState<FetchTask[]>([]);
  const [cacheFiles, setCacheFiles] = useState<CacheFile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [gapReport, setGapReport] = useState<GapReport | null>(null);
  const [checkingGaps, setCheckingGaps] = useState(false);
  const [expandedGapFiles, setExpandedGapFiles] = useState<Set<string>>(new Set());

  const handleCheckGaps = async () => {
    setCheckingGaps(true);
    setGapReport(null);
    try {
      const resp = await fetch(`${API_BASE}/tools/ohlcv/check-gaps`);
      if (resp.ok) {
        const data = await resp.json();
        setGapReport(data);
        setExpandedGapFiles(new Set());
      } else {
        setError('Failed to check gaps');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setCheckingGaps(false);
    }
  };

  const toggleGapExpand = (filename: string) => {
    setExpandedGapFiles(prev => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });
  };

  const [startDate, setStartDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 15);
    return d.toISOString().split('T')[0];
  });
  const [endDate, setEndDate] = useState(() => new Date().toISOString().split('T')[0]);

  const availableTimeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1d'];

  // Fetch providers and cache status on mount, restore running tasks
  useEffect(() => {
    fetchProviders();
    fetchCacheStatus();
    // Restore any running/queued OHLCV batch tasks
    (async () => {
      try {
        const responses = await Promise.all([
          fetch(`${API_BASE}/tasks?task_type=ohlcv_cache_fetch&status=running`),
          fetch(`${API_BASE}/tasks?task_type=ohlcv_cache_fetch&status=queued`),
        ]);
        const restored: FetchTask[] = [];
        for (const r of responses) {
          if (r.ok) {
            const d = await r.json();
            for (const t of (d.tasks || [])) {
              const sym = t.payload?.symbol || t.name?.replace('Cache OHLCV: ', '') || '?';
              restored.push({ symbol: sym, task_id: t.task_id, status: t.status, progress: t.progress, progress_message: t.progress_message });
            }
          }
        }
        if (restored.length > 0) {
          setTasks(restored);
          setFetching(true);
        }
      } catch { /* ignore */ }
    })();
  }, []);

  // Poll task progress when tasks are active
  useEffect(() => {
    const terminalStatuses = ['completed', 'failed', 'cancelled', 'stopped'];
    const activeTasks = tasks.filter(t => !terminalStatuses.includes(t.status || ''));
    if (activeTasks.length === 0) return;

    const interval = setInterval(async () => {
      const updatedTasks = await Promise.all(
        tasks.map(async (task) => {
          if (terminalStatuses.includes(task.status || '')) return task;
          try {
            const resp = await fetch(`${API_BASE}/tasks/${task.task_id}/progress`);
            if (resp.ok) {
              const data = await resp.json();
              return { ...task, status: data.status, progress: data.progress, progress_message: data.progress_message };
            }
          } catch { /* ignore */ }
          return task;
        })
      );
      setTasks(updatedTasks);

      // Refresh cache status when all tasks complete
      const ohlcvStillActive = updatedTasks.filter(t => !terminalStatuses.includes(t.status || ''));
      if (ohlcvStillActive.length === 0) {
        setFetching(false);
        fetchCacheStatus();
      }
    }, 2000);

    return () => clearInterval(interval);
  }, [tasks]);

  const fetchProviders = async () => {
    try {
      const resp = await fetch(`${API_BASE}/tools/ohlcv/providers`);
      if (resp.ok) {
        const data = await resp.json();
        setProviders(data.providers || []);
      }
    } catch (err) {
      console.error('Failed to fetch OHLCV providers:', err);
    }
  };

  const fetchCacheStatus = async () => {
    try {
      const resp = await fetch(`${API_BASE}/tools/ohlcv/cache-status`);
      if (resp.ok) {
        const data = await resp.json();
        setCacheFiles(data.cache_files || []);
      }
    } catch (err) {
      console.error('Failed to fetch cache status:', err);
    }
  };

  const parseSymbols = (text: string): string[] => {
    return text
      .split(/[\n,;]+/)
      .map(s => s.trim().toUpperCase())
      .filter(s => s.length > 0 && /^[A-Z]{1,5}(\.[A-Z]{1,2})?$/.test(s));
  };

  const handleSymbolInputChange = (text: string) => {
    setSymbolInput(text);
    setSymbols(parseSymbols(text));
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (event) => {
      const text = event.target?.result as string;
      setSymbolInput(text);
      setSymbols(parseSymbols(text));
    };
    reader.readAsText(file);
    // Reset input so same file can be re-uploaded
    e.target.value = '';
  };

  const toggleTimeframe = (tf: string) => {
    setTimeframes(prev =>
      prev.includes(tf) ? prev.filter(t => t !== tf) : [...prev, tf]
    );
  };

  const handleFetchCache = async () => {
    if (symbols.length === 0) {
      setError('Please enter at least one symbol');
      return;
    }
    if (timeframes.length === 0) {
      setError('Please select at least one timeframe');
      return;
    }
    if (startDate > endDate) {
      setError('Start date must be before end date');
      return;
    }

    setFetching(true);
    setError(null);
    setMessage(null);

    try {
      const resp = await fetch(`${API_BASE}/tools/ohlcv/fetch-cache`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, symbols, timeframes, start_date: startDate, end_date: endDate, parallel_jobs: parallelJobs, executor_workers: executorWorkers })
      });

      if (resp.ok) {
        const data = await resp.json();
        const newTasks: FetchTask[] = (data.task_ids || []).map((t: any) => ({
          symbol: t.symbol,
          task_id: t.task_id,
          status: 'queued',
          progress: 0,
          progress_message: 'Queued'
        }));
        setTasks(newTasks);
        setMessage(`Queued ${data.count} cache fetch tasks`);
      } else {
        const errData = await resp.json();
        setError(errData.detail || 'Failed to queue cache fetch');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Fetch Form */}
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
        <h2 className="text-xl font-semibold mb-4 text-gray-900 dark:text-gray-100">
          OHLCV Cache Prefetcher
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Prefetch and cache OHLCV data for multiple symbols and timeframes. Cached data speeds up dataset creation.
        </p>

        <div className="space-y-4">
          {/* Provider */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Provider
              </label>
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              >
                {providers.map(p => (
                  <option key={p.id} value={p.id} disabled={!p.available}>
                    {p.name} {!p.available && '(Not configured)'}
                  </option>
                ))}
                {providers.length === 0 && (
                  <option value="yfinance">Yahoo Finance</option>
                )}
              </select>
            </div>
          </div>

          {/* Symbols */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Symbols ({symbols.length} parsed)
            </label>
            <div className="flex gap-2">
              <textarea
                value={symbolInput}
                onChange={(e) => handleSymbolInputChange(e.target.value)}
                placeholder="Enter symbols, one per line (e.g., AAPL, MSFT, GOOGL)"
                rows={4}
                className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 font-mono text-sm"
              />
              <div className="flex flex-col gap-2">
                <label className="px-3 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 cursor-pointer flex items-center gap-1 text-sm">
                  <Upload size={14} />
                  Upload .txt
                  <input
                    type="file"
                    accept=".txt"
                    onChange={handleFileUpload}
                    className="hidden"
                  />
                </label>
              </div>
            </div>
            {symbols.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {symbols.map(s => (
                  <span key={s} className="px-2 py-0.5 text-xs bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 rounded-full">
                    {s}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Timeframes */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Timeframes
            </label>
            <div className="flex flex-wrap gap-2">
              {availableTimeframes.map(tf => (
                <label
                  key={tf}
                  className={`px-3 py-1.5 rounded-full cursor-pointer text-sm transition-colors ${
                    timeframes.includes(tf)
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={timeframes.includes(tf)}
                    onChange={() => toggleTimeframe(tf)}
                    className="sr-only"
                  />
                  {tf}
                </label>
              ))}
            </div>
          </div>

          {/* Date Range */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Start Date
              </label>
              <input
                type="date"
                value={startDate}
                onChange={e => setStartDate(e.target.value)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                End Date
              </label>
              <input
                type="date"
                value={endDate}
                onChange={e => setEndDate(e.target.value)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
          </div>

          {/* Parallel Settings */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Parallel Jobs
              </label>
              <input
                type="number"
                min={1}
                max={20}
                value={parallelJobs}
                onChange={e => setParallelJobs(Math.max(1, Number(e.target.value)))}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Symbols processed simultaneously</p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Fetch Workers
              </label>
              <input
                type="number"
                min={1}
                max={20}
                value={executorWorkers}
                onChange={e => setExecutorWorkers(Math.max(1, Number(e.target.value)))}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Parallel fetch workers per symbol</p>
            </div>
          </div>

          {/* Fetch Button */}
          <button
            onClick={handleFetchCache}
            disabled={fetching || symbols.length === 0 || timeframes.length === 0}
            className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
          >
            {fetching ? (
              <>
                <Loader size={16} className="animate-spin" />
                Queueing...
              </>
            ) : (
              <>
                <Database size={16} />
                Fetch & Cache ({symbols.length} symbols x {timeframes.length} timeframes)
              </>
            )}
          </button>
        </div>

        {error && (
          <div className="mt-4 p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-md flex items-center gap-2">
            <XCircle size={16} />
            {error}
          </div>
        )}

        {message && (
          <div className="mt-4 p-4 bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 rounded-md flex items-center gap-2">
            <CheckCircle size={16} />
            {message}
          </div>
        )}
      </div>

      {/* Active Tasks */}
      {tasks.length > 0 && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <h3 className="text-lg font-semibold mb-4 text-gray-900 dark:text-gray-100">
            Fetch Tasks ({tasks.filter(t => t.status === 'completed').length}/{tasks.length} completed)
          </h3>
          <div className="space-y-2">
            {tasks.map(task => (
              <div key={task.task_id} className="flex items-center gap-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg">
                <div className="flex-shrink-0">
                  {task.status === 'completed' ? (
                    <CheckCircle size={16} className="text-green-500" />
                  ) : task.status === 'failed' ? (
                    <XCircle size={16} className="text-red-500" />
                  ) : (
                    <Loader size={16} className="text-blue-500 animate-spin" />
                  )}
                </div>
                <div className="flex-1">
                  <span className="font-medium text-gray-900 dark:text-gray-100">{task.symbol}</span>
                  <span className="text-sm text-gray-500 dark:text-gray-400 ml-2">
                    {task.progress_message || task.status}
                  </span>
                </div>
                {task.progress !== undefined && task.status !== 'completed' && task.status !== 'failed' && (
                  <div className="w-24 bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                    <div
                      className="bg-blue-600 h-2 rounded-full transition-all"
                      style={{ width: `${task.progress}%` }}
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Cache Files */}
      {cacheFiles.length > 0 && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              Cached Data ({cacheFiles.length} files)
            </h3>
            <div className="flex gap-2">
              <button
                onClick={handleCheckGaps}
                disabled={checkingGaps}
                className="px-3 py-1 text-sm bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-300 rounded hover:bg-orange-200 dark:hover:bg-orange-900/50 flex items-center gap-1 disabled:opacity-50"
              >
                {checkingGaps ? <Loader size={14} className="animate-spin" /> : <Search size={14} />}
                Check Gaps
              </button>
              <button
                onClick={fetchCacheStatus}
                className="px-3 py-1 text-sm bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
              >
                Refresh
              </button>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 dark:bg-gray-700">
                <tr>
                  <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Provider</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Symbol</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Interval</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Date From</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Date To</th>
                  <th className="px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Rows</th>
                  <th className="px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Size (MB)</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Last Modified</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                {cacheFiles.map((cf, idx) => (
                  <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                    <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{cf.provider || '–'}</td>
                    <td className="px-4 py-2 text-gray-900 dark:text-gray-100 font-medium">{cf.symbol}</td>
                    <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{cf.interval}</td>
                    <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{cf.date_from ? new Date(cf.date_from).toLocaleDateString() : '–'}</td>
                    <td className="px-4 py-2 text-gray-600 dark:text-gray-400">{cf.date_to ? new Date(cf.date_to).toLocaleDateString() : '–'}</td>
                    <td className="px-4 py-2 text-right text-gray-600 dark:text-gray-400">{cf.rows.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right text-gray-600 dark:text-gray-400">{cf.file_size_mb}</td>
                    <td className="px-4 py-2 text-gray-600 dark:text-gray-400">
                      {new Date(cf.last_modified).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Gap Report */}
      {gapReport && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
              <Search size={18} />
              Gap Analysis Report
            </h3>
            <button
              onClick={() => setGapReport(null)}
              className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              title="Close report"
            >
              <XCircle size={18} />
            </button>
          </div>

          {/* Summary */}
          <div className="flex flex-wrap gap-3 mb-4">
            <span className={`px-3 py-1.5 rounded-full text-sm font-medium flex items-center gap-1 ${gapReport.files_with_gaps > 0 ? 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300' : 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300'}`}>
              {gapReport.files_with_gaps > 0 ? <AlertCircle size={14} /> : <CheckCircle size={14} />}
              {gapReport.files_with_gaps} file{gapReport.files_with_gaps !== 1 ? 's' : ''} with gaps
            </span>
            <span className="px-3 py-1.5 rounded-full text-sm font-medium bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300 flex items-center gap-1">
              <CheckCircle size={14} />
              {gapReport.files_without_gaps} clean file{gapReport.files_without_gaps !== 1 ? 's' : ''}
            </span>
            <span className="px-3 py-1.5 rounded-full text-sm font-medium bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
              {gapReport.total_gaps} total gap{gapReport.total_gaps !== 1 ? 's' : ''}
            </span>
          </div>

          {gapReport.files_with_gaps === 0 ? (
            <div className="flex items-center gap-2 text-green-600 dark:text-green-400 py-4">
              <CheckCircle size={20} />
              <span>All cache files are gap-free.</span>
            </div>
          ) : (
            <div className="space-y-2">
              {gapReport.results.filter(r => r.has_gaps).map(result => (
                <div key={result.filename} className="border border-red-200 dark:border-red-800 rounded-lg overflow-hidden">
                  <button
                    className="w-full flex items-center gap-3 px-4 py-3 bg-red-50 dark:bg-red-900/20 hover:bg-red-100 dark:hover:bg-red-900/30 text-left"
                    onClick={() => toggleGapExpand(result.filename)}
                  >
                    {expandedGapFiles.has(result.filename) ? <ChevronDown size={16} className="text-red-500 flex-shrink-0" /> : <ChevronRight size={16} className="text-red-500 flex-shrink-0" />}
                    <span className="font-medium text-gray-900 dark:text-gray-100">{result.symbol}</span>
                    <span className="text-sm text-gray-500 dark:text-gray-400">{result.interval}</span>
                    <span className="text-xs text-gray-400 dark:text-gray-500">[{result.provider}]</span>
                    <span className="ml-auto flex items-center gap-2 text-sm">
                      <span className="px-2 py-0.5 bg-red-200 dark:bg-red-800 text-red-800 dark:text-red-200 rounded-full font-medium">
                        {result.gap_count} gap{result.gap_count !== 1 ? 's' : ''}
                      </span>
                      <span className="text-gray-500 dark:text-gray-400">
                        max {Math.max(...result.gaps.map(g => g.gap_days))}d
                      </span>
                    </span>
                  </button>
                  {expandedGapFiles.has(result.filename) && (
                    <div className="px-4 py-3 bg-white dark:bg-gray-800 border-t border-red-200 dark:border-red-800">
                      <div className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                        {result.rows.toLocaleString()} rows &bull; {result.date_from ? new Date(result.date_from).toLocaleDateString() : '?'} → {result.date_to ? new Date(result.date_to).toLocaleDateString() : '?'}
                      </div>
                      <table className="min-w-full text-xs">
                        <thead>
                          <tr className="text-gray-500 dark:text-gray-400">
                            <th className="text-left pr-6 pb-1">Gap Start</th>
                            <th className="text-left pr-6 pb-1">Gap End</th>
                            <th className="text-right pb-1">Duration</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                          {result.gaps.map((gap, i) => (
                            <tr key={i} className="text-gray-700 dark:text-gray-300">
                              <td className="pr-6 py-0.5">{new Date(gap.gap_start).toLocaleDateString()}</td>
                              <td className="pr-6 py-0.5">{new Date(gap.gap_end).toLocaleDateString()}</td>
                              <td className="text-right py-0.5 font-medium text-red-600 dark:text-red-400">{gap.gap_days}d</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// News Batch Fetch Tool Component
interface NewsBatchTask {
  symbol: string;
  task_id: string;
  status?: string;
  progress?: number;
  progress_message?: string;
}

interface SymbolStats {
  count: number;
  with_sentiment: number;
  with_content: number;
}

interface NewsCacheStats {
  total_articles: number;
  with_sentiment: number;
  with_content: number;
  by_provider: Record<string, number>;
  by_provider_symbol?: Record<string, Record<string, SymbolStats>>;
}

const NewsBatchFetchTool: React.FC = () => {
  const [provider, setProvider] = useState('fmp');
  const [providers, setProviders] = useState<NewsProvider[]>([]);
  const [symbolInput, setSymbolInput] = useState('');
  const [symbols, setSymbols] = useState<string[]>([]);

  const [startDate, setStartDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 1);
    return d.toISOString().split('T')[0];
  });
  const [endDate, setEndDate] = useState(() => new Date().toISOString().split('T')[0]);

  const [fetching, setFetching] = useState(false);
  const [activeTasks, setActiveTasks] = useState<NewsBatchTask[]>([]);
  const [cacheStats, setCacheStats] = useState<NewsCacheStats | null>(null);
  const [expandedProviders, setExpandedProviders] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    fetchProviders();
    fetchCacheStats();
    restoreActiveTasks();
  }, []);

  const restoreActiveTasks = async () => {
    try {
      // Check for running or queued news_batch_fetch tasks
      const responses = await Promise.all([
        fetch(`${API_BASE}/tasks?task_type=news_batch_fetch&status=running`),
        fetch(`${API_BASE}/tasks?task_type=news_batch_fetch&status=queued`),
      ]);
      const allTasks: NewsBatchTask[] = [];
      for (const r of responses) {
        if (r.ok) {
          const d = await r.json();
          for (const t of (d.tasks || [])) {
            // Extract symbol from payload
            const sym = t.payload?.symbols?.[0] || t.name?.replace('News Batch: ', '') || '?';
            allTasks.push({
              symbol: sym,
              task_id: t.task_id,
              status: t.status,
              progress: t.progress,
              progress_message: t.progress_message,
            });
          }
        }
      }
      if (allTasks.length > 0) {
        setActiveTasks(allTasks);
        setFetching(true);
      }
    } catch { /* ignore */ }
  };

  // Poll active tasks
  useEffect(() => {
    if (activeTasks.length === 0) return;
    const interval = setInterval(async () => {
      const updatedTasks = await Promise.all(
        activeTasks.map(async t => {
          try {
            const r = await fetch(`${API_BASE}/tasks/${t.task_id}/progress`);
            if (r.ok) {
              const d = await r.json();
              return { ...t, status: d.status, progress: d.progress, progress_message: d.progress_message };
            }
          } catch { /* ignore */ }
          return t;
        })
      );
      setActiveTasks(updatedTasks);
      const terminalStatuses = ['completed', 'failed', 'cancelled', 'stopped'];
      const stillActive = updatedTasks.filter(t => !terminalStatuses.includes(t.status || ''));
      if (stillActive.length === 0) {
        setFetching(false);
        fetchCacheStats();
        const failedCount = updatedTasks.filter(t => t.status === 'failed' || t.status === 'stopped').length;
        if (failedCount > 0) {
          setMessage(`Tasks finished: ${updatedTasks.length - failedCount} completed, ${failedCount} failed/stopped`);
        } else {
          setMessage('All tasks completed!');
        }
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [activeTasks]);

  const fetchProviders = async () => {
    try {
      const r = await fetch(`${API_BASE}/tools/news/providers`);
      if (r.ok) {
        const d = await r.json();
        // Filter providers that support company news (exclude localfiles)
        setProviders((d.providers || []).filter((p: NewsProvider) => p.id !== 'localfiles'));
      }
    } catch { /* ignore */ }
  };

  const fetchCacheStats = async () => {
    try {
      const r = await fetch(`${API_BASE}/tools/news/cache-status`);
      if (r.ok) {
        const d = await r.json();
        setCacheStats(d);
      }
    } catch { /* ignore */ }
  };

  const parseSymbols = (text: string): string[] => {
    return text
      .split(/[\n,;\s]+/)
      .map(s => s.trim().toUpperCase())
      .filter(s => s.length > 0 && /^[A-Z]{1,5}(\.[A-Z]{1,2})?$/.test(s));
  };

  const handleSymbolInputChange = (text: string) => {
    setSymbolInput(text);
    setSymbols([...new Set(parseSymbols(text))]);
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (event) => {
      const text = event.target?.result as string;
      setSymbolInput(text);
      setSymbols([...new Set(parseSymbols(text))]);
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  const handleFetchBatch = async () => {
    if (symbols.length === 0) { setError('Please enter at least one symbol'); return; }
    if (!startDate || !endDate) { setError('Please select a date range'); return; }
    if (startDate > endDate) { setError('Start date must be before end date'); return; }

    setError(null);
    setMessage(null);
    setFetching(true);
    setActiveTasks([]);

    try {
      const resp = await fetch(`${API_BASE}/tools/news/batch-fetch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, symbols, start_date: startDate, end_date: endDate })
      });
      if (!resp.ok) {
        const e = await resp.json();
        throw new Error(e.detail || 'Failed to queue tasks');
      }
      const data = await resp.json();
      setActiveTasks(data.task_ids.map((t: any) => ({ symbol: t.symbol, task_id: t.task_id, status: 'pending' })));
      setMessage(`Queued ${data.count} task(s). Fetching news for: ${symbols.join(', ')}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
      setFetching(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Fetch Form */}
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
        <h2 className="text-xl font-semibold mb-4 text-gray-900 dark:text-gray-100">
          News Batch Fetch
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Bulk-fetch news articles for one or more symbols over a date range.
          Article webpages are fetched (Wayback Machine for articles older than 1 year)
          and FinBERT sentiment is analyzed. All results are persisted to the news cache.
        </p>

        <div className="space-y-4">
          {/* Provider */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              News Provider
            </label>
            <select
              value={provider}
              onChange={e => setProvider(e.target.value)}
              className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            >
              {providers.length === 0 && <option value="fmp">FMP (Financial Modeling Prep)</option>}
              {providers.map(p => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>

          {/* Symbols */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Symbols ({symbols.length} parsed)
            </label>
            <div className="flex gap-2">
              <textarea
                value={symbolInput}
                onChange={(e) => handleSymbolInputChange(e.target.value)}
                placeholder="Enter symbols, one per line or comma-separated (e.g., AAPL, MSFT, GOOGL)"
                rows={4}
                className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 font-mono text-sm"
              />
              <div className="flex flex-col gap-2">
                <label className="px-3 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 cursor-pointer flex items-center gap-1 text-sm">
                  <Upload size={14} />
                  Upload .txt
                  <input
                    type="file"
                    accept=".txt"
                    onChange={handleFileUpload}
                    className="hidden"
                  />
                </label>
              </div>
            </div>
            {symbols.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {symbols.map(s => (
                  <span key={s} className="px-2 py-0.5 text-xs bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 rounded-full">
                    {s}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Date Range */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Start Date
              </label>
              <input
                type="date"
                value={startDate}
                onChange={e => setStartDate(e.target.value)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                End Date
              </label>
              <input
                type="date"
                value={endDate}
                onChange={e => setEndDate(e.target.value)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
          </div>

          {error && <p className="text-red-500 text-sm">{error}</p>}
          {message && <p className="text-green-600 dark:text-green-400 text-sm">{message}</p>}

          <button
            onClick={handleFetchBatch}
            disabled={fetching || symbols.length === 0}
            className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
          >
            {fetching ? (
              <>
                <Loader size={16} className="animate-spin" />
                Queueing...
              </>
            ) : (
              <>
                <Download size={16} />
                Fetch & Analyze ({symbols.length} symbols)
              </>
            )}
          </button>
        </div>
      </div>

      {/* Active Tasks */}
      {activeTasks.length > 0 && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <h3 className="text-lg font-semibold mb-4 text-gray-900 dark:text-gray-100">
            Fetch Tasks ({activeTasks.filter(t => t.status === 'completed').length}/{activeTasks.length} completed)
          </h3>
          <div className="space-y-2">
            {activeTasks.map(t => (
              <div key={t.task_id} className="flex items-center gap-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg">
                <div className="flex-shrink-0">
                  {t.status === 'completed' ? (
                    <CheckCircle size={16} className="text-green-500" />
                  ) : t.status === 'failed' || t.status === 'stopped' ? (
                    <XCircle size={16} className="text-red-500" />
                  ) : (
                    <Loader size={16} className="text-blue-500 animate-spin" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <span className="font-medium text-gray-900 dark:text-gray-100">{t.symbol}</span>
                  <span className="text-sm text-gray-500 dark:text-gray-400 ml-2 truncate">
                    {t.progress_message || t.status || 'pending'}
                  </span>
                </div>
                {t.progress !== undefined && t.status !== 'completed' && t.status !== 'failed' && (
                  <div className="w-24 bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                    <div
                      className="bg-blue-600 h-2 rounded-full transition-all"
                      style={{ width: `${t.progress}%` }}
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Cache Stats */}
      {cacheStats && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              News Cache Stats
            </h3>
            <button
              onClick={fetchCacheStats}
              className="px-3 py-1 text-sm bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
            >
              Refresh
            </button>
          </div>
          <div className="grid grid-cols-3 gap-4 mb-4">
            <div className="text-center p-3 bg-gray-50 dark:bg-gray-700 rounded">
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{cacheStats.total_articles.toLocaleString()}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">Total Articles</div>
            </div>
            <div className="text-center p-3 bg-gray-50 dark:bg-gray-700 rounded">
              <div className="text-2xl font-bold text-purple-600">{cacheStats.with_sentiment.toLocaleString()}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">With Sentiment</div>
            </div>
            <div className="text-center p-3 bg-gray-50 dark:bg-gray-700 rounded">
              <div className="text-2xl font-bold text-green-600">{cacheStats.with_content.toLocaleString()}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">With Content</div>
            </div>
          </div>
          {Object.keys(cacheStats.by_provider).length > 0 && (
            <div>
              <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">By Provider</h4>
              <div className="space-y-1">
                {Object.entries(cacheStats.by_provider).map(([prov, count]) => {
                  const isExpanded = expandedProviders.has(prov);
                  const symbolStats = cacheStats.by_provider_symbol?.[prov];
                  const hasSymbols = symbolStats && Object.keys(symbolStats).length > 0;
                  return (
                    <div key={prov}>
                      <div
                        className={`flex justify-between items-center text-sm text-gray-600 dark:text-gray-400 ${hasSymbols ? 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 rounded px-2 py-1 -mx-2' : ''}`}
                        onClick={() => {
                          if (!hasSymbols) return;
                          setExpandedProviders(prev => {
                            const next = new Set(prev);
                            if (next.has(prov)) next.delete(prov); else next.add(prov);
                            return next;
                          });
                        }}
                      >
                        <span className="flex items-center gap-1">
                          {hasSymbols && (isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />)}
                          {prov}
                        </span>
                        <span className="font-medium">{(count as number).toLocaleString()}</span>
                      </div>
                      {isExpanded && symbolStats && (
                        <div className="ml-5 mt-1 mb-2 border-l-2 border-gray-200 dark:border-gray-600 pl-3">
                          <table className="w-full text-xs text-gray-500 dark:text-gray-400">
                            <thead>
                              <tr className="border-b border-gray-200 dark:border-gray-600">
                                <th className="text-left py-1 font-medium">Symbol</th>
                                <th className="text-right py-1 font-medium">Articles</th>
                                <th className="text-right py-1 font-medium">Sentiment</th>
                                <th className="text-right py-1 font-medium">Content</th>
                              </tr>
                            </thead>
                            <tbody>
                              {Object.entries(symbolStats)
                                .sort(([, a], [, b]) => b.count - a.count)
                                .map(([sym, stats]) => (
                                <tr key={sym} className="border-b border-gray-100 dark:border-gray-700">
                                  <td className="py-1 font-medium text-gray-700 dark:text-gray-300">{sym}</td>
                                  <td className="py-1 text-right">{stats.count.toLocaleString()}</td>
                                  <td className="py-1 text-right">{stats.with_sentiment.toLocaleString()}</td>
                                  <td className="py-1 text-right">{stats.with_content.toLocaleString()}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

const NewsProviderTester: React.FC = () => {
  const [symbol, setSymbol] = useState('AAPL');
  const [provider, setProvider] = useState('fmp');
  const [newsType, setNewsType] = useState<'company' | 'global'>('company');
  // Default to last 30 days
  const getDefaultDates = () => {
    const end = new Date();
    const start = new Date();
    start.setDate(start.getDate() - 30);
    return {
      start: start.toISOString().split('T')[0],
      end: end.toISOString().split('T')[0]
    };
  };
  const defaultDates = getDefaultDates();
  const [startDate, setStartDate] = useState(defaultDates.start);
  const [endDate, setEndDate] = useState(defaultDates.end);
  const [providers, setProviders] = useState<NewsProvider[]>([]);
  const [articles, setArticles] = useState<NewsArticle[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sentimentResults, setSentimentResults] = useState<Record<number, SentimentResult | null>>({});
  const [analyzingIndex, setAnalyzingIndex] = useState<number | null>(null);

  // Pagination state
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(20);
  const totalPages = Math.ceil(articles.length / itemsPerPage);
  const paginatedArticles = articles.slice(
    (currentPage - 1) * itemsPerPage,
    currentPage * itemsPerPage
  );

  // Fetch available providers on mount
  useEffect(() => {
    const fetchProviders = async () => {
      try {
        const response = await fetch(`${API_BASE}/tools/news/providers`);
        if (response.ok) {
          const data = await response.json();
          setProviders(data.providers || []);
        }
      } catch (err) {
        console.error('Failed to fetch providers:', err);
      }
    };
    fetchProviders();
  }, []);

  const fetchNews = async () => {
    setLoading(true);
    setError(null);
    setArticles([]);
    setSentimentResults({});
    setCurrentPage(1); // Reset to first page

    try {
      const params = new URLSearchParams({
        provider,
        news_type: newsType,
        start_date: startDate,
        end_date: endDate,
        limit: '500' // Fetch more articles, paginate client-side
      });

      // Only add symbol for company news
      if (newsType === 'company' && symbol) {
        params.set('symbol', symbol);
      }

      const response = await fetch(`${API_BASE}/tools/news/fetch?${params}`);

      if (response.ok) {
        const data = await response.json();
        setArticles(data.articles || []);
        if (data.articles?.length === 0) {
          setError('No articles found for this symbol and date range');
        }
      } else {
        const errorData = await response.json();
        setError(errorData.detail || 'Failed to fetch news');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setLoading(false);
    }
  };

  const analyzeSentiment = async (index: number, article: NewsArticle) => {
    setAnalyzingIndex(index);

    try {
      const params = new URLSearchParams({
        title: article.title,
        content: article.summary || article.content || ''
      });

      const response = await fetch(`${API_BASE}/tools/news/analyze-single?${params}`, {
        method: 'POST'
      });

      if (response.ok) {
        const data = await response.json();
        setSentimentResults(prev => ({
          ...prev,
          [index]: {
            sentiment: data.sentiment,
            sentiment_score: data.sentiment_score,
            confidence: data.confidence
          }
        }));
      } else {
        setSentimentResults(prev => ({
          ...prev,
          [index]: null
        }));
      }
    } catch (err) {
      console.error('Sentiment analysis error:', err);
      setSentimentResults(prev => ({
        ...prev,
        [index]: null
      }));
    } finally {
      setAnalyzingIndex(null);
    }
  };

  const analyzeAllSentiments = async () => {
    for (let i = 0; i < articles.length; i++) {
      if (!sentimentResults[i]) {
        await analyzeSentiment(i, articles[i]);
      }
    }
  };

  const [exporting, setExporting] = useState(false);
  const [exportMessage, setExportMessage] = useState<string | null>(null);

  const exportToJson = async () => {
    setExporting(true);
    setExportMessage(null);

    try {
      // First fetch ALL news for the date range (no limit)
      const fetchParams = new URLSearchParams({
        provider,
        news_type: newsType,
        start_date: startDate,
        end_date: endDate,
        limit: '10000' // Fetch all articles for export
      });
      if (newsType === 'company' && symbol) {
        fetchParams.set('symbol', symbol);
      }

      setExportMessage('Fetching all articles...');
      const fetchResponse = await fetch(`${API_BASE}/tools/news/fetch?${fetchParams}`);

      if (!fetchResponse.ok) {
        const errorData = await fetchResponse.json();
        throw new Error(errorData.detail || 'Failed to fetch articles for export');
      }

      const fetchData = await fetchResponse.json();
      const allArticles = fetchData.articles || [];

      if (allArticles.length === 0) {
        setExportMessage('No articles to export');
        setExporting(false);
        return;
      }

      setExportMessage(`Exporting ${allArticles.length} articles...`);

      // Build export URL with appropriate parameters
      const exportParams = new URLSearchParams({
        provider,
        news_type: newsType
      });
      if (newsType === 'company' && symbol) {
        exportParams.set('symbol', symbol);
      }

      const response = await fetch(
        `${API_BASE}/tools/news/export?${exportParams}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(allArticles)
        }
      );

      if (response.ok) {
        const data = await response.json();
        setExportMessage(`Exported ${allArticles.length} articles to ${data.filename}`);
      } else {
        const errorData = await response.json();
        setExportMessage(`Export failed: ${errorData.detail}`);
      }
    } catch (err) {
      setExportMessage(`Export error: ${err instanceof Error ? err.message : 'Unknown error'}`);
    } finally {
      setExporting(false);
    }
  };

  const getSentimentColor = (sentiment: string) => {
    switch (sentiment) {
      case 'positive':
        return 'text-green-600 bg-green-100 dark:text-green-400 dark:bg-green-900/30';
      case 'negative':
        return 'text-red-600 bg-red-100 dark:text-red-400 dark:bg-red-900/30';
      default:
        return 'text-orange-600 bg-orange-100 dark:text-orange-400 dark:bg-orange-900/30';
    }
  };

  const getSentimentIcon = (sentiment: string) => {
    switch (sentiment) {
      case 'positive':
        return <CheckCircle size={16} />;
      case 'negative':
        return <XCircle size={16} />;
      default:
        return <AlertCircle size={16} />;
    }
  };

  return (
    <div className="space-y-6">
      {/* Search Form */}
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
        <h2 className="text-xl font-bold mb-4 text-gray-900 dark:text-gray-100">Test News Provider</h2>

        {/* News Type Toggle */}
        <div className="flex gap-2 mb-4">
          <button
            onClick={() => setNewsType('company')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              newsType === 'company'
                ? 'bg-blue-600 text-white'
                : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
            }`}
          >
            Company News
          </button>
          <button
            onClick={() => setNewsType('global')}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              newsType === 'global'
                ? 'bg-blue-600 text-white'
                : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
            }`}
          >
            Global/Market News
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-5 gap-4 mb-4">
          {newsType === 'company' && (
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Symbol
              </label>
              <input
                type="text"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                placeholder="AAPL"
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Provider
            </label>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            >
              {providers.map((p) => (
                <option key={p.id} value={p.id} disabled={!p.api_key_configured}>
                  {p.name} {!p.api_key_configured && '(No API Key)'}
                </option>
              ))}
              {providers.length === 0 && (
                <>
                  <option value="fmp">Financial Modeling Prep</option>
                  <option value="alpaca">Alpaca</option>
                </>
              )}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Start Date
            </label>
            <input
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              End Date
            </label>
            <input
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
            />
          </div>

          <div className="flex items-end">
            <button
              onClick={fetchNews}
              disabled={loading || (newsType === 'company' && !symbol)}
              className="w-full px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <Loader size={16} className="animate-spin" />
                  Fetching...
                </>
              ) : (
                <>
                  <Search size={16} />
                  Fetch News
                </>
              )}
            </button>
          </div>
        </div>

        {/* Provider Status */}
        {providers.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-2">
            {providers.map((p) => (
              <span
                key={p.id}
                className={`px-2 py-1 text-xs rounded-full ${
                  p.api_key_configured
                    ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                    : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                }`}
              >
                {p.name}: {p.api_key_configured ? 'Configured' : 'Missing API Key'}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Error Display */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <div className="flex items-center gap-2 text-red-700 dark:text-red-300">
            <AlertCircle size={20} />
            <span>{error}</span>
          </div>
        </div>
      )}

      {/* Results */}
      {articles.length > 0 && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100">
              Results ({articles.length} articles)
            </h2>
            <div className="flex items-center gap-3">
              {exportMessage && (
                <span className={`text-sm ${exportMessage.includes('failed') || exportMessage.includes('error') ? 'text-red-500' : 'text-green-500'}`}>
                  {exportMessage}
                </span>
              )}
              <button
                onClick={exportToJson}
                disabled={exporting || articles.length === 0}
                className="px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 flex items-center gap-2"
              >
                {exporting ? (
                  <>
                    <Loader size={16} className="animate-spin" />
                    Exporting...
                  </>
                ) : (
                  <>
                    <Download size={16} />
                    Export JSON
                  </>
                )}
              </button>
              <button
                onClick={analyzeAllSentiments}
                disabled={analyzingIndex !== null}
                className="px-4 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700 disabled:opacity-50 flex items-center gap-2"
              >
                <MessageSquare size={16} />
                Analyze All Sentiments
              </button>
            </div>
          </div>

          <div className="space-y-4">
            {paginatedArticles.map((article, pageIndex) => {
              // Calculate actual index in full articles array for sentiment tracking
              const actualIndex = (currentPage - 1) * itemsPerPage + pageIndex;
              return (
                <div
                  key={actualIndex}
                  className="border border-gray-200 dark:border-gray-700 rounded-lg p-4"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1">
                      <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-1">
                        {article.title}
                      </h3>
                      <div className="flex items-center gap-3 text-sm text-gray-500 dark:text-gray-400 mb-2">
                        {article.source && <span>{article.source}</span>}
                        {(article.date || article.published_at) && (
                          <span>
                            {new Date(article.date || article.published_at || '').toLocaleDateString()}
                          </span>
                        )}
                      </div>
                      {(article.summary || article.content) && (
                        <p className="text-sm text-gray-600 dark:text-gray-300 line-clamp-2">
                          {article.summary || article.content}
                        </p>
                      )}
                      {article.url && (
                        <a
                          href={article.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-sm text-blue-600 dark:text-blue-400 hover:underline mt-1 inline-block"
                        >
                          Read more
                        </a>
                      )}
                    </div>

                    <div className="flex flex-col items-end gap-2">
                      {/* Show provider's built-in sentiment if available */}
                      {article.sentiment && (
                        <div className="px-3 py-1.5 rounded-full flex items-center gap-1.5 bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
                          <span className="text-xs opacity-75">API:</span>
                          <span className="text-sm font-medium capitalize">
                            {article.sentiment}
                          </span>
                          {article.sentiment_score !== undefined && (
                            <span className="text-xs opacity-75">
                              ({(article.sentiment_score * 100).toFixed(0)}%)
                            </span>
                          )}
                        </div>
                      )}
                      {/* Show FinBERT analysis or analyze button */}
                      {sentimentResults[actualIndex] ? (
                        <div className={`px-3 py-1.5 rounded-full flex items-center gap-1.5 ${getSentimentColor(sentimentResults[actualIndex]!.sentiment)}`}>
                          {getSentimentIcon(sentimentResults[actualIndex]!.sentiment)}
                          <span className="text-sm font-medium capitalize">
                            {sentimentResults[actualIndex]!.sentiment}
                          </span>
                          <span className="text-xs opacity-75">
                            ({(sentimentResults[actualIndex]!.sentiment_score * 100).toFixed(0)}%)
                          </span>
                        </div>
                      ) : (
                        <button
                          onClick={() => analyzeSentiment(actualIndex, article)}
                          disabled={analyzingIndex === actualIndex}
                          className="px-3 py-1.5 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50 text-sm flex items-center gap-1"
                        >
                          {analyzingIndex === actualIndex ? (
                            <>
                              <Loader size={14} className="animate-spin" />
                              Analyzing...
                            </>
                          ) : (
                            <>
                              <MessageSquare size={14} />
                              FinBERT
                            </>
                          )}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Pagination Controls */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-6 pt-4 border-t border-gray-200 dark:border-gray-700">
              <div className="flex items-center gap-2">
                <span className="text-sm text-gray-600 dark:text-gray-400">
                  Showing {(currentPage - 1) * itemsPerPage + 1}-{Math.min(currentPage * itemsPerPage, articles.length)} of {articles.length}
                </span>
                <select
                  value={itemsPerPage}
                  onChange={(e) => {
                    setItemsPerPage(Number(e.target.value));
                    setCurrentPage(1);
                  }}
                  className="ml-2 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-300"
                >
                  <option value={10}>10 per page</option>
                  <option value={20}>20 per page</option>
                  <option value={50}>50 per page</option>
                  <option value={100}>100 per page</option>
                </select>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setCurrentPage(1)}
                  disabled={currentPage === 1}
                  className="px-3 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  First
                </button>
                <button
                  onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                  disabled={currentPage === 1}
                  className="px-3 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Previous
                </button>
                <span className="px-3 py-1 text-sm text-gray-600 dark:text-gray-400">
                  Page {currentPage} of {totalPages}
                </span>
                <button
                  onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                  disabled={currentPage === totalPages}
                  className="px-3 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Next
                </button>
                <button
                  onClick={() => setCurrentPage(totalPages)}
                  disabled={currentPage === totalPages}
                  className="px-3 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Last
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// Fundamentals Tester Component
const FundamentalsTester: React.FC = () => {
  const [symbol, setSymbol] = useState('AAPL');
  const [provider, setProvider] = useState('yfinance');
  const [dataType, setDataType] = useState('balance_sheet');
  const [frequency, setFrequency] = useState('quarterly');
  const [lookbackPeriods, setLookbackPeriods] = useState(8);
  const [useCustomDates, setUseCustomDates] = useState(false);
  const [startDate, setStartDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 2);
    return d.toISOString().split('T')[0];
  });
  const [endDate, setEndDate] = useState(() => new Date().toISOString().split('T')[0]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fundamentals, setFundamentals] = useState<Record<string, any> | null>(null);

  const availableProviders = [
    { id: 'yfinance', name: 'Yahoo Finance', description: 'Free, no API key required' },
    { id: 'fmp', name: 'Financial Modeling Prep', description: 'Requires FMP API key' },
    { id: 'alphavantage', name: 'Alpha Vantage', description: 'Requires Alpha Vantage API key' },
  ];

  const dataTypes = [
    { id: 'overview', name: 'Company Overview', description: 'P/E, EPS, Market Cap, etc.' },
    { id: 'balance_sheet', name: 'Balance Sheet', description: 'Assets, liabilities, equity' },
    { id: 'income_statement', name: 'Income Statement', description: 'Revenue, expenses, profit' },
    { id: 'cash_flow', name: 'Cash Flow Statement', description: 'Operating, investing, financing' },
    { id: 'earnings', name: 'Earnings History', description: 'Historical EPS and surprises' },
  ];

  const fetchFundamentals = async () => {
    setLoading(true);
    setError(null);
    setFundamentals(null);

    try {
      const params = new URLSearchParams({
        symbol,
        provider,
        data_type: dataType,
        frequency,
      });

      if (useCustomDates) {
        params.set('start_date', startDate);
        params.set('end_date', endDate);
      } else {
        params.set('lookback_periods', lookbackPeriods.toString());
        params.set('end_date', endDate);
      }

      const response = await fetch(`${API_BASE}/tools/fundamentals/fetch?${params}`);

      if (response.ok) {
        const data = await response.json();
        setFundamentals(data);
      } else {
        const errorData = await response.json();
        setError(errorData.detail || 'Failed to fetch fundamentals');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setLoading(false);
    }
  };

  // Format large numbers for display
  const formatValue = (value: any): string => {
    if (value === null || value === undefined) return '-';
    if (typeof value === 'number') {
      if (Math.abs(value) >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
      if (Math.abs(value) >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
      if (Math.abs(value) >= 1e3) return `$${(value / 1e3).toFixed(2)}K`;
      return value.toLocaleString();
    }
    return String(value);
  };

  // Render periods data (balance sheet, income statement, cash flow)
  const renderPeriods = () => {
    // Handle both 'periods' (yfinance/unified) and 'statements' (FMP) keys
    const periods = fundamentals?.periods || fundamentals?.statements || [];
    if (periods.length === 0) {
      return <p className="text-gray-500 dark:text-gray-400">No period data available</p>;
    }

    return (
      <div className="space-y-6">
        {periods.map((period: any, idx: number) => {
          // Support both old format (items dict) and new format (flat dict with fiscal_date)
          const periodDate = period.date || period.fiscal_date || period.fiscal_date_ending || 'Unknown';
          const items = period.items || Object.fromEntries(
            Object.entries(period).filter(([k]) => !['date', 'fiscal_date', 'fiscal_date_ending', 'reported_currency'].includes(k))
          );

          return (
            <div key={idx} className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
              <h4 className="font-medium text-gray-900 dark:text-gray-100 mb-3">
                Period: {periodDate}
              </h4>
              <div className="overflow-x-auto max-h-64 overflow-y-auto">
                <table className="min-w-full text-sm">
                  <thead className="bg-gray-50 dark:bg-gray-700 sticky top-0">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Item</th>
                      <th className="px-3 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Value</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                    {Object.entries(items).map(([key, value]) => (
                      <tr key={key} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                        <td className="px-3 py-1 text-gray-900 dark:text-gray-100">{key}</td>
                        <td className="px-3 py-1 text-right text-gray-600 dark:text-gray-400 font-mono">
                          {formatValue(value)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  // Render earnings data
  const renderEarnings = () => {
    // Handle both 'earnings' (FMP) and 'periods' (unified service) keys
    const earnings = fundamentals?.earnings || fundamentals?.periods || [];
    if (earnings.length === 0) {
      return <p className="text-gray-500 dark:text-gray-400">No earnings data available</p>;
    }

    return (
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50 dark:bg-gray-700">
            <tr>
              <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Date</th>
              <th className="px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Reported EPS</th>
              <th className="px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Estimated EPS</th>
              <th className="px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Surprise</th>
              <th className="px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Surprise %</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
            {earnings.map((earning: any, idx: number) => {
              const fiscalDate = earning.fiscal_date_ending || earning.fiscal_date || 'Unknown';
              return (
              <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                <td className="px-4 py-2 text-gray-900 dark:text-gray-100">{fiscalDate}</td>
                <td className="px-4 py-2 text-right font-mono text-gray-600 dark:text-gray-400">
                  ${earning.reported_eps?.toFixed(2) || '-'}
                </td>
                <td className="px-4 py-2 text-right font-mono text-gray-600 dark:text-gray-400">
                  {earning.estimated_eps != null ? `$${earning.estimated_eps.toFixed(2)}` : '-'}
                </td>
                <td className={`px-4 py-2 text-right font-mono ${earning.surprise > 0 ? 'text-green-600' : earning.surprise < 0 ? 'text-red-600' : 'text-gray-600'}`}>
                  {earning.surprise != null ? `$${earning.surprise.toFixed(2)}` : '-'}
                </td>
                <td className={`px-4 py-2 text-right font-mono ${earning.surprise_percent > 0 ? 'text-green-600' : earning.surprise_percent < 0 ? 'text-red-600' : 'text-gray-600'}`}>
                  {earning.surprise_percent != null ? `${earning.surprise_percent.toFixed(1)}%` : '-'}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  };

  // Render overview data (current metrics)
  const renderOverview = () => {
    const current = fundamentals?.current || fundamentals?.metrics || {};
    if (Object.keys(current).length === 0) {
      return <p className="text-gray-500 dark:text-gray-400">No overview data available</p>;
    }

    return (
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-gray-50 dark:bg-gray-700">
            <tr>
              <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Metric</th>
              <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Value</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
            {Object.entries(current).map(([key, value]) => (
              <tr key={key} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                <td className="px-4 py-2 text-gray-900 dark:text-gray-100 font-medium">
                  {key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}
                </td>
                <td className="px-4 py-2 text-gray-600 dark:text-gray-400 font-mono">
                  {formatValue(value)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  };

  // Render the appropriate data based on data type
  const renderData = () => {
    if (!fundamentals) return null;

    if (dataType === 'overview') {
      return renderOverview();
    } else if (dataType === 'earnings' || dataType === 'past_earnings') {
      return renderEarnings();
    } else {
      return renderPeriods();
    }
  };

  return (
    <div className="space-y-6">
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
        <h2 className="text-xl font-semibold mb-4 text-gray-900 dark:text-gray-100">
          Test Fundamentals Provider
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Fetch historical fundamental data (balance sheets, income statements, cash flow, earnings) for a ticker.
        </p>

        <div className="space-y-4">
          {/* Row 1: Symbol, Provider, Data Type */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Symbol
              </label>
              <input
                type="text"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                placeholder="AAPL"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Provider
              </label>
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              >
                {availableProviders.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Data Type
              </label>
              <select
                value={dataType}
                onChange={(e) => setDataType(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              >
                {dataTypes.map((dt) => (
                  <option key={dt.id} value={dt.id}>
                    {dt.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Frequency
              </label>
              <select
                value={frequency}
                onChange={(e) => setFrequency(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                disabled={dataType === 'overview'}
              >
                <option value="quarterly">Quarterly</option>
                <option value="annual">Annual</option>
              </select>
            </div>
          </div>

          {/* Row 2: Date Range Options */}
          <div className="flex flex-wrap gap-4 items-end">
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="useCustomDates"
                checked={useCustomDates}
                onChange={(e) => setUseCustomDates(e.target.checked)}
                className="rounded border-gray-300 dark:border-gray-600"
              />
              <label htmlFor="useCustomDates" className="text-sm text-gray-700 dark:text-gray-300">
                Use custom date range
              </label>
            </div>

            {useCustomDates ? (
              <>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Start Date
                  </label>
                  <input
                    type="date"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                    className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    End Date
                  </label>
                  <input
                    type="date"
                    value={endDate}
                    onChange={(e) => setEndDate(e.target.value)}
                    className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                  />
                </div>
              </>
            ) : (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Lookback Periods
                </label>
                <input
                  type="number"
                  value={lookbackPeriods}
                  onChange={(e) => setLookbackPeriods(parseInt(e.target.value) || 8)}
                  min={1}
                  max={20}
                  className="w-24 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                />
              </div>
            )}

            <button
              onClick={fetchFundamentals}
              disabled={loading || !symbol}
              className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
            >
              {loading ? (
                <>
                  <Loader size={16} className="animate-spin" />
                  Fetching...
                </>
              ) : (
                <>
                  <Search size={16} />
                  Fetch Data
                </>
              )}
            </button>
          </div>
        </div>

        {error && (
          <div className="mt-4 p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-md flex items-center gap-2">
            <XCircle size={16} />
            {error}
          </div>
        )}
      </div>

      {fundamentals && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              {dataTypes.find(dt => dt.id === dataType)?.name} for {fundamentals.symbol || symbol}
            </h3>
            <div className="flex items-center gap-2">
              <span className="px-2 py-1 text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 rounded">
                Provider: {fundamentals.provider || provider}
              </span>
              {fundamentals.frequency && (
                <span className="px-2 py-1 text-xs bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded">
                  {fundamentals.frequency}
                </span>
              )}
            </div>
          </div>

          {renderData()}
        </div>
      )}
    </div>
  );
};

// Macro Indicators Tester Component
const MacroTester: React.FC = () => {
  const [indicators, setIndicators] = useState<string[]>(['interest_rate', 'gdp', 'inflation', 'unemployment']);
  const [provider, setProvider] = useState('fred');
  const [startDate, setStartDate] = useState(() => {
    const d = new Date();
    d.setFullYear(d.getFullYear() - 1);
    return d.toISOString().split('T')[0];
  });
  const [endDate, setEndDate] = useState(() => new Date().toISOString().split('T')[0]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [macroData, setMacroData] = useState<Record<string, any> | null>(null);

  const availableProviders = [
    { id: 'fred', name: 'FRED', description: 'Federal Reserve Economic Data' },
  ];

  const availableIndicators = [
    { id: 'interest_rate', name: 'Federal Funds Rate' },
    { id: 'gdp', name: 'GDP' },
    { id: 'inflation', name: 'CPI (Inflation)' },
    { id: 'unemployment', name: 'Unemployment Rate' },
    { id: 'vix', name: 'VIX Volatility' },
    { id: 'yield_10y', name: '10-Year Treasury Yield' },
    { id: 'yield_2y', name: '2-Year Treasury Yield' },
  ];

  const toggleIndicator = (id: string) => {
    setIndicators(prev =>
      prev.includes(id) ? prev.filter(i => i !== id) : [...prev, id]
    );
  };

  const fetchMacroData = async () => {
    setLoading(true);
    setError(null);
    setMacroData(null);

    try {
      const params = new URLSearchParams({
        indicators: indicators.join(','),
        provider,
        start_date: startDate,
        end_date: endDate,
      });

      const response = await fetch(`${API_BASE}/tools/macro/fetch?${params}`);

      if (response.ok) {
        const data = await response.json();
        setMacroData(data);
      } else {
        const errorData = await response.json();
        setError(errorData.detail || 'Failed to fetch macro data');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
        <h2 className="text-xl font-semibold mb-4 text-gray-900 dark:text-gray-100">
          Test Macro Indicators Provider
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Fetch macroeconomic indicators from FRED (Federal Reserve Economic Data).
        </p>

        <div className="space-y-4">
          <div className="flex flex-wrap gap-4 items-end">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Provider
              </label>
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              >
                {availableProviders.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Indicators
            </label>
            <div className="flex flex-wrap gap-2">
              {availableIndicators.map(ind => (
                <label
                  key={ind.id}
                  className={`px-3 py-1.5 rounded-full cursor-pointer text-sm transition-colors ${
                    indicators.includes(ind.id)
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={indicators.includes(ind.id)}
                    onChange={() => toggleIndicator(ind.id)}
                    className="sr-only"
                  />
                  {ind.name}
                </label>
              ))}
            </div>
          </div>

          <div className="flex flex-wrap gap-4 items-end">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Start Date
              </label>
              <input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                End Date
              </label>
              <input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
            </div>

            <button
              onClick={fetchMacroData}
              disabled={loading || indicators.length === 0}
              className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
            >
              {loading ? (
                <>
                  <Loader size={16} className="animate-spin" />
                  Fetching...
                </>
              ) : (
                <>
                  <Search size={16} />
                  Fetch Macro Data
                </>
              )}
            </button>
          </div>
        </div>

        {error && (
          <div className="mt-4 p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-md flex items-center gap-2">
            <XCircle size={16} />
            {error}
          </div>
        )}
      </div>

      {macroData && macroData.indicators && (
        <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              Macro Data Results
            </h3>
            <span className="px-2 py-1 text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 rounded">
              Provider: {macroData.provider || 'fred'}
            </span>
          </div>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
            {macroData.start_date} to {macroData.end_date}
          </p>

          <div className="space-y-6">
            {Object.entries(macroData.indicators).map(([indicator, data]: [string, any]) => (
              <div key={indicator} className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
                <h4 className="font-medium text-gray-900 dark:text-gray-100 mb-2">
                  {data.name || indicator}
                </h4>
                <p className="text-sm text-gray-500 dark:text-gray-400 mb-2">
                  {data.description} ({data.unit})
                </p>
                {data.data && data.data.length > 0 ? (
                  <div className="overflow-x-auto max-h-48 overflow-y-auto">
                    <table className="min-w-full text-sm">
                      <thead className="bg-gray-50 dark:bg-gray-700 sticky top-0">
                        <tr>
                          <th className="px-3 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Date</th>
                          <th className="px-3 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Value</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                        {data.data.slice(-20).map((row: any, idx: number) => (
                          <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                            <td className="px-3 py-1 text-gray-600 dark:text-gray-400">{row.date}</td>
                            <td className="px-3 py-1 text-gray-900 dark:text-gray-100 font-mono">{row.value?.toFixed(2) || '-'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {data.data.length > 20 && (
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                        Showing last 20 of {data.data.length} data points
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="text-gray-500 dark:text-gray-400">No data available</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

// Maintenance Panel Component
interface OrphanModel {
  file_path: string;
  file_name: string;
  job_id: string;
  size_mb: number;
  job_exists: boolean;
  in_inventory: boolean;
}

const MaintenancePanel: React.FC = () => {
  const [scanning, setScanning] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [orphanModels, setOrphanModels] = useState<OrphanModel[]>([]);
  const [totalSizeMB, setTotalSizeMB] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });

  const scanOrphanModels = async () => {
    setScanning(true);
    setError(null);
    setMessage(null);

    try {
      const response = await fetch(`${API_BASE}/tools/maintenance/orphan-models`);
      if (response.ok) {
        const data = await response.json();
        setOrphanModels(data.orphan_models || []);
        setTotalSizeMB(data.total_size_mb || 0);
        setMessage(`Found ${data.total} orphan model files (${data.total_size_mb} MB)`);
      } else {
        const errorData = await response.json();
        setError(errorData.detail || 'Failed to scan orphan models');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setScanning(false);
    }
  };

  const cleanupOrphanModels = async (dryRun: boolean) => {
    setCleaning(true);
    setError(null);
    setMessage(null);

    try {
      const response = await fetch(
        `${API_BASE}/tools/maintenance/orphan-models?dry_run=${dryRun}`,
        { method: 'DELETE' }
      );
      if (response.ok) {
        const data = await response.json();
        setMessage(data.message || `Cleaned up ${data.total} items`);
        if (!dryRun) {
          // Refresh the scan
          await scanOrphanModels();
        }
      } else {
        const errorData = await response.json();
        setError(errorData.detail || 'Failed to cleanup orphan models');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error');
    } finally {
      setCleaning(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="bg-white dark:bg-gray-800 p-6 rounded-lg shadow">
        <h2 className="text-xl font-semibold mb-4 text-gray-900 dark:text-gray-100 flex items-center gap-2">
          <Trash2 size={24} className="text-red-500" />
          Orphan Model Cleanup
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Find and remove trained model files that are no longer associated with any job or saved in the model inventory.
          These are models whose training jobs have been deleted but the files remain on disk.
        </p>

        <div className="flex items-center gap-4 mb-4">
          <button
            onClick={scanOrphanModels}
            disabled={scanning}
            className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
          >
            {scanning ? (
              <>
                <Loader size={16} className="animate-spin" />
                Scanning...
              </>
            ) : (
              <>
                <Search size={16} />
                Scan for Orphan Models
              </>
            )}
          </button>

          {orphanModels.length > 0 && (
            <>
              <button
                onClick={() => cleanupOrphanModels(true)}
                disabled={cleaning}
                className="px-4 py-2 bg-yellow-600 text-white rounded-md hover:bg-yellow-700 disabled:opacity-50 flex items-center gap-2"
              >
                {cleaning ? (
                  <>
                    <Loader size={16} className="animate-spin" />
                    Processing...
                  </>
                ) : (
                  <>
                    <AlertCircle size={16} />
                    Dry Run (Preview)
                  </>
                )}
              </button>

              <button
                onClick={() => {
                  setConfirmDialog({
                    isOpen: true,
                    title: 'Delete Orphan Models',
                    message: `Are you sure you want to delete ${orphanModels.length} orphan model folders (${totalSizeMB} MB)? This cannot be undone.`,
                    variant: 'danger',
                    onConfirm: () => cleanupOrphanModels(false),
                  });
                }}
                disabled={cleaning}
                className="px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50 flex items-center gap-2"
              >
                <Trash2 size={16} />
                Delete Orphan Models
              </button>
            </>
          )}
        </div>

        {message && (
          <div className="p-4 bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 rounded-md flex items-center gap-2 mb-4">
            <CheckCircle size={16} />
            {message}
          </div>
        )}

        {error && (
          <div className="p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-md flex items-center gap-2 mb-4">
            <XCircle size={16} />
            {error}
          </div>
        )}

        {orphanModels.length > 0 && (
          <div className="border border-gray-200 dark:border-gray-700 rounded-lg">
            <div className="p-3 bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600 flex justify-between items-center">
              <span className="font-medium text-gray-700 dark:text-gray-300">
                Orphan Models ({orphanModels.length})
              </span>
              <span className="text-sm text-gray-500 dark:text-gray-400">
                Total Size: {totalSizeMB} MB
              </span>
            </div>
            <div className="max-h-64 overflow-y-auto">
              <table className="min-w-full text-sm">
                <thead className="bg-gray-50 dark:bg-gray-700 sticky top-0">
                  <tr>
                    <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">File</th>
                    <th className="px-4 py-2 text-left font-medium text-gray-700 dark:text-gray-300">Job ID</th>
                    <th className="px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">Size (MB)</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                  {orphanModels.map((model, idx) => (
                    <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                      <td className="px-4 py-2 text-gray-900 dark:text-gray-100 font-mono text-xs truncate max-w-xs">
                        {model.file_name}
                      </td>
                      <td className="px-4 py-2 text-gray-600 dark:text-gray-400 font-mono text-xs">
                        {model.job_id}
                      </td>
                      <td className="px-4 py-2 text-right text-gray-600 dark:text-gray-400">
                        {model.size_mb}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {!scanning && orphanModels.length === 0 && message && (
          <div className="text-center py-8 text-gray-500 dark:text-gray-400">
            <CheckCircle size={48} className="mx-auto mb-2 text-green-500" />
            <p>No orphan models found. Your storage is clean!</p>
          </div>
        )}
      </div>

      {/* Confirm Dialog */}
      <ConfirmDialog
        isOpen={confirmDialog.isOpen}
        onClose={() => setConfirmDialog(prev => ({ ...prev, isOpen: false }))}
        onConfirm={confirmDialog.onConfirm}
        title={confirmDialog.title}
        message={confirmDialog.message}
        variant={confirmDialog.variant}
        confirmText="Delete"
      />
    </div>
  );
};

export default Tools;
