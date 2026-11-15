import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { AlertCircle, ArrowLeft, Loader2 } from 'lucide-react';
import { getWfOptions } from '../lib/wfApi';
import type { WfOptions } from '../lib/wfApi';
import { WalkForwardForm } from '../components/WalkForwardForm';

export default function WalkForwardNew() {
  const navigate = useNavigate();
  const [options, setOptions] = useState<WfOptions | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getWfOptions().then(setOptions).catch(e => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="p-6">
      <div className="mb-6">
        <Link to="/walk-forward" className="text-sm text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1">
          <ArrowLeft className="w-4 h-4" /> Walk-forward runs
        </Link>
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-2">New walk-forward run</h1>
        <p className="text-gray-600 dark:text-gray-400 mt-1">
          Runs the same chain as the goal2027 grid: fold 1 cold-starts, each later fold is seeded from the
          previous fold's final population, the top-N of every fold is re-run once on its test year.
        </p>
      </div>
      {error && (
        <div className="mb-4 p-3 bg-red-100 text-red-700 rounded-lg flex items-center gap-2">
          <AlertCircle className="w-4 h-4" /> {error}
        </div>
      )}
      {!options && !error && <div className="flex justify-center py-8"><Loader2 className="w-6 h-6 animate-spin text-blue-500" /></div>}
      {options && <WalkForwardForm options={options} onCreated={run => navigate(`/walk-forward/${run.id}`)} />}
    </div>
  );
}
