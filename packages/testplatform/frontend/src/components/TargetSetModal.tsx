import React, { useState, useEffect } from 'react';
import { X, Save, Trash2, FolderOpen } from 'lucide-react';
import type { TargetConfig, TargetSet } from '../types/targets';

interface TargetSetModalProps {
  isOpen: boolean;
  mode: 'save' | 'load';
  currentTargets: TargetConfig[];
  onClose: () => void;
  onSave: (name: string, description: string) => void;
  onLoad: (targets: TargetConfig[]) => void;
}

const TargetSetModal: React.FC<TargetSetModalProps> = ({
  isOpen,
  mode,
  currentTargets,
  onClose,
  onSave,
  onLoad,
}) => {
  const [targetSets, setTargetSets] = useState<TargetSet[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Save form state
  const [saveName, setSaveName] = useState('');
  const [saveDescription, setSaveDescription] = useState('');

  // Delete confirmation
  const [deleteConfirmId, setDeleteConfirmId] = useState<number | null>(null);

  // Fetch target sets when modal opens in load mode
  useEffect(() => {
    if (isOpen && mode === 'load') {
      fetchTargetSets();
    }
  }, [isOpen, mode]);

  const fetchTargetSets = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch('http://localhost:8000/api/target-sets');
      if (!response.ok) throw new Error('Failed to fetch target sets');
      const data = await response.json();
      setTargetSets(data.target_sets || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load target sets');
    } finally {
      setIsLoading(false);
    }
  };

  const handleSave = async () => {
    if (!saveName.trim()) {
      setError('Name is required');
      return;
    }

    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch('http://localhost:8000/api/target-sets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: saveName.trim(),
          description: saveDescription.trim() || null,
          targets: currentTargets,
        }),
      });

      if (!response.ok) throw new Error('Failed to save target set');

      onSave(saveName, saveDescription);
      setSaveName('');
      setSaveDescription('');
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save target set');
    } finally {
      setIsLoading(false);
    }
  };

  const handleLoad = (targetSet: TargetSet) => {
    onLoad(targetSet.targets);
    onClose();
  };

  const handleDelete = async (id: number) => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch(`http://localhost:8000/api/target-sets/${id}`, {
        method: 'DELETE',
      });

      if (!response.ok) throw new Error('Failed to delete target set');

      setTargetSets(targetSets.filter(ts => ts.id !== id));
      setDeleteConfirmId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete target set');
    } finally {
      setIsLoading(false);
    }
  };

  const getTargetsSummary = (targets: TargetConfig[]): string => {
    const types = targets.map(t => t.type);
    const uniqueTypes = [...new Set(types)];
    return `${targets.length} target${targets.length !== 1 ? 's' : ''} (${uniqueTypes.join(', ')})`;
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-lg max-h-[80vh] flex flex-col m-4">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-bold flex items-center gap-2">
            {mode === 'save' ? (
              <>
                <Save size={20} className="text-green-500" />
                Save Target Set
              </>
            ) : (
              <>
                <FolderOpen size={20} className="text-blue-500" />
                Load Target Set
              </>
            )}
          </h2>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
          >
            <X size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4">
          {error && (
            <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 rounded-lg text-sm">
              {error}
            </div>
          )}

          {mode === 'save' ? (
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Name *
                </label>
                <input
                  type="text"
                  value={saveName}
                  onChange={(e) => setSaveName(e.target.value)}
                  placeholder="e.g., Aggressive Swing Trading"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Description
                </label>
                <textarea
                  value={saveDescription}
                  onChange={(e) => setSaveDescription(e.target.value)}
                  placeholder="Optional description..."
                  rows={3}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700"
                />
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
                <p className="text-sm text-gray-600 dark:text-gray-400">
                  <strong>Targets to save:</strong> {getTargetsSummary(currentTargets)}
                </p>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              {isLoading ? (
                <p className="text-center text-gray-500 py-8">Loading...</p>
              ) : targetSets.length === 0 ? (
                <p className="text-center text-gray-500 py-8">No saved target sets</p>
              ) : (
                targetSets.map((targetSet) => (
                  <div
                    key={targetSet.id}
                    className="p-4 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50"
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <h3 className="font-medium">{targetSet.name}</h3>
                        {targetSet.description && (
                          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                            {targetSet.description}
                          </p>
                        )}
                        <p className="text-xs text-gray-400 mt-2">
                          {getTargetsSummary(targetSet.targets)}
                        </p>
                      </div>
                      <div className="flex items-center gap-2 ml-4">
                        {deleteConfirmId === targetSet.id ? (
                          <>
                            <button
                              onClick={() => handleDelete(targetSet.id)}
                              disabled={isLoading}
                              className="px-2 py-1 text-xs bg-red-500 text-white rounded hover:bg-red-600 disabled:opacity-50"
                            >
                              Confirm
                            </button>
                            <button
                              onClick={() => setDeleteConfirmId(null)}
                              className="px-2 py-1 text-xs bg-gray-300 dark:bg-gray-600 rounded hover:bg-gray-400 dark:hover:bg-gray-500"
                            >
                              Cancel
                            </button>
                          </>
                        ) : (
                          <>
                            <button
                              onClick={() => handleLoad(targetSet)}
                              className="px-3 py-1.5 text-sm bg-blue-500 text-white rounded hover:bg-blue-600"
                            >
                              Load
                            </button>
                            <button
                              onClick={() => setDeleteConfirmId(targetSet.id)}
                              className="p-1.5 text-gray-400 hover:text-red-500"
                            >
                              <Trash2 size={16} />
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        {mode === 'save' && (
          <div className="p-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-3">
            <button
              onClick={onClose}
              className="px-4 py-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={isLoading || !saveName.trim() || currentTargets.length === 0}
              className="px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 flex items-center gap-2"
            >
              <Save size={16} />
              {isLoading ? 'Saving...' : 'Save'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default TargetSetModal;
