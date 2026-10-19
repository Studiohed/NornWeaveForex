"""Cross-platform advisory file lock for the live runner.

A lock file that refuses to let two live instances trade the same account at
once. On Windows this uses ``msvcrt.locking``; on POSIX ``fcntl.flock``. Both
are advisory — a process that does not check the lock ignores it — but every
path in this codebase goes through :class:`SlotPool`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

__all__ = ["SlotPool", "SlotPoolUnusable"]

log = logging.getLogger(__name__)

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class SlotPoolUnusable(RuntimeError):
    """The lock file cannot be created or locked."""


class SlotPool:
    """One lock file, one holder, counted acquires for nested calls."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: Any = None
        self._acquired = 0

    def _open(self) -> Any:
        if self._fd is not None:
            return self._fd
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = open(self._path, "a+b")
        except OSError as exc:
            raise SlotPoolUnusable(f"cannot open lock file {self._path}: {exc}") from exc
        return self._fd

    def acquire(self, timeout_s: float = 5.0) -> bool:
        import time
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._acquire_once()
                self._acquired += 1
                return True
            except SlotPoolUnusable:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)

    def _acquire_once(self) -> None:
        fd = self._open()
        if msvcrt is not None:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise SlotPoolUnusable(f"msvcrt.locking failed: {exc}") from exc
        elif fcntl is not None:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SlotPoolUnusable(f"fcntl.flock failed: {exc}") from exc

    def release(self) -> None:
        if self._fd is None or self._acquired == 0:
            return
        self._acquired -= 1
        if self._acquired > 0:
            return
        try:
            if msvcrt is not None:
                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - closing releases the lock anyway
            pass
        self._fd.close()
        self._fd = None

    def describe(self) -> str:
        return f"SlotPool({self._path})"

    def __enter__(self) -> "SlotPool":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()
