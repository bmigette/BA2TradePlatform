import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Target, BarChart3, Trash2, RefreshCw, ChevronDown, ChevronRight,
  AlertCircle, X, Loader2, Search, ChevronLeft
} from 'lucide-react';
import ConfirmDialog from '../components/ConfirmDialog';

interface IndicatorConfig {
  type: string;
  name: string;
  timeframe?: string;
  period?: number;
  [key: string]: unknown;
}

interface IndicatorCollection {
  id: number;
  name: string;
  description: string | null;
  is_default: boolean;
  indicators: IndicatorConfig[];
  created_at: string | null;
  updated_at: string | null;
}

interface BacktestStrategy {
  id: number;
  name: string;
  description: string | null;
  createdAt: string;
  updatedAt: string | null;
}

const API_BASE = 'http://localhost:8000/api';
const ITEMS_PER_PAGE = 10;

const SavedData: React.FC = () => {
  const [error, setError] = useState<string | null>(null);

  // Indicator Collections state
  const [indicatorCollections, setIndicatorCollections] = useState<IndicatorCollection[]>([]);
  const [collectionsLoading, setCollectionsLoading] = useState(true);
  const [expandedCollections, setExpandedCollections] = useState<Set<number>>(new Set());
  const [collectionsPage, setCollectionsPage] = useState(1);

  // Backtest Strategies state
  const [strategies, setStrategies] = useState<BacktestStrategy[]>([]);
  const [strategiesLoading, setStrategiesLoading] = useState(true);
  const [strategiesPage, setStrategiesPage] = useState(1);
  const [strategiesSearch, setStrategiesSearch] = useState('');

  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });

  const fetchIndicatorCollections = useCallback(async () => {
    try {
      setCollectionsLoading(true);
      const response = await fetch(`${API_BASE}/indicator-collections`);
      if (!response.ok) throw new Error('Failed to fetch indicator collections');
      const data = await response.json();
      setIndicatorCollections(data.collections || []);
    } catch (err) {
      console.error('Failed to fetch indicator collections:', err);
    } finally {
      setCollectionsLoading(false);
    }
  }, []);

  const fetchStrategies = useCallback(async () => {
    try {
      setStrategiesLoading(true);
      const response = await fetch(`${API_BASE}/strategies`);
      if (!response.ok) throw new Error('Failed to fetch strategies');
      const data = await response.json();
      setStrategies(data.strategies || []);
    } catch (err) {
      console.error('Failed to fetch strategies:', err);
    } finally {
      setStrategiesLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchIndicatorCollections();
    fetchStrategies();
  }, [fetchIndicatorCollections, fetchStrategies]);

  const handleDeleteCollection = (collection: IndicatorCollection) => {
    if (collection.is_default) {
      setError('Cannot delete default collections');
      return;
    }
    setConfirmDialog({
      isOpen: true,
      title: 'Delete Indicator Collection',
      message: `Are you sure you want to delete "${collection.name}"? This action cannot be undone.`,
      variant: 'danger',
      onConfirm: async () => {
        try {
          const response = await fetch(`${API_BASE}/indicator-collections/${collection.id}`, { method: 'DELETE' });
          if (!response.ok) throw new Error('Failed to delete collection');
          fetchIndicatorCollections();
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Failed to delete collection');
        }
      },
    });
  };

  const handleDeleteStrategy = (strategy: BacktestStrategy) => {
    setConfirmDialog({
      isOpen: true,
      title: 'Delete Backtest Strategy',
      message: `Are you sure you want to delete "${strategy.name}"? This action cannot be undone.`,
      variant: 'danger',
      onConfirm: async () => {
        try {
          const response = await fetch(`${API_BASE}/strategies/${strategy.id}`, { method: 'DELETE' });
          if (!response.ok) throw new Error('Failed to delete strategy');
          fetchStrategies();
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Failed to delete strategy');
        }
      },
    });
  };

  const toggleCollectionExpanded = (id: number) => {
    setExpandedCollections(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // Filtered + paginated strategies
  const filteredStrategies = useMemo(() => {
    if (!strategiesSearch.trim()) return strategies;
    const q = strategiesSearch.toLowerCase();
    return strategies.filter(
      s => s.name.toLowerCase().includes(q) || (s.description && s.description.toLowerCase().includes(q))
    );
  }, [strategies, strategiesSearch]);

  const strategiesTotalPages = Math.max(1, Math.ceil(filteredStrategies.length / ITEMS_PER_PAGE));
  const paginatedStrategies = filteredStrategies.slice(
    (strategiesPage - 1) * ITEMS_PER_PAGE,
    strategiesPage * ITEMS_PER_PAGE
  );

  // Paginated collections
  const collectionsTotalPages = Math.max(1, Math.ceil(indicatorCollections.length / ITEMS_PER_PAGE));
  const paginatedCollections = indicatorCollections.slice(
    (collectionsPage - 1) * ITEMS_PER_PAGE,
    collectionsPage * ITEMS_PER_PAGE
  );

  // Reset page when search changes
  useEffect(() => {
    setStrategiesPage(1);
  }, [strategiesSearch]);

  const PaginationControls: React.FC<{
    currentPage: number;
    totalPages: number;
    onPageChange: (page: number) => void;
    totalItems: number;
    label: string;
  }> = ({ currentPage, totalPages, onPageChange, totalItems, label }) => {
    if (totalItems <= ITEMS_PER_PAGE) return null;
    return (
      <div className="flex items-center justify-between pt-4 border-t border-gray-200 dark:border-gray-700">
        <span className="text-sm text-gray-500 dark:text-gray-400">
          Showing {(currentPage - 1) * ITEMS_PER_PAGE + 1}–{Math.min(currentPage * ITEMS_PER_PAGE, totalItems)} of {totalItems} {label}
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onPageChange(currentPage - 1)}
            disabled={currentPage <= 1}
            className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <ChevronLeft className="w-4 h-4" />
            Previous
          </button>
          <span className="text-sm text-gray-600 dark:text-gray-400 px-2">
            Page {currentPage} of {totalPages}
          </span>
          <button
            onClick={() => onPageChange(currentPage + 1)}
            disabled={currentPage >= totalPages}
            className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Next
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      </div>
    );
  };

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Saved Data</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-1">
            Manage your saved backtest strategies and indicator collections
          </p>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-100 text-red-700 rounded-lg flex items-center gap-2">
          <AlertCircle className="w-4 h-4" />
          {error}
          <button onClick={() => setError(null)} className="ml-auto">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Backtest Strategies Section */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Target className="w-5 h-5 text-green-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Backtest Strategies</h2>
            <span className="text-sm text-gray-500 dark:text-gray-400">
              ({filteredStrategies.length} strategies)
            </span>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                type="text"
                value={strategiesSearch}
                onChange={e => setStrategiesSearch(e.target.value)}
                placeholder="Filter strategies..."
                className="pl-9 pr-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 w-56"
              />
            </div>
            <button
              onClick={fetchStrategies}
              className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
              title="Refresh"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="p-4">
          {strategiesLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-green-500" />
            </div>
          ) : filteredStrategies.length === 0 ? (
            <p className="text-center text-gray-500 dark:text-gray-400 py-8">
              {strategiesSearch ? 'No strategies match your filter' : 'No backtest strategies saved'}
            </p>
          ) : (
            <>
              <div className="space-y-2">
                {paginatedStrategies.map(strategy => (
                  <div
                    key={strategy.id}
                    className="p-3 border rounded-lg bg-white dark:bg-gray-700 border-gray-200 dark:border-gray-600 flex items-center justify-between"
                  >
                    <div>
                      <h3 className="font-medium text-gray-900 dark:text-gray-100">{strategy.name}</h3>
                      {strategy.description && (
                        <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{strategy.description}</p>
                      )}
                      <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                        Created: {new Date(strategy.createdAt).toLocaleDateString()}
                      </p>
                    </div>
                    <button
                      onClick={() => handleDeleteStrategy(strategy)}
                      className="p-2 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg"
                      title="Delete"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                ))}
              </div>
              <PaginationControls
                currentPage={strategiesPage}
                totalPages={strategiesTotalPages}
                onPageChange={setStrategiesPage}
                totalItems={filteredStrategies.length}
                label="strategies"
              />
            </>
          )}
        </div>
      </div>

      {/* Indicator Collections Section */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 mt-6">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-purple-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Indicator Collections</h2>
            <span className="text-sm text-gray-500 dark:text-gray-400">
              ({indicatorCollections.length} collections)
            </span>
          </div>
          <button
            onClick={fetchIndicatorCollections}
            className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
            title="Refresh"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4">
          {collectionsLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-purple-500" />
            </div>
          ) : indicatorCollections.length === 0 ? (
            <p className="text-center text-gray-500 dark:text-gray-400 py-8">No indicator collections found</p>
          ) : (
            <>
              <div className="space-y-2">
                {paginatedCollections.map(collection => (
                  <div
                    key={collection.id}
                    className={`border rounded-lg ${
                      collection.is_default
                        ? 'bg-purple-50 dark:bg-purple-900/20 border-purple-200 dark:border-purple-800'
                        : 'bg-white dark:bg-gray-700 border-gray-200 dark:border-gray-600'
                    }`}
                  >
                    <div className="p-3 flex items-center justify-between">
                      <div className="flex items-center gap-3 flex-1 cursor-pointer" onClick={() => toggleCollectionExpanded(collection.id)}>
                        {expandedCollections.has(collection.id) ? (
                          <ChevronDown className="w-4 h-4 text-gray-500" />
                        ) : (
                          <ChevronRight className="w-4 h-4 text-gray-500" />
                        )}
                        <div>
                          <div className="flex items-center gap-2">
                            <h3 className="font-medium text-gray-900 dark:text-gray-100">{collection.name}</h3>
                            {collection.is_default && (
                              <span className="px-2 py-0.5 text-xs bg-purple-100 dark:bg-purple-800 text-purple-800 dark:text-purple-100 rounded">
                                Default
                              </span>
                            )}
                            <span className="text-sm text-gray-500 dark:text-gray-400">
                              ({collection.indicators.length} indicators)
                            </span>
                          </div>
                          {collection.description && (
                            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{collection.description}</p>
                          )}
                        </div>
                      </div>
                      {!collection.is_default && (
                        <button
                          onClick={() => handleDeleteCollection(collection)}
                          className="p-2 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg"
                          title="Delete"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>

                    {/* Expanded indicator list */}
                    {expandedCollections.has(collection.id) && (
                      <div className="px-4 pb-3 pt-0">
                        <div className="bg-gray-50 dark:bg-gray-800 rounded-lg p-3 max-h-64 overflow-y-auto">
                          <table className="w-full text-sm">
                            <thead>
                              <tr className="text-left text-gray-500 dark:text-gray-400">
                                <th className="pb-2 font-medium">Type</th>
                                <th className="pb-2 font-medium">Name</th>
                                <th className="pb-2 font-medium">Timeframe</th>
                                <th className="pb-2 font-medium">Parameters</th>
                              </tr>
                            </thead>
                            <tbody className="text-gray-700 dark:text-gray-300">
                              {collection.indicators.map((ind, idx) => (
                                <tr key={idx} className="border-t border-gray-200 dark:border-gray-700">
                                  <td className="py-1.5 font-mono text-xs">{ind.type}</td>
                                  <td className="py-1.5">{ind.name}</td>
                                  <td className="py-1.5">
                                    {ind.timeframe ? (
                                      <span className="px-1.5 py-0.5 bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 rounded text-xs">
                                        {ind.timeframe.toUpperCase()}
                                      </span>
                                    ) : (
                                      <span className="text-gray-400">-</span>
                                    )}
                                  </td>
                                  <td className="py-1.5 font-mono text-xs text-gray-500 dark:text-gray-400">
                                    {Object.entries(ind)
                                      .filter(([k]) => !['type', 'name', 'timeframe'].includes(k))
                                      .map(([k, v]) => `${k}=${v}`)
                                      .join(', ') || '-'}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <PaginationControls
                currentPage={collectionsPage}
                totalPages={collectionsTotalPages}
                onPageChange={setCollectionsPage}
                totalItems={indicatorCollections.length}
                label="collections"
              />
            </>
          )}
        </div>
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

export default SavedData;
