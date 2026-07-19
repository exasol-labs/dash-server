"""Single-process asynchronous coordinator for Phase 1 export jobs."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from threading import Lock


class LocalJobCoordinator:
    def __init__(self, runner: Callable[[str], None], *, max_workers: int) -> None:
        self.runner = runner
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="dash-consumption",
        )
        self._futures: dict[str, Future[None]] = {}
        self._lock = Lock()

    def submit(self, job_id: str) -> None:
        with self._lock:
            current = self._futures.get(job_id)
            if current is not None and not current.done():
                return
            future = self._executor.submit(self.runner, job_id)
            self._futures[job_id] = future
            future.add_done_callback(lambda _future: self._forget(job_id))

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)


__all__ = ["LocalJobCoordinator"]
