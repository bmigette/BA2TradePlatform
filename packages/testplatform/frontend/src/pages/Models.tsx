import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Brain,
  Loader2,
  AlertCircle,
  CheckCircle,
  Clock,
  Trash2,
  Download,
  ChevronRight,
  Target,
  Layers,
  TrendingUp,
  Search,
  Filter,
  Grid3X3,
  List,
  ArrowUpDown,
  Database,
  ChevronUp,
  ChevronDown
} from 'lucide-react';
import ConfirmDialog from '../components/ConfirmDialog';

interface Model {
  id: string;
  name: string;
  modelType: string;
  datasetId: number;
  datasetName?: string;
  symbol?: string;
  timeframe?: string;
  trainPeriod?: string;
  jobId: string;
  status: string;
  createdAt: string;
  trainedAt: string | null;
  generations: number;
  bestGeneration: number;
  fitness: number;
  lossFunction?: string;
  performanceMetrics: {
    accuracy: number;
    precision: number;
    recall: number;
    f1Score: number;
    auc: number;
  };
}

const API_BASE = 'http://localhost:8000/api';

type ViewMode = 'grid' | 'list';
type SortField = 'date' | 'accuracy' | 'name' | 'fitness';
type SortDirection = 'asc' | 'desc';

const Models: React.FC = () => {
  const navigate = useNavigate();
  const [models, setModels] = useState<Model[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterType, setFilterType] = useState<string>('all');
  const [filterDataset, setFilterDataset] = useState<string>('all');
  const [viewMode, setViewMode] = useState<ViewMode>('grid');
  const [sortField, setSortField] = useState<SortField>('date');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });

  useEffect(() => {
    fetchModels();
  }, []);

  const fetchModels = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_BASE}/models`);
      if (!response.ok) throw new Error('Failed to fetch models');
      const data = await response.json();
      setModels(data.models || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = (e: React.MouseEvent, modelId: string) => {
    e.stopPropagation();
    setConfirmDialog({
      isOpen: true,
      title: 'Delete Model',
      message: 'Are you sure you want to delete this model?',
      variant: 'danger',
      onConfirm: async () => {
        try {
          const res = await fetch(`${API_BASE}/models/${modelId}`, { method: 'DELETE' });
          if (res.ok) {
            setModels(prev => prev.filter(m => m.id !== modelId));
          }
        } catch (err) {
          alert('Failed to delete model');
        }
      },
    });
  };

  const handleExport = async (e: React.MouseEvent, modelId: string) => {
    e.stopPropagation();
    try {
      const res = await fetch(`${API_BASE}/models/${modelId}/export?format=pytorch`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        alert(`Model exported: ${data.path}`);
      }
    } catch (err) {
      alert('Failed to export model');
    }
  };

  const getModelTypeColor = (type: string) => {
    const colors: Record<string, string> = {
      'LSTM': 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300',
      'GRU': 'bg-teal-100 text-teal-700 dark:bg-teal-900 dark:text-teal-300',
      'N-BEATS': 'bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300',
      'TCN': 'bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-300',
      'TRANSFORMER': 'bg-pink-100 text-pink-700 dark:bg-pink-900 dark:text-pink-300',
      'TFT': 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900 dark:text-cyan-300',
    };
    return colors[type.toUpperCase()] || 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300';
  };

  const formatLossFunction = (loss: string | undefined) => {
    if (!loss) return 'N/A';
    return loss.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  };

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(prev => prev === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDirection('desc');
    }
  };

  // Get unique values for filters
  const modelTypes = ['all', ...Array.from(new Set(models.map(m => m.modelType)))];

  // Build dataset ID to name map and get unique datasets
  const datasetMap = new Map<string, string>();
  models.forEach(m => {
    const id = String(m.datasetId);
    if (!datasetMap.has(id)) {
      datasetMap.set(id, m.datasetName || `Dataset #${m.datasetId}`);
    }
  });
  const datasetOptions = [
    { id: 'all', name: 'All Datasets' },
    ...Array.from(datasetMap.entries()).map(([id, name]) => ({ id, name }))
  ];

  // Filter and sort models
  const filteredModels = models
    .filter(model => {
      const matchesSearch = model.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
                            model.modelType.toLowerCase().includes(searchQuery.toLowerCase());
      const matchesType = filterType === 'all' || model.modelType === filterType;
      const matchesDataset = filterDataset === 'all' || String(model.datasetId) === filterDataset;
      return matchesSearch && matchesType && matchesDataset;
    })
    .sort((a, b) => {
      let cmp = 0;
      switch (sortField) {
        case 'date':
          cmp = new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime();
          break;
        case 'accuracy':
          cmp = (a.performanceMetrics?.accuracy || 0) - (b.performanceMetrics?.accuracy || 0);
          break;
        case 'name':
          cmp = a.name.localeCompare(b.name);
          break;
        case 'fitness':
          cmp = (a.fitness || 0) - (b.fitness || 0);
          break;
      }
      return sortDirection === 'asc' ? cmp : -cmp;
    });

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center min-h-96">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
            <AlertCircle className="w-5 h-5" />
            <span>Failed to load models: {error}</span>
          </div>
        </div>
      </div>
    );
  }

  const SortButton = ({ field, label }: { field: SortField; label: string }) => (
    <button
      onClick={() => handleSort(field)}
      className={`flex items-center gap-1 text-xs px-2 py-1 rounded ${
        sortField === field
          ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300'
          : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-400 dark:hover:bg-gray-600'
      }`}
    >
      {label}
      {sortField === field && (
        sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
      )}
    </button>
  );

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2 text-gray-900 dark:text-gray-100">
            <Brain className="w-8 h-8 text-purple-500" />
            Model Library
          </h1>
          <p className="text-gray-500 dark:text-gray-400 mt-1">Browse and manage your trained models</p>
        </div>
        <div className="text-sm text-gray-500 dark:text-gray-400">
          {filteredModels.length} of {models.length} model{models.length !== 1 ? 's' : ''}
        </div>
      </div>

      {/* Filters and Controls */}
      <div className="flex flex-col lg:flex-row gap-4">
        {/* Search */}
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-gray-400" />
          <input
            type="text"
            placeholder="Search models..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-10 pr-4 py-2 border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-400"
          />
        </div>

        {/* Filters Row */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Filter by Model Type */}
          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4 text-gray-400" />
            <select
              value={filterType}
              onChange={(e) => setFilterType(e.target.value)}
              className="px-3 py-2 text-sm border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500"
            >
              {modelTypes.map(type => (
                <option key={type} value={type}>
                  {type === 'all' ? 'All Types' : type}
                </option>
              ))}
            </select>
          </div>

          {/* Filter by Dataset */}
          <div className="flex items-center gap-2">
            <Database className="w-4 h-4 text-gray-400" />
            <select
              value={filterDataset}
              onChange={(e) => setFilterDataset(e.target.value)}
              className="px-3 py-2 text-sm border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-blue-500"
            >
              {datasetOptions.map(opt => (
                <option key={opt.id} value={opt.id}>
                  {opt.name}
                </option>
              ))}
            </select>
          </div>

          {/* Sort Options */}
          <div className="flex items-center gap-2">
            <ArrowUpDown className="w-4 h-4 text-gray-400" />
            <div className="flex gap-1">
              <SortButton field="date" label="Date" />
              <SortButton field="accuracy" label="Accuracy" />
              <SortButton field="fitness" label="Fitness" />
              <SortButton field="name" label="Name" />
            </div>
          </div>

          {/* View Toggle */}
          <div className="flex border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <button
              onClick={() => setViewMode('grid')}
              className={`p-2 ${
                viewMode === 'grid'
                  ? 'bg-blue-500 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
              title="Grid view"
            >
              <Grid3X3 className="w-4 h-4" />
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={`p-2 ${
                viewMode === 'list'
                  ? 'bg-blue-500 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
              }`}
              title="List view"
            >
              <List className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Models Display */}
      {filteredModels.length === 0 ? (
        <div className="text-center py-16">
          <Brain className="w-16 h-16 mx-auto text-gray-300 mb-4" />
          <h3 className="text-xl font-medium text-gray-600 dark:text-gray-300">No models found</h3>
          <p className="text-gray-500 dark:text-gray-400 mt-2">
            {models.length === 0
              ? 'Run an optimization job to train your first model'
              : 'Try adjusting your search or filter criteria'}
          </p>
        </div>
      ) : viewMode === 'grid' ? (
        /* Grid View */
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredModels.map(model => (
            <div
              key={model.id}
              onClick={() => navigate(`/models/${model.id}`)}
              className="bg-white dark:bg-gray-800 rounded-lg shadow hover:shadow-lg transition-shadow cursor-pointer border border-transparent hover:border-blue-500 group"
            >
              {/* Card Header */}
              <div className="p-4 border-b border-gray-100 dark:border-gray-700">
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <h3 className="font-semibold truncate text-gray-900 dark:text-gray-100 group-hover:text-blue-600">{model.name}</h3>
                    <p className="text-sm text-gray-500 dark:text-gray-400 truncate">
                      {model.datasetName || `Dataset #${model.datasetId}`}
                    </p>
                    {model.symbol && (
                      <p className="text-xs text-gray-400 dark:text-gray-500">
                        {model.symbol} {model.timeframe && `• ${model.timeframe}`}
                      </p>
                    )}
                  </div>
                  <span className={`px-2 py-1 text-xs font-medium rounded-full ${getModelTypeColor(model.modelType)}`}>
                    {model.modelType}
                  </span>
                </div>
              </div>

              {/* Card Body */}
              <div className="p-4 space-y-3">
                {/* Performance Metrics */}
                <div className="grid grid-cols-2 gap-2">
                  <div className="flex items-center gap-2 text-sm">
                    <Target className="w-4 h-4 text-green-500" />
                    <span className="text-gray-500 dark:text-gray-400">Accuracy:</span>
                    <span className="font-medium text-gray-900 dark:text-gray-100">{((model.performanceMetrics?.accuracy || 0) * 100).toFixed(1)}%</span>
                  </div>
                  <div className="flex items-center gap-2 text-sm">
                    <TrendingUp className="w-4 h-4 text-blue-500" />
                    <span className="text-gray-500 dark:text-gray-400">Fitness:</span>
                    <span className="font-medium text-gray-900 dark:text-gray-100">{(model.fitness || 0).toFixed(5)}</span>
                  </div>
                </div>

                {/* Loss Function */}
                {model.lossFunction && (
                  <div className="text-sm text-gray-500 dark:text-gray-400">
                    Loss: <span className="font-medium text-gray-700 dark:text-gray-300">{formatLossFunction(model.lossFunction)}</span>
                  </div>
                )}

                {/* Status and Info */}
                <div className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    {model.status === 'trained' ? (
                      <CheckCircle className="w-4 h-4 text-green-500" />
                    ) : (
                      <Clock className="w-4 h-4 text-yellow-500" />
                    )}
                    <span className="text-gray-600 dark:text-gray-400 capitalize">{model.status}</span>
                  </div>
                  <div className="flex items-center gap-1 text-gray-500 dark:text-gray-400">
                    <Layers className="w-4 h-4" />
                    <span>Gen {model.bestGeneration}/{model.generations}</span>
                  </div>
                </div>

                {/* Date */}
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Created: {new Date(model.createdAt).toLocaleDateString()}
                </p>
              </div>

              {/* Card Footer */}
              <div className="px-4 py-3 bg-gray-50 dark:bg-gray-700/50 rounded-b-lg flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <button
                    onClick={(e) => handleExport(e, model.id)}
                    className="p-2 text-gray-500 dark:text-gray-400 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded-lg transition-colors"
                    title="Export model"
                  >
                    <Download className="w-4 h-4" />
                  </button>
                  <button
                    onClick={(e) => handleDelete(e, model.id)}
                    className="p-2 text-gray-500 dark:text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/30 rounded-lg transition-colors"
                    title="Delete model"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
                <div className="flex items-center gap-1 text-sm text-blue-600 group-hover:translate-x-1 transition-transform">
                  View Details
                  <ChevronRight className="w-4 h-4" />
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        /* List View */
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50 dark:bg-gray-700/50">
              <tr>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-600 dark:text-gray-300">Name</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-600 dark:text-gray-300">Type</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-600 dark:text-gray-300">Loss</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-600 dark:text-gray-300">Dataset</th>
                <th className="px-4 py-3 text-right text-sm font-medium text-gray-600 dark:text-gray-300">Accuracy</th>
                <th className="px-4 py-3 text-right text-sm font-medium text-gray-600 dark:text-gray-300">Fitness</th>
                <th className="px-4 py-3 text-center text-sm font-medium text-gray-600 dark:text-gray-300">Status</th>
                <th className="px-4 py-3 text-left text-sm font-medium text-gray-600 dark:text-gray-300">Created</th>
                <th className="px-4 py-3 text-center text-sm font-medium text-gray-600 dark:text-gray-300">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {filteredModels.map(model => (
                <tr
                  key={model.id}
                  onClick={() => navigate(`/models/${model.id}`)}
                  className="hover:bg-gray-50 dark:hover:bg-gray-700/30 cursor-pointer"
                >
                  <td className="px-4 py-3">
                    <div className="font-medium text-gray-900 dark:text-gray-100">{model.name}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400">{model.id}</div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 text-xs font-medium rounded-full ${getModelTypeColor(model.modelType)}`}>
                      {model.modelType}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-400">
                    {formatLossFunction(model.lossFunction)}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-400">
                    <div>{model.datasetName || `#${model.datasetId}`}</div>
                    {model.symbol && (
                      <div className="text-xs text-gray-400">{model.symbol} {model.timeframe && `• ${model.timeframe}`}</div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span className="text-sm font-medium text-green-600">
                      {((model.performanceMetrics?.accuracy || 0) * 100).toFixed(1)}%
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span className="text-sm font-medium text-blue-600">
                      {(model.fitness || 0).toFixed(5)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-center">
                    {model.status === 'trained' ? (
                      <span className="inline-flex items-center gap-1 text-xs text-green-600">
                        <CheckCircle className="w-3 h-3" />
                        Trained
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs text-yellow-600">
                        <Clock className="w-3 h-3" />
                        {model.status}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                    {new Date(model.createdAt).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-center gap-1">
                      <button
                        onClick={(e) => handleExport(e, model.id)}
                        className="p-1.5 text-gray-500 dark:text-gray-400 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded transition-colors"
                        title="Export"
                      >
                        <Download className="w-4 h-4" />
                      </button>
                      <button
                        onClick={(e) => handleDelete(e, model.id)}
                        className="p-1.5 text-gray-500 dark:text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/30 rounded transition-colors"
                        title="Delete"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); navigate(`/models/${model.id}`); }}
                        className="p-1.5 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
                        title="View Details"
                      >
                        <ChevronRight className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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

export default Models;
