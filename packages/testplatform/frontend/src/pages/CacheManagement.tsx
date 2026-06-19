import React, { useState, useEffect } from 'react';
import {
  HardDrive,
  Database,
  Newspaper,
  Briefcase,
  Library,
  Download,
  Archive,
  FileText,
  Trash2,
  RefreshCw,
  Loader,
  CheckCircle,
  XCircle,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Play,
} from 'lucide-react';
import ConfirmDialog from '../components/ConfirmDialog';
import {
  buildOhlcv,
  buildScreenerMetrics,
  buildOptions,
  prewarmData,
} from '../lib/btApi';
import type { BuildResult } from '../lib/btApi';
import { parseSymbols } from '../lib/symbols';

// Flatten a build endpoint's {task_id} | {tasks:[...]} response into the task-id strings
// so they can be surfaced to the user (the platform's task mechanism then tracks them).
const taskIdsOf = (r: BuildResult): string[] =>
  'tasks' in r ? (r.tasks || []).map((t) => t.task_id) : r.task_id ? [r.task_id] : [];

// ---- Contract (matches app/api/cache.py / cache_manager.get_usage) ----
interface CacheTypeUsage {
  bytes: number;
  files: number;
  oldest: string | null;
  newest: string | null;
  exists: boolean;
  destructive: boolean;
  ttl_hours: number | null;
  db_stats?: { total_articles?: number } | null;
}
type Usage = Record<string, CacheTypeUsage>;

interface ClearResult {
  bytes_freed?: number;
  files_removed?: number;
  db_rows_deleted?: number;
  skipped?: string;
}

const API = 'http://localhost:8000/api/cache';

const TYPE_META: Record<string, { label: string; icon: React.ComponentType<{ size?: number; className?: string }>; description: string }> = {
  ohlcv: { label: 'OHLCV', icon: HardDrive, description: 'Per-provider price-bar CSV/parquet cache (24h TTL).' },
  jobs: { label: 'Jobs', icon: Briefcase, description: 'Per-task training/job model cache.' },
  news: { label: 'News', icon: Newspaper, description: 'News article content files + DB rows.' },
  datasets: { label: 'Datasets', icon: Database, description: 'Generated dataset CSVs (irreplaceable).' },
  models: { label: 'Models', icon: Library, description: 'Trained model artifacts (irreplaceable).' },
  exports: { label: 'Exports', icon: Download, description: 'Exported news JSON files.' },
  asof: { label: 'As-Of Cache', icon: Archive, description: 'ba2_providers point-in-time provider cache.' },
  fmp_history: { label: 'FMP History', icon: FileText, description: 'Backtest-only per-symbol FMP history JSON (analyst grades, price targets, earnings, insider, financial statements).' },
};

const fmtBytes = (b: number): string => {
  if (!b) return '0 B';
  if (b < 1024) return `${b} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  let n = b;
  do {
    n /= 1024;
    i++;
  } while (n >= 1024 && i < units.length - 1);
  return `${n.toFixed(1)} ${units[i]}`;
};

const CacheManagement: React.FC = () => {
  const [usage, setUsage] = useState<Usage>({});
  const [loading, setLoading] = useState(false);
  const [beforeDate, setBeforeDate] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busyType, setBusyType] = useState<string | null>(null);

  // Drill-down state (per type)
  const [expanded, setExpanded] = useState<string | null>(null);
  const [drillItems, setDrillItems] = useState<Record<string, any>[]>([]);
  const [drillLoading, setDrillLoading] = useState(false);

  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });

  // --- Build / prewarm panel state (P2.6). Mirrors the ba2-test CLI data-prep commands. ---
  const [buildOpen, setBuildOpen] = useState(false);
  const [buildBusy, setBuildBusy] = useState<string | null>(null);  // which build is in-flight
  // OHLCV
  const [ohlcvSymbols, setOhlcvSymbols] = useState('');
  const [ohlcvTimeframes, setOhlcvTimeframes] = useState('1d');
  const [ohlcvStart, setOhlcvStart] = useState('2024-01-01');
  const [ohlcvEnd, setOhlcvEnd] = useState('2024-12-31');
  const [ohlcvProvider, setOhlcvProvider] = useState('');
  // Screener metrics
  const [smStore, setSmStore] = useState('~/Documents/ba2/trade/screener/metric_store');
  const [smStart, setSmStart] = useState('2024-01-01');
  const [smEnd, setSmEnd] = useState('2024-12-31');
  const [smMarketCapMin, setSmMarketCapMin] = useState(1e9);
  const [smPriceMin, setSmPriceMin] = useState('');
  const [smVolumeMin, setSmVolumeMin] = useState('');
  const [smCadenceDays, setSmCadenceDays] = useState(7);
  const [smDropDays, setSmDropDays] = useState('');
  // Options
  const [optUnderlyings, setOptUnderlyings] = useState('');
  const [optStart, setOptStart] = useState('2024-01-01');
  const [optEnd, setOptEnd] = useState('2024-12-31');
  const [optCacheDb, setOptCacheDb] = useState('');
  const [optFeed, setOptFeed] = useState('');
  // Prewarm
  const [pwSymbols, setPwSymbols] = useState('');
  const [pwExperts, setPwExperts] = useState('');
  const [pwWorkers, setPwWorkers] = useState('');
  const [pwEnd, setPwEnd] = useState('');

  // Run a build action, surfacing the returned task id(s) in the shared message banner.
  const runBuild = async (key: string, fn: () => Promise<BuildResult>) => {
    setBuildBusy(key);
    setError(null);
    setMessage(null);
    try {
      const r = await fn();
      const ids = taskIdsOf(r);
      setMessage(ids.length
        ? `${key}: queued ${ids.length} task(s) — ${ids.join(', ')}. Track progress in Running jobs.`
        : `${key}: queued.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Build request failed');
    } finally {
      setBuildBusy(null);
    }
  };

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`${API}/usage`);
      if (!r.ok) throw new Error(`usage request failed (${r.status})`);
      const data = await r.json();
      setUsage(data.types || {});
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const loadDrillDown = async (type: string) => {
    setDrillItems([]);
    setDrillLoading(true);
    try {
      const r = await fetch(`${API}/usage/${type}`);
      if (!r.ok) throw new Error(`drill-down failed (${r.status})`);
      const data = await r.json();
      setDrillItems(data.items || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error');
    } finally {
      setDrillLoading(false);
    }
  };

  const toggleDrillDown = async (type: string) => {
    if (expanded === type) {
      setExpanded(null);
      setDrillItems([]);
      return;
    }
    setExpanded(type);
    await loadDrillDown(type);
  };

  const summarizeResult = (res: ClearResult): string => {
    if (res.skipped) return res.skipped;
    const parts: string[] = [];
    if (res.files_removed != null) parts.push(`${res.files_removed} files`);
    if (res.bytes_freed != null) parts.push(fmtBytes(res.bytes_freed));
    if (res.db_rows_deleted != null) parts.push(`${res.db_rows_deleted} DB rows`);
    return parts.length ? `Removed ${parts.join(', ')}` : 'Nothing to remove';
  };

  const doClearType = async (type: string, useDate: boolean) => {
    setBusyType(type);
    setError(null);
    setMessage(null);
    try {
      const qs = useDate && beforeDate ? `?before=${beforeDate}` : '';
      const r = await fetch(`${API}/${type}${qs}`, { method: 'DELETE' });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `delete ${type} -> ${r.status}`);
      }
      const res: ClearResult = await r.json();
      setMessage(`${TYPE_META[type]?.label || type}: ${summarizeResult(res)}`);
      await refresh();
      if (expanded === type) await loadDrillDown(type);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error');
    } finally {
      setBusyType(null);
    }
  };

  const requestCleanType = (type: string, useDate: boolean) => {
    const t = usage[type];
    const dateClause = useDate && beforeDate ? ` older than ${beforeDate}` : '';
    if (t?.destructive) {
      setConfirmDialog({
        isOpen: true,
        title: `Delete "${TYPE_META[type]?.label || type}" cache`,
        message: `"${TYPE_META[type]?.label || type}" is DESTRUCTIVE and irreplaceable. Really delete all entries${dateClause}? This cannot be undone.`,
        variant: 'danger',
        onConfirm: () => doClearType(type, useDate),
      });
    } else {
      setConfirmDialog({
        isOpen: true,
        title: `Clean "${TYPE_META[type]?.label || type}" cache`,
        message: `Clean cache "${TYPE_META[type]?.label || type}"${dateClause}?`,
        variant: 'warning',
        onConfirm: () => doClearType(type, useDate),
      });
    }
  };

  const doCleanAll = async () => {
    setBusyType('__all__');
    setError(null);
    setMessage(null);
    try {
      const qs = beforeDate ? `?before=${beforeDate}` : '';
      const r = await fetch(`${API}${qs}`, { method: 'DELETE' });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `clean all -> ${r.status}`);
      }
      const res: Record<string, ClearResult> = await r.json();
      const freed = Object.values(res).reduce((acc, v) => acc + (v.bytes_freed || 0), 0);
      const removed = Object.values(res).reduce((acc, v) => acc + (v.files_removed || 0), 0);
      setMessage(`Clean All: removed ${removed} files (${fmtBytes(freed)}); datasets + models kept.`);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error');
    } finally {
      setBusyType(null);
    }
  };

  const requestCleanAll = () => {
    const dateClause = beforeDate ? ` older than ${beforeDate}` : '';
    setConfirmDialog({
      isOpen: true,
      title: 'Clean All Caches',
      message: `Clean ALL non-destructive caches${dateClause}? (datasets + trained_models are kept — clear those explicitly per-type.)`,
      variant: 'warning',
      onConfirm: doCleanAll,
    });
  };

  const renderDrillRow = (item: Record<string, any>, idx: number) => {
    // OHLCV: provider/symbol/interval/bytes/mtime/stale
    if ('symbol' in item || 'interval' in item) {
      return (
        <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
          <td className="px-3 py-1 text-gray-900 dark:text-gray-100 font-mono text-xs">{item.provider || '-'}</td>
          <td className="px-3 py-1 text-gray-900 dark:text-gray-100 font-mono text-xs">{item.symbol}</td>
          <td className="px-3 py-1 text-gray-600 dark:text-gray-400 text-xs">{item.interval}</td>
          <td className="px-3 py-1 text-right text-gray-600 dark:text-gray-400 text-xs">{fmtBytes(item.bytes || 0)}</td>
          <td className="px-3 py-1 text-xs">
            {item.stale ? (
              <span className="text-orange-600 dark:text-orange-400">stale</span>
            ) : (
              <span className="text-green-600 dark:text-green-400">fresh</span>
            )}
          </td>
        </tr>
      );
    }
    // News: provider/articles
    if ('articles' in item) {
      return (
        <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
          <td className="px-3 py-1 text-gray-900 dark:text-gray-100 font-mono text-xs">{item.provider}</td>
          <td className="px-3 py-1 text-right text-gray-600 dark:text-gray-400 text-xs" colSpan={4}>
            {item.articles} articles
          </td>
        </tr>
      );
    }
    // FMP History: namespace/files/bytes
    if ('namespace' in item) {
      return (
        <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
          <td className="px-3 py-1 text-gray-900 dark:text-gray-100 font-mono text-xs" colSpan={2}>{item.namespace}</td>
          <td className="px-3 py-1 text-right text-gray-600 dark:text-gray-400 text-xs">{item.files} files</td>
          <td className="px-3 py-1 text-right text-gray-600 dark:text-gray-400 text-xs" colSpan={2}>{fmtBytes(item.bytes || 0)}</td>
        </tr>
      );
    }
    // Jobs/models: task_id/bytes
    if ('task_id' in item) {
      return (
        <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
          <td className="px-3 py-1 text-gray-900 dark:text-gray-100 font-mono text-xs" colSpan={3}>{item.task_id}</td>
          <td className="px-3 py-1 text-right text-gray-600 dark:text-gray-400 text-xs" colSpan={2}>{fmtBytes(item.bytes || 0)}</td>
        </tr>
      );
    }
    // Flat (datasets/exports/asof): name/bytes/mtime
    return (
      <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
        <td className="px-3 py-1 text-gray-900 dark:text-gray-100 font-mono text-xs truncate max-w-xs" colSpan={3}>{item.name}</td>
        <td className="px-3 py-1 text-right text-gray-600 dark:text-gray-400 text-xs" colSpan={2}>{fmtBytes(item.bytes || 0)}</td>
      </tr>
    );
  };

  const orderedTypes = Object.keys(usage).sort((a, b) => {
    // keep destructive types last for safety/visual grouping
    const da = usage[a].destructive ? 1 : 0;
    const db = usage[b].destructive ? 1 : 0;
    if (da !== db) return da - db;
    return a.localeCompare(b);
  });

  return (
    <div className="p-6">
      <div className="mb-6 flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <HardDrive size={32} />
            Cache Management
          </h1>
          <p className="text-gray-600 dark:text-gray-400 mt-1">
            Disk usage per cache type — clean all, by type, or by date. Datasets + trained models are
            protected from "Clean All".
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div>
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-0.5">
              Older than
            </label>
            <input
              type="date"
              value={beforeDate}
              onChange={(e) => setBeforeDate(e.target.value)}
              className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              title="Optional: only delete entries older than this date"
            />
          </div>
          <button
            onClick={refresh}
            disabled={loading}
            className="px-4 py-2 mt-5 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50 flex items-center gap-2"
          >
            {loading ? <Loader size={16} className="animate-spin" /> : <RefreshCw size={16} />}
            Refresh
          </button>
          <button
            onClick={requestCleanAll}
            disabled={busyType === '__all__'}
            className="px-4 py-2 mt-5 bg-red-600 text-white rounded-md hover:bg-red-700 disabled:opacity-50 flex items-center gap-2"
          >
            {busyType === '__all__' ? <Loader size={16} className="animate-spin" /> : <Trash2 size={16} />}
            Clean All
          </button>
        </div>
      </div>

      {message && (
        <div className="mb-4 p-4 bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 rounded-md flex items-center gap-2">
          <CheckCircle size={16} />
          {message}
        </div>
      )}
      {error && (
        <div className="mb-4 p-4 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-md flex items-center gap-2">
          <XCircle size={16} />
          {error}
        </div>
      )}

      {/* Build / prewarm panel (P2.6) — POSTs to /api/data/* and surfaces the returned task id(s). */}
      <div className="mb-6 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-sm">
        <button
          onClick={() => setBuildOpen((o) => !o)}
          className="w-full flex items-center justify-between px-4 py-3 text-left"
        >
          <span className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <Download size={18} />
            Build / Prewarm data
          </span>
          {buildOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>
        {buildOpen && (
          <div className="px-4 pb-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* OHLCV */}
            <div className="border border-gray-200 dark:border-gray-700 rounded-md p-3 space-y-2">
              <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">OHLCV history</h4>
              <textarea
                value={ohlcvSymbols}
                onChange={(e) => setOhlcvSymbols(e.target.value)}
                placeholder="Symbols: AAPL, MSFT, NVDA …"
                rows={2}
                className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
              <div className="grid grid-cols-2 gap-2">
                <input value={ohlcvTimeframes} onChange={(e) => setOhlcvTimeframes(e.target.value)}
                  placeholder="Timeframes: 1d,5m" title="Comma-separated timeframes"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input value={ohlcvProvider} onChange={(e) => setOhlcvProvider(e.target.value)}
                  placeholder="Provider (optional)"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="date" value={ohlcvStart} onChange={(e) => setOhlcvStart(e.target.value)}
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="date" value={ohlcvEnd} onChange={(e) => setOhlcvEnd(e.target.value)}
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              </div>
              <button
                onClick={() => runBuild('OHLCV', () => buildOhlcv({
                  symbols: parseSymbols(ohlcvSymbols),
                  timeframes: ohlcvTimeframes.split(',').map((s) => s.trim()).filter(Boolean),
                  start: ohlcvStart, end: ohlcvEnd,
                  ...(ohlcvProvider.trim() ? { provider: ohlcvProvider.trim() } : {}),
                }))}
                disabled={buildBusy === 'OHLCV' || parseSymbols(ohlcvSymbols).length === 0}
                className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
              >
                {buildBusy === 'OHLCV' ? <Loader size={14} className="animate-spin" /> : <Play size={14} />}
                Build OHLCV
              </button>
            </div>

            {/* Screener metrics */}
            <div className="border border-gray-200 dark:border-gray-700 rounded-md p-3 space-y-2">
              <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Screener metrics</h4>
              <input value={smStore} onChange={(e) => setSmStore(e.target.value)}
                placeholder="Metric-store path"
                className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              <div className="grid grid-cols-2 gap-2">
                <input type="date" value={smStart} onChange={(e) => setSmStart(e.target.value)}
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="date" value={smEnd} onChange={(e) => setSmEnd(e.target.value)}
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="number" value={smMarketCapMin} onChange={(e) => setSmMarketCapMin(Number(e.target.value))}
                  placeholder="Market cap min" title="market_cap_min"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="number" value={smCadenceDays} onChange={(e) => setSmCadenceDays(Number(e.target.value))}
                  placeholder="Cadence days" title="cadence_days"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="number" value={smPriceMin} onChange={(e) => setSmPriceMin(e.target.value)}
                  placeholder="Price min (opt)"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="number" value={smVolumeMin} onChange={(e) => setSmVolumeMin(e.target.value)}
                  placeholder="Volume min (opt)"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="number" value={smDropDays} onChange={(e) => setSmDropDays(e.target.value)}
                  placeholder="Drop days (opt)"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              </div>
              <button
                onClick={() => runBuild('Screener metrics', () => buildScreenerMetrics({
                  store: smStore.trim(), start: smStart, end: smEnd, market_cap_min: smMarketCapMin,
                  cadence_days: smCadenceDays,
                  ...(smPriceMin.trim() ? { price_min: Number(smPriceMin) } : {}),
                  ...(smVolumeMin.trim() ? { volume_min: Number(smVolumeMin) } : {}),
                  ...(smDropDays.trim() ? { drop_days: Number(smDropDays) } : {}),
                }))}
                disabled={buildBusy === 'Screener metrics' || !smStore.trim()}
                className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
              >
                {buildBusy === 'Screener metrics' ? <Loader size={14} className="animate-spin" /> : <Play size={14} />}
                Build screener metrics
              </button>
            </div>

            {/* Options */}
            <div className="border border-gray-200 dark:border-gray-700 rounded-md p-3 space-y-2">
              <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Options chains</h4>
              <textarea
                value={optUnderlyings}
                onChange={(e) => setOptUnderlyings(e.target.value)}
                placeholder="Underlyings: AAPL, SPY …"
                rows={2}
                className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
              <input value={optCacheDb} onChange={(e) => setOptCacheDb(e.target.value)}
                placeholder="Cache DB path"
                className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              <div className="grid grid-cols-2 gap-2">
                <input type="date" value={optStart} onChange={(e) => setOptStart(e.target.value)}
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="date" value={optEnd} onChange={(e) => setOptEnd(e.target.value)}
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input value={optFeed} onChange={(e) => setOptFeed(e.target.value)}
                  placeholder="Feed (optional)"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              </div>
              <button
                onClick={() => runBuild('Options', () => buildOptions({
                  underlyings: parseSymbols(optUnderlyings), start: optStart, end: optEnd,
                  cache_db: optCacheDb.trim(),
                  ...(optFeed.trim() ? { feed: optFeed.trim() } : {}),
                }))}
                disabled={buildBusy === 'Options' || parseSymbols(optUnderlyings).length === 0 || !optCacheDb.trim()}
                className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
              >
                {buildBusy === 'Options' ? <Loader size={14} className="animate-spin" /> : <Play size={14} />}
                Build options
              </button>
            </div>

            {/* Prewarm */}
            <div className="border border-gray-200 dark:border-gray-700 rounded-md p-3 space-y-2">
              <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Prewarm expert caches</h4>
              <textarea
                value={pwSymbols}
                onChange={(e) => setPwSymbols(e.target.value)}
                placeholder="Symbols: AAPL, MSFT …"
                rows={2}
                className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
              />
              <input value={pwExperts} onChange={(e) => setPwExperts(e.target.value)}
                placeholder="Experts (optional, comma-separated)"
                className="w-full px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              <div className="grid grid-cols-2 gap-2">
                <input type="number" value={pwWorkers} onChange={(e) => setPwWorkers(e.target.value)}
                  placeholder="Workers (opt)"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
                <input type="date" value={pwEnd} onChange={(e) => setPwEnd(e.target.value)}
                  title="End date (optional)"
                  className="px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100" />
              </div>
              <button
                onClick={() => runBuild('Prewarm', () => prewarmData({
                  symbols: parseSymbols(pwSymbols),
                  ...(pwExperts.trim() ? { experts: pwExperts.split(',').map((s) => s.trim()).filter(Boolean) } : {}),
                  ...(pwWorkers.trim() ? { workers: Number(pwWorkers) } : {}),
                  ...(pwEnd.trim() ? { end: pwEnd } : {}),
                }))}
                disabled={buildBusy === 'Prewarm' || parseSymbols(pwSymbols).length === 0}
                className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
              >
                {buildBusy === 'Prewarm' ? <Loader size={14} className="animate-spin" /> : <Play size={14} />}
                Prewarm
              </button>
            </div>
          </div>
        )}
      </div>

      {loading && Object.keys(usage).length === 0 ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          <Loader size={32} className="mx-auto mb-2 animate-spin" />
          <p>Loading cache usage…</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {orderedTypes.map((type) => {
            const t = usage[type];
            const meta = TYPE_META[type] || { label: type, icon: HardDrive, description: '' };
            const Icon = meta.icon;
            const isBusy = busyType === type;
            return (
              <div
                key={type}
                className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-sm p-4 flex flex-col"
              >
                <div className="flex items-start justify-between mb-2">
                  <h3 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                    <Icon size={18} />
                    {meta.label}
                  </h3>
                  {t.destructive && (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400 flex items-center gap-1">
                      <AlertCircle size={12} />
                      protected
                    </span>
                  )}
                </div>
                {meta.description && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">{meta.description}</p>
                )}

                <div className="space-y-1 text-sm mb-3">
                  <div className="flex justify-between">
                    <span className="text-gray-500 dark:text-gray-400">Size</span>
                    <span className="font-mono text-gray-900 dark:text-gray-100">{fmtBytes(t.bytes)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500 dark:text-gray-400">Items</span>
                    <span className="font-mono text-gray-900 dark:text-gray-100">
                      {t.files}
                      {t.db_stats?.total_articles != null ? ` (+${t.db_stats.total_articles} rows)` : ''}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500 dark:text-gray-400">Newest</span>
                    <span className="font-mono text-gray-900 dark:text-gray-100">
                      {t.newest ? t.newest.slice(0, 10) : '—'}
                    </span>
                  </div>
                  {t.ttl_hours != null && (
                    <div className="flex justify-between">
                      <span className="text-gray-500 dark:text-gray-400">TTL</span>
                      <span className="font-mono text-gray-900 dark:text-gray-100">{t.ttl_hours}h</span>
                    </div>
                  )}
                </div>

                <div className="flex flex-wrap gap-2 mt-auto pt-2 border-t border-gray-100 dark:border-gray-700">
                  <button
                    onClick={() => requestCleanType(type, false)}
                    disabled={isBusy || t.files === 0}
                    className="px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-md text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 flex items-center gap-1"
                  >
                    {isBusy ? <Loader size={14} className="animate-spin" /> : <Trash2 size={14} />}
                    Clean
                  </button>
                  <button
                    onClick={() => requestCleanType(type, true)}
                    disabled={isBusy || !beforeDate || t.files === 0}
                    title={!beforeDate ? 'Pick an "Older than" date first' : undefined}
                    className="px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-md text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50 flex items-center gap-1"
                  >
                    <Trash2 size={14} />
                    Clean by Date
                  </button>
                  <button
                    onClick={() => toggleDrillDown(type)}
                    className="px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-md text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-1 ml-auto"
                  >
                    {expanded === type ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    Details
                  </button>
                </div>

                {expanded === type && (
                  <div className="mt-3 border-t border-gray-100 dark:border-gray-700 pt-3">
                    {drillLoading ? (
                      <div className="text-center py-3 text-gray-500 dark:text-gray-400 text-sm flex items-center justify-center gap-2">
                        <Loader size={14} className="animate-spin" />
                        Loading…
                      </div>
                    ) : drillItems.length === 0 ? (
                      <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-2">No items.</p>
                    ) : (
                      <div className="max-h-48 overflow-y-auto">
                        <table className="min-w-full text-sm">
                          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                            {drillItems.map((item, idx) => renderDrillRow(item, idx))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <ConfirmDialog
        isOpen={confirmDialog.isOpen}
        onClose={() => setConfirmDialog((prev) => ({ ...prev, isOpen: false }))}
        onConfirm={confirmDialog.onConfirm}
        title={confirmDialog.title}
        message={confirmDialog.message}
        variant={confirmDialog.variant}
        confirmText="Delete"
      />
    </div>
  );
};

export default CacheManagement;
