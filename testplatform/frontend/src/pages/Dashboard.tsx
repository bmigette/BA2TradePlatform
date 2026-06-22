import { API_BASE } from '../lib/config';
import React, { useEffect, useState } from 'react';
import {
  Activity,
  Cpu,
  HardDrive,
  Loader2,
  CheckCircle,
  XCircle,
  Clock,
  Pause,
  Play,
  Database,
  Brain,
  BarChart3,
  AlertCircle,
  RefreshCw,
  Server
} from 'lucide-react';

interface JobStats {
  running: number;
  completed: number;
  failed: number;
  paused: number;
  queued: number;
  cancelled: number;
  total: number;
}

interface ActivityItem {
  id: string;
  type: 'job' | 'dataset' | 'backtest' | 'model';
  action: 'created' | 'completed' | 'failed' | 'started';
  title: string;
  timestamp: string;
  status?: string;
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

interface WorkerStat {
  name: string;
  status: string;
  isLocal: boolean;
  isEnabled: boolean;
  activeJobs: number;
  cores: number | null;
}

interface DashboardData {
  jobStats: JobStats;
  recentActivity: ActivityItem[];
  systemResources: SystemResources;
  workers?: WorkerStat[];
}


const Dashboard: React.FC = () => {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const fetchDashboardData = async (showRefresh = false) => {
    if (showRefresh) setRefreshing(true);
    try {
      const response = await fetch(`${API_BASE}/dashboard/stats`);
      if (!response.ok) {
        throw new Error('Failed to fetch dashboard data');
      }
      const dashboardData = await response.json();
      setData(dashboardData);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchDashboardData();
    // Refresh every 5 seconds
    const interval = setInterval(() => fetchDashboardData(), 5000);
    return () => clearInterval(interval);
  }, []);

  const formatTimestamp = (timestamp: string) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  };

  const getActivityIcon = (item: ActivityItem) => {
    if (item.type === 'dataset') return <Database className="w-4 h-4" />;
    if (item.type === 'model') return <Brain className="w-4 h-4" />;
    if (item.type === 'backtest') return <BarChart3 className="w-4 h-4" />;

    // Job icons based on action
    if (item.action === 'completed') return <CheckCircle className="w-4 h-4 text-green-500" />;
    if (item.action === 'failed') return <XCircle className="w-4 h-4 text-red-500" />;
    if (item.action === 'started') return <Play className="w-4 h-4 text-blue-500" />;
    return <Clock className="w-4 h-4 text-gray-500" />;
  };

  const getActivityColor = (item: ActivityItem) => {
    if (item.action === 'completed') return 'border-l-green-500';
    if (item.action === 'failed') return 'border-l-red-500';
    if (item.action === 'started') return 'border-l-blue-500';
    return 'border-l-gray-400';
  };

  const formatBytes = (mb: number) => {
    if (mb >= 1024) {
      return `${(mb / 1024).toFixed(1)} GB`;
    }
    return `${mb} MB`;
  };

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
            <span>Failed to load dashboard: {error}</span>
          </div>
        </div>
      </div>
    );
  }

  const { jobStats, recentActivity, systemResources } = data!;
  const workers = data!.workers ?? [];

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100">Dashboard</h1>
        <button
          onClick={() => fetchDashboardData(true)}
          disabled={refreshing}
          className="flex items-center gap-2 px-3 py-2 text-sm bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg transition-colors"
        >
          <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* Job Stats Overview - Feature 115 */}
      <section>
        <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
          <Activity className="w-5 h-5" />
          Optimization Jobs Overview
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow border-l-4 border-l-blue-500">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Running</h3>
              <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
            </div>
            <p className="text-2xl font-bold text-blue-600 mt-1">{jobStats.running}</p>
          </div>

          <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow border-l-4 border-l-green-500">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Completed</h3>
              <CheckCircle className="w-4 h-4 text-green-500" />
            </div>
            <p className="text-2xl font-bold text-green-600 mt-1">{jobStats.completed}</p>
          </div>

          <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow border-l-4 border-l-red-500">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Failed</h3>
              <XCircle className="w-4 h-4 text-red-500" />
            </div>
            <p className="text-2xl font-bold text-red-600 mt-1">{jobStats.failed}</p>
          </div>

          <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow border-l-4 border-l-yellow-500">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Paused</h3>
              <Pause className="w-4 h-4 text-yellow-500" />
            </div>
            <p className="text-2xl font-bold text-yellow-600 mt-1">{jobStats.paused}</p>
          </div>

          <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow border-l-4 border-l-gray-400">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Queued</h3>
              <Clock className="w-4 h-4 text-gray-400" />
            </div>
            <p className="text-2xl font-bold text-gray-600 dark:text-gray-300 mt-1">{jobStats.queued}</p>
          </div>

          <div className="bg-white dark:bg-gray-800 p-4 rounded-lg shadow border-l-4 border-l-purple-500">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Total</h3>
              <Activity className="w-4 h-4 text-purple-500" />
            </div>
            <p className="text-2xl font-bold text-purple-600 mt-1">{jobStats.total}</p>
          </div>
        </div>
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Activity Timeline - Feature 116 */}
        <section className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
            <Clock className="w-5 h-5" />
            Recent Activity
          </h2>

          {recentActivity.length === 0 ? (
            <div className="text-center py-8 text-gray-500 dark:text-gray-400">
              <Activity className="w-12 h-12 mx-auto mb-2 opacity-50" />
              <p>No recent activity</p>
              <p className="text-sm">Create datasets and run optimization jobs to see activity here</p>
            </div>
          ) : (
            <div className="space-y-3 max-h-[calc(100vh-15rem)] overflow-y-auto pr-2">
              {recentActivity.map((item) => (
                <div
                  key={item.id}
                  className={`flex items-start gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg border-l-4 ${getActivityColor(item)}`}
                >
                  <div className="flex-shrink-0 mt-0.5">
                    {getActivityIcon(item)}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium truncate text-gray-800 dark:text-gray-100">{item.title}</p>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      {formatTimestamp(item.timestamp)}
                    </p>
                  </div>
                  {/* Badge contract: SOLID mid-tone bg + white text (the app's dark theme makes
                      `dark:` variants inert and force-lightens pill text, so light `-100` pills
                      render as invisible text). */}
                  <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${
                    item.type === 'job' ? 'bg-blue-600 text-white border-blue-600' :
                    item.type === 'dataset' ? 'bg-purple-600 text-white border-purple-600' :
                    item.type === 'model' ? 'bg-emerald-600 text-white border-emerald-600' :
                    'bg-amber-600 text-white border-amber-600'
                  }`}>
                    {item.type}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* System Resources - Feature 117 */}
        <section className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
            <Cpu className="w-5 h-5" />
            System Resources
          </h2>

          <div className="space-y-4">
            {/* CPU Usage */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium flex items-center gap-2">
                  <Cpu className="w-4 h-4 text-blue-500" />
                  CPU Usage
                </span>
                <span className="text-sm font-semibold">{systemResources.cpuPercent}%</span>
              </div>
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                <div
                  className={`h-3 rounded-full transition-all duration-500 ${
                    systemResources.cpuPercent > 80 ? 'bg-red-500' :
                    systemResources.cpuPercent > 60 ? 'bg-yellow-500' : 'bg-blue-500'
                  }`}
                  style={{ width: `${systemResources.cpuPercent}%` }}
                />
              </div>
            </div>

            {/* Memory Usage */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium flex items-center gap-2">
                  <HardDrive className="w-4 h-4 text-green-500" />
                  Memory Usage
                </span>
                <span className="text-sm font-semibold">
                  {formatBytes(systemResources.memoryUsedMB)} / {formatBytes(systemResources.memoryTotalMB)}
                </span>
              </div>
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                <div
                  className={`h-3 rounded-full transition-all duration-500 ${
                    systemResources.memoryPercent > 80 ? 'bg-red-500' :
                    systemResources.memoryPercent > 60 ? 'bg-yellow-500' : 'bg-green-500'
                  }`}
                  style={{ width: `${systemResources.memoryPercent}%` }}
                />
              </div>
              <p className="text-xs text-gray-500 mt-1">{systemResources.memoryPercent}% used</p>
            </div>

            {/* GPU Usage */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium flex items-center gap-2">
                  <svg className="w-4 h-4 text-purple-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <rect x="2" y="3" width="20" height="14" rx="2" />
                    <path d="M8 21h8" />
                    <path d="M12 17v4" />
                    <path d="M6 7h4" />
                    <path d="M6 11h4" />
                    <path d="M14 7h4" />
                    <path d="M14 11h4" />
                  </svg>
                  GPU Usage
                </span>
                {systemResources.gpuUtilization !== null ? (
                  <span className="text-sm font-semibold">{systemResources.gpuUtilization}%</span>
                ) : (
                  <span className="text-xs text-gray-500 px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded">
                    No GPU detected
                  </span>
                )}
              </div>
              {systemResources.gpuUtilization !== null ? (
                <>
                  <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                    <div
                      className={`h-3 rounded-full transition-all duration-500 ${
                        systemResources.gpuUtilization > 80 ? 'bg-red-500' :
                        systemResources.gpuUtilization > 60 ? 'bg-yellow-500' : 'bg-purple-500'
                      }`}
                      style={{ width: `${systemResources.gpuUtilization}%` }}
                    />
                  </div>
                  {systemResources.gpuMemoryTotalMB && (
                    <p className="text-xs text-gray-500 mt-1">
                      VRAM: {formatBytes(systemResources.gpuMemoryUsedMB || 0)} / {formatBytes(systemResources.gpuMemoryTotalMB)}
                    </p>
                  )}
                </>
              ) : (
                <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                  <div className="h-3 rounded-full bg-gray-400 w-0" />
                </div>
              )}
            </div>
          </div>

          {/* Quick Stats */}
          <div className="mt-6 pt-4 border-t border-gray-200 dark:border-gray-700">
            <h3 className="text-sm font-medium mb-3">Quick Stats</h3>
            <div className="grid grid-cols-2 gap-4">
              <div className="text-center p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-2xl font-bold text-blue-600">{jobStats.running}</p>
                <p className="text-xs text-gray-500">Active Jobs</p>
              </div>
              <div className="text-center p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-2xl font-bold text-green-600">
                  {jobStats.total > 0
                    ? Math.round((jobStats.completed / jobStats.total) * 100)
                    : 0}%
                </p>
                <p className="text-xs text-gray-500">Success Rate</p>
              </div>
            </div>
          </div>

          {/* Workers */}
          <div className="mt-6 pt-4 border-t border-gray-200 dark:border-gray-700">
            <h3 className="text-sm font-medium mb-3 flex items-center gap-2">
              <Server className="w-4 h-4 text-blue-500" />
              Workers
              <span className="text-xs text-gray-500">
                ({workers.filter(w => w.isEnabled && w.status === 'online').length} online / {workers.length} total)
              </span>
            </h3>
            {workers.length === 0 ? (
              <p className="text-xs text-gray-500">No workers configured.</p>
            ) : (
              <div className="space-y-2">
                {workers.map((w) => (
                  <div key={w.name} className="flex items-center justify-between gap-2 p-2 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-sm font-medium truncate text-gray-800 dark:text-gray-100">{w.name}</span>
                      {w.isLocal && (
                        <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-blue-600 text-white">Local</span>
                      )}
                      {/* Badge contract: solid mid-tone bg + white text (dark theme makes `dark:`
                          variants inert and force-lightens pill text). */}
                      <span className={`px-2 py-0.5 text-xs font-medium rounded-full ${
                        !w.isEnabled ? 'bg-slate-500 text-white' :
                        w.status === 'online' ? 'bg-emerald-600 text-white' :
                        w.status === 'busy' ? 'bg-amber-600 text-white' :
                        'bg-red-600 text-white'
                      }`}>
                        {w.isEnabled ? w.status : 'disabled'}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400 flex-shrink-0">
                      {w.cores != null && (
                        <span className="flex items-center gap-1"><Cpu className="w-3 h-3" />{w.cores}</span>
                      )}
                      <span className="flex items-center gap-1"><Activity className="w-3 h-3" />{w.activeJobs} jobs</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
};

export default Dashboard;
