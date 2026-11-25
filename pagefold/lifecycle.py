"""Local lifecycle gates and in-request accounting for graceful upgrades.

The HTTP server is thread-per-request.  Counting admitted requests under one
condition variable lets SIGUSR1 stop new writes early while already accepted
rehearsals, seals and downloads continue, and lets SIGTERM wait for that work
before the process exits.
"""

from __future__ import annotations

import threading
import time

from .errors import Problem


class Lifecycle:
    def __init__(self, drain_timeout: float = 30.0):
        self.drain_timeout = drain_timeout
        self._condition = threading.Condition()
        self._write_drain = False
        self._stopping = False
        self._active = 0
        self._active_writes = 0
        self._active_downloads = 0

    def drain_writes(self) -> None:
        """Stop accepting new mutating operations, but preserve reads."""
        with self._condition:
            if not self._write_drain:
                self._write_drain = True
                self._condition.notify_all()

    def begin_shutdown(self) -> None:
        """Reject new work and close the admission gate."""
        with self._condition:
            self._write_drain = True
            self._stopping = True
            self._condition.notify_all()

    @property
    def stopping(self) -> bool:
        with self._condition:
            return self._stopping

    def status(self) -> dict:
        with self._condition:
            if self._stopping:
                mode = "stopping"
            elif self._write_drain:
                mode = "writes_draining"
            else:
                mode = "running"
            return {
                "mode": mode,
                "accepting": not self._stopping,
                "accepting_writes": not self._write_drain and not self._stopping,
                "active_requests": self._active,
                "active_writes": self._active_writes,
                "active_downloads": self._active_downloads,
                "drain_timeout_seconds": self.drain_timeout,
            }

    def admit(self, *, write: bool = False, download: bool = False) -> None:
        with self._condition:
            if self._stopping:
                raise Problem("draining", "Pagefold 正在关闭并排空已有请求，请稍后重连。", 503)
            if write and self._write_drain:
                raise Problem("draining", "Pagefold 已停止接收新操作；已接受的操作会继续完成。",
                              503, headers=(("Retry-After", "30"),))
            self._active += 1
            if write:
                self._active_writes += 1
            if download:
                self._active_downloads += 1

    def finish(self, *, write: bool = False, download: bool = False) -> None:
        with self._condition:
            self._active -= 1
            if write:
                self._active_writes -= 1
            if download:
                self._active_downloads -= 1
            if self._active == 0:
                self._condition.notify_all()

    def wait_for_writes(self, timeout: float | None = None) -> bool:
        return self._wait(lambda: self._active_writes == 0, timeout)

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        return self._wait(lambda: self._active == 0, timeout if timeout is not None else self.drain_timeout)

    def _wait(self, predicate, timeout: float | None) -> bool:
        if timeout is None:
            timeout = self.drain_timeout
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True
