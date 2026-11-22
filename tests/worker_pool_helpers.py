"""Picklable worker functions for test_worker_pool.py.

Kept in their own light module: `spawn` children re-import it, so it must not pull
in anything heavy (no Paddle, no FastAPI).
"""

import os
import time

_state: dict = {}


def init_ok(tag: str = "") -> None:
    _state["tag"] = tag
    _state["pid"] = os.getpid()


def init_slow(delay_s: float) -> None:
    """Init that takes a while: lets a test act while a worker is starting."""
    time.sleep(delay_s)
    init_ok()


def init_fail() -> None:
    raise ValueError("init boom")


def init_crash() -> None:
    os._exit(3)


def pid_after(delay_s: float = 0.0) -> int:
    """Return the worker pid after `delay_s` (lets tests overlap tasks)."""
    time.sleep(delay_s)
    return os.getpid()


def echo(value):
    return value


def raise_value_error(msg: str) -> None:
    raise ValueError(msg)


class _Unrebuildable(Exception):
    """Pickles fine, but its __init__ needs two args: unpickling fails."""

    def __init__(self, a, b):
        super().__init__(f"{a}-{b}")


def raise_unrebuildable() -> None:
    raise _Unrebuildable(1, 2)


def die() -> None:
    os._exit(7)


def grow_rss(mb: int) -> int:
    """Keep `mb` MB alive in the worker (forces a high RSS)."""
    _state.setdefault("ballast", []).append(bytearray(mb * 2**20))
    for chunk in _state["ballast"]:
        chunk[::4096] = b"\x01" * len(chunk[::4096])  # touch every page
    return os.getpid()


def raise_after(delay_s: float, msg: str) -> None:
    """Fail after `delay_s` (lets a test cancel the caller first)."""
    time.sleep(delay_s)
    raise ValueError(msg)


def fake_predict(file_path: str, start: int, stop: int, use_ocr: bool, max_mp: float):
    """Stand-in for pipeline._worker_predict: each page '<stem>:<page>@<pid>@<t0>@<t1>'."""
    from pathlib import Path

    t0 = time.time()
    time.sleep(0.15)
    t1 = time.time()
    stem = Path(file_path).stem
    pages = [f"{stem}:{i}@{os.getpid()}@{t0}@{t1}" for i in range(start, stop)]
    return pages, {}, t1 - t0


def init_queue_logging(queue) -> None:
    """Worker init: log records go to `queue` (settings.setup_worker_logging)."""
    from settings import setup_worker_logging

    setup_worker_logging(queue)
    init_ok()


def log_message(message: str) -> int:
    import logging

    logging.getLogger("worker_test").warning(message)
    return os.getpid()
