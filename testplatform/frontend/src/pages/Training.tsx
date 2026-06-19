import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, X, BarChart2, Target, Clock, CheckCircle, AlertCircle, Loader2, Pause, SkipForward, XCircle, ArrowLeft, Activity, Timer, Zap, FileText } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts';
import JobWizard from '../components/JobWizard';
import ConfirmDialog from '../components/ConfirmDialog';

interface Dataset {
  id: number;
  name: string;
  ticker: string;
  timeframe: string;
  start_date: string;
  end_date: string;
  rows_count: number;
  created_at: string;
}

interface ParameterRanges {
  layersMin: number;
  layersMax: number;
  layersStep: number;
  layerSizeMin: number;
  layerSizeMax: number;
  layerSizeStep: number;
  learningRateMin: number;
  learningRateMax: number;
  learningRateStep: number;
  dropoutMin: number;
  dropoutMax: number;
  dropoutStep: number;
  seqLen?: number;
}

interface GeneticConfig {
  populationSize: number;
  generations: number;
  elitismPercent: number;
  crossoverProb: number;
  mutationProb: number;
  earlyStoppingGenerations: number;
  trainingEpochs: number;
}

interface MetricsConfig {
  optimizeMetric: string;
  classificationMetric?: string;
  regressionMetric?: string;
  lossFunction?: string;
}

interface JobProfile {
  id: number;
  name: string;
  createdAt: string;
  updatedAt?: string;
  selectedModels: string[];
  parameterRanges: ParameterRanges;
  predictionTargets: Record<string, unknown>[];  // Can be old or new format
  trainTestSplit: number;
  geneticConfig?: GeneticConfig;
  metricsConfig?: MetricsConfig;
  predictionHorizon?: number;
}


interface Job {
  id: string;
  datasetId: number;
  selectedModels: string[];
  status: 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';
  progress: number;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  error?: string;
  currentGeneration?: number;
  totalGenerations?: number;
  currentLoss?: number;
  currentAccuracy?: number;
  bestFitness?: number;
  gpuUtilization?: number;
  estimatedTimeRemaining?: string;
  errorCount?: number;
  successCount?: number;
}

interface TrainingMetric {
  generation: number;
  loss: number;
  accuracy: number;
  valLoss: number;
  valAccuracy: number;
  fitness: number;
  timestamp: string;
}

interface JobProgress {
  job: Job;
  metrics: TrainingMetric[];
  logs: string[];
}

interface Individual {
  generation: number;
  individual: number;
  model_type: string;
  params: Record<string, number | string>;
  fitness: number;
  metrics: Record<string, number>;
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

const Training: React.FC = () => {
  const navigate = useNavigate();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [_isLoading, setIsLoading] = useState(true);
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [_error, setError] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<JobProfile[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [jobProgress, setJobProgress] = useState<JobProgress | null>(null);
  const [isLoadingProgress, _setIsLoadingProgress] = useState(false);
  const [showLogs, setShowLogs] = useState(false);
  const [showIndividuals, setShowIndividuals] = useState(false);
  const [individualsData, setIndividualsData] = useState<IndividualsData | null>(null);
  const [generationsData, setGenerationsData] = useState<GenerationsData | null>(null);
  const [selectedGeneration, setSelectedGeneration] = useState<number | null>(null);
  const [selectedModelTypeFilter, setSelectedModelTypeFilter] = useState<string>('');

  // Confirm dialog state
  const [confirmDialog, setConfirmDialog] = useState<{
    isOpen: boolean;
    title: string;
    message: string;
    variant: 'danger' | 'warning' | 'info';
    onConfirm: () => void;
  }>({ isOpen: false, title: '', message: '', variant: 'warning', onConfirm: () => {} });

  // Load profiles from API on mount
  useEffect(() => {
    const fetchProfiles = async () => {
      try {
        const response = await fetch('http://localhost:8000/api/jobs/profiles');
        if (response.ok) {
          const data = await response.json();
          setProfiles(data.profiles || []);
        }
      } catch (e) {
        console.error('Failed to load profiles:', e);
      }
    };
    fetchProfiles();
  }, []);

  const fetchDatasets = async () => {
    try {
      const response = await fetch('http://localhost:8000/api/datasets');
      if (!response.ok) {
        throw new Error('Failed to fetch datasets');
      }
      const data = await response.json();
      setDatasets((data.datasets || []).slice().sort((a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name)));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchDatasets();
    fetchJobs();
  }, []);

  const fetchJobs = async () => {
    try {
      const response = await fetch('http://localhost:8000/api/jobs');
      if (response.ok) {
        const data = await response.json();
        setJobs(data.jobs);
      }
    } catch (err) {
      console.error('Failed to fetch jobs:', err);
    }
  };

  const fetchJobProgress = useCallback(async (jobId: string) => {
    try {
      const response = await fetch(`http://localhost:8000/api/jobs/${jobId}/progress`);
      if (response.ok) {
        const data: JobProgress = await response.json();
        setJobProgress(data);
        // Update job in jobs list too
        setJobs(prev => prev.map(j => j.id === jobId ? data.job : j));
      }
    } catch (err) {
      console.error('Failed to fetch job progress:', err);
    }
  }, []);

  const fetchIndividuals = useCallback(async (jobId: string) => {
    try {
      const response = await fetch(`http://localhost:8000/api/jobs/${jobId}/individuals`);
      if (response.ok) {
        const data: IndividualsData = await response.json();
        setIndividualsData(data);
      }
    } catch (err) {
      console.error('Failed to fetch individuals:', err);
    }
  }, []);

  const fetchGenerations = useCallback(async (jobId: string) => {
    try {
      const response = await fetch(`http://localhost:8000/api/jobs/${jobId}/generations`);
      if (response.ok) {
        const data: GenerationsData = await response.json();
        setGenerationsData(data);
      }
    } catch (err) {
      console.error('Failed to fetch generations:', err);
    }
  }, []);

  const handlePauseJob = async (jobId: string) => {
    try {
      const response = await fetch(`http://localhost:8000/api/jobs/${jobId}/pause`, {
        method: 'POST',
      });
      if (response.ok) {
        fetchJobProgress(jobId);
      }
    } catch (err) {
      console.error('Failed to pause job:', err);
    }
  };

  const handleResumeJob = async (jobId: string) => {
    try {
      const response = await fetch(`http://localhost:8000/api/jobs/${jobId}/resume`, {
        method: 'POST',
      });
      if (response.ok) {
        fetchJobProgress(jobId);
      }
    } catch (err) {
      console.error('Failed to resume job:', err);
    }
  };

  const handleCancelJob = (jobId: string) => {
    setConfirmDialog({
      isOpen: true,
      title: 'Cancel Job',
      message: 'Are you sure you want to cancel this job?',
      variant: 'warning',
      onConfirm: async () => {
        try {
          const response = await fetch(`http://localhost:8000/api/jobs/${jobId}/cancel`, {
            method: 'POST',
          });
          if (response.ok) {
            fetchJobProgress(jobId);
          }
        } catch (err) {
          console.error('Failed to cancel job:', err);
        }
      },
    });
  };

  const handleDeleteJob = (jobId: string, e: React.MouseEvent) => {
    e.stopPropagation(); // Prevent opening job monitor
    setConfirmDialog({
      isOpen: true,
      title: 'Delete Job',
      message: 'Are you sure you want to delete this job? This cannot be undone.',
      variant: 'danger',
      onConfirm: async () => {
        try {
          const response = await fetch(`http://localhost:8000/api/jobs/${jobId}`, {
            method: 'DELETE',
          });
          if (response.ok) {
            // Refresh jobs list
            fetchJobs();
            // Close monitor if this job was selected
            if (selectedJobId === jobId) {
              setSelectedJobId(null);
            }
          }
        } catch (err) {
          console.error('Failed to delete job:', err);
        }
      },
    });
  };

  const openJobMonitor = (jobId: string) => {
    navigate(`/training/${jobId}`);
  };

  const closeJobMonitor = () => {
    setSelectedJobId(null);
    setJobProgress(null);
    setShowLogs(false);
  };

  // Auto-refresh job progress when monitoring
  useEffect(() => {
    if (!selectedJobId || !jobProgress) return;

    // Only poll if job is running or paused
    if (!['running', 'paused', 'queued'].includes(jobProgress.job.status)) return;

    const interval = setInterval(() => {
      fetchJobProgress(selectedJobId);
    }, 1000);

    return () => clearInterval(interval);
  }, [selectedJobId, jobProgress?.job.status, fetchJobProgress]);

  // Auto-refresh jobs list when there are active jobs
  useEffect(() => {
    const hasActiveJobs = jobs.some(j => ['running', 'paused', 'queued'].includes(j.status));

    if (!hasActiveJobs) return;

    const interval = setInterval(() => {
      fetchJobs();
    }, 3000); // Poll every 3 seconds

    return () => clearInterval(interval);
  }, [jobs]);

  const handleOpenForm = () => {
    setIsFormOpen(true);
  };

  const handleCloseForm = () => {
    setIsFormOpen(false);
  };

  const deleteProfile = async (profileId: number) => {
    try {
      const response = await fetch(`http://localhost:8000/api/jobs/profiles/${profileId}`, {
        method: 'DELETE',
      });

      if (response.ok) {
        setProfiles(profiles.filter(p => p.id !== profileId));
      } else {
        console.error('Failed to delete profile');
      }
    } catch (e) {
      console.error('Failed to delete profile:', e);
    }
  };

  // Adapter for JobWizard's onSaveProfile prop
  const handleSaveProfile = async (name: string, data: any) => {
    try {
      const profileData = { name, ...data };
      const response = await fetch('http://localhost:8000/api/jobs/profiles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(profileData),
      });
      if (response.ok) {
        const savedProfile = await response.json();
        setProfiles([...profiles, savedProfile]);
      }
    } catch (e) {
      console.error('Failed to save profile:', e);
    }
  };

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Model Training</h1>
        <button
          onClick={handleOpenForm}
          className="px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 flex items-center space-x-2"
        >
          <Plus size={16} />
          <span>Create New Job</span>
        </button>
      </div>

      <p className="text-gray-600 dark:text-gray-400 mb-6">
        Create and monitor optimization jobs here.
      </p>

      {/* Job Creation Wizard */}
      <JobWizard
        isOpen={isFormOpen}
        onClose={handleCloseForm}
        onComplete={(job) => {
          setJobs(prev => [job, ...prev]);
          setIsFormOpen(false);
        }}
        datasets={datasets}
        profiles={profiles}
        onSaveProfile={handleSaveProfile}
        onDeleteProfile={deleteProfile}
      />

      {/* Job Monitor View */}
      {selectedJobId && jobProgress && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-5xl mx-4 max-h-[90vh] flex flex-col">
            {/* Header */}
            <div className="flex justify-between items-center p-4 border-b border-gray-200 dark:border-gray-700">
              <div className="flex items-center space-x-4">
                <button
                  onClick={closeJobMonitor}
                  className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                >
                  <ArrowLeft size={20} />
                </button>
                <div>
                  <h2 className="text-xl font-semibold">Job #{jobProgress.job.id}</h2>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    {datasets.find(d => d.id === jobProgress.job.datasetId)?.ticker || 'Unknown Dataset'}
                  </p>
                </div>
              </div>
              <div className="flex items-center space-x-2">
                {/* Control Buttons */}
                {jobProgress.job.status === 'running' && (
                  <button
                    onClick={() => handlePauseJob(jobProgress.job.id)}
                    className="flex items-center space-x-1 px-3 py-2 bg-yellow-500 text-white rounded-md hover:bg-yellow-600 transition-colors"
                  >
                    <Pause size={16} />
                    <span>Pause</span>
                  </button>
                )}
                {jobProgress.job.status === 'paused' && (
                  <button
                    onClick={() => handleResumeJob(jobProgress.job.id)}
                    className="flex items-center space-x-1 px-3 py-2 bg-green-500 text-white rounded-md hover:bg-green-600 transition-colors"
                  >
                    <SkipForward size={16} />
                    <span>Resume</span>
                  </button>
                )}
                {['running', 'paused', 'queued'].includes(jobProgress.job.status) && (
                  <button
                    onClick={() => handleCancelJob(jobProgress.job.id)}
                    className="flex items-center space-x-1 px-3 py-2 bg-red-500 text-white rounded-md hover:bg-red-600 transition-colors"
                  >
                    <XCircle size={16} />
                    <span>Cancel</span>
                  </button>
                )}
                <button
                  onClick={closeJobMonitor}
                  className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                >
                  <X size={20} />
                </button>
              </div>
            </div>

            <div className="p-6 overflow-y-auto flex-1">
              {isLoadingProgress ? (
                <div className="flex items-center justify-center h-64">
                  <Loader2 className="animate-spin text-blue-500" size={48} />
                </div>
              ) : (
                <>
                  {/* Status Overview */}
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
                    {/* Generation Progress */}
                    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                      <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
                        <Activity size={16} />
                        <span className="text-xs">Generation</span>
                      </div>
                      <div className="text-2xl font-bold">
                        {jobProgress.job.currentGeneration || 0}
                        <span className="text-sm text-gray-500 dark:text-gray-400 font-normal">
                          /{jobProgress.job.totalGenerations || 50}
                        </span>
                      </div>
                    </div>

                    {/* Best Fitness */}
                    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                      <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
                        <Target size={16} />
                        <span className="text-xs">Best Fitness</span>
                      </div>
                      <div className="text-2xl font-bold text-green-600 dark:text-green-400">
                        {jobProgress.job.bestFitness?.toFixed(2) || '--'}
                      </div>
                    </div>

                    {/* GPU Utilization */}
                    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                      <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
                        <Zap size={16} />
                        <span className="text-xs">GPU Usage</span>
                      </div>
                      <div className="text-2xl font-bold text-purple-600 dark:text-purple-400">
                        {jobProgress.job.gpuUtilization?.toFixed(0) || '--'}%
                      </div>
                    </div>

                    {/* ETA */}
                    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                      <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
                        <Timer size={16} />
                        <span className="text-xs">Time Remaining</span>
                      </div>
                      <div className="text-2xl font-bold">
                        {jobProgress.job.estimatedTimeRemaining || '--'}
                      </div>
                    </div>

                    {/* Status */}
                    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                      <div className="flex items-center space-x-2 text-gray-500 dark:text-gray-400 mb-1">
                        <Clock size={16} />
                        <span className="text-xs">Status</span>
                      </div>
                      <div className={`text-2xl font-bold ${
                        jobProgress.job.status === 'completed' ? 'text-green-600 dark:text-green-400' :
                        jobProgress.job.status === 'running' ? 'text-blue-600 dark:text-blue-400' :
                        jobProgress.job.status === 'paused' ? 'text-yellow-600 dark:text-yellow-400' :
                        jobProgress.job.status === 'failed' || jobProgress.job.status === 'cancelled' ? 'text-red-600 dark:text-red-400' :
                        'text-gray-600 dark:text-gray-400'
                      }`}>
                        {jobProgress.job.status.charAt(0).toUpperCase() + jobProgress.job.status.slice(1)}
                      </div>
                    </div>
                  </div>

                  {/* Overall Progress Bar */}
                  <div className="mb-6">
                    <div className="flex justify-between text-sm mb-1">
                      <span className="text-gray-500 dark:text-gray-400">Progress</span>
                      <span className="font-medium">{jobProgress.job.progress.toFixed(1)}%</span>
                    </div>
                    <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                      <div
                        className={`h-3 rounded-full transition-all duration-300 ${
                          jobProgress.job.status === 'completed' ? 'bg-green-500' :
                          jobProgress.job.status === 'paused' ? 'bg-yellow-500' :
                          'bg-blue-500'
                        }`}
                        style={{ width: `${jobProgress.job.progress}%` }}
                      />
                    </div>
                  </div>

                  {/* Charts */}
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
                    {/* Loss Chart */}
                    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                      <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">Loss Over Generations</h3>
                      {jobProgress.metrics.length > 0 ? (
                        <ResponsiveContainer width="100%" height={200}>
                          <LineChart data={jobProgress.metrics}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                            <XAxis dataKey="generation" stroke="#6B7280" fontSize={12} />
                            <YAxis stroke="#6B7280" fontSize={12} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#1F2937', border: 'none', borderRadius: '8px' }}
                              labelStyle={{ color: '#9CA3AF' }}
                            />
                            <Legend />
                            <Line type="monotone" dataKey="loss" stroke="#EF4444" name="Train Loss" dot={false} strokeWidth={2} />
                            <Line type="monotone" dataKey="valLoss" stroke="#F97316" name="Val Loss" dot={false} strokeWidth={2} />
                          </LineChart>
                        </ResponsiveContainer>
                      ) : (
                        <div className="h-48 flex items-center justify-center text-gray-500 dark:text-gray-400">
                          Waiting for training data...
                        </div>
                      )}
                    </div>

                    {/* Accuracy Chart */}
                    <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                      <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">Accuracy Over Generations</h3>
                      {jobProgress.metrics.length > 0 ? (
                        <ResponsiveContainer width="100%" height={200}>
                          <LineChart data={jobProgress.metrics}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                            <XAxis dataKey="generation" stroke="#6B7280" fontSize={12} />
                            <YAxis stroke="#6B7280" fontSize={12} domain={[0, 1]} />
                            <Tooltip
                              contentStyle={{ backgroundColor: '#1F2937', border: 'none', borderRadius: '8px' }}
                              labelStyle={{ color: '#9CA3AF' }}
                            />
                            <Legend />
                            <Line type="monotone" dataKey="accuracy" stroke="#22C55E" name="Train Acc" dot={false} strokeWidth={2} />
                            <Line type="monotone" dataKey="valAccuracy" stroke="#10B981" name="Val Acc" dot={false} strokeWidth={2} />
                          </LineChart>
                        </ResponsiveContainer>
                      ) : (
                        <div className="h-48 flex items-center justify-center text-gray-500 dark:text-gray-400">
                          Waiting for training data...
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Logs Section */}
                  <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4">
                    <button
                      onClick={() => setShowLogs(!showLogs)}
                      className="flex items-center justify-between w-full text-left"
                    >
                      <div className="flex items-center space-x-2">
                        <FileText size={16} className="text-gray-500" />
                        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">Training Logs</h3>
                      </div>
                      <span className="text-gray-500">{showLogs ? '▲' : '▼'}</span>
                    </button>
                    {showLogs && (
                      <div className="mt-4 bg-gray-900 rounded-md p-4 max-h-48 overflow-y-auto font-mono text-sm">
                        {jobProgress.logs.length > 0 ? (
                          jobProgress.logs.map((log, idx) => (
                            <div key={idx} className="text-green-400">
                              {log}
                            </div>
                          ))
                        ) : (
                          <div className="text-gray-500">No logs yet...</div>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Model Individuals Visualization Section */}
                  <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-4 mt-6">
                    <button
                      onClick={() => {
                        setShowIndividuals(!showIndividuals);
                        if (!showIndividuals && !individualsData) {
                          fetchIndividuals(jobProgress.job.id);
                          fetchGenerations(jobProgress.job.id);
                        }
                      }}
                      className="flex items-center justify-between w-full text-left"
                    >
                      <div className="flex items-center space-x-2">
                        <BarChart2 size={16} className="text-gray-500" />
                        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">
                          Model Individuals ({individualsData?.summary?.total_individuals || 0} total)
                        </h3>
                      </div>
                      <span className="text-gray-500">{showIndividuals ? '▲' : '▼'}</span>
                    </button>

                    {showIndividuals && (
                      <div className="mt-4 space-y-4">
                        {/* Summary Stats */}
                        {individualsData && (
                          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                            <div className="bg-white dark:bg-gray-800 rounded-lg p-3">
                              <div className="text-xs text-gray-500 dark:text-gray-400">Total Individuals</div>
                              <div className="text-xl font-bold">{individualsData.summary.total_individuals}</div>
                            </div>
                            <div className="bg-white dark:bg-gray-800 rounded-lg p-3">
                              <div className="text-xs text-gray-500 dark:text-gray-400">Generations</div>
                              <div className="text-xl font-bold">{individualsData.summary.generations.length}</div>
                            </div>
                            <div className="bg-white dark:bg-gray-800 rounded-lg p-3">
                              <div className="text-xs text-gray-500 dark:text-gray-400">Best Fitness</div>
                              <div className="text-xl font-bold text-green-600">{individualsData.summary.best_fitness.toFixed(4)}</div>
                            </div>
                            <div className="bg-white dark:bg-gray-800 rounded-lg p-3">
                              <div className="text-xs text-gray-500 dark:text-gray-400">Avg Fitness</div>
                              <div className="text-xl font-bold">{individualsData.summary.avg_fitness.toFixed(4)}</div>
                            </div>
                          </div>
                        )}

                        {/* Best Individual */}
                        {individualsData?.best_individual && (
                          <div className="bg-white dark:bg-gray-800 rounded-lg p-4 border-2 border-green-500">
                            <h4 className="text-sm font-semibold text-green-600 mb-2">Best Individual</h4>
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
                              <div>
                                <span className="text-gray-500">Model:</span>{' '}
                                <span className="font-medium">{individualsData.best_individual.model_type.toUpperCase()}</span>
                              </div>
                              <div>
                                <span className="text-gray-500">Generation:</span>{' '}
                                <span className="font-medium">{individualsData.best_individual.generation}</span>
                              </div>
                              <div>
                                <span className="text-gray-500">Fitness:</span>{' '}
                                <span className="font-medium text-green-600">{individualsData.best_individual.fitness.toFixed(4)}</span>
                              </div>
                              <div>
                                <span className="text-gray-500">MAPE:</span>{' '}
                                <span className="font-medium">{(individualsData.best_individual.metrics?.mape || 0).toFixed(2)}%</span>
                              </div>
                            </div>
                            <div className="mt-2 text-xs text-gray-500">
                              <strong>Params:</strong>{' '}
                              {Object.entries(individualsData.best_individual.params || {}).map(([k, v]) => (
                                <span key={k} className="mr-2">{k}={typeof v === 'number' ? v.toFixed(4) : v}</span>
                              ))}
                            </div>
                          </div>
                        )}

                        {/* Filters */}
                        <div className="flex items-center space-x-4">
                          <div>
                            <label className="text-xs text-gray-500 dark:text-gray-400 mr-2">Generation:</label>
                            <select
                              value={selectedGeneration ?? ''}
                              onChange={(e) => setSelectedGeneration(e.target.value ? parseInt(e.target.value) : null)}
                              className="px-2 py-1 text-sm border rounded dark:bg-gray-800 dark:border-gray-600"
                            >
                              <option value="">All</option>
                              {individualsData?.summary.generations.map(g => (
                                <option key={g} value={g}>Gen {g}</option>
                              ))}
                            </select>
                          </div>
                          <div>
                            <label className="text-xs text-gray-500 dark:text-gray-400 mr-2">Model:</label>
                            <select
                              value={selectedModelTypeFilter}
                              onChange={(e) => setSelectedModelTypeFilter(e.target.value)}
                              className="px-2 py-1 text-sm border rounded dark:bg-gray-800 dark:border-gray-600"
                            >
                              <option value="">All</option>
                              {individualsData?.summary.model_types.map(m => (
                                <option key={m} value={m}>{m.toUpperCase()}</option>
                              ))}
                            </select>
                          </div>
                          <button
                            onClick={() => {
                              fetchIndividuals(jobProgress.job.id);
                              fetchGenerations(jobProgress.job.id);
                            }}
                            className="px-3 py-1 text-sm bg-blue-500 text-white rounded hover:bg-blue-600"
                          >
                            Refresh
                          </button>
                        </div>

                        {/* Fitness by Generation Chart */}
                        {generationsData && generationsData.generations.length > 0 && (
                          <div className="bg-white dark:bg-gray-800 rounded-lg p-4">
                            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">Fitness by Generation</h4>
                            <ResponsiveContainer width="100%" height={200}>
                              <LineChart data={generationsData.generations}>
                                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                                <XAxis dataKey="generation" stroke="#6B7280" fontSize={12} />
                                <YAxis stroke="#6B7280" fontSize={12} domain={[0, 1]} />
                                <Tooltip
                                  contentStyle={{ backgroundColor: '#1F2937', border: 'none', borderRadius: '8px' }}
                                  labelStyle={{ color: '#9CA3AF' }}
                                />
                                <Legend />
                                <Line type="monotone" dataKey="best_fitness" stroke="#22C55E" name="Best" strokeWidth={2} />
                                <Line type="monotone" dataKey="avg_fitness" stroke="#3B82F6" name="Average" strokeWidth={2} />
                                <Line type="monotone" dataKey="min_fitness" stroke="#EF4444" name="Min" strokeWidth={1} dot={false} />
                              </LineChart>
                            </ResponsiveContainer>
                          </div>
                        )}

                        {/* Model Type Distribution */}
                        {generationsData && generationsData.generations.length > 0 && (
                          <div className="bg-white dark:bg-gray-800 rounded-lg p-4">
                            <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-4">Model Type Distribution (Latest Gen)</h4>
                            <div className="flex flex-wrap gap-2">
                              {Object.entries(generationsData.generations[generationsData.generations.length - 1]?.model_types || {}).map(([type, count]) => {
                                const colors: Record<string, string> = {
                                  lstm: 'bg-blue-500',
                                  gru: 'bg-green-500',
                                  nbeats: 'bg-purple-500',
                                  tcn: 'bg-orange-500',
                                  transformer: 'bg-pink-500',
                                  tft: 'bg-cyan-500',
                                };
                                return (
                                  <div key={type} className={`${colors[type] || 'bg-gray-500'} text-white px-3 py-1 rounded-full text-sm`}>
                                    {type.toUpperCase()}: {count}
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        )}

                        {/* Individuals Table */}
                        {individualsData && (
                          <div className="bg-white dark:bg-gray-800 rounded-lg overflow-hidden">
                            <div className="max-h-64 overflow-y-auto">
                              <table className="w-full text-sm">
                                <thead className="bg-gray-100 dark:bg-gray-700 sticky top-0">
                                  <tr>
                                    <th className="px-3 py-2 text-left">Gen</th>
                                    <th className="px-3 py-2 text-left">Model</th>
                                    <th className="px-3 py-2 text-left">Fitness</th>
                                    <th className="px-3 py-2 text-left">MAPE</th>
                                    <th className="px-3 py-2 text-left">Key Params</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {individualsData.individuals
                                    .filter(ind =>
                                      (selectedGeneration === null || ind.generation === selectedGeneration) &&
                                      (selectedModelTypeFilter === '' || ind.model_type === selectedModelTypeFilter)
                                    )
                                    .sort((a, b) => b.fitness - a.fitness)
                                    .slice(0, 50)
                                    .map((ind, idx) => (
                                      <tr key={idx} className={`border-t dark:border-gray-700 ${idx === 0 ? 'bg-green-50 dark:bg-green-900/20' : ''}`}>
                                        <td className="px-3 py-2">{ind.generation}</td>
                                        <td className="px-3 py-2">
                                          <span className={`px-2 py-0.5 rounded text-xs ${
                                            ind.model_type === 'lstm' ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300' :
                                            ind.model_type === 'gru' ? 'bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-300' :
                                            ind.model_type === 'nbeats' ? 'bg-purple-100 text-purple-800 dark:bg-purple-900/50 dark:text-purple-300' :
                                            ind.model_type === 'tcn' ? 'bg-orange-100 text-orange-800 dark:bg-orange-900/50 dark:text-orange-300' :
                                            ind.model_type === 'tft' ? 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/50 dark:text-cyan-300' :
                                            'bg-pink-100 text-pink-800 dark:bg-pink-900/50 dark:text-pink-300'
                                          }`}>
                                            {ind.model_type.toUpperCase()}
                                          </span>
                                        </td>
                                        <td className="px-3 py-2 font-medium">{ind.fitness.toFixed(4)}</td>
                                        <td className="px-3 py-2">{(ind.metrics?.mape || 0).toFixed(2)}%</td>
                                        <td className="px-3 py-2 text-xs text-gray-500">
                                          {ind.params?.hidden_dim && `dim=${ind.params.hidden_dim}`}
                                          {ind.params?.n_rnn_layers && ` layers=${ind.params.n_rnn_layers}`}
                                          {ind.params?.learning_rate && ` lr=${Number(ind.params.learning_rate).toFixed(4)}`}
                                        </td>
                                      </tr>
                                    ))}
                                </tbody>
                              </table>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Jobs List */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
        <h2 className="text-lg font-semibold mb-4">Optimization Jobs</h2>
        {jobs.length === 0 ? (
          <p className="text-gray-500 dark:text-gray-400">
            No optimization jobs yet. Click "Create New Job" to get started.
          </p>
        ) : (
          <div className="space-y-3">
            {jobs.map((job) => {
              const dataset = datasets.find(d => d.id === job.datasetId);
              const statusIcons: Record<string, React.ReactNode> = {
                queued: <Clock size={16} className="text-yellow-500" />,
                running: <Loader2 size={16} className="text-blue-500 animate-spin" />,
                paused: <Pause size={16} className="text-yellow-500" />,
                stopped: <AlertCircle size={16} className="text-orange-500" />,
                completed: <CheckCircle size={16} className="text-green-500" />,
                failed: <AlertCircle size={16} className="text-red-500" />,
                cancelled: <XCircle size={16} className="text-gray-500" />,
              };
              const statusIcon = statusIcons[job.status] || statusIcons.queued;
              // SOLID mid-tone bg + white text: readable in both themes and immune to the dead
              // native `dark:` variant + the global `.dark .font-semibold` text-lightening here.
              const statusColors: Record<string, string> = {
                queued: 'bg-amber-600 text-white',
                running: 'bg-blue-600 text-white',
                paused: 'bg-amber-600 text-white',
                stopped: 'bg-orange-600 text-white',
                completed: 'bg-emerald-600 text-white',
                failed: 'bg-red-600 text-white',
                cancelled: 'bg-slate-500 text-white',
              };
              const statusColor = statusColors[job.status] || statusColors.queued;

              return (
                <div
                  key={job.id}
                  onClick={() => openJobMonitor(job.id)}
                  className="flex items-center justify-between p-4 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors cursor-pointer"
                >
                  <div className="flex items-center space-x-4">
                    {statusIcon}
                    <div>
                      <div className="font-medium">
                        Job #{job.id}
                        {dataset && (
                          <span className="text-gray-500 dark:text-gray-400 ml-2">
                            - {dataset.ticker} ({dataset.timeframe})
                          </span>
                        )}
                      </div>
                      <div className="text-sm text-gray-500 dark:text-gray-400">
                        {job.selectedModels.length} model{job.selectedModels.length !== 1 ? 's' : ''} |
                        Created: {new Date(job.createdAt).toLocaleString()}
                        {job.status === 'completed' && job.startedAt && job.completedAt && (() => {
                          const ms = new Date(job.completedAt!).getTime() - new Date(job.startedAt!).getTime();
                          const totalMin = Math.floor(ms / 60000);
                          const h = Math.floor(totalMin / 60);
                          const m = totalMin % 60;
                          return <span className="ml-2">| Duration: {h > 0 ? `${h}h ${m}m` : `${m}m`}</span>;
                        })()}
                        {job.status === 'running' && job.currentGeneration !== undefined && (
                          <span className="ml-2">| Gen {job.currentGeneration}/{job.totalGenerations || 50}</span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center space-x-3">
                    {/* Error/Success count */}
                    {(job.errorCount !== undefined || job.successCount !== undefined) && (
                      <div className="flex items-center space-x-1 text-xs">
                        {job.errorCount !== undefined && job.errorCount > 0 && (
                          <span className="px-2 py-0.5 bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300 rounded-full" title="Errors">
                            {job.errorCount} err
                          </span>
                        )}
                        {job.successCount !== undefined && job.successCount > 0 && (
                          <span className="px-2 py-0.5 bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300 rounded-full" title="Successes">
                            {job.successCount} ok
                          </span>
                        )}
                      </div>
                    )}
                    <span className={`px-2 py-1 text-xs rounded-full ${statusColor}`}>
                      {job.status.charAt(0).toUpperCase() + job.status.slice(1)}
                    </span>
                    {(job.status === 'running' || job.status === 'paused') && (
                      <div className="w-24 bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                        <div
                          className={`h-2 rounded-full transition-all ${
                            job.status === 'paused' ? 'bg-yellow-500' : 'bg-blue-500'
                          }`}
                          style={{ width: `${job.progress}%` }}
                        />
                      </div>
                    )}
                    <button
                      onClick={(e) => handleDeleteJob(job.id, e)}
                      className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition-colors"
                      title="Delete job"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                  </div>
                </div>
              );
            })}
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
        confirmText={confirmDialog.variant === 'danger' ? 'Delete' : 'Confirm'}
      />
    </div>
  );
};

export default Training;
