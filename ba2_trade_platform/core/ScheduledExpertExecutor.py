"""Pass APScheduler's actual fire time, including coalesced/misfired jobs.

Wall-clock time inside a callback cannot identify its cohort: the executor may
start it minutes late. The pool executor's submission hook supplies run_times.
"""
from apscheduler.executors.pool import ThreadPoolExecutor


class _Invocation:
    """Delegate job metadata without serializing/copying a bound-method Job.

    Job.__getstate__ rewrites bound method args and drops _jobstore_alias, so
    copy.copy(job) is not a safe way to prepare a thread-pool invocation.
    """
    def __init__(self, job, scheduled_for):
        self._job = job
        self.kwargs = dict(job.kwargs, scheduled_for=scheduled_for)

    def __getattr__(self, name):
        return getattr(self._job, name)

    def __str__(self):
        return str(self._job)


class ScheduledExpertExecutor(ThreadPoolExecutor):
    def _do_submit_job(self, job, run_times):
        invocation = _Invocation(job, run_times[-1])
        # Expert jobs coalesce; never mutate the scheduler-owned Job object.
        super()._do_submit_job(invocation, run_times[-1:])
