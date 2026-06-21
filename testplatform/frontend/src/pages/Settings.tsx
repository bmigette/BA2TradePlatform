import { API_BASE } from '../lib/config';
import React, { useState, useEffect, useCallback } from 'react';
import {
  Server, Plus, Trash2, Edit2, X, RefreshCw, Cpu, HardDrive,
  Activity, Clock, Download, Upload, Power, PowerOff, AlertCircle,
  CheckCircle, Loader2, KeyRound, Eye, EyeOff, Save
} from 'lucide-react';
import ConfirmDialog from '../components/ConfirmDialog';

interface WorkerCapabilities {
  train: boolean;
  infer: boolean;
}

interface GpuInfo {
  name: string;
  memory: number;
  count: number;
}

interface CpuInfo {
  cores: number;
  model: string;
}

interface Worker {
  id: number;
  name: string;
  url: string;
  description: string | null;
  workerType: 'local' | 'remote';
  capabilities: WorkerCapabilities;
  hasPassword?: boolean;
  isEnabled: boolean;
  isLocal: boolean;
  status: 'online' | 'offline' | 'busy';
  gpuInfo: GpuInfo | null;
  cpuInfo: CpuInfo | null;
  lastHeartbeat: string | null;
  activeJobsCount: number;
  totalJobsCompleted: number;
  createdAt: string | null;
  updatedAt: string | null;
}

interface WorkerFormData {
  name: string;
  url: string;
  description: string;
  capabilities: WorkerCapabilities;
  password: string;
}

interface CredentialKey {
  key: string;
  is_set: boolean;
  masked_value: string | null;
}


const Settings: React.FC = () => {
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [editingWorker, setEditingWorker] = useState<Worker | null>(null);
  const [healthChecking, setHealthChecking] = useState<number | null>(null);
  const [workerAction, setWorkerAction] = useState<{ id: number; kind: string } | null>(null);
  const [importingKeys, setImportingKeys] = useState(false);
  const [keyImportMessage, setKeyImportMessage] = useState<string | null>(null);

  // Credential (API) keys: masked values from the server + local edits/reveal state.
  const [credentialKeys, setCredentialKeys] = useState<CredentialKey[]>([]);
  const [keyEdits, setKeyEdits] = useState<Record<string, string>>({});
  const [revealedKeys, setRevealedKeys] = useState<Record<string, boolean>>({});
  const [savingKeys, setSavingKeys] = useState(false);
  const [keySaveMessage, setKeySaveMessage] = useState<string | null>(null);

  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });
  const [formData, setFormData] = useState<WorkerFormData>({
    name: '',
    url: '',
    description: '',
    capabilities: { train: true, infer: true },
    password: ''
  });

  const fetchWorkers = useCallback(async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_BASE}/workers`);
      if (!response.ok) throw new Error('Failed to fetch workers');
      const data = await response.json();
      setWorkers(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch workers');
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchCredentialKeys = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/settings/credential-keys`);
      if (!response.ok) throw new Error('Failed to fetch credential keys');
      const data = await response.json();
      setCredentialKeys(data.keys || []);
      // Drop any in-progress edits so the refreshed masked values are shown.
      setKeyEdits({});
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch credential keys');
    }
  }, []);

  useEffect(() => {
    fetchWorkers();
    fetchCredentialKeys();
  }, [fetchWorkers, fetchCredentialKeys]);

  const handleImportKeysFromTrade = async () => {
    try {
      setImportingKeys(true);
      setKeyImportMessage(null);
      setError(null);
      const response = await fetch(`${API_BASE}/settings/import-keys-from-trade`, {
        method: 'POST',
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || 'Failed to import keys from trade platform');
      }
      const data = await response.json();
      setKeyImportMessage(
        data.count > 0
          ? `Imported ${data.count} key(s) from the trade platform: ${data.imported.join(', ')}`
          : 'No credential keys found in the trade platform DB.'
      );
      // Refresh masked values so newly-imported keys show as set.
      fetchCredentialKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import keys from trade platform');
    } finally {
      setImportingKeys(false);
    }
  };

  const handleSaveCredentialKeys = async () => {
    const values = Object.fromEntries(
      Object.entries(keyEdits).filter(([, v]) => v !== '')
    );
    if (Object.keys(values).length === 0) {
      setKeySaveMessage('No changes to save.');
      return;
    }
    try {
      setSavingKeys(true);
      setKeySaveMessage(null);
      setError(null);
      const response = await fetch(`${API_BASE}/settings/credential-keys`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || 'Failed to save credential keys');
      }
      const data = await response.json();
      setKeySaveMessage(
        data.count > 0
          ? `Saved ${data.count} key(s): ${data.updated.join(', ')}`
          : 'No keys saved.'
      );
      fetchCredentialKeys();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save credential keys');
    } finally {
      setSavingKeys(false);
    }
  };

  const handleAddWorker = async () => {
    try {
      const response = await fetch(`${API_BASE}/workers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: formData.name,
          url: formData.url,
          description: formData.description || null,
          workerType: 'remote',
          capabilities: formData.capabilities,
          ...(formData.password ? { password: formData.password } : {})
        })
      });
      if (!response.ok) throw new Error('Failed to add worker');
      setShowAddModal(false);
      setFormData({ name: '', url: '', description: '', capabilities: { train: true, infer: true }, password: '' });
      fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add worker');
    }
  };

  const handleUpdateWorker = async () => {
    if (!editingWorker) return;
    try {
      const response = await fetch(`${API_BASE}/workers/${editingWorker.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: formData.name,
          url: formData.url,
          description: formData.description || null,
          capabilities: formData.capabilities,
          ...(formData.password ? { password: formData.password } : {})
        })
      });
      if (!response.ok) throw new Error('Failed to update worker');
      setEditingWorker(null);
      setFormData({ name: '', url: '', description: '', capabilities: { train: true, infer: true }, password: '' });
      fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update worker');
    }
  };

  const handleDeleteWorker = (workerId: number) => {
    setConfirmDialog({
      isOpen: true,
      title: 'Delete Worker',
      message: 'Are you sure you want to delete this worker?',
      variant: 'danger',
      onConfirm: async () => {
        try {
          const response = await fetch(`${API_BASE}/workers/${workerId}`, { method: 'DELETE' });
          if (!response.ok) throw new Error('Failed to delete worker');
          fetchWorkers();
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Failed to delete worker');
        }
      },
    });
  };

  const handleToggleEnabled = async (worker: Worker) => {
    try {
      const endpoint = worker.isEnabled ? 'disable' : 'enable';
      const response = await fetch(`${API_BASE}/workers/${worker.id}/${endpoint}`, { method: 'POST' });
      if (!response.ok) throw new Error(`Failed to ${endpoint} worker`);
      fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle worker');
    }
  };

  const handleWorkerAction = async (workerId: number, action: 'sync-cache' | 'update') => {
    setWorkerAction({ id: workerId, kind: action });
    try {
      const response = await fetch(`${API_BASE}/workers/${workerId}/${action}`, { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `${action} failed`);
      setError(action === 'sync-cache'
        ? `Cache sync: ${data.pushed ?? 0} file(s) pushed`
        : `Update: ${data.synced ? 'worker now matches master' : 'did not converge'}`);
      fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : `${action} failed`);
    } finally {
      setWorkerAction(null);
    }
  };

  const handleHealthCheck = async (workerId: number) => {
    setHealthChecking(workerId);
    try {
      const response = await fetch(`${API_BASE}/workers/${workerId}/health-check`, { method: 'POST' });
      if (!response.ok) throw new Error('Health check failed');
      fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Health check failed');
    } finally {
      setHealthChecking(null);
    }
  };

  const handleExport = async () => {
    try {
      const response = await fetch(`${API_BASE}/workers/export`, { method: 'POST' });
      if (!response.ok) throw new Error('Failed to export workers');
      const data = await response.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `workers-export-${new Date().toISOString().split('T')[0]}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to export workers');
    }
  };

  const handleImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const response = await fetch(`${API_BASE}/workers/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });
      if (!response.ok) throw new Error('Failed to import workers');
      fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import workers');
    }
    event.target.value = '';
  };

  const openEditModal = (worker: Worker) => {
    setEditingWorker(worker);
    setFormData({
      name: worker.name,
      url: worker.url,
      description: worker.description || '',
      capabilities: worker.capabilities,
      password: ''  // write-only: blank means "keep existing"
    });
  };

  const getStatusIcon = (status: string, isEnabled: boolean) => {
    if (!isEnabled) return <PowerOff className="w-4 h-4 text-gray-400" />;
    switch (status) {
      case 'online': return <CheckCircle className="w-4 h-4 text-green-500" />;
      case 'busy': return <Activity className="w-4 h-4 text-yellow-500" />;
      default: return <AlertCircle className="w-4 h-4 text-red-500" />;
    }
  };

  // SOLID mid-tone bg + white text: readable in both themes (native `dark:` is inert here and a
  // global `.dark .font-*` rule force-lightens pill text, so light `-100` pills were unreadable).
  const getStatusColor = (status: string, isEnabled: boolean) => {
    if (!isEnabled) return 'bg-slate-500 text-white border border-slate-500';
    switch (status) {
      case 'online': return 'bg-emerald-600 text-white border border-emerald-600';
      case 'busy': return 'bg-amber-600 text-white border border-amber-600';
      default: return 'bg-red-600 text-white border border-red-600';
    }
  };

  const formatMemory = (mb: number) => {
    if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
    return `${mb} MB`;
  };

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Settings</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-1">
            Configure workers, API keys, and application settings
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

      {/* API Keys Section */}
      <div className="mb-6 bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <KeyRound className="w-5 h-5 text-blue-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">API Keys</h2>
          </div>
          <button
            onClick={handleImportKeysFromTrade}
            disabled={importingKeys}
            className="flex items-center gap-2 px-3 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-60"
            title="Copy provider API keys from the live trade platform's database"
          >
            {importingKeys ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Download className="w-4 h-4" />
            )}
            Import keys from trade platform
          </button>
        </div>
        <div className="p-4">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            View and set provider credentials (API keys, tokens, secrets). Stored values
            are masked. Enter a new value and Save to update; or import them all from the
            live trade platform's database so backtests can resolve them.
          </p>
          {keyImportMessage && (
            <div className="mt-3 p-3 bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300 rounded-lg flex items-start gap-2 text-sm">
              <CheckCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
              <span>{keyImportMessage}</span>
            </div>
          )}

          <div className="mt-4 space-y-3">
            {credentialKeys.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400">No credential keys available.</p>
            ) : (
              credentialKeys.map(ck => {
                const editing = keyEdits[ck.key] !== undefined;
                const revealed = !!revealedKeys[ck.key];
                return (
                  <div key={ck.key} className="flex flex-col sm:flex-row sm:items-center gap-2">
                    <label className="sm:w-64 flex-shrink-0 text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
                      <span className="truncate">{ck.key}</span>
                      {ck.is_set ? (
                        <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-green-100 text-green-800 border border-green-300 dark:bg-green-700/40 dark:text-green-100 dark:border-green-600/50">
                          set
                        </span>
                      ) : (
                        <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600 border border-gray-300 dark:bg-gray-600/40 dark:text-gray-200 dark:border-gray-500/50">
                          unset
                        </span>
                      )}
                    </label>
                    <div className="relative flex-1">
                      <input
                        type={revealed ? 'text' : 'password'}
                        value={editing ? keyEdits[ck.key] : ''}
                        onChange={e =>
                          setKeyEdits(prev => ({ ...prev, [ck.key]: e.target.value }))
                        }
                        placeholder={ck.masked_value || 'Not set — enter a value'}
                        className="w-full px-3 py-2 pr-10 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                      />
                      <button
                        type="button"
                        onClick={() =>
                          setRevealedKeys(prev => ({ ...prev, [ck.key]: !prev[ck.key] }))
                        }
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                        title={revealed ? 'Hide' : 'Reveal'}
                      >
                        {revealed ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>

          {credentialKeys.length > 0 && (
            <div className="mt-4 flex items-center gap-3">
              <button
                onClick={handleSaveCredentialKeys}
                disabled={savingKeys}
                className="flex items-center gap-2 px-3 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-60"
              >
                {savingKeys ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Save className="w-4 h-4" />
                )}
                Save keys
              </button>
              {keySaveMessage && (
                <span className="text-sm text-green-700 dark:text-green-300">{keySaveMessage}</span>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Workers Section */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server className="w-5 h-5 text-blue-500" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Workers</h2>
            <span className="text-sm text-gray-500 dark:text-gray-400">
              ({workers.filter(w => w.isEnabled).length} enabled)
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={fetchWorkers}
              className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
              title="Refresh"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
            <label className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg cursor-pointer" title="Import">
              <Upload className="w-4 h-4" />
              <input type="file" accept=".json" onChange={handleImport} className="hidden" />
            </label>
            <button
              onClick={handleExport}
              className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
              title="Export"
            >
              <Download className="w-4 h-4" />
            </button>
            <button
              onClick={() => setShowAddModal(true)}
              className="flex items-center gap-2 px-3 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600"
            >
              <Plus className="w-4 h-4" />
              Add Worker
            </button>
          </div>
        </div>

        <div className="p-4">
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
            </div>
          ) : workers.length === 0 ? (
            <p className="text-center text-gray-500 dark:text-gray-400 py-8">No workers configured</p>
          ) : (
            <div className="grid gap-4">
              {workers.map(worker => (
                <div
                  key={worker.id}
                  className={`p-4 border rounded-lg ${
                    worker.isLocal
                      ? 'bg-blue-50 dark:bg-gray-800/60 border-blue-200 dark:border-gray-600'
                      : 'bg-white dark:bg-gray-800/60 border-gray-200 dark:border-gray-600'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-3">
                      <div className="mt-1">
                        {getStatusIcon(worker.status, worker.isEnabled)}
                      </div>
                      <div>
                        <div className="flex items-center gap-2">
                          <h3 className="font-semibold text-gray-900 dark:text-gray-100">{worker.name}</h3>
                          {worker.isLocal && (
                            <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 text-blue-800 border border-blue-300 dark:bg-blue-700/40 dark:text-blue-100 dark:border-blue-600/50">
                              Local
                            </span>
                          )}
                          <span className={`px-2 py-0.5 text-xs font-medium rounded-full ${getStatusColor(worker.status, worker.isEnabled)}`}>
                            {worker.isEnabled ? worker.status : 'disabled'}
                          </span>
                        </div>
                        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                          {worker.isLocal ? 'Running on backend host' : worker.url}
                        </p>
                        {worker.description && (
                          <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">{worker.description}</p>
                        )}

                        {/* Hardware Info */}
                        <div className="flex flex-wrap gap-4 mt-3 text-sm">
                          {worker.gpuInfo && (
                            <div className="flex items-center gap-1 text-gray-600 dark:text-gray-400">
                              <HardDrive className="w-4 h-4" />
                              <span>{worker.gpuInfo.name} ({formatMemory(worker.gpuInfo.memory)})</span>
                              {worker.gpuInfo.count > 1 && <span>x{worker.gpuInfo.count}</span>}
                            </div>
                          )}
                          {worker.cpuInfo && (
                            <div className="flex items-center gap-1 text-gray-600 dark:text-gray-400">
                              <Cpu className="w-4 h-4" />
                              <span>{worker.cpuInfo.cores} cores</span>
                            </div>
                          )}
                          <div className="flex items-center gap-1 text-gray-600 dark:text-gray-400">
                            <Activity className="w-4 h-4" />
                            <span>{worker.activeJobsCount} active jobs</span>
                          </div>
                          {worker.lastHeartbeat && (
                            <div className="flex items-center gap-1 text-gray-500 dark:text-gray-400">
                              <Clock className="w-4 h-4" />
                              <span>Last seen: {new Date(worker.lastHeartbeat).toLocaleTimeString()}</span>
                            </div>
                          )}
                        </div>

                        {/* Capabilities */}
                        <div className="flex gap-2 mt-2">
                          {worker.capabilities.train && (
                            <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-purple-100 text-purple-800 border border-purple-300 dark:bg-purple-700/40 dark:text-purple-100 dark:border-purple-600/50">
                              Training
                            </span>
                          )}
                          {worker.capabilities.infer && (
                            <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-green-100 text-green-800 border border-green-300 dark:bg-green-700/40 dark:text-green-100 dark:border-green-600/50">
                              Inference
                            </span>
                          )}
                        </div>
                      </div>
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => handleHealthCheck(worker.id)}
                        disabled={healthChecking === worker.id}
                        className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50"
                        title="Health Check"
                      >
                        {healthChecking === worker.id ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          <RefreshCw className="w-4 h-4" />
                        )}
                      </button>
                      <button
                        onClick={() => handleToggleEnabled(worker)}
                        className={`p-2 rounded-lg ${
                          worker.isEnabled ? 'text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20' : 'text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
                        }`}
                        title={worker.isEnabled ? 'Disable' : 'Enable'}
                      >
                        {worker.isEnabled ? <Power className="w-4 h-4" /> : <PowerOff className="w-4 h-4" />}
                      </button>
                      {!worker.isLocal && (
                        <>
                          <button
                            onClick={() => handleWorkerAction(worker.id, 'sync-cache')}
                            disabled={workerAction?.id === worker.id}
                            className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50"
                            title="Push cache to this worker"
                          >
                            {workerAction?.id === worker.id && workerAction.kind === 'sync-cache'
                              ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                          </button>
                          <button
                            onClick={() => handleWorkerAction(worker.id, 'update')}
                            disabled={workerAction?.id === worker.id}
                            className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg disabled:opacity-50"
                            title="Update worker to master version (git pull + restart)"
                          >
                            {workerAction?.id === worker.id && workerAction.kind === 'update'
                              ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                          </button>
                          <button
                            onClick={() => openEditModal(worker)}
                            className="p-2 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
                            title="Edit"
                          >
                            <Edit2 className="w-4 h-4" />
                          </button>
                          <button
                            onClick={() => handleDeleteWorker(worker.id)}
                            className="p-2 text-red-600 hover:bg-red-50 rounded-lg"
                            title="Delete"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Add/Edit Worker Modal */}
      {(showAddModal || editingWorker) && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                {editingWorker ? 'Edit Worker' : 'Add Worker'}
              </h3>
              <button
                onClick={() => {
                  setShowAddModal(false);
                  setEditingWorker(null);
                  setFormData({ name: '', url: '', description: '', capabilities: { train: true, infer: true }, password: '' });
                }}
                className="text-gray-500 hover:text-gray-700"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4 space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">Name</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={e => setFormData({ ...formData, name: e.target.value })}
                  placeholder="GPU Server 1"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">URL</label>
                <input
                  type="text"
                  value={formData.url}
                  onChange={e => setFormData({ ...formData, url: e.target.value })}
                  placeholder="http://192.168.1.100:8001"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">Description (optional)</label>
                <input
                  type="text"
                  value={formData.description}
                  onChange={e => setFormData({ ...formData, description: e.target.value })}
                  placeholder="Description of this worker"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">
                  Password{editingWorker?.hasPassword ? ' (set — leave blank to keep)' : ''}
                </label>
                <input
                  type="password"
                  value={formData.password}
                  onChange={e => setFormData({ ...formData, password: e.target.value })}
                  placeholder={editingWorker?.hasPassword ? '••••••••' : 'Worker auth password'}
                  autoComplete="new-password"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
                <p className="text-xs text-gray-500 mt-1">The master sends this to authenticate to the worker. Must match the worker's <code>--password</code>.</p>
              </div>
              <div>
                <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-300">Capabilities</label>
                <div className="flex gap-4">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={formData.capabilities.train}
                      onChange={e => setFormData({
                        ...formData,
                        capabilities: { ...formData.capabilities, train: e.target.checked }
                      })}
                      className="rounded border-gray-300"
                    />
                    <span>Training</span>
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={formData.capabilities.infer}
                      onChange={e => setFormData({
                        ...formData,
                        capabilities: { ...formData.capabilities, infer: e.target.checked }
                      })}
                      className="rounded border-gray-300"
                    />
                    <span>Inference</span>
                  </label>
                </div>
              </div>
            </div>
            <div className="p-4 border-t border-gray-200 dark:border-gray-700 flex justify-end gap-2">
              <button
                onClick={() => {
                  setShowAddModal(false);
                  setEditingWorker(null);
                  setFormData({ name: '', url: '', description: '', capabilities: { train: true, infer: true }, password: '' });
                }}
                className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
              >
                Cancel
              </button>
              <button
                onClick={editingWorker ? handleUpdateWorker : handleAddWorker}
                disabled={!formData.name || !formData.url}
                className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {editingWorker ? 'Save Changes' : 'Add Worker'}
              </button>
            </div>
          </div>
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

export default Settings;
