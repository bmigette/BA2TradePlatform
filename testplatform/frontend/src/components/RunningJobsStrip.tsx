import { useEffect, useState } from 'react';
import { listTasks, cancelTask } from '../lib/btApi';
import type { TaskInfo } from '../lib/btApi';

const BT_TASK_TYPES = new Set(['daily_backtest', 'backtest', 'strategy_optimization']);

export function RunningJobsStrip() {
  const [jobs, setJobs] = useState<TaskInfo[]>([]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const all = await listTasks('running');
        if (alive) setJobs(all.filter(t => !t.task_type || BT_TASK_TYPES.has(t.task_type)));
      } catch {
        if (alive) setJobs([]);
      }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (!jobs.length) return null;

  const onCancel = (taskId: string) => {
    cancelTask(taskId)
      .then(() => setJobs(prev => prev.filter(x => x.task_id !== taskId)))
      .catch(() => { /* keep row; next poll reconciles */ });
  };

  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3 mb-3">
      <div className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-2">Running jobs ({jobs.length})</div>
      {jobs.map(j => (
        <div key={j.task_id} className="flex items-center gap-3 p-2 mt-2 bg-gray-50 dark:bg-gray-700/50 rounded border border-gray-200 dark:border-gray-600">
          <span className="text-sm text-gray-700 dark:text-gray-300 w-48 truncate">
            {j.task_type ?? 'job'} · {j.name ?? j.task_id}
          </span>
          <div className="flex-1 bg-gray-200 dark:bg-gray-600 rounded-full h-2">
            <div className="bg-blue-500 h-2 rounded-full transition-all" style={{ width: `${j.progress ?? 0}%` }} />
          </div>
          <span className="text-xs text-gray-600 dark:text-gray-400 w-10 text-right">{Math.round(j.progress ?? 0)}%</span>
          <button type="button" onClick={() => onCancel(j.task_id)}
            className="px-2 py-1 text-xs text-red-600 dark:text-red-400 border border-red-300 dark:border-red-600 rounded hover:bg-red-50 dark:hover:bg-red-900/20">
            Cancel
          </button>
        </div>
      ))}
    </div>
  );
}
