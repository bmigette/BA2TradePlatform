"""
Performance logging utilities.

Usage:
    from app.services.perf import perf_timer, perf_log

    # Context manager
    with perf_timer("load_dataset"):
        df = pd.read_csv(path)

    # Decorator
    @perf_log
    def heavy_function():
        ...
"""

import functools
import logging
import time
from contextlib import contextmanager

logger = logging.getLogger("perf")

# Threshold in seconds
WARN_THRESHOLD = 4.0   # > 4s → WARNING [SLOW]
INFO_THRESHOLD = 1.0   # > 1s → INFO


@contextmanager
def perf_timer(label: str, warn_threshold: float = WARN_THRESHOLD):
    """Context manager that logs execution time of a block.

    Always logs at DEBUG level. Logs at WARNING if duration exceeds threshold.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        if elapsed > warn_threshold:
            logger.warning(f"[SLOW] {label}: {elapsed:.3f}s")
        elif elapsed > INFO_THRESHOLD:
            logger.info(f"{label}: {elapsed:.3f}s")
        else:
            logger.debug(f"{label}: {elapsed:.3f}s")


def perf_log(func=None, *, label: str = None, warn_threshold: float = WARN_THRESHOLD):
    """Decorator that logs function execution time.

    Usage:
        @perf_log
        def my_func(): ...

        @perf_log(label="custom_name", warn_threshold=5.0)
        def my_func(): ...
    """
    def decorator(fn):
        fn_label = label or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                if elapsed > warn_threshold:
                    logger.warning(f"[SLOW] {fn_label}: {elapsed:.3f}s")
                elif elapsed > INFO_THRESHOLD:
                    logger.info(f"{fn_label}: {elapsed:.3f}s")
                else:
                    logger.debug(f"{fn_label}: {elapsed:.3f}s")

        return wrapper

    if func is not None:
        return decorator(func)
    return decorator
