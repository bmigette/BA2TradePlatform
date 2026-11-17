import type { WfStatus } from '../lib/wfApi';
import { WF_STATUS_STYLES } from '../lib/walkForward';

/** Status pill of a walk-forward run (solid colours, as the optimization jobs table). */
export function WfStatusBadge({ status }: { status: WfStatus }) {
  return (
    <span className={`inline-block px-2 py-0.5 text-xs font-semibold rounded-full border ${WF_STATUS_STYLES[status] ?? 'bg-slate-500 text-white'}`}>
      {status}
    </span>
  );
}
