import { API_BASE } from '../lib/config';
import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Trash2, RefreshCw, Copy, Edit, CheckCircle, AlertCircle, Loader } from 'lucide-react';
import DatasetWizard from '../components/DatasetWizard';
import ConfirmDialog from '../components/ConfirmDialog';
import Toast from '../components/Toast';
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
  created_at: string;
  technical_indicators?: any;
  generation_config?: any;
  labels?: string[];
}

type WizardMode = 'create' | 'duplicate' | 'edit';

const Datasets: React.FC = () => {
  const navigate = useNavigate();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isWizardOpen, setIsWizardOpen] = useState(false);
  const [wizardMode, setWizardMode] = useState<WizardMode>('create');
  const [selectedDataset, setSelectedDataset] = useState<Dataset | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [datasetToDelete, setDatasetToDelete] = useState<number | null>(null);
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' | 'info' | 'warning' } | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkDeleteConfirmOpen, setBulkDeleteConfirmOpen] = useState(false);
  const [batchRegenerateOpen, setBatchRegenerateOpen] = useState(false);

  const fetchDatasets = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/datasets`);
      if (!response.ok) {
        throw new Error('Failed to fetch datasets');
      }
      const data = await response.json();
      setDatasets((data.datasets || []).slice().sort((a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name)));
      setSelectedIds(new Set());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchDatasets();
  }, []);

  const handleDeleteClick = (id: number) => {
    setDatasetToDelete(id);
    setDeleteConfirmOpen(true);
  };

  const handleDeleteConfirm = async () => {
    if (datasetToDelete === null) return;

    try {
      const response = await fetch(`${API_BASE}/datasets/${datasetToDelete}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error('Failed to delete dataset');
      }

      setToast({ message: 'Dataset deleted successfully', type: 'success' });
      fetchDatasets();
    } catch (err) {
      setToast({
        message: err instanceof Error ? err.message : 'An error occurred',
        type: 'error',
      });
    } finally {
      setDatasetToDelete(null);
    }
  };

  const handleBulkDeleteConfirm = async () => {
    const ids = Array.from(selectedIds);
    let successCount = 0;
    let failCount = 0;

    await Promise.all(
      ids.map(async (id) => {
        try {
          const response = await fetch(`${API_BASE}/datasets/${id}`, { method: 'DELETE' });
          if (response.ok) successCount++;
          else failCount++;
        } catch {
          failCount++;
        }
      })
    );

    if (failCount === 0) {
      setToast({ message: `Deleted ${successCount} dataset(s)`, type: 'success' });
    } else {
      setToast({ message: `Deleted ${successCount}, failed ${failCount}`, type: 'warning' });
    }
    fetchDatasets();
  };

  const handleBatchRegenerate = async (options: RegenOptions) => {
    setBatchRegenerateOpen(false);
    const ids = Array.from(selectedIds);

    try {
      const response = await fetch(`${API_BASE}/datasets/batch-regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dataset_ids: ids, regenerate_options: options }),
      });

      if (!response.ok) throw new Error('Batch regeneration failed');

      const result = await response.json();
      setToast({ message: `Queued ${result.count} dataset(s) for regeneration`, type: 'success' });
      fetchDatasets();
    } catch (err) {
      setToast({ message: err instanceof Error ? err.message : 'An error occurred', type: 'error' });
    }
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === datasets.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(datasets.map((d) => d.id)));
    }
  };

  const toggleSelect = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString();
  };

  const handleDuplicate = (dataset: Dataset) => {
    setSelectedDataset(dataset);
    setWizardMode('duplicate');
    setIsWizardOpen(true);
  };

  const handleEdit = async (dataset: Dataset) => {
    try {
      const response = await fetch(`${API_BASE}/datasets/${dataset.id}`);
      if (response.ok) {
        const freshDataset = await response.json();
        setSelectedDataset(freshDataset);
      } else {
        setSelectedDataset(dataset);
      }
    } catch {
      setSelectedDataset(dataset);
    }
    setWizardMode('edit');
    setIsWizardOpen(true);
  };

  const handleCreateNew = () => {
    setSelectedDataset(null);
    setWizardMode('create');
    setIsWizardOpen(true);
  };

  const handleWizardClose = () => {
    setIsWizardOpen(false);
    setSelectedDataset(null);
    setWizardMode('create');
  };

  const allSelected = datasets.length > 0 && selectedIds.size === datasets.length;
  const someSelected = selectedIds.size > 0 && selectedIds.size < datasets.length;

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Datasets</h1>
        <div className="flex space-x-2">
          {selectedIds.size > 0 && (
            <>
              <button
                onClick={() => setBatchRegenerateOpen(true)}
                className="px-4 py-2 bg-orange-500 text-white rounded-md hover:bg-orange-600 flex items-center space-x-2"
              >
                <RefreshCw size={16} />
                <span>Regenerate Selected ({selectedIds.size})</span>
              </button>
              <button
                onClick={() => setBulkDeleteConfirmOpen(true)}
                className="px-4 py-2 bg-red-500 text-white rounded-md hover:bg-red-600 flex items-center space-x-2"
              >
                <Trash2 size={16} />
                <span>Delete Selected ({selectedIds.size})</span>
              </button>
            </>
          )}
          <button
            onClick={fetchDatasets}
            className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600 flex items-center space-x-2"
          >
            <RefreshCw size={16} />
            <span>Refresh</span>
          </button>
          <button
            onClick={handleCreateNew}
            className="px-4 py-2 bg-blue-500 text-white rounded-md hover:bg-blue-600 flex items-center space-x-2"
          >
            <Plus size={16} />
            <span>Create New Dataset</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-4 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-200 rounded-md">
          {error}
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-12">
          <p className="text-gray-600 dark:text-gray-400">Loading datasets...</p>
        </div>
      ) : datasets.length === 0 ? (
        <div className="text-center py-12 bg-gray-50 dark:bg-gray-800 rounded-lg">
          <p className="text-gray-600 dark:text-gray-400 mb-4">No datasets yet</p>
          <button
            onClick={handleCreateNew}
            className="px-6 py-3 bg-blue-500 text-white rounded-md hover:bg-blue-600 inline-flex items-center space-x-2"
          >
            <Plus size={20} />
            <span>Create Your First Dataset</span>
          </button>
        </div>
      ) : (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="bg-gray-50 dark:bg-gray-700">
              <tr>
                <th className="px-4 py-3 w-10">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    ref={(el) => { if (el) el.indeterminate = someSelected; }}
                    onChange={toggleSelectAll}
                    className="w-4 h-4 rounded border-gray-300 text-blue-600 cursor-pointer"
                    onClick={(e) => e.stopPropagation()}
                  />
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Name
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Status
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Ticker
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Timeframe
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Date Range
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Rows
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Labels
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Created
                </th>
                <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
              {datasets.map((dataset) => (
                <tr
                  key={dataset.id}
                  className={`hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer ${selectedIds.has(dataset.id) ? 'bg-blue-50 dark:bg-blue-900/20' : ''}`}
                  onClick={() => navigate(`/datasets/${dataset.id}`)}
                >
                  <td className="px-4 py-4 w-10" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectedIds.has(dataset.id)}
                      onChange={() => toggleSelect(dataset.id)}
                      className="w-4 h-4 rounded border-gray-300 text-blue-600 cursor-pointer"
                    />
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900 dark:text-gray-100">
                    {dataset.name}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm">
                    {dataset.status === 'ready' && (
                      <span className="px-2 py-1 text-xs font-medium bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300 rounded-full inline-flex items-center gap-1">
                        <CheckCircle size={10} />
                        Ready
                      </span>
                    )}
                    {dataset.status === 'building' && (
                      <span className="px-2 py-1 text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300 rounded-full inline-flex items-center gap-1">
                        <Loader size={10} className="animate-spin" />
                        Building
                      </span>
                    )}
                    {dataset.status === 'pending' && (
                      <span className="px-2 py-1 text-xs font-medium bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300 rounded-full inline-flex items-center gap-1">
                        <Loader size={10} />
                        Pending
                      </span>
                    )}
                    {dataset.status === 'error' && (
                      <span className="px-2 py-1 text-xs font-medium bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300 rounded-full inline-flex items-center gap-1" title={dataset.error_message || 'Error'}>
                        <AlertCircle size={10} />
                        Error
                      </span>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {dataset.ticker}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {dataset.timeframe}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {formatDate(dataset.start_date)} - {formatDate(dataset.end_date)}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {dataset.rows_count.toLocaleString()}
                  </td>
                  <td className="px-6 py-4 text-sm">
                    {dataset.labels && dataset.labels.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {dataset.labels.map((label, idx) => (
                          <span
                            key={idx}
                            className="bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 rounded-full px-2 py-0.5 text-xs"
                          >
                            {label}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <span className="text-gray-400 dark:text-gray-600">-</span>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {formatDate(dataset.created_at)}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                    <div className="flex items-center justify-end space-x-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleEdit(dataset); }}
                        className="text-blue-600 hover:text-blue-900 dark:text-blue-400 dark:hover:text-blue-300"
                        title="Edit dataset"
                      >
                        <Edit size={16} />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDuplicate(dataset); }}
                        className="text-green-600 hover:text-green-900 dark:text-green-400 dark:hover:text-green-300"
                        title="Duplicate dataset"
                      >
                        <Copy size={16} />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDeleteClick(dataset.id); }}
                        className="text-red-600 hover:text-red-900 dark:text-red-400 dark:hover:text-red-300"
                        title="Delete dataset"
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <DatasetWizard
        isOpen={isWizardOpen}
        onClose={handleWizardClose}
        onComplete={fetchDatasets}
        mode={wizardMode}
        initialData={selectedDataset}
      />

      <ConfirmDialog
        isOpen={deleteConfirmOpen}
        onClose={() => setDeleteConfirmOpen(false)}
        onConfirm={handleDeleteConfirm}
        title="Delete Dataset"
        message="Are you sure you want to delete this dataset? This action cannot be undone."
        confirmText="Delete"
        cancelText="Cancel"
        variant="danger"
      />

      <ConfirmDialog
        isOpen={bulkDeleteConfirmOpen}
        onClose={() => setBulkDeleteConfirmOpen(false)}
        onConfirm={handleBulkDeleteConfirm}
        title="Delete Selected Datasets"
        message={`Are you sure you want to delete ${selectedIds.size} dataset(s)? This action cannot be undone.`}
        confirmText="Delete All"
        cancelText="Cancel"
        variant="danger"
      />

      <RegenerateDialog
        isOpen={batchRegenerateOpen}
        onClose={() => setBatchRegenerateOpen(false)}
        onConfirm={handleBatchRegenerate}
        title="Regenerate Datasets"
        description={`${selectedIds.size} dataset(s) selected`}
        confirmLabel={`Regenerate ${selectedIds.size} Dataset(s)`}
      />

      {toast && (
        <Toast
          message={toast.message}
          type={toast.type}
          duration={5000}
          onClose={() => setToast(null)}
        />
      )}
    </div>
  );
};

export default Datasets;
