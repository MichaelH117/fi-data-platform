"""Background file writing for the publish and reports steps (runtime, docs/V3_DESIGN.md 8.4).

Writing Excel files (openpyxl, pure Python) was most of the reports step (about 180 of 210 seconds) and a
large share of publish (one packet workbook per dealer), one file after another on one core. The files are
independent, so inside ``with workbook_pool():`` the writers hand their (already computed) frames to a
process pool and return at once; leaving the block waits for every file and re-raises the first error.
The content written is the same: only the order changes.

Outside a ``with workbook_pool():`` block (tests, notebooks, a plug-in called directly) ``run`` simply calls
the writer, so nothing is ever left half-written. ``WORKPOOL_WORKERS=1`` turns the pool off.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import contextmanager
from typing import Any

_pool: ProcessPoolExecutor | None = None
_pending: list[Future] = []


def workers() -> int:
    try:
        n = int(os.environ.get("WORKPOOL_WORKERS", "0"))
    except ValueError:
        n = 0
    return n if n > 0 else max(1, min(6, (os.cpu_count() or 2) - 2))


def run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Call ``fn(*args, **kwargs)`` now, or in the pool when one is open (``fn`` must be importable)."""
    if _pool is None:
        fn(*args, **kwargs)
    else:
        _pending.append(_pool.submit(fn, *args, **kwargs))


def wait() -> int:
    """Block until every submitted file is written; re-raises the first writer error. Returns the count."""
    done = 0
    while _pending:
        _pending.pop(0).result()
        done += 1
    return done


@contextmanager
def workbook_pool(n: int | None = None) -> Iterator[None]:
    global _pool
    n = workers() if n is None else n
    if n <= 1 or _pool is not None:
        yield
        return
    _pool = ProcessPoolExecutor(max_workers=n)
    try:
        yield
        wait()
    finally:
        pool, _pool = _pool, None
        _pending.clear()
        pool.shutdown(wait=True, cancel_futures=True)
