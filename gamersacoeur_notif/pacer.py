"""Space out alerts: at most one alert every N seconds per Streamlabs account.

Each account (key) gets its own FIFO queue and thread, so a burst of emails
is played one by one on every stream, in arrival order, while different
streamers' alerts don't wait for each other.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable

log = logging.getLogger(__name__)


class Pacer:
    def __init__(self, interval: float):
        self.interval = max(0.0, float(interval))
        self._queues: dict[str, queue.Queue] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def submit(self, key: str, fn: Callable[[], object]) -> Future:
        """Run ``fn`` in ``key``'s queue, at least ``interval`` s after the previous one."""
        fut: Future = Future()
        with self._lock:
            q = self._queues.get(key)
            if q is None:
                q = self._queues[key] = queue.Queue()
                threading.Thread(target=self._run, args=(key, q), daemon=True,
                                 name=f"pacer-{key}").start()
        q.put((fn, fut))
        return fut

    def pending(self, key: str) -> int:
        q = self._queues.get(key)
        return q.unfinished_tasks if q else 0

    def wait_idle(self) -> None:
        """Block until every queue is empty (used by tests and on shutdown)."""
        for q in list(self._queues.values()):
            q.join()

    def _run(self, key: str, q: queue.Queue) -> None:
        while True:
            fn, fut = q.get()
            try:
                last = self._last.get(key)
                if last is not None:
                    delay = last + self.interval - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                self._last[key] = time.monotonic()
                if fut.set_running_or_notify_cancel():
                    try:
                        fut.set_result(fn())
                    except BaseException as exc:  # noqa: BLE001 - keep the queue alive
                        log.exception("Paced job for %s failed", key)
                        fut.set_exception(exc)
            finally:
                q.task_done()
