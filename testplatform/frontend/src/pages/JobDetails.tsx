import { API_BASE } from '../lib/config';
import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  ArrowLeft, Clock, CheckCircle, AlertCircle, Loader2, Pause, Play,
  XCircle, Activity, Target, Zap, Timer, ChevronDown, ChevronRight,
  Info, FileText, Cpu, MemoryStick, RefreshCw, Save, Trophy, Award, Download, Wifi, WifiOff
} from 'lucide-react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend
} from 'recharts';
import ConfirmDialog from '../components/ConfirmDialog';
import { useJobWebSocket } from '../hooks/useJobWebSocket';
import type { JobProgressData } from '../hooks/useJobWebSocket';

interface EpochMetric {
  epoch: number;
  train_loss?: number;
  val_loss?: number;
  accuracy?: number;
  val_accuracy?: number;
  [key: string]: number | undefined;
}

interface Job {
  id: string;
  datasetId: number;
  selectedModels: string[];
  status: 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled' | 'stopped';
  progress: number;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  error?: string;
  currentGeneration?: number;
  totalGenerations?: number;
  bestFitness?: number;
  gpuUtilization?: number;
  // Training progress
  currentEpoch?: number;
  totalEpochs?: number;
  currentIndividual?: number;
  populationSize?: number;
  currentModelType?: string;
  // Error tracking
  errorCount?: number;
  successCount?: number;
  // Epoch-level tracking
  currentModelParams?: Record<string, number | string>;
  epochHistory?: EpochMetric[];
  // Optimization settings
  optimizeMetric?: string;
  lossFunction?: string;
  lossFunctions?: string[];
  optimizeLossFunction?: boolean;
  // Real-time individuals tracking
  individualsCount?: number;
  allIndividuals?: Individual[];
  // Dataset statistics
  trainRows?: number;
  testRows?: number;
  targetColumn?: string;
  trainPositives?: number;
  testPositives?: number;
  trainPositivesPct?: number;
  testPositivesPct?: number;
  // Retrain fields
  isRetrain?: boolean;
  sourceModelId?: string;
  retrainMode?: string;
}

interface Individual {
  generation: number;
  individual: number;
  model_type: string;
  params: Record<string, number | string>;
  loss_function?: string;
  fitness: number;
  metrics: Record<string, number>;
  training_history?: Array<{ epoch: number; loss: number; val_loss?: number }>;
}

interface GenerationSummary {
  generation: number;
  individual_count: number;
  best_fitness: number;
  avg_fitness: number;
  min_fitness: number;
  model_types: Record<string, number>;
  best_individual: Individual | null;
}

interface GenerationsData {
  job_id: string;
  total_generations: number;
  generations: GenerationSummary[];
}

interface IndividualsData {
  job_id: string;
  summary: {
    total_individuals: number;
    generations: number[];
    model_types: string[];
    best_fitness: number;
    avg_fitness: number;
  };
  best_individual: Individual | null;
  individuals: Individual[];
}

interface SystemResources {
  cpuPercent: number;
  memoryUsedMB: number;
  memoryTotalMB: number;
  memoryPercent: number;
  gpuUtilization: number | null;
  gpuMemoryUsedMB: number | null;
  gpuMemoryTotalMB: number | null;
}

interface EliteModel {
  rank: number;
  model_type: string;
  fitness: number;
  file_path: string;
  file_name: string;
  metrics: Record<string, number>;
  params: Record<string, number | string>;
  generation?: number;
  individual?: number;
}

const MODEL_COLORS: Record<string, string> = {
  lstm: 'bg-blue-500',
  gru: 'bg-green-500',
  nbeats: 'bg-purple-500',
  tcn: 'bg-orange-500',
  transformer: 'bg-pink-500',
  tft: 'bg-cyan-500',
};

const MODEL_TEXT_COLORS: Record<string, string> = {
  lstm: 'text-blue-600 bg-blue-100 dark:bg-blue-900/50 dark:text-blue-300',
  gru: 'text-green-600 bg-green-100 dark:bg-green-900/50 dark:text-green-300',
  nbeats: 'text-purple-600 bg-purple-100 dark:bg-purple-900/50 dark:text-purple-300',
  tcn: 'text-orange-600 bg-orange-100 dark:bg-orange-900/50 dark:text-orange-300',
  transformer: 'text-pink-600 bg-pink-100 dark:bg-pink-900/50 dark:text-pink-300',
  tft: 'text-cyan-600 bg-cyan-100 dark:bg-cyan-900/50 dark:text-cyan-300',
  inception: 'text-indigo-600 bg-indigo-100 dark:bg-indigo-900/50 dark:text-indigo-300',
  resnet: 'text-rose-600 bg-rose-100 dark:bg-rose-900/50 dark:text-rose-300',
  xception: 'text-amber-600 bg-amber-100 dark:bg-amber-900/50 dark:text-amber-300',
  omniscale: 'text-teal-600 bg-teal-100 dark:bg-teal-900/50 dark:text-teal-300',
  minirocket: 'text-violet-600 bg-violet-100 dark:bg-violet-900/50 dark:text-violet-300',
  lstm_fcn: 'text-sky-600 bg-sky-100 dark:bg-sky-900/50 dark:text-sky-300',
  tst: 'text-fuchsia-600 bg-fuchsia-100 dark:bg-fuchsia-900/50 dark:text-fuchsia-300',
  patchtst: 'text-lime-600 bg-lime-100 dark:bg-lime-900/50 dark:text-lime-300',
};

const DEFAULT_MODEL_COLOR = 'text-gray-600 bg-gray-100 dark:bg-gray-700 dark:text-gray-300';

const formatLossFunction = (loss: string | undefined) => {
  if (!loss) return 'N/A';
  return loss.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
};

const JobDetails: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [job, setJob] = useState<Job | null>(null);
  const [generationsData, setGenerationsData] = useState<GenerationsData | null>(null);
  const [individualsData, setIndividualsData] = useState<IndividualsData | null>(null);
  const [resources, setResources] = useState<SystemResources | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  // Constants for memory management
  const MAX_LOGS = 500;  // Limit logs to prevent memory bloat
  const MAX_INDIVIDUALS_DISPLAY = 1000;  // Limit individuals in memory
  const [error, setError] = useState<string | null>(null);
  const [expandedGenerations, setExpandedGenerations] = useState<Set<number>>(new Set());
  const [showLogs, setShowLogs] = useState(false);
  const [selectedIndividual, setSelectedIndividual] = useState<Individual | null>(null);
  const [elapsedTime, setElapsedTime] = useState<string>('');
  const [eliteModels, setEliteModels] = useState<EliteModel[]>([]);
  const [savingModel, setSavingModel] = useState<number | null>(null);
  const [savingRetrainResult, setSavingRetrainResult] = useState(false);
  const [newModelName, setNewModelName] = useState('');
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });
  const [shouldRefreshOnComplete, setShouldRefreshOnComplete] = useState(false);

  const fetchJob = useCallback(async () => {
    if (!id) return;
    try {
      const response = await fetch(`${API_BASE}/jobs/${id}/progress`);
      if (!response.ok) throw new Error('Failed to fetch job');
      const data = await response.json();
      // Limit allIndividuals to prevent memory bloat (full list available via individuals endpoint)
      if (data.job?.allIndividuals && data.job.allIndividuals.length > MAX_INDIVIDUALS_DISPLAY) {
        data.job.allIndividuals = data.job.allIndividuals.slice(-MAX_INDIVIDUALS_DISPLAY);
      }
      setJob(data.job);
      // Limit logs to most recent to prevent memory accumulation
      const allLogs = data.logs || [];
      setLogs(allLogs.length > MAX_LOGS ? allLogs.slice(-MAX_LOGS) : allLogs);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load job');
    } finally {
      setIsLoading(false);
    }
  }, [id]);

  const fetchGenerations = useCallback(async () => {
    if (!id) return;
    try {
      const response = await fetch(`${API_BASE}/jobs/${id}/generations`);
      if (response.ok) {
        const data: GenerationsData = await response.json();
        setGenerationsData(data);
      }
    } catch (err) {
      console.error('Failed to fetch generations:', err);
    }
  }, [id]);

  const fetchIndividuals = useCallback(async () => {
    if (!id) return;
    try {
      const response = await fetch(`${API_BASE}/jobs/${id}/individuals`);
      if (response.ok) {
        const data: IndividualsData = await response.json();
        // Limit individuals in memory - keep most recent generations
        if (data.individuals && data.individuals.length > MAX_INDIVIDUALS_DISPLAY) {
          // Sort by generation desc, individual desc to keep newest
          data.individuals.sort((a, b) =>
            b.generation !== a.generation ? b.generation - a.generation : b.individual - a.individual
          );
          data.individuals = data.individuals.slice(0, MAX_INDIVIDUALS_DISPLAY);
          // Re-sort for display (generation asc)
          data.individuals.sort((a, b) =>
            a.generation !== b.generation ? a.generation - b.generation : a.individual - b.individual
          );
        }
        setIndividualsData(data);
      }
    } catch (err) {
      console.error('Failed to fetch individuals:', err);
    }
  }, [id]);

  const fetchResources = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/dashboard/stats`);
      if (response.ok) {
        const data = await response.json();
        setResources(data.systemResources);
      }
    } catch (err) {
      console.error('Failed to fetch resources:', err);
    }
  }, []);

  const fetchEliteModels = useCallback(async () => {
    if (!id) return;
    try {
      const response = await fetch(`${API_BASE}/jobs/${id}/elite-models`);
      if (response.ok) {
        const data = await response.json();
        setEliteModels(data.elite_models || []);
      }
    } catch (err) {
      console.error('Failed to fetch elite models:', err);
    }
  }, [id]);

  // WebSocket for real-time job updates
  const handleWebSocketProgress = useCallback((data: JobProgressData) => {
    // Update job state with all progress fields
    setJob(prev => {
      if (!prev) return prev;
      return {
        ...prev,
        status: (data.status as Job['status']) || prev.status,
        progress: data.progress ?? prev.progress,
        // Generation/Individual progress
        currentGeneration: data.currentGeneration ?? prev.currentGeneration,
        totalGenerations: data.totalGenerations ?? prev.totalGenerations,
        currentIndividual: data.currentIndividual ?? prev.currentIndividual,
        populationSize: data.populationSize ?? prev.populationSize,
        // Epoch/Training progress
        currentEpoch: data.currentEpoch ?? prev.currentEpoch,
        totalEpochs: data.totalEpochs ?? prev.totalEpochs,
        currentModelType: data.currentModelType ?? prev.currentModelType,
        currentModelParams: data.currentModelParams ?? prev.currentModelParams,
        // Metrics
        bestFitness: data.bestFitness ?? prev.bestFitness,
        // Error tracking
        errorCount: data.errorCount ?? prev.errorCount,
        successCount: data.successCount ?? prev.successCount,
        // Individuals count
        individualsCount: data.individualsCount ?? prev.individualsCount,
        // GPU utilization
        gpuUtilization: data.gpuUtilization ?? prev.gpuUtilization,
        // Epoch history (for live chart)
        epochHistory: data.epochHistory && data.epochHistory.length > 0
          ? data.epochHistory
          : prev.epochHistory,
      };
    });

    // Update system resources if available
    if (data.systemResources) {
      setResources({
        cpuPercent: data.systemResources.cpuPercent ?? 0,
        memoryUsedMB: data.systemResources.memoryUsedMB ?? 0,
        memoryTotalMB: data.systemResources.memoryTotalMB ?? 0,
        memoryPercent: data.systemResources.memoryPercent ?? 0,
        gpuUtilization: data.systemResources.gpuUtilization ?? null,
        gpuMemoryUsedMB: data.systemResources.gpuMemoryUsedMB ?? null,
        gpuMemoryTotalMB: data.systemResources.gpuMemoryTotalMB ?? null,
      });
    }
  }, []);

  const handleWebSocketLog = useCallback((message: string) => {
    setLogs(prev => {
      const newLogs = [...prev, message];
      return newLogs.length > MAX_LOGS ? newLogs.slice(-MAX_LOGS) : newLogs;
    });
  }, []);

  const handleWebSocketComplete = useCallback((_data: JobProgressData) => {
    // Trigger a refresh on completion
    setShouldRefreshOnComplete(true);
  }, []);

  // Handle refresh on job completion
  useEffect(() => {
    if (shouldRefreshOnComplete) {
      setShouldRefreshOnComplete(false);
      fetchJob();
      fetchEliteModels();
    }
  }, [shouldRefreshOnComplete, fetchJob, fetchEliteModels]);

  const { isConnected: wsConnected, error: wsError, reconnect: wsReconnect } = useJobWebSocket(
    job?.status === 'running' || job?.status === 'paused' ? id || null : null,
    {
      onProgress: handleWebSocketProgress,
      onLog: handleWebSocketLog,
      onComplete: handleWebSocketComplete,
      enabled: true,
    }
  );

  const handleSaveToInventory = async (rank: number, _modelType: string) => {
    if (!id) return;
    setSavingModel(rank);
    try {
      const response = await fetch(`${API_BASE}/jobs/${id}/elite-models/${rank}/save-to-inventory`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({})
      });
      if (response.ok) {
        const data = await response.json();
        alert(`Model saved: ${data.modelName}`);
      } else {
        const error = await response.json();
        alert(`Failed to save: ${error.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error('Failed to save model:', err);
      alert('Failed to save model to inventory');
    } finally {
      setSavingModel(null);
    }
  };

  const handleSaveRetrainResult = async (saveMode: 'update_original' | 'new') => {
    if (!id || !job) return;
    setSavingRetrainResult(true);
    try {
      const response = await fetch(`${API_BASE}/jobs/${id}/retrain-save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          saveMode,
          newModelName: saveMode === 'new' ? newModelName || undefined : undefined
        })
      });
      if (response.ok) {
        const data = await response.json();
        alert(data.message);
        if (saveMode === 'update_original' && job.sourceModelId) {
          navigate(`/model/${job.sourceModelId}`);
        } else if (data.modelId) {
          navigate(`/model/${data.modelId}`);
        }
      } else {
        const error = await response.json();
        alert(`Failed to save: ${error.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error('Failed to save retrain result:', err);
      alert('Failed to save retrain result');
    } finally {
      setSavingRetrainResult(false);
    }
  };

  // Initial load
  useEffect(() => {
    fetchJob();
    fetchGenerations();
    fetchIndividuals();
    fetchResources();
    fetchEliteModels();
  }, [fetchJob, fetchGenerations, fetchIndividuals, fetchResources, fetchEliteModels]);

  // Auto-refresh for running jobs
  // Always poll via HTTP — subprocess training doesn't push via WebSocket
  useEffect(() => {
    if (job?.status === 'running' || job?.status === 'paused') {
      // Poll job progress, resources, and all data every 5 seconds
      const progressInterval = setInterval(() => {
        fetchJob();
        fetchResources();
        fetchGenerations();
        fetchIndividuals();
        fetchEliteModels();
      }, 5000);

      return () => {
        clearInterval(progressInterval);
      };
    }
  }, [job?.status, wsConnected, fetchJob, fetchGenerations, fetchIndividuals, fetchResources, fetchEliteModels]);

  // Clear real-time data from job object when completed to free memory
  // (generations/individuals endpoints still available for historical view)
  useEffect(() => {
    if (job?.status === 'completed' || job?.status === 'failed' || job?.status === 'cancelled') {
      // Clear epoch history and allIndividuals from job to free memory
      // These are only needed during live training
      setJob(prev => {
        if (!prev) return prev;
        if (prev.epochHistory || prev.allIndividuals) {
          return {
            ...prev,
            epochHistory: undefined,
            allIndividuals: undefined,
          };
        }
        return prev;
      });
      // Clear resources since job is no longer running
      setResources(null);
    }
  }, [job?.status]);

  // Update elapsed time
  useEffect(() => {
    const updateElapsed = () => {
      if (!job?.startedAt) {
        setElapsedTime('--');
        return;
      }
      const start = new Date(job.startedAt).getTime();
      const end = job.completedAt ? new Date(job.completedAt).getTime() : Date.now();
      const diff = end - start;
      const hours = Math.floor(diff / 3600000);
      const minutes = Math.floor((diff % 3600000) / 60000);
      const seconds = Math.floor((diff % 60000) / 1000);
      setElapsedTime(
        hours > 0 ? `${hours}h ${minutes}m ${seconds}s` : `${minutes}m ${seconds}s`
      );
    };

    updateElapsed();
    if (job?.status === 'running') {
      const interval = setInterval(updateElapsed, 1000);
      return () => clearInterval(interval);
    }
  }, [job?.startedAt, job?.completedAt, job?.status]);

  const handlePause = async () => {
    try {
      await fetch(`${API_BASE}/jobs/${id}/pause`, { method: 'POST' });
      fetchJob();
    } catch (err) {
      console.error('Failed to pause job:', err);
    }
  };

  const handleResume = async () => {
    try {
      await fetch(`${API_BASE}/jobs/${id}/resume`, { method: 'POST' });
      fetchJob();
    } catch (err) {
      console.error('Failed to resume job:', err);
    }
  };

  const handleCancel = () => {
    setConfirmDialog({
      isOpen: true,
      title: 'Cancel Job',
      message: 'Are you sure you want to cancel this job?',
      variant: 'warning',
      onConfirm: async () => {
        try {
          await fetch(`${API_BASE}/jobs/${id}/cancel`, { method: 'POST' });
          fetchJob();
        } catch (err) {
          console.error('Failed to cancel job:', err);
        }
      },
    });
  };

  const toggleGeneration = (gen: number) => {
    setExpandedGenerations(prev => {
      const next = new Set(prev);
      if (next.has(gen)) {
        next.delete(gen);
      } else {
        next.add(gen);
      }
      return next;
    });
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'running': return <Loader2 size={20} className="text-blue-500 animate-spin" />;
      case 'completed': return <CheckCircle size={20} className="text-green-500" />;
      case 'failed': return <AlertCircle size={20} className="text-red-500" />;
      case 'paused': return <Pause size={20} className="text-yellow-500" />;
      case 'stopped': return <AlertCircle size={20} className="text-orange-500" />;
      case 'cancelled': return <XCircle size={20} className="text-gray-500" />;
      default: return <Clock size={20} className="text-gray-500" />;
    }
  };

  // SOLID mid-tone bg + white text: readable in both themes (native `dark:` is inert here and a
  // global `.dark .font-*` rule force-lightens pill text, so light `-100` pills were unreadable).
  const getStatusColor = (status: string) => {
    switch (status) {
      case 'running': return 'bg-blue-600 text-white';
      case 'completed': return 'bg-emerald-600 text-white';
      case 'failed': return 'bg-red-600 text-white';
      case 'paused': return 'bg-amber-600 text-white';
      case 'stopped': return 'bg-orange-600 text-white';
      default: return 'bg-slate-500 text-white';
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 size={48} className="animate-spin text-blue-500" />
      </div>
    );
  }

  if (error || !job) {
    return (
      <div className="p-6">
        <div className="bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 p-4 rounded-lg">
          {error || 'Job not found'}
        </div>
        <button
          onClick={() => navigate('/training')}
          className="mt-4 flex items-center space-x-2 text-blue-500 hover:underline"
        >
          <ArrowLeft size={16} />
          <span>Back to Training</span>
        </button>
      </div>
    );
  }

  const chartData = generationsData?.generations.map(g => ({
    generation: g.generation + 1,
    best: g.best_fitness,
    avg: g.avg_fitness,
    min: g.min_fitness,
  })) || [];

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-4">
          <button
            onClick={() => navigate('/training')}
            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
          >
            <ArrowLeft size={20} />
          </button>
          <div>
            <div className="flex items-center space-x-3">
              <h1 className="text-2xl font-bold">Job #{job.id}</h1>
              <span className={`px-3 py-1 rounded-full text-sm font-medium flex items-center space-x-1 ${getStatusColor(job.status)}`}>
                {getStatusIcon(job.status)}
                <span className="ml-1">{job.status.charAt(0).toUpperCase() + job.status.slice(1)}</span>
              </span>
            </div>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
              Models: {job.selectedModels.map(m => m.toUpperCase()).join(', ')}
            </p>
          </div>
        </div>
        <div className="flex items-center space-x-2">
          {/* WebSocket connection indicator */}
          {(job.status === 'running' || job.status === 'paused') && (
            <div
              className={`flex items-center space-x-1 px-2 py-1 rounded-full text-xs ${
                wsConnected
                  ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                  : 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400'
              }`}
              title={wsConnected ? 'Real-time updates active' : wsError || 'Connecting...'}
            >
              {wsConnected ? <Wifi size={12} /> : <WifiOff size={12} />}
              <span>{wsConnected ? 'Live' : 'Polling'}</span>
              {!wsConnected && (
                <button
                  onClick={wsReconnect}
                  className="ml-1 hover:text-yellow-900 dark:hover:text-yellow-300"
                  title="Retry connection"
                >
                  <RefreshCw size={10} />
                </button>
              )}
            </div>
          )}
          <button
            onClick={() => { fetchJob(); fetchGenerations(); fetchIndividuals(); }}
            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
            title="Refresh"
          >
            <RefreshCw size={18} />
          </button>
          {job.status === 'running' && (
            <button
              onClick={handlePause}
              className="px-4 py-2 bg-yellow-500 text-white rounded-lg hover:bg-yellow-600 flex items-center space-x-2"
            >
              <Pause size={16} />
              <span>Pause</span>
            </button>
          )}
          {(job.status === 'paused' || job.status === 'stopped') && (
            <button
              onClick={handleResume}
              className="px-4 py-2 bg-green-500 text-white rounded-lg hover:bg-green-600 flex items-center space-x-2"
            >
              <Play size={16} />
              <span>Resume</span>
            </button>
          )}
          {['running', 'paused', 'queued'].includes(job.status) && (
            <button
              onClick={handleCancel}
              className="px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 flex items-center space-x-2"
            >
              <XCircle size={16} />
              <span>Cancel</span>
            </button>
          )}
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
            <Target size={16} />
            <span className="text-xs">Best Fitness</span>
          </div>
          <div className="text-2xl font-bold text-green-600">
            {(() => {
              // Priority: job.bestFitness (real-time) > individualsData > allIndividuals computed
              const jobBest = job.bestFitness;
              const summaryBest = individualsData?.summary?.best_fitness;
              const computedBest = job.allIndividuals?.length
                ? Math.max(...job.allIndividuals.map(i => i.fitness || 0))
                : null;
              const best = jobBest ?? summaryBest ?? computedBest;
              return best != null && best > 0 ? best.toFixed(4) : '--';
            })()}
          </div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
            <Activity size={16} />
            <span className="text-xs">Generations</span>
          </div>
          <div className="text-2xl font-bold">
            {generationsData?.total_generations || job.currentGeneration || 0}
            <span className="text-sm text-gray-500 font-normal">/{job.totalGenerations || 50}</span>
          </div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
            <Timer size={16} />
            <span className="text-xs">{job.completedAt ? 'Total Time' : 'Elapsed'}</span>
          </div>
          <div className="text-2xl font-bold">{elapsedTime}</div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
            <Info size={16} />
            <span className="text-xs">Individuals</span>
          </div>
          <div className="text-2xl font-bold">
            {job.individualsCount || job.allIndividuals?.length || individualsData?.summary?.total_individuals || 0}
          </div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
            <Zap size={16} />
            <span className="text-xs">GPU Usage</span>
          </div>
          <div className="text-2xl font-bold text-purple-600">
            {resources?.gpuUtilization != null ? `${resources.gpuUtilization}%` : '--'}
          </div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
            <Clock size={16} />
            <span className="text-xs">Progress</span>
          </div>
          <div className="text-2xl font-bold">
            {(() => {
              // Use generation-based progress for consistency with overall progress bar
              const totalGens = job.totalGenerations || 50;
              const currentGen = job.currentGeneration || 0;
              const currentInd = job.currentIndividual || 0;
              const popSize = job.populationSize || 20;
              const genProgress = ((currentGen + (currentInd / popSize)) / totalGens) * 100;
              return `${genProgress.toFixed(1)}%`;
            })()}
          </div>
        </div>
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
            <AlertCircle size={16} />
            <span className="text-xs">Errors / Success</span>
          </div>
          <div className="text-2xl font-bold">
            <span className={(job.errorCount || 0) > 0 ? 'text-red-600' : 'text-gray-400'}>{job.errorCount || 0}</span>
            <span className="text-gray-400 mx-1">/</span>
            <span className="text-green-600">{job.successCount || 0}</span>
          </div>
        </div>
      </div>

      {/* Dataset Statistics */}
      {(job.trainRows || job.testRows) && (
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Dataset Statistics</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4 text-sm">
            <div>
              <span className="text-gray-500 dark:text-gray-400">Target:</span>
              <span className="ml-2 font-mono text-xs">{job.targetColumn || '--'}</span>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Loss:</span>
              <span className="ml-2 font-medium">
                {job.optimizeLossFunction && job.lossFunctions && job.lossFunctions.length > 1
                  ? `Optimizing (${job.lossFunctions.length})`
                  : formatLossFunction(job.lossFunction)}
              </span>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Train Rows:</span>
              <span className="ml-2 font-bold">{job.trainRows?.toLocaleString() || '--'}</span>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Test Rows:</span>
              <span className="ml-2 font-bold">{job.testRows?.toLocaleString() || '--'}</span>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Train Positives:</span>
              <span className="ml-2 font-bold text-green-600">
                {job.trainPositives ?? '--'}
                {job.trainPositivesPct != null && <span className="text-gray-400 ml-1">({job.trainPositivesPct}%)</span>}
              </span>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Test Positives:</span>
              <span className="ml-2 font-bold text-blue-600">
                {job.testPositives ?? '--'}
                {job.testPositivesPct != null && <span className="text-gray-400 ml-1">({job.testPositivesPct}%)</span>}
              </span>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Total:</span>
              <span className="ml-2 font-bold">{((job.trainRows || 0) + (job.testRows || 0)).toLocaleString()}</span>
            </div>
          </div>

          {/* Dataset Download Buttons */}
          <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700 space-y-3">
            <span className="text-sm text-gray-500 dark:text-gray-400 block">Download Datasets:</span>

            {/* RNN Datasets (LSTM/GRU) */}
            <div className="flex items-center space-x-2">
              <span className="text-xs text-purple-600 dark:text-purple-400 font-medium w-20">LSTM/GRU:</span>
              <a
                href={`${API_BASE}/jobs/${job.id}/datasets/train_rnn.csv`}
                download
                className="inline-flex items-center px-2 py-1 text-xs font-medium rounded bg-purple-100 text-purple-700 hover:bg-purple-200 dark:bg-purple-900/30 dark:text-purple-400 dark:hover:bg-purple-900/50 transition-colors"
              >
                <Download size={10} className="mr-1" />
                Train
              </a>
              <a
                href={`${API_BASE}/jobs/${job.id}/datasets/test_rnn.csv`}
                download
                className="inline-flex items-center px-2 py-1 text-xs font-medium rounded bg-purple-100 text-purple-700 hover:bg-purple-200 dark:bg-purple-900/30 dark:text-purple-400 dark:hover:bg-purple-900/50 transition-colors"
              >
                <Download size={10} className="mr-1" />
                Test
              </a>
              <span className="text-xs text-gray-400">(shifted targets)</span>
            </div>

            {/* Multi-step Datasets (NBEATS/TCN/Transformer) */}
            <div className="flex items-center space-x-2">
              <span className="text-xs text-green-600 dark:text-green-400 font-medium w-20">Multi-step:</span>
              <a
                href={`${API_BASE}/jobs/${job.id}/datasets/train_multistep.csv`}
                download
                className="inline-flex items-center px-2 py-1 text-xs font-medium rounded bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50 transition-colors"
              >
                <Download size={10} className="mr-1" />
                Train
              </a>
              <a
                href={`${API_BASE}/jobs/${job.id}/datasets/test_multistep.csv`}
                download
                className="inline-flex items-center px-2 py-1 text-xs font-medium rounded bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50 transition-colors"
              >
                <Download size={10} className="mr-1" />
                Test
              </a>
              <span className="text-xs text-gray-400">(original targets)</span>
            </div>

            {/* Combined/Debug */}
            <div className="flex items-center space-x-2">
              <span className="text-xs text-gray-500 dark:text-gray-400 font-medium w-20">Debug:</span>
              <a
                href={`${API_BASE}/jobs/${job.id}/datasets/combined_dataset.csv`}
                download
                className="inline-flex items-center px-2 py-1 text-xs font-medium rounded bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600 transition-colors"
              >
                <Download size={10} className="mr-1" />
                Combined
              </a>
              <a
                href={`${API_BASE}/jobs/${job.id}/datasets/metadata.json`}
                download
                className="inline-flex items-center px-2 py-1 text-xs font-medium rounded bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600 transition-colors"
              >
                <Download size={10} className="mr-1" />
                Metadata
              </a>
            </div>
          </div>
        </div>
      )}

      {/* Training Progress (for running jobs) */}
      {job.status === 'running' && (
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">Training Progress</h3>
          <div className="space-y-4">
            {/* Current Model Epoch Progress */}
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="flex items-center space-x-2">
                  <Activity size={14} className="text-orange-500" />
                  <span>Current Model</span>
                  {job.currentModelType && (
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${MODEL_TEXT_COLORS[job.currentModelType] || DEFAULT_MODEL_COLOR}`}>
                      {job.currentModelType.toUpperCase()}
                    </span>
                  )}
                </span>
                <span className="font-mono">
                  Epoch {job.currentEpoch || 0}/{job.totalEpochs || 10}
                </span>
              </div>
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-4">
                <div
                  className="h-4 rounded-full bg-gradient-to-r from-orange-400 to-orange-500 transition-all duration-300 flex items-center justify-center"
                  style={{ width: `${job.totalEpochs ? ((job.currentEpoch || 0) / job.totalEpochs) * 100 : 0}%`, minWidth: job.currentEpoch ? '2rem' : 0 }}
                >
                  {job.currentEpoch ? (
                    <span className="text-xs text-white font-medium">
                      {Math.round(((job.currentEpoch || 0) / (job.totalEpochs || 10)) * 100)}%
                    </span>
                  ) : null}
                </div>
              </div>
            </div>

            {/* Current Model Parameters */}
            {job.currentModelParams && Object.keys(job.currentModelParams).length > 0 && (
              <div className="bg-gray-50 dark:bg-gray-900/50 rounded-lg p-3">
                <div className="text-xs text-gray-500 mb-2">Current Model Parameters</div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(job.currentModelParams).map(([key, value]) => (
                    <span key={key} className="bg-gray-200 dark:bg-gray-700 px-2 py-1 rounded text-xs">
                      <span className="text-gray-500">{key}:</span>{' '}
                      <span className="font-mono font-medium">
                        {typeof value === 'number' ? value.toFixed(4) : value}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Generation Progress (individuals in current generation) */}
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="flex items-center space-x-2">
                  <Target size={14} className="text-blue-500" />
                  <span>Generation {(job.currentGeneration || 0) + 1}</span>
                </span>
                <span className="font-mono">
                  Individual {job.currentIndividual || 0}/{job.populationSize || 20}
                </span>
              </div>
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-4">
                <div
                  className="h-4 rounded-full bg-gradient-to-r from-blue-400 to-blue-500 transition-all duration-300 flex items-center justify-center"
                  style={{ width: `${job.populationSize ? ((job.currentIndividual || 0) / job.populationSize) * 100 : 0}%`, minWidth: job.currentIndividual ? '2rem' : 0 }}
                >
                  {job.currentIndividual ? (
                    <span className="text-xs text-white font-medium">
                      {Math.round(((job.currentIndividual || 0) / (job.populationSize || 20)) * 100)}%
                    </span>
                  ) : null}
                </div>
              </div>
            </div>

            {/* Overall Progress (generations) */}
            <div>
              {(() => {
                // Calculate generation-based progress (not including data loading overhead)
                const totalGens = job.totalGenerations || 50;
                const currentGen = job.currentGeneration || 0;
                const currentInd = job.currentIndividual || 0;
                const popSize = job.populationSize || 20;
                const genProgress = ((currentGen + (currentInd / popSize)) / totalGens) * 100;
                return (
                  <>
                    <div className="flex justify-between text-sm mb-1">
                      <span className="flex items-center space-x-2">
                        <Timer size={14} className="text-green-500" />
                        <span>Overall Progress</span>
                      </span>
                      <span className="font-mono">
                        Generation {currentGen + 1}/{totalGens}
                      </span>
                    </div>
                    <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-4">
                      <div
                        className="h-4 rounded-full bg-gradient-to-r from-green-400 to-green-500 transition-all duration-300 flex items-center justify-center"
                        style={{ width: `${genProgress}%`, minWidth: genProgress > 0 ? '2rem' : 0 }}
                      >
                        <span className="text-xs text-white font-medium">
                          {genProgress.toFixed(0)}%
                        </span>
                      </div>
                    </div>
                  </>
                );
              })()}
            </div>
          </div>
        </div>
      )}

      {/* System Resources (for running jobs) */}
      {job.status === 'running' && resources && (
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">System Resources</h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* CPU */}
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="flex items-center space-x-1">
                  <Cpu size={14} />
                  <span>CPU</span>
                </span>
                <span>{resources.cpuPercent.toFixed(1)}%</span>
              </div>
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                <div
                  className="h-3 rounded-full bg-blue-500 transition-all duration-300"
                  style={{ width: `${resources.cpuPercent}%` }}
                />
              </div>
            </div>
            {/* Memory */}
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="flex items-center space-x-1">
                  <MemoryStick size={14} />
                  <span>Memory</span>
                </span>
                <span>{resources.memoryPercent.toFixed(1)}%</span>
              </div>
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                <div
                  className="h-3 rounded-full bg-green-500 transition-all duration-300"
                  style={{ width: `${resources.memoryPercent}%` }}
                />
              </div>
            </div>
            {/* GPU */}
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span className="flex items-center space-x-1">
                  <Zap size={14} />
                  <span>GPU</span>
                </span>
                <span>{resources.gpuUtilization != null ? `${resources.gpuUtilization}%` : 'N/A'}</span>
              </div>
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                <div
                  className="h-3 rounded-full bg-purple-500 transition-all duration-300"
                  style={{ width: `${resources.gpuUtilization || 0}%` }}
                />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Epoch Metrics Chart (for running jobs with epoch history) */}
      {job.epochHistory && job.epochHistory.length > 0 && (() => {
        // Define metric colors and labels
        const metricConfig: Record<string, { color: string; name: string }> = {
          train_loss: { color: '#EF4444', name: 'Train Loss' },
          val_loss: { color: '#F97316', name: 'Val Loss' },
          loss: { color: '#EF4444', name: 'Loss' },
          accuracy: { color: '#22C55E', name: 'Accuracy' },
          val_accuracy: { color: '#10B981', name: 'Val Accuracy' },
          train_accuracy: { color: '#84CC16', name: 'Train Accuracy' },
          f1_score: { color: '#8B5CF6', name: 'F1 Score' },
          precision: { color: '#06B6D4', name: 'Precision' },
          recall: { color: '#EC4899', name: 'Recall' },
        };

        // Find all metric keys present in the data (excluding 'epoch')
        const availableMetrics = new Set<string>();
        job.epochHistory!.forEach(entry => {
          Object.keys(entry).forEach(key => {
            if (key !== 'epoch' && entry[key] !== undefined) {
              availableMetrics.add(key);
            }
          });
        });

        // Generate colors for unknown metrics
        const defaultColors = ['#6366F1', '#14B8A6', '#F59E0B', '#DC2626', '#7C3AED'];
        let colorIndex = 0;

        return (
          <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
            <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">
              Current Model Epoch Metrics
              <span className="text-xs text-gray-500 ml-2">
                ({job.epochHistory!.length} epochs)
              </span>
            </h3>
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={job.epochHistory}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis dataKey="epoch" stroke="#6B7280" fontSize={12} />
                <YAxis stroke="#6B7280" fontSize={12} />
                <Tooltip
                  contentStyle={{ backgroundColor: '#1F2937', border: 'none', borderRadius: '8px' }}
                  labelStyle={{ color: '#9CA3AF' }}
                  formatter={(value) => typeof value === 'number' ? value.toFixed(4) : value}
                />
                <Legend />
                {Array.from(availableMetrics).map(metricKey => {
                  const config = metricConfig[metricKey] || {
                    color: defaultColors[colorIndex++ % defaultColors.length],
                    name: metricKey.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
                  };
                  return (
                    <Line
                      key={metricKey}
                      type="monotone"
                      dataKey={metricKey}
                      stroke={config.color}
                      name={config.name}
                      strokeWidth={2}
                      dot={false}
                    />
                  );
                })}
              </LineChart>
            </ResponsiveContainer>
          </div>
        );
      })()}

      {/* Fitness Chart - Genetic Optimization Progress */}
      {chartData.length > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">
            Genetic Optimization Progress
            <span className="text-xs text-gray-500 ml-2">
              (Optimizing: {(job.optimizeMetric || 'fitness').replace(/_/g, ' ')})
            </span>
          </h3>
          <ResponsiveContainer width="100%" height={250}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="generation" stroke="#6B7280" fontSize={12} label={{ value: 'Generation', position: 'insideBottom', offset: -5, fontSize: 11, fill: '#6B7280' }} />
              <YAxis stroke="#6B7280" fontSize={12} domain={[0, 'auto']} label={{ value: 'Fitness', angle: -90, position: 'insideLeft', fontSize: 11, fill: '#6B7280' }} />
              <Tooltip
                contentStyle={{ backgroundColor: '#1F2937', border: 'none', borderRadius: '8px' }}
                labelStyle={{ color: '#9CA3AF' }}
                formatter={(value) => typeof value === 'number' ? value.toFixed(4) : value}
              />
              <Legend />
              <Line type="monotone" dataKey="best" stroke="#22C55E" name="Best Fitness" strokeWidth={2} dot={{ r: 3 }} />
              <Line type="monotone" dataKey="avg" stroke="#3B82F6" name="Avg Fitness" strokeWidth={2} dot={{ r: 2 }} />
              <Line type="monotone" dataKey="min" stroke="#EF4444" name="Min Fitness" strokeWidth={1} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Best Individual */}
      {individualsData?.best_individual && (
        <div className="bg-white dark:bg-gray-800 rounded-lg p-4 shadow border-2 border-green-500">
          <h3 className="text-sm font-semibold text-green-600 mb-3">Best Individual</h3>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
            <div>
              <span className="text-gray-500">Model:</span>{' '}
              <span className={`px-2 py-0.5 rounded text-xs font-medium ${MODEL_TEXT_COLORS[individualsData.best_individual.model_type] || DEFAULT_MODEL_COLOR}`}>
                {individualsData.best_individual.model_type.toUpperCase()}
              </span>
            </div>
            <div>
              <span className="text-gray-500">Loss:</span>{' '}
              <span className="font-medium">{formatLossFunction(individualsData.best_individual.loss_function)}</span>
            </div>
            <div>
              <span className="text-gray-500">Generation:</span>{' '}
              <span className="font-medium">{individualsData.best_individual.generation + 1}</span>
            </div>
            <div>
              <span className="text-gray-500">Fitness:</span>{' '}
              <span className="font-medium text-green-600">{individualsData.best_individual.fitness.toFixed(4)}</span>
            </div>
            <div>
              <span className="text-gray-500">{(job.optimizeMetric || 'f1_score').replace(/_/g, ' ').toUpperCase()}:</span>{' '}
              <span className="font-medium">
                {(() => {
                  const metric = job.optimizeMetric || 'f1_score';
                  const value = individualsData.best_individual.metrics?.[metric];
                  if (value === undefined || value === null) return '--';
                  return metric === 'mape' ? `${value.toFixed(2)}%` : value.toFixed(4);
                })()}
              </span>
            </div>
          </div>
          <div className="mt-3 text-xs text-gray-500 flex flex-wrap gap-2">
            <strong>Metrics:</strong>
            {Object.entries(individualsData.best_individual.metrics || {}).slice(0, 6).map(([k, v]) => (
              <span key={k} className="bg-blue-100 dark:bg-blue-900/50 px-2 py-0.5 rounded text-blue-700 dark:text-blue-300">
                {k}={typeof v === 'number' ? v.toFixed(4) : v}
              </span>
            ))}
          </div>
          <div className="mt-2 text-xs text-gray-500 flex flex-wrap gap-2">
            <strong>Params:</strong>
            {Object.entries(individualsData.best_individual.params || {}).map(([k, v]) => (
              <span key={k} className="bg-gray-100 dark:bg-gray-700 px-2 py-0.5 rounded">
                {k}={typeof v === 'number' ? v.toFixed(4) : v}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Retrain Results Panel (for completed retrain jobs) */}
      {job.status === 'completed' && job.isRetrain && eliteModels.length > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow border-2 border-green-500">
          <div className="p-4 border-b border-gray-200 dark:border-gray-700">
            <div className="flex items-center space-x-2">
              <RefreshCw size={20} className="text-green-500" />
              <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-300">
                Retrain Results
              </h3>
            </div>
            <p className="text-sm text-gray-500 mt-1">
              Model retrained with mode: <span className="font-medium">{job.retrainMode === 'load_weights' ? 'Continue Training' : 'From Scratch'}</span>
            </p>
          </div>
          <div className="p-4 space-y-4">
            {/* Best result summary */}
            <div className="p-3 rounded-lg bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800">
              <div className="flex items-center justify-between">
                <div>
                  <span className="font-medium">{eliteModels[0]?.model_type?.toUpperCase()}</span>
                  <span className="ml-3 text-green-600 font-bold">Fitness: {eliteModels[0]?.fitness?.toFixed(4)}</span>
                </div>
                <div className="flex items-center space-x-2 text-sm text-gray-500">
                  {eliteModels[0]?.metrics?.f1_score !== undefined && (
                    <span>F1: {eliteModels[0].metrics.f1_score.toFixed(4)}</span>
                  )}
                  {eliteModels[0]?.metrics?.accuracy !== undefined && (
                    <span>Acc: {(eliteModels[0].metrics.accuracy * 100).toFixed(1)}%</span>
                  )}
                </div>
              </div>
            </div>

            {/* Save options */}
            <div className="border-t border-gray-200 dark:border-gray-700 pt-4">
              <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Save Results</h4>
              <div className="grid grid-cols-2 gap-4">
                {/* Update Original */}
                <div className="p-4 border border-gray-200 dark:border-gray-600 rounded-lg">
                  <h5 className="font-medium mb-2">Update Original Model</h5>
                  <p className="text-xs text-gray-500 mb-3">
                    Replace the original model's weights and metrics with these new results.
                  </p>
                  <button
                    onClick={() => handleSaveRetrainResult('update_original')}
                    disabled={savingRetrainResult}
                    className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50"
                  >
                    {savingRetrainResult ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
                    Update Original
                  </button>
                </div>

                {/* Save as New */}
                <div className="p-4 border border-gray-200 dark:border-gray-600 rounded-lg">
                  <h5 className="font-medium mb-2">Save as New Model</h5>
                  <input
                    type="text"
                    placeholder="New model name (optional)"
                    value={newModelName}
                    onChange={(e) => setNewModelName(e.target.value)}
                    className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 mb-2"
                  />
                  <button
                    onClick={() => handleSaveRetrainResult('new')}
                    disabled={savingRetrainResult}
                    className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-green-500 text-white rounded-lg hover:bg-green-600 disabled:opacity-50"
                  >
                    {savingRetrainResult ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
                    Save as New
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Elite Models Panel (shows during training and after completion for non-retrain jobs) */}
      {!job.isRetrain && eliteModels.length > 0 && (
        <div className={`bg-white dark:bg-gray-800 rounded-lg shadow border-2 ${
          job.status === 'completed' ? 'border-yellow-500' : 'border-blue-400'
        }`}>
          <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <Trophy size={20} className={job.status === 'completed' ? 'text-yellow-500' : 'text-blue-400'} />
              <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-300">
                {job.status === 'completed' ? 'Best Trained Models' : 'Current Elite Models'} ({eliteModels.length})
              </h3>
              {job.status === 'running' && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 animate-pulse">
                  Live
                </span>
              )}
            </div>
            <span className="text-xs text-gray-500">
              {job.status === 'completed'
                ? 'Save models to inventory for use in predictions'
                : 'Save promising models early for testing'}
            </span>
          </div>
          <div className="p-4 space-y-3">
            {eliteModels.map((model) => (
              <div
                key={model.rank}
                className={`p-3 rounded-lg border ${
                  model.rank === 1
                    ? 'border-yellow-400 bg-yellow-50 dark:bg-yellow-900/20'
                    : model.rank === 2
                    ? 'border-gray-300 bg-gray-50 dark:bg-gray-700/30'
                    : model.rank === 3
                    ? 'border-orange-300 bg-orange-50 dark:bg-orange-900/20'
                    : 'border-gray-200 dark:border-gray-700'
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-4">
                    <div className="flex items-center space-x-2">
                      {model.rank === 1 ? (
                        <Award size={24} className="text-yellow-500" />
                      ) : model.rank === 2 ? (
                        <Award size={20} className="text-gray-400" />
                      ) : model.rank === 3 ? (
                        <Award size={20} className="text-orange-400" />
                      ) : (
                        <span className="w-6 text-center font-bold text-gray-500">#{model.rank}</span>
                      )}
                    </div>
                    <span className={`px-2 py-1 rounded text-xs font-medium ${MODEL_TEXT_COLORS[model.model_type] || DEFAULT_MODEL_COLOR}`}>
                      {model.model_type.toUpperCase()}
                    </span>
                    <span className="text-sm">
                      <span className="text-gray-500">Fitness:</span>{' '}
                      <span className="font-bold text-green-600">{model.fitness.toFixed(4)}</span>
                    </span>
                    {model.generation !== undefined && (
                      <span className="text-xs px-2 py-0.5 rounded bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400">
                        Gen {model.generation}
                      </span>
                    )}
                    {/* Key metrics */}
                    <div className="flex items-center space-x-2 text-xs text-gray-500">
                      {model.metrics.f1_score !== undefined && (
                        <span>F1: {model.metrics.f1_score.toFixed(4)}</span>
                      )}
                      {model.metrics.accuracy !== undefined && (
                        <span>Acc: {(model.metrics.accuracy * 100).toFixed(1)}%</span>
                      )}
                      {model.metrics.precision !== undefined && (
                        <span>Prec: {model.metrics.precision.toFixed(4)}</span>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={() => handleSaveToInventory(model.rank, model.model_type)}
                    disabled={savingModel === model.rank}
                    className={`flex items-center space-x-1 px-3 py-1.5 text-white rounded-lg disabled:opacity-50 disabled:cursor-not-allowed text-sm ${
                      job.status === 'running'
                        ? 'bg-orange-500 hover:bg-orange-600'
                        : 'bg-blue-500 hover:bg-blue-600'
                    }`}
                    title={job.status === 'running' ? 'Save current best - training still in progress' : 'Save to model inventory'}
                  >
                    {savingModel === model.rank ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : (
                      <Save size={14} />
                    )}
                    <span>{job.status === 'running' ? 'Save Early' : 'Save to Inventory'}</span>
                  </button>
                </div>
                {/* Parameters */}
                <div className="mt-2 flex flex-wrap gap-1 text-xs">
                  {Object.entries(model.params).slice(0, 6).map(([k, v]) => (
                    <span key={k} className="bg-gray-100 dark:bg-gray-700 px-2 py-0.5 rounded">
                      {k}={typeof v === 'number' ? v.toFixed(4) : v}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Generations Table */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Generations ({generationsData?.total_generations || 0})
          </h3>
        </div>
        <div className="max-h-96 overflow-y-auto">
          {generationsData?.generations.map((gen) => {
            const isExpanded = expandedGenerations.has(gen.generation);
            const genIndividuals = individualsData?.individuals.filter(i => i.generation === gen.generation) || [];

            return (
              <div key={gen.generation} className="border-b border-gray-200 dark:border-gray-700 last:border-b-0">
                <button
                  onClick={() => toggleGeneration(gen.generation)}
                  className="w-full px-4 py-3 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
                >
                  <div className="flex items-center space-x-4">
                    {isExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                    <span className="font-medium">Gen {gen.generation + 1}</span>
                    <span className="text-sm text-gray-500">
                      Best: <span className="text-green-600 font-medium">{gen.best_fitness.toFixed(4)}</span>
                    </span>
                    <span className="text-sm text-gray-500">
                      Avg: {gen.avg_fitness.toFixed(4)}
                    </span>
                  </div>
                  <div className="flex items-center space-x-2">
                    {Object.entries(gen.model_types).map(([type, count]) => (
                      <span key={type} className={`px-2 py-0.5 rounded text-xs text-white ${MODEL_COLORS[type] || 'bg-gray-500'}`}>
                        {type.toUpperCase()}: {count}
                      </span>
                    ))}
                    <span className="text-sm text-gray-500">{gen.individual_count} ind.</span>
                  </div>
                </button>

                {isExpanded && genIndividuals.length > 0 && (
                  <div className="bg-gray-50 dark:bg-gray-900/50 px-4 py-2">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-gray-500 text-xs">
                          <th className="text-left py-1 px-2">#</th>
                          <th className="text-left py-1 px-2">Model</th>
                          <th className="text-left py-1 px-2">Loss</th>
                          <th className="text-left py-1 px-2">Fitness</th>
                          <th className="text-left py-1 px-2">{(job.optimizeMetric || 'f1_score').replace(/_/g, ' ')}</th>
                          <th className="text-left py-1 px-2">Other Metrics</th>
                          <th className="text-right py-1 px-2">Details</th>
                        </tr>
                      </thead>
                      <tbody>
                        {genIndividuals
                          .sort((a, b) => b.fitness - a.fitness)
                          .map((ind, idx) => (
                            <tr key={idx} className={`border-t border-gray-200 dark:border-gray-700 ${idx === 0 ? 'bg-green-50 dark:bg-green-900/20' : ''}`}>
                              <td className="py-2 px-2">{ind.individual}</td>
                              <td className="py-2 px-2">
                                <span className={`px-2 py-0.5 rounded text-xs font-medium ${MODEL_TEXT_COLORS[ind.model_type] || DEFAULT_MODEL_COLOR}`}>
                                  {ind.model_type.toUpperCase()}
                                </span>
                              </td>
                              <td className="py-2 px-2 text-xs text-gray-600 dark:text-gray-400">
                                {formatLossFunction(ind.loss_function)}
                              </td>
                              <td className="py-2 px-2 font-medium">{ind.fitness.toFixed(4)}</td>
                              <td className="py-2 px-2">
                                {(() => {
                                  const metric = job.optimizeMetric || 'f1_score';
                                  const value = ind.metrics?.[metric];
                                  if (value === undefined || value === null) return '--';
                                  return metric === 'mape' ? `${value.toFixed(2)}%` : value.toFixed(4);
                                })()}
                              </td>
                              <td className="py-2 px-2 text-xs text-gray-500">
                                {Object.entries(ind.metrics || {})
                                  .filter(([k]) => k !== (job.optimizeMetric || 'f1_score'))
                                  .slice(0, 3)
                                  .map(([k, v]) => `${k}=${typeof v === 'number' ? v.toFixed(2) : v}`)
                                  .join(', ')}
                              </td>
                              <td className="py-2 px-2 text-right">
                                <button
                                  onClick={() => setSelectedIndividual(ind)}
                                  className="p-1 hover:bg-gray-200 dark:hover:bg-gray-600 rounded"
                                  title="View details"
                                >
                                  <Info size={14} />
                                </button>
                              </td>
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

      {/* Training Logs */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow">
        <button
          onClick={() => setShowLogs(!showLogs)}
          className="w-full p-4 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
        >
          <div className="flex items-center space-x-2">
            <FileText size={16} className="text-gray-500" />
            <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">Training Logs</h3>
          </div>
          {showLogs ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>
        {showLogs && (
          <div className="px-4 pb-4">
            <div className="bg-gray-900 rounded-md p-4 max-h-48 overflow-y-auto font-mono text-sm">
              {logs.length > 0 ? (
                logs.map((log, idx) => (
                  <div key={idx} className="text-green-400">{log}</div>
                ))
              ) : (
                <div className="text-gray-500">No logs yet...</div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Individual Detail Modal */}
      {selectedIndividual && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => setSelectedIndividual(null)}>
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
              <h3 className="text-lg font-semibold">
                Individual Details - {selectedIndividual.model_type.toUpperCase()}
              </h3>
              <button onClick={() => setSelectedIndividual(null)} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded">
                <XCircle size={20} />
              </button>
            </div>
            <div className="p-4 space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                <div>
                  <span className="text-sm text-gray-500">Generation</span>
                  <div className="font-medium">{selectedIndividual.generation + 1}</div>
                </div>
                <div>
                  <span className="text-sm text-gray-500">Individual #</span>
                  <div className="font-medium">{selectedIndividual.individual}</div>
                </div>
                <div>
                  <span className="text-sm text-gray-500">Loss Function</span>
                  <div className="font-medium">{formatLossFunction(selectedIndividual.loss_function)}</div>
                </div>
                <div>
                  <span className="text-sm text-gray-500">Fitness</span>
                  <div className="font-medium text-green-600">{selectedIndividual.fitness.toFixed(4)}</div>
                </div>
                <div>
                  <span className="text-sm text-gray-500">MAPE</span>
                  <div className="font-medium">{(selectedIndividual.metrics?.mape || 0).toFixed(2)}%</div>
                </div>
              </div>

              <div>
                <span className="text-sm text-gray-500 block mb-2">Parameters</span>
                <div className="bg-gray-50 dark:bg-gray-700 rounded p-3 text-sm">
                  {Object.entries(selectedIndividual.params || {}).map(([k, v]) => (
                    <div key={k} className="flex justify-between py-1 border-b border-gray-200 dark:border-gray-600 last:border-b-0">
                      <span className="text-gray-600 dark:text-gray-400">{k}</span>
                      <span className="font-mono">{typeof v === 'number' ? v.toFixed(6) : v}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div>
                <span className="text-sm text-gray-500 block mb-2">Metrics</span>
                <div className="bg-gray-50 dark:bg-gray-700 rounded p-3 text-sm">
                  {Object.entries(selectedIndividual.metrics || {}).map(([k, v]) => (
                    <div key={k} className="flex justify-between py-1 border-b border-gray-200 dark:border-gray-600 last:border-b-0">
                      <span className="text-gray-600 dark:text-gray-400">{k}</span>
                      <span className="font-mono">{typeof v === 'number' ? v.toFixed(4) : v}</span>
                    </div>
                  ))}
                </div>
              </div>

              {selectedIndividual.training_history && selectedIndividual.training_history.length > 0 && (
                <div>
                  <span className="text-sm text-gray-500 block mb-2">Training History</span>
                  <ResponsiveContainer width="100%" height={200}>
                    <LineChart data={selectedIndividual.training_history}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                      <XAxis dataKey="epoch" stroke="#6B7280" fontSize={12} />
                      <YAxis stroke="#6B7280" fontSize={12} />
                      <Tooltip />
                      <Legend />
                      <Line type="monotone" dataKey="loss" stroke="#EF4444" name="Loss" />
                      <Line type="monotone" dataKey="val_loss" stroke="#F97316" name="Val Loss" />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
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
      />
    </div>
  );
};

export default JobDetails;
