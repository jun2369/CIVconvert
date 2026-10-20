"""Background work (converting uploaded PDFs).

Threads inside the web process for now. The rest of the code only calls submit(), so a real
job queue (Celery, RQ, a cloud queue) can take over later without other changes.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .settings import sync_jobs

log = logging.getLogger("civconvert")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="civ-job")


def submit(fn: Callable[..., Any], *args: Any) -> None:
    if sync_jobs():  # tests: run now, inside the request
        _run(fn, *args)
    else:
        _pool.submit(_run, fn, *args)


def _run(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except Exception:
        log.exception("background job %s%s failed", fn.__name__, args)
