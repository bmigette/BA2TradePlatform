import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  Brain,
  Cpu,
  Download,
  Trash2,
  Copy,
  Loader2,
  AlertCircle,
  CheckCircle,
  TrendingUp,
  Target,
  Activity,
  BarChart3,
  Layers,
  Zap,
  RefreshCw,
  X,
  Database,
  Eye,
  Play
} from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import ConfirmDialog from '../components/ConfirmDialog';
import PredictionsChart from '../components/PredictionsChart';

interface HyperParameters {
  layers: number;
  layerSize: number;
  learningRate: number;
  dropout: number;
  batchSize: number;
  epochs: number;
}

interface TrainingHistory {
  epoch: number;
  loss: number;
  accuracy: number;
  valLoss: number;
  valAccuracy: number;
}

interface PerformanceMetrics {
  accuracy: number;
  precision: number;
  recall: number;
  f1Score: number;
  auc: number;
}

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
  hyperparameters: HyperParameters;
  trainingHistory: TrainingHistory[];
  performanceMetrics: PerformanceMetrics;
  confusionMatrix?: number[][];
  allMetrics?: Record<string, number>;
  predictionTargets?: Array<{
    type: string;
    category: string;
    [key: string]: unknown;
  }>;
  predictionHorizon?: number;
  createdAt: string;
  trainedAt: string | null;
  filePath: string | null;
  fileSize: number | null;
  generations: number;
  bestGeneration: number;
  fitness: number;
}

interface ConfusionMatrix {
  labels: string[];
  matrix: number[][];
  metrics: {
    accuracy: number;
    precision: number;
    recall: number;
    specificity: number;
  };
}

interface PredictionResult {
  date: string;
  close: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  actual: number | null;
  probability: number;
  predictedClass: number;
  correct: boolean | null;
}

interface AvailableTarget {
  column: string;
  label: string;
  type: string;
}

interface PredictionsResponse {
  modelId: string;
  datasetId: number;
  targetColumn: string;
  targetIndex: number;
  availableTargets: AvailableTarget[];
  predictionHorizon: number;
  predictionMode: string;
  threshold: number;
  summary: {
    totalPredictions: number;
    accuracy: number;
    avgProbability: number;
    predictedClass0: number;
    predictedClass1: number;
    actualClass0: number;
    actualClass1: number;
  };
  predictions: PredictionResult[];
}

const API_BASE = 'http://localhost:8000/api';

const ModelDetails: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [model, setModel] = useState<Model | null>(null);
  const [confusionMatrix, setConfusionMatrix] = useState<ConfusionMatrix | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'overview' | 'training' | 'confusion' | 'predictions'>('overview');
  const [exporting, setExporting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [showRetrainDialog, setShowRetrainDialog] = useState(false);
  const [retrainConfig, setRetrainConfig] = useState({
    datasetId: null as number | null,
    retrainMode: 'from_scratch' as 'load_weights' | 'from_scratch',
    epochs: 10,
    useCustomDateRange: false,
    startDate: '',
    endDate: ''
  });
  const [datasets, setDatasets] = useState<Array<{id: number, name: string, ticker: string, start_date: string, end_date: string}>>([]);
  const [submittingRetrain, setSubmittingRetrain] = useState(false);
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });
  const [predictionsData, setPredictionsData] = useState<PredictionsResponse | null>(null);
  const [predictionsLoading, setPredictionsLoading] = useState(false);
  const [predictionsError, setPredictionsError] = useState<string | null>(null);
  const [selectedTargetIndex, setSelectedTargetIndex] = useState(0); // Which target to show
  const [minProbability, setMinProbability] = useState<number | null>(null); // null = use model threshold, 0-100 for slider
  const [showActualTargets, setShowActualTargets] = useState(true); // Show ground truth markers
  const [showAllActualTargets, setShowAllActualTargets] = useState(true); // Show all vs transitions only

  // Get effective min probability (from slider or model threshold)
  const effectiveMinProbability = minProbability !== null
    ? minProbability
    : (predictionsData?.threshold ?? 0.5) * 100;

  useEffect(() => {
    fetchModelDetails();
    fetchDatasets();
  }, [id]);

  const fetchDatasets = async () => {
    try {
      const res = await fetch(`${API_BASE}/datasets`);
      if (res.ok) {
        const data = await res.json();
        setDatasets((data.datasets || []).slice().sort((a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name)));
      }
    } catch (err) {
      console.error('Failed to fetch datasets:', err);
    }
  };

  const handleRetrain = async () => {
    if (!model) return;

    setSubmittingRetrain(true);
    try {
      const retrainPayload: any = {
        sourceModelId: model.id,
        retrainMode: retrainConfig.retrainMode,
        epochs: retrainConfig.epochs
      };

      if (retrainConfig.datasetId) {
        retrainPayload.datasetId = retrainConfig.datasetId;
      }

      if (retrainConfig.useCustomDateRange && retrainConfig.startDate && retrainConfig.endDate) {
        retrainPayload.trainingDateRange = {
          startDate: retrainConfig.startDate,
          endDate: retrainConfig.endDate
        };
      }

      const res = await fetch(`${API_BASE}/jobs/retrain`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(retrainPayload)
      });

      if (res.ok) {
        const job = await res.json();
        setShowRetrainDialog(false);
        navigate(`/training/${job.id}`);
      } else {
        const error = await res.json();
        alert(`Retrain failed: ${error.detail || 'Unknown error'}`);
      }
    } catch (err) {
      console.error('Retrain error:', err);
      alert('Failed to create retrain job');
    } finally {
      setSubmittingRetrain(false);
    }
  };

  const fetchModelDetails = async () => {
    try {
      setLoading(true);

      // Fetch model details
      const modelRes = await fetch(`${API_BASE}/models/${id}`);
      if (!modelRes.ok) throw new Error('Model not found');
      const modelData = await modelRes.json();
      setModel(modelData);

      // Fetch confusion matrix
      const cmRes = await fetch(`${API_BASE}/models/${id}/confusion-matrix`);
      if (cmRes.ok) {
        const cmData = await cmRes.json();
        setConfusionMatrix(cmData);
      }

      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load model');
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async (format: string) => {
    setExporting(true);
    try {
      const res = await fetch(`${API_BASE}/models/${id}/export?format=${format}`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        alert(`Model exported: ${data.path}`);
      }
    } finally {
      setExporting(false);
    }
  };

  const handleDelete = () => {
    setConfirmDialog({
      isOpen: true,
      title: 'Delete Model',
      message: 'Are you sure you want to delete this model?',
      variant: 'danger',
      onConfirm: async () => {
        setDeleting(true);
        try {
          const res = await fetch(`${API_BASE}/models/${id}`, { method: 'DELETE' });
          if (res.ok) {
            navigate('/models');
          }
        } finally {
          setDeleting(false);
        }
      },
    });
  };

  const handleClone = async () => {
    try {
      const res = await fetch(`${API_BASE}/models/${id}/clone`, { method: 'POST' });
      if (res.ok) {
        const cloned = await res.json();
        navigate(`/models/${cloned.id}`);
      }
    } catch (err) {
      alert('Failed to clone model');
    }
  };

  const handleRunPredictions = async (targetIndex: number = 0) => {
    if (!model) return;

    setPredictionsLoading(true);
    setPredictionsError(null);
    try {
      const res = await fetch(`${API_BASE}/models/${id}/run-predictions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_index: targetIndex })
      });

      if (res.ok) {
        const data = await res.json();
        setPredictionsData(data);
        setSelectedTargetIndex(data.targetIndex ?? 0);
      } else {
        const error = await res.json();
        setPredictionsError(error.detail || 'Failed to run predictions');
      }
    } catch (err) {
      console.error('Predictions error:', err);
      setPredictionsError('Failed to run predictions');
    } finally {
      setPredictionsLoading(false);
    }
  };

  const formatBytes = (bytes: number | null) => {
    if (!bytes) return 'N/A';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  if (loading) {
    return (
      <div className="p-6 flex items-center justify-center min-h-96">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  if (error || !model) {
    return (
      <div className="p-6">
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
            <AlertCircle className="w-5 h-5" />
            <span>{error || 'Model not found'}</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/models')}
            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg"
          >
            <ArrowLeft className="w-5 h-5" />
          </button>
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Brain className="w-6 h-6 text-purple-500" />
              {model.name}
            </h1>
            <p className="text-sm text-gray-500">
              {model.modelType} Model - {model.datasetName || `Dataset #${model.datasetId}`}
              {model.symbol && (
                <span className="ml-2 text-blue-500">
                  {model.symbol} {model.timeframe && `• ${model.timeframe}`}
                </span>
              )}
            </p>
            {model.trainPeriod && (
              <p className="text-xs text-gray-400">{model.trainPeriod}</p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              setRetrainConfig(prev => ({ ...prev, datasetId: model?.datasetId || null }));
              setShowRetrainDialog(true);
            }}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-green-500 text-white hover:bg-green-600 rounded-lg"
          >
            <RefreshCw className="w-4 h-4" />
            Retrain
          </button>
          <button
            onClick={handleClone}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg"
          >
            <Copy className="w-4 h-4" />
            Clone
          </button>
          <div className="relative group">
            <button
              disabled={exporting}
              className="flex items-center gap-2 px-3 py-2 text-sm bg-blue-500 text-white hover:bg-blue-600 rounded-lg disabled:opacity-50"
            >
              {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
              Export
            </button>
            <div className="absolute right-0 mt-1 w-40 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-10">
              <button
                onClick={() => handleExport('pytorch')}
                className="block w-full text-left px-4 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                PyTorch (.pt)
              </button>
              <button
                onClick={() => handleExport('onnx')}
                className="block w-full text-left px-4 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                ONNX (.onnx)
              </button>
              <button
                onClick={() => handleExport('tensorflow')}
                className="block w-full text-left px-4 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                TensorFlow (.h5)
              </button>
            </div>
          </div>
          <button
            onClick={handleDelete}
            disabled={deleting}
            className="flex items-center gap-2 px-3 py-2 text-sm bg-red-500 text-white hover:bg-red-600 rounded-lg disabled:opacity-50"
          >
            {deleting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
            Delete
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 dark:border-gray-700">
        <nav className="flex gap-4">
          {[
            { id: 'overview', label: 'Overview', icon: Activity },
            { id: 'training', label: 'Training History', icon: TrendingUp },
            { id: 'confusion', label: 'Confusion Matrix', icon: BarChart3 },
            { id: 'predictions', label: 'View Predictions', icon: Eye }
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as any)}
              className={`flex items-center gap-2 px-4 py-3 border-b-2 transition-colors ${
                activeTab === tab.id
                  ? 'border-blue-500 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              <tab.icon className="w-4 h-4" />
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'overview' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Performance Metrics */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Target className="w-5 h-5 text-green-500" />
              Performance Metrics
            </h3>
            <div className="grid grid-cols-2 gap-4">
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Accuracy</p>
                <p className="text-2xl font-bold text-green-600">{((model.performanceMetrics?.accuracy || 0) * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Precision</p>
                <p className="text-2xl font-bold text-blue-600">{((model.performanceMetrics?.precision || 0) * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Recall</p>
                <p className="text-2xl font-bold text-purple-600">{((model.performanceMetrics?.recall || 0) * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">F1 Score</p>
                <p className="text-2xl font-bold text-orange-600">{((model.performanceMetrics?.f1Score || 0) * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">AUC</p>
                <p className="text-2xl font-bold text-indigo-600">{((model.performanceMetrics?.auc || 0) * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Fitness</p>
                <p className="text-2xl font-bold text-amber-500 dark:text-amber-400">{(model.fitness || 0).toFixed(5)}</p>
              </div>
            </div>
          </div>

          {/* Hyperparameters */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Layers className="w-5 h-5 text-blue-500" />
              Hyperparameters
            </h3>
            <table className="w-full">
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                <tr>
                  <td className="py-2 text-sm text-gray-500">Layers</td>
                  <td className="py-2 text-sm font-medium text-right">{model.hyperparameters?.layers ?? 'N/A'}</td>
                </tr>
                <tr>
                  <td className="py-2 text-sm text-gray-500">Layer Size</td>
                  <td className="py-2 text-sm font-medium text-right">{model.hyperparameters?.layerSize ? `${model.hyperparameters.layerSize} neurons` : 'N/A'}</td>
                </tr>
                <tr>
                  <td className="py-2 text-sm text-gray-500">Learning Rate</td>
                  <td className="py-2 text-sm font-medium text-right">{model.hyperparameters?.learningRate ?? 'N/A'}</td>
                </tr>
                <tr>
                  <td className="py-2 text-sm text-gray-500">Dropout</td>
                  <td className="py-2 text-sm font-medium text-right">{model.hyperparameters?.dropout != null ? `${(model.hyperparameters.dropout * 100).toFixed(0)}%` : 'N/A'}</td>
                </tr>
                <tr>
                  <td className="py-2 text-sm text-gray-500">Batch Size</td>
                  <td className="py-2 text-sm font-medium text-right">{model.hyperparameters?.batchSize ?? 'N/A'}</td>
                </tr>
                <tr>
                  <td className="py-2 text-sm text-gray-500">Epochs</td>
                  <td className="py-2 text-sm font-medium text-right">{model.hyperparameters?.epochs ?? 'N/A'}</td>
                </tr>
              </tbody>
            </table>
          </div>

          {/* Architecture Diagram */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Cpu className="w-5 h-5 text-purple-500" />
              Architecture
            </h3>
            <div className="flex items-center justify-center gap-2 py-8">
              {/* Input Layer */}
              <div className="flex flex-col items-center">
                <div className="w-16 h-24 bg-blue-100 dark:bg-blue-900 border-2 border-blue-500 rounded-lg flex items-center justify-center">
                  <span className="text-xs font-medium text-blue-700 dark:text-blue-300">Input</span>
                </div>
                <span className="text-xs mt-1 text-gray-500">Features</span>
              </div>

              <div className="text-gray-400">→</div>

              {/* Hidden Layers */}
              {Array.from({ length: model.hyperparameters?.layers || 2 }).map((_, i) => (
                <React.Fragment key={i}>
                  <div className="flex flex-col items-center">
                    <div className="w-16 h-24 bg-purple-100 dark:bg-purple-900 border-2 border-purple-500 rounded-lg flex flex-col items-center justify-center">
                      <span className="text-xs font-medium text-purple-700 dark:text-purple-300">{model.modelType}</span>
                      <span className="text-xs text-purple-600 dark:text-purple-400">{model.hyperparameters?.layerSize ?? '?'}</span>
                    </div>
                    <span className="text-xs mt-1 text-gray-500">Layer {i + 1}</span>
                  </div>
                  <div className="text-gray-400">→</div>
                </React.Fragment>
              ))}

              {/* Output Layer */}
              <div className="flex flex-col items-center">
                <div className="w-16 h-24 bg-green-100 dark:bg-green-900 border-2 border-green-500 rounded-lg flex items-center justify-center">
                  <span className="text-xs font-medium text-green-700 dark:text-green-300">Output</span>
                </div>
                <span className="text-xs mt-1 text-gray-500">Prediction</span>
              </div>
            </div>
          </div>

          {/* Model Info */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
              <Zap className="w-5 h-5 text-yellow-500" />
              Model Info
            </h3>
            <div className="space-y-3">
              <div className="flex justify-between">
                <span className="text-sm text-gray-500">Status</span>
                <span className={`flex items-center gap-1 text-sm font-medium ${
                  model.status === 'trained' ? 'text-green-600' : 'text-gray-600'
                }`}>
                  {model.status === 'trained' && <CheckCircle className="w-4 h-4" />}
                  {model.status}
                </span>
              </div>
              {model.symbol && (
                <div className="flex justify-between">
                  <span className="text-sm text-gray-500">Symbol</span>
                  <span className="text-sm font-medium">{model.symbol}</span>
                </div>
              )}
              {model.timeframe && (
                <div className="flex justify-between">
                  <span className="text-sm text-gray-500">Timeframe</span>
                  <span className="text-sm font-medium">{model.timeframe}</span>
                </div>
              )}
              {model.trainPeriod && (
                <div className="flex justify-between">
                  <span className="text-sm text-gray-500">Training Period</span>
                  <span className="text-sm font-medium">{model.trainPeriod}</span>
                </div>
              )}
              <div className="flex justify-between">
                <span className="text-sm text-gray-500">Created</span>
                <span className="text-sm font-medium">{new Date(model.createdAt).toLocaleString()}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-sm text-gray-500">Trained</span>
                <span className="text-sm font-medium">{model.trainedAt ? new Date(model.trainedAt).toLocaleString() : 'N/A'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-sm text-gray-500">Generations</span>
                <span className="text-sm font-medium">{model.generations ?? 'N/A'} (best: #{model.bestGeneration ?? 'N/A'})</span>
              </div>
              <div className="flex justify-between">
                <span className="text-sm text-gray-500">File Size</span>
                <span className="text-sm font-medium">{formatBytes(model.fileSize)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-sm text-gray-500">Job ID</span>
                <button
                  onClick={() => navigate(`/training/${model.jobId}`)}
                  className="text-sm font-mono text-blue-600 hover:text-blue-800 hover:underline"
                >
                  {model.jobId}
                </button>
              </div>
            </div>
          </div>

          {/* Prediction Targets */}
          {model.predictionTargets && model.predictionTargets.length > 0 && (
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
              <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
                <Target className="w-5 h-5 text-purple-500" />
                Prediction Targets
              </h3>
              {model.predictionHorizon && (
                <div className="mb-4 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg">
                  <div className="text-sm">
                    <span className="text-gray-600 dark:text-gray-400">Prediction Horizon: </span>
                    <span className="font-medium text-blue-700 dark:text-blue-300">
                      {model.predictionHorizon} bar(s) ahead
                    </span>
                  </div>
                </div>
              )}
              <div className="space-y-3">
                {model.predictionTargets?.map((target, index) => {
                  // Infer type and category for legacy targets
                  const inferredType = target?.type ||
                    (('profitPct' in target || 'maxDd' in target) ? 'price_based' :
                    ('indicator' in target) ? 'trend_reversal' :
                    ('horizon' in target && 'direction' in target) ? 'directional' : 'legacy');
                  const inferredCategory = target?.category ||
                    (inferredType === 'price_based' || inferredType === 'directional' || inferredType === 'trend_reversal' ? 'binary_classification' :
                    inferredType === 'triple_barrier' ? 'multiclass_classification' : 'binary_classification');

                  return (
                  <div key={index} className="p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
                    <div className="flex items-center justify-between mb-2">
                      <span className="font-medium text-sm capitalize">
                        {(inferredType as string).replace(/_/g, ' ')}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded ${
                        inferredCategory === 'binary_classification' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300' :
                        inferredCategory === 'multiclass_classification' ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300' :
                        inferredCategory === 'regression' ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300' :
                        'bg-gray-200 text-gray-700 dark:bg-gray-600 dark:text-gray-200'
                      }`}>
                        {(inferredCategory as string).replace(/_/g, ' ')}
                      </span>
                    </div>
                    <div className="text-xs text-gray-500 dark:text-gray-400 grid grid-cols-2 gap-2">
                      {Object.entries(target).filter(([k]) => !['type', 'category', 'enabled', 'color'].includes(k)).map(([key, value]) => (
                        <div key={key}>
                          <span className="capitalize">{key.replace(/([A-Z])/g, ' $1').trim()}: </span>
                          <span className="font-medium">
                            {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {activeTab === 'training' && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <h3 className="text-lg font-semibold mb-4">Training History</h3>
          <div className="h-96">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={model.trainingHistory}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="epoch" label={{ value: 'Epoch', position: 'bottom' }} />
                <YAxis yAxisId="loss" label={{ value: 'Loss', angle: -90, position: 'left' }} />
                <YAxis yAxisId="accuracy" orientation="right" label={{ value: 'Accuracy', angle: 90, position: 'right' }} />
                <Tooltip />
                <Legend />
                <Line yAxisId="loss" type="monotone" dataKey="loss" stroke="#ef4444" name="Train Loss" />
                <Line yAxisId="loss" type="monotone" dataKey="valLoss" stroke="#f97316" name="Val Loss" strokeDasharray="5 5" />
                <Line yAxisId="accuracy" type="monotone" dataKey="accuracy" stroke="#22c55e" name="Train Accuracy" />
                <Line yAxisId="accuracy" type="monotone" dataKey="valAccuracy" stroke="#3b82f6" name="Val Accuracy" strokeDasharray="5 5" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {activeTab === 'confusion' && confusionMatrix && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <h3 className="text-lg font-semibold mb-4">Confusion Matrix</h3>
          <div className="flex gap-8">
            {/* Matrix */}
            <div className="flex-1">
              <div className="flex items-center justify-center">
                <table className="border-collapse">
                  <thead>
                    <tr>
                      <th className="p-2"></th>
                      <th className="p-2"></th>
                      <th colSpan={2} className="p-2 text-center text-sm font-medium text-gray-600">Predicted</th>
                    </tr>
                    <tr>
                      <th className="p-2"></th>
                      <th className="p-2"></th>
                      {confusionMatrix.labels.map(label => (
                        <th key={label} className="p-2 text-sm font-medium text-gray-600 w-24 text-center">{label}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {confusionMatrix.matrix.map((row, i) => (
                      <tr key={i}>
                        {i === 0 && (
                          <th rowSpan={2} className="p-2 text-sm font-medium text-gray-600 writing-mode-vertical" style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}>
                            Actual
                          </th>
                        )}
                        <th className="p-2 text-sm font-medium text-gray-600">{confusionMatrix.labels[i]}</th>
                        {row.map((val, j) => (
                          <td
                            key={j}
                            className={`p-4 text-center text-lg font-bold w-24 h-24 ${
                              i === j ? 'bg-green-100 dark:bg-green-900 text-green-700 dark:text-green-300' :
                              'bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300'
                            }`}
                          >
                            {val}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Metrics */}
            <div className="w-64 space-y-3">
              <h4 className="font-medium">Classification Metrics</h4>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Accuracy</p>
                <p className="text-xl font-bold">{(confusionMatrix.metrics.accuracy * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Precision</p>
                <p className="text-xl font-bold">{(confusionMatrix.metrics.precision * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Recall (Sensitivity)</p>
                <p className="text-xl font-bold">{(confusionMatrix.metrics.recall * 100).toFixed(1)}%</p>
              </div>
              <div className="p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-sm text-gray-500">Specificity</p>
                <p className="text-xl font-bold">{(confusionMatrix.metrics.specificity * 100).toFixed(1)}%</p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Predictions Tab */}
      {activeTab === 'predictions' && (
        <div className="space-y-6">
          {/* Run Predictions Button */}
          {!predictionsData && !predictionsLoading && (
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-8 text-center">
              <Eye className="w-16 h-16 mx-auto text-gray-400 mb-4" />
              <h3 className="text-lg font-semibold mb-2">View Model Predictions</h3>
              <p className="text-gray-500 dark:text-gray-400 mb-6">
                Run predictions on the training dataset to see how the model performs on each data point.
              </p>
              <button
                onClick={() => handleRunPredictions(0)}
                className="flex items-center gap-2 px-6 py-3 bg-blue-500 text-white rounded-lg hover:bg-blue-600 mx-auto"
              >
                <Play className="w-5 h-5" />
                Run Predictions
              </button>
              {predictionsError && (
                <div className="mt-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                  <p className="text-red-600 dark:text-red-400 text-sm">{predictionsError}</p>
                </div>
              )}
            </div>
          )}

          {/* Loading State */}
          {predictionsLoading && (
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-8 text-center">
              <Loader2 className="w-12 h-12 mx-auto text-blue-500 animate-spin mb-4" />
              <p className="text-gray-500 dark:text-gray-400">Running predictions...</p>
              <p className="text-sm text-gray-400 dark:text-gray-500 mt-2">
                This may take a moment for large datasets.
              </p>
            </div>
          )}

          {/* Predictions Results */}
          {predictionsData && !predictionsLoading && (
            <>
              {/* Summary Cards */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <p className="text-sm text-gray-500 dark:text-gray-400">Total Predictions</p>
                  <p className="text-2xl font-bold text-blue-600">{predictionsData.summary.totalPredictions}</p>
                </div>
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <p className="text-sm text-gray-500 dark:text-gray-400">Accuracy</p>
                  <p className="text-2xl font-bold text-green-600">{(predictionsData.summary.accuracy * 100).toFixed(1)}%</p>
                </div>
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <p className="text-sm text-gray-500 dark:text-gray-400">Avg Probability</p>
                  <p className="text-2xl font-bold text-purple-600">{(predictionsData.summary.avgProbability * 100).toFixed(1)}%</p>
                </div>
                <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                  <p className="text-sm text-gray-500 dark:text-gray-400">Class Distribution</p>
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-red-600">
                      {predictionsData.summary.predictedClass0} Down
                    </span>
                    <span className="text-gray-400">/</span>
                    <span className="text-sm text-green-600">
                      {predictionsData.summary.predictedClass1} Up
                    </span>
                  </div>
                </div>
              </div>

              {/* Chart - TradingView style with prediction markers */}
              <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
                  <TrendingUp className="w-5 h-5 text-blue-500" />
                  Price with Predictions Overlay
                  {predictionsData.availableTargets && predictionsData.availableTargets[selectedTargetIndex] && (
                    <span className="ml-2 text-sm font-normal px-2 py-0.5 bg-purple-100 dark:bg-purple-900/50 text-purple-700 dark:text-purple-300 rounded">
                      Target: {predictionsData.availableTargets[selectedTargetIndex].label}
                    </span>
                  )}
                </h3>
                {/* Chart Controls */}
                <div className="mb-4 p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
                  <div className="flex flex-wrap items-center gap-6">
                    {/* Target Selector (for multi-target models) */}
                    {predictionsData.availableTargets && predictionsData.availableTargets.length > 1 && (
                      <div className="flex items-center gap-2">
                        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
                          Target:
                        </label>
                        <select
                          value={selectedTargetIndex}
                          onChange={(e) => {
                            const newIndex = Number(e.target.value);
                            setSelectedTargetIndex(newIndex);
                            handleRunPredictions(newIndex);
                          }}
                          className="px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800"
                        >
                          {predictionsData.availableTargets.map((target, idx) => (
                            <option key={idx} value={idx}>
                              {target.label}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}

                    {/* Model Threshold Info */}
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      Model threshold: <span className="font-mono font-medium text-blue-600 dark:text-blue-400">{((predictionsData?.threshold ?? 0.5) * 100).toFixed(0)}%</span>
                    </div>

                    {/* Probability Filter Slider */}
                    <div className="flex items-center gap-3">
                      <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
                        Min Confidence:
                      </label>
                      <input
                        type="range"
                        min="0"
                        max="100"
                        value={effectiveMinProbability}
                        onChange={(e) => setMinProbability(Number(e.target.value))}
                        className="w-32 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer dark:bg-gray-600"
                      />
                      <span className="text-sm font-mono w-12 text-gray-600 dark:text-gray-400">
                        {effectiveMinProbability.toFixed(0)}%
                      </span>
                      {minProbability !== null && minProbability !== (predictionsData?.threshold ?? 0.5) * 100 && (
                        <button
                          onClick={() => setMinProbability(null)}
                          className="text-xs text-blue-500 hover:text-blue-700"
                          title="Reset to model threshold"
                        >
                          Reset
                        </button>
                      )}
                    </div>

                    {/* Show Actual Targets Toggle */}
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={showActualTargets}
                        onChange={(e) => setShowActualTargets(e.target.checked)}
                        className="w-4 h-4 text-blue-600 rounded"
                      />
                      <span className="text-sm text-gray-700 dark:text-gray-300">
                        Show Actual Targets (Ground Truth)
                      </span>
                    </label>

                    {/* Show All vs Transitions Toggle */}
                    {showActualTargets && (
                      <label className="flex items-center gap-2 cursor-pointer ml-4">
                        <input
                          type="checkbox"
                          checked={showAllActualTargets}
                          onChange={(e) => setShowAllActualTargets(e.target.checked)}
                          className="w-4 h-4 text-blue-600 rounded"
                        />
                        <span className="text-sm text-gray-700 dark:text-gray-300">
                          Show All (vs Transitions Only)
                        </span>
                      </label>
                    )}
                  </div>
                </div>

                {/* Legend */}
                <div className="mb-2 flex items-center gap-4 text-xs text-gray-500">
                  <span className="flex items-center gap-1">
                    <span className="w-3 h-3 bg-green-500 rounded-full"></span>
                    Correct Prediction
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="w-3 h-3 bg-red-500 rounded-full"></span>
                    Incorrect Prediction
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="text-lg">&#x25B2;</span>
                    Up Prediction
                  </span>
                  <span className="flex items-center gap-1">
                    <span className="text-lg">&#x25BC;</span>
                    Down Prediction
                  </span>
                  {showActualTargets && (
                    <span className="flex items-center gap-1">
                      <span className="w-3 h-3 bg-blue-500 rounded-full"></span>
                      Actual Target (T)
                    </span>
                  )}
                </div>
                <PredictionsChart
                  predictions={predictionsData.predictions}
                  height={450}
                  showOnlyTransitions={false}
                  minProbability={effectiveMinProbability / 100}
                  showActualTargets={showActualTargets}
                  showAllActualTargets={showAllActualTargets}
                />
              </div>

              {/* Prediction Markers */}
              <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
                <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
                  <Target className="w-5 h-5 text-green-500" />
                  Prediction Details
                </h3>
                <div className="overflow-x-auto max-h-96">
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-gray-50 dark:bg-gray-700">
                      <tr>
                        <th className="p-2 text-left">Date</th>
                        <th className="p-2 text-right">Close</th>
                        <th className="p-2 text-right">Probability</th>
                        <th className="p-2 text-center">Predicted</th>
                        <th className="p-2 text-center">Actual</th>
                        <th className="p-2 text-center">Result</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                      {predictionsData.predictions.slice(0, 100).map((pred, idx) => (
                        <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-700/50">
                          <td className="p-2">{new Date(pred.date).toLocaleString()}</td>
                          <td className="p-2 text-right">${pred.close?.toFixed(2)}</td>
                          <td className="p-2 text-right">{(pred.probability * 100).toFixed(1)}%</td>
                          <td className="p-2 text-center">
                            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                              pred.predictedClass === 1
                                ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
                                : 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300'
                            }`}>
                              {pred.predictedClass === 1 ? 'Up' : 'Down'}
                            </span>
                          </td>
                          <td className="p-2 text-center">
                            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                              pred.actual === 1
                                ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
                                : 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300'
                            }`}>
                              {pred.actual === 1 ? 'Up' : 'Down'}
                            </span>
                          </td>
                          <td className="p-2 text-center">
                            {pred.correct ? (
                              <CheckCircle className="w-4 h-4 text-green-500 mx-auto" />
                            ) : (
                              <AlertCircle className="w-4 h-4 text-red-500 mx-auto" />
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {predictionsData.predictions.length > 100 && (
                    <p className="text-center text-sm text-gray-500 dark:text-gray-400 mt-2">
                      Showing first 100 of {predictionsData.predictions.length} predictions
                    </p>
                  )}
                </div>
              </div>

              {/* Re-run button */}
              <div className="text-center">
                <button
                  onClick={() => handleRunPredictions(selectedTargetIndex)}
                  className="flex items-center gap-2 px-4 py-2 text-sm bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 rounded-lg mx-auto"
                >
                  <RefreshCw className="w-4 h-4" />
                  Re-run Predictions
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {/* Retrain Dialog */}
      {showRetrainDialog && model && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full mx-4">
            <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
              <h3 className="text-lg font-semibold flex items-center gap-2">
                <RefreshCw className="w-5 h-5 text-green-500" />
                Retrain Model
              </h3>
              <button onClick={() => setShowRetrainDialog(false)} className="text-gray-500 hover:text-gray-700">
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-4 space-y-4">
              {/* Retrain Mode */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Retrain Mode
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <label className={`flex items-center p-3 rounded-lg border cursor-pointer ${
                    retrainConfig.retrainMode === 'from_scratch'
                      ? 'border-green-500 bg-green-50 dark:bg-green-900/20'
                      : 'border-gray-200 dark:border-gray-600'
                  }`}>
                    <input
                      type="radio"
                      name="retrainMode"
                      value="from_scratch"
                      checked={retrainConfig.retrainMode === 'from_scratch'}
                      onChange={() => setRetrainConfig(prev => ({ ...prev, retrainMode: 'from_scratch' }))}
                      className="w-4 h-4 text-green-600"
                    />
                    <div className="ml-3">
                      <div className="font-medium text-sm">From Scratch</div>
                      <div className="text-xs text-gray-500">Same parameters, new weights</div>
                    </div>
                  </label>
                  <label className={`flex items-center p-3 rounded-lg border cursor-pointer ${
                    retrainConfig.retrainMode === 'load_weights'
                      ? 'border-green-500 bg-green-50 dark:bg-green-900/20'
                      : 'border-gray-200 dark:border-gray-600'
                  }`}>
                    <input
                      type="radio"
                      name="retrainMode"
                      value="load_weights"
                      checked={retrainConfig.retrainMode === 'load_weights'}
                      onChange={() => setRetrainConfig(prev => ({ ...prev, retrainMode: 'load_weights' }))}
                      className="w-4 h-4 text-green-600"
                    />
                    <div className="ml-3">
                      <div className="font-medium text-sm">Continue Training</div>
                      <div className="text-xs text-gray-500">Load weights, additional epochs</div>
                    </div>
                  </label>
                </div>
              </div>

              {/* Dataset Selection */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  <Database className="w-4 h-4 inline mr-1" />
                  Dataset
                </label>
                <select
                  value={retrainConfig.datasetId || model.datasetId}
                  onChange={(e) => {
                    const dsId = e.target.value ? Number(e.target.value) : null;
                    const ds = datasets.find(d => d.id === dsId);
                    setRetrainConfig(prev => ({
                      ...prev,
                      datasetId: dsId,
                      startDate: ds?.start_date?.split('T')[0] || '',
                      endDate: ds?.end_date?.split('T')[0] || ''
                    }));
                  }}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700"
                >
                  {datasets.map(ds => (
                    <option key={ds.id} value={ds.id}>
                      {ds.name} ({ds.ticker})
                    </option>
                  ))}
                </select>
              </div>

              {/* Epochs */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Training Epochs
                </label>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={retrainConfig.epochs}
                  onChange={(e) => setRetrainConfig(prev => ({ ...prev, epochs: parseInt(e.target.value) || 10 }))}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700"
                />
              </div>

              {/* Date Range */}
              <div>
                <label className="flex items-center space-x-2 cursor-pointer mb-2">
                  <input
                    type="checkbox"
                    checked={retrainConfig.useCustomDateRange}
                    onChange={(e) => setRetrainConfig(prev => ({ ...prev, useCustomDateRange: e.target.checked }))}
                    className="w-4 h-4 text-green-600 rounded"
                  />
                  <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    Use custom date range
                  </span>
                </label>
                {retrainConfig.useCustomDateRange && (
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-gray-500 mb-1">Start Date</label>
                      <input
                        type="date"
                        value={retrainConfig.startDate}
                        onChange={(e) => setRetrainConfig(prev => ({ ...prev, startDate: e.target.value }))}
                        className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700"
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-500 mb-1">End Date</label>
                      <input
                        type="date"
                        value={retrainConfig.endDate}
                        onChange={(e) => setRetrainConfig(prev => ({ ...prev, endDate: e.target.value }))}
                        className="w-full px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700"
                      />
                    </div>
                  </div>
                )}
              </div>
            </div>

            <div className="flex justify-end gap-2 p-4 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => setShowRetrainDialog(false)}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
              >
                Cancel
              </button>
              <button
                onClick={handleRetrain}
                disabled={submittingRetrain}
                className="flex items-center gap-2 px-4 py-2 text-sm bg-green-500 text-white rounded-lg hover:bg-green-600 disabled:opacity-50"
              >
                {submittingRetrain ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                Start Retrain
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

export default ModelDetails;
