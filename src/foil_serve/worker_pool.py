"""A small process pool for non-thread-safe, leaky native workloads (PaddleOCR).

Why not `multiprocessing.Pool`
------------------------------
- A worker killed while it runs a task (OOM killer, segfault in native code) leaves
  the task's `AsyncResult` unresolved forever: the request hangs.
- `maxtasksperchild` recycles on a task count only, and every worker reaches the
  count at about the same time: with N workers the whole pool can reload at once.
- No per-task timeout.

Design
------
- N *slots*, each one a `spawn`ed process talking to the parent over its own `Pipe`.
- A slot is handed out by an `asyncio.Queue` of idle slots: one slot runs exactly
  one task at a time, and goes back to the queue only once its process answered
  (or once its replacement is ready). No two tasks ever share a pipe.
- The blocking wait on a slot runs in a pool-owned thread executor (one thread per
  slot), so neither the event loop nor the default executor is held for the
  duration of a document.
- The wait watches both the pipe and `Process.sentinel`: a dead worker raises
  `WorkerCrashed` at once, and the slot is respawned in the background.
- After each task the worker reports its RSS; the slot is recycled when it ran
  `max_tasks` tasks or its RSS reached `max_rss_mb`. At most N-1 slots reload at the
  same time (N=1: reload inline), so a pool of N>=2 never stops serving.
- A task cancelled on the caller side keeps its slot until the worker answers:
  the slot cannot be handed to another task while a reply is still in flight.

Workers are started one after the other, never concurrently, so the GPU memory
peaks of model loading never stack up.
"""

import asyncio
import concurrent.futures
import logging
import multiprocessing
import os
import pickle
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from typing import Any

import psutil

logger = logging.getLogger(__name__)

# Init errors, task results and task errors share the same pipe.
_READY = "ready"
_INIT_ERROR = "init_error"
_OK = "ok"
_ERR = "err"
_STOP = None  # parent → worker: exit after the current task


class WorkerCrashed(RuntimeError):
    """The worker process died while running the task (or while starting)."""


class WorkerTaskTimeout(TimeoutError):
    """The task exceeded the pool's `task_timeout_s`; its worker was killed."""


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / 2**20


def _worker_main(
    conn: Connection, init_fn: Callable[..., None], init_args: tuple
) -> None:
    """Worker process entry point: init once, then serve tasks until told to stop."""
    try:
        init_fn(*init_args)
    except BaseException as e:  # noqa: BLE001 — reported to the parent, then exit
        conn.send((_INIT_ERROR, f"{e!r}\n{traceback.format_exc()}"))
        return
    conn.send((_READY, os.getpid()))
    while True:
        try:
            msg = conn.recv()
        except EOFError:  # parent is gone
            return
        if msg is _STOP:
            return
        fn, args = msg
        try:
            result = fn(*args)
        except BaseException as e:  # noqa: BLE001 — every task error goes back to the caller
            try:
                # Round-trip first: an exception that pickles but cannot be rebuilt
                # would otherwise fail in the parent, far from its cause.
                pickle.loads(pickle.dumps(e))
            except Exception:
                e = RuntimeError(f"{e!r}")
            conn.send((_ERR, e, _rss_mb()))
            continue
        conn.send((_OK, result, _rss_mb()))


@dataclass(eq=False)
class _Slot:
    index: int
    process: BaseProcess | None = None
    conn: Connection | None = None
    tasks: int = 0  # tasks run by the current process
    rss_mb: float = 0.0
    started_at: float = field(default_factory=time.perf_counter)
    # Guards `process` / `conn`: shutdown() (event loop) and a background respawn
    # (executor thread) may touch the same slot at once.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def detach(self) -> tuple[BaseProcess | None, Connection | None]:
        """Take the slot's process and pipe: the caller alone stops and closes them."""
        with self.lock:
            proc, conn = self.process, self.conn
            self.process, self.conn = None, None
        return proc, conn

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None


class WorkerPool:
    """N spawned worker processes, recycled on task count or RSS, crash-safe.

    Parameters
    ----------
    size : number of worker processes (slots).
    init_fn, init_args : module-level callable run once in each new worker (it must
        be picklable for `spawn`), e.g. loading a model into a global.
    max_tasks : recycle a worker after this many tasks (0 = never).
    max_rss_mb : recycle a worker once its RSS reaches this (0 = never).
    task_timeout_s : kill a worker whose task runs longer (0 = no timeout).
    init_timeout_s : a worker that is not ready after this long is a failed start.
    """

    def __init__(
        self,
        size: int,
        init_fn: Callable[..., None],
        init_args: tuple = (),
        *,
        max_tasks: int = 0,
        max_rss_mb: float = 0,
        task_timeout_s: float = 0,
        init_timeout_s: float = 600,
        name: str = "worker",
    ) -> None:
        if size < 1:
            raise ValueError(f"WorkerPool size must be >= 1, got {size}")
        self.size = size
        self._init_fn = init_fn
        self._init_args = init_args
        self.max_tasks = max_tasks
        self.max_rss_mb = max_rss_mb
        self.task_timeout_s = task_timeout_s
        self.init_timeout_s = init_timeout_s
        self.name = name
        self._ctx = multiprocessing.get_context("spawn")
        self._slots = [_Slot(i) for i in range(size)]
        self._idle: asyncio.Queue[_Slot] | None = None
        # Model loading is serialized: the GPU memory peaks of two starting
        # workers must never stack up.
        self._spawn_lock: asyncio.Lock | None = None
        self._reloading = 0
        self._background: set[asyncio.Task] = set()
        self._closed = False
        # One thread per slot: a slot waits on at most one blocking call at a time.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=size + 1, thread_name_prefix=f"{name}-pool"
        )

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(
        self,
        smoke: tuple[Callable[..., Any], tuple, Callable[[Any], None]] | None = None,
    ) -> None:
        """Start every worker, one after the other.

        `smoke = (fn, args, check)`: run `fn(*args)` once, on the first worker, before
        anything is served; `check(result)` raises to abort the start. It checks the
        environment (model weights, remote services), which every worker shares: the
        other workers and the respawned ones are not tested again.
        """
        self._idle = asyncio.Queue()
        self._spawn_lock = asyncio.Lock()
        try:
            for slot in self._slots:
                await self._run_blocking(self._spawn, slot)
                if smoke is not None and slot.index == 0:
                    fn, args, check = smoke
                    status, payload, slot.rss_mb = await self._run_blocking(
                        self._call, slot, fn, args, self.task_timeout_s
                    )
                    if status == _ERR:
                        raise payload
                    check(payload)
                    logger.info(
                        f"[{self.name} {slot.index}: pid={slot.pid}] smoke test passed "
                        f"(rss={slot.rss_mb:.0f} MB)"
                    )
                self._idle.put_nowait(slot)
        except BaseException:
            self.shutdown(wait=False)
            raise

    def shutdown(self, wait: bool = True) -> None:
        """Stop every worker.

        wait=True: each worker exits after its current task (graceful).
        wait=False: workers are killed at once.
        """
        self._closed = True
        for task in self._background:
            task.cancel()
        for slot in self._slots:
            # A respawn still running in an executor thread finds the slot empty
            # (or, if it spawns afterwards, sees `_closed` and stops its own worker).
            proc, conn = slot.detach()
            _terminate(proc, conn, kill=not wait, graceful_timeout_s=None)
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── task submission ──────────────────────────────────────────────────────

    async def submit(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run `fn(*args)` in a free worker and return its result.

        `fn` must be a picklable module-level callable. Its exceptions are re-raised
        here. Raises `WorkerCrashed` if the worker dies, `WorkerTaskTimeout` on
        timeout. Waits (without blocking the event loop) while every slot is busy.
        """
        if self._idle is None or self._closed:
            raise RuntimeError(f"{self.name} pool is not running")
        slot = await self._idle.get()
        # The task owns the slot until the worker answered, even if the caller is
        # cancelled meanwhile (shield): the slot is never released mid-reply.
        task = asyncio.create_task(self._run_on_slot(slot, fn, args))
        # A cancelled caller never reads the task's outcome: retrieve it here so a
        # failure is not reported as "Task exception was never retrieved".
        task.add_done_callback(_retrieve_exception)
        return await asyncio.shield(task)

    async def _run_on_slot(
        self, slot: _Slot, fn: Callable[..., Any], args: tuple
    ) -> Any:
        try:
            status, payload, rss = await self._run_blocking(
                self._call, slot, fn, args, self.task_timeout_s
            )
        except (WorkerCrashed, WorkerTaskTimeout) as e:
            # the process is already gone from the slot: the message carries its pid
            self._respawn_in_background(slot, reason=str(e))
            raise
        except BaseException:
            # Unexpected parent-side failure: the pipe state is unknown → fresh worker
            self._respawn_in_background(slot, reason="parent-side error")
            raise
        slot.tasks += 1
        slot.rss_mb = rss
        self._release(slot)
        if status == _ERR:
            raise payload
        return payload

    def _release(self, slot: _Slot) -> None:
        """Give a slot that just finished a task back, or recycle it."""
        assert self._idle is not None
        reason = None
        if self.max_tasks and slot.tasks >= self.max_tasks:
            reason = f"{slot.tasks} tasks"
        elif self.max_rss_mb and slot.rss_mb >= self.max_rss_mb:
            reason = f"rss {slot.rss_mb:.0f} MB >= {self.max_rss_mb:.0f} MB"
        # Staggered recycling: keep at least one slot serving when size > 1.
        # A slot that is due but cannot reload now serves again and is re-checked
        # after its next task.
        if reason and (self.size == 1 or self._reloading < self.size - 1):
            self._respawn_in_background(slot, reason=reason)
        else:
            if reason:
                logger.info(
                    f"[{self.name} {slot.index}: pid={slot.pid}] recycle due ({reason}) "
                    f"but {self._reloading} slot(s) already reloading — deferred"
                )
            self._idle.put_nowait(slot)

    # ── respawn ──────────────────────────────────────────────────────────────

    def _respawn_in_background(self, slot: _Slot, reason: str) -> None:
        if self._closed:
            return
        self._reloading += 1
        task = asyncio.create_task(self._respawn(slot, reason))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _respawn(self, slot: _Slot, reason: str) -> None:
        assert self._idle is not None
        old_pid = slot.pid
        logger.info(
            f"[{self.name} {slot.index}: pid={old_pid}] recycling after "
            f"{slot.tasks} task(s), reason: {reason}"
        )
        t0 = time.perf_counter()
        delay = 2.0
        try:
            await self._run_blocking(self._stop_process, slot)
            assert self._spawn_lock is not None
            while not self._closed:
                try:
                    async with self._spawn_lock:
                        await self._run_blocking(self._spawn, slot)
                    break
                except Exception as e:
                    logger.error(
                        f"[{self.name} {slot.index}] restart failed: {e!r} — retrying in {delay:.0f}s"
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)
            else:
                return
            logger.info(
                f"[{self.name} {slot.index}: pid={slot.pid}] replaced "
                f"{f'pid={old_pid}' if old_pid else 'the dead worker'} "
                f"in {time.perf_counter() - t0:.1f}s"
            )
            self._idle.put_nowait(slot)
        finally:
            self._reloading -= 1

    # ── blocking helpers (run in the pool executor) ──────────────────────────

    async def _run_blocking(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _spawn(self, slot: _Slot) -> None:
        """Start a worker in `slot` and wait until its init completed."""
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_worker_main,
            args=(child_conn, self._init_fn, self._init_args),
            name=f"{self.name}-{slot.index}",
            daemon=True,
        )
        t0 = time.perf_counter()
        proc.start()
        child_conn.close()  # the child holds its own end; EOF detection needs this
        with slot.lock:
            closed = self._closed
            if not closed:
                slot.process, slot.conn = proc, parent_conn
                slot.tasks, slot.rss_mb = 0, 0.0
        if closed:  # shutdown() ran while this worker was being created
            _terminate(proc, parent_conn, kill=True)
            raise WorkerCrashed(
                f"{self.name} {slot.index}: pool shut down during start"
            )
        ready = wait([parent_conn, proc.sentinel], timeout=self.init_timeout_s)
        if parent_conn in ready:
            try:
                status, payload = parent_conn.recv()
            except EOFError:  # the process died: its pipe end closed with it
                proc.join(5)
                status = None
                payload = f"exited during init (exitcode={proc.exitcode})"
            if status == _READY:
                slot.started_at = time.perf_counter()
                logger.info(
                    f"[{self.name} {slot.index}: pid={proc.pid}] ready in "
                    f"{time.perf_counter() - t0:.1f}s"
                )
                return
            detail = payload
        elif ready:
            detail = f"exited during init (exitcode={proc.exitcode})"
        else:
            detail = f"not ready after {self.init_timeout_s:.0f}s"
        self._stop_process(slot, kill=True)
        raise WorkerCrashed(f"{self.name} {slot.index} failed to start: {detail}")

    def _call(
        self, slot: _Slot, fn: Callable[..., Any], args: tuple, timeout_s: float
    ) -> tuple[str, Any, float]:
        """Send one task to `slot` and wait for its reply or the worker's death."""
        # Under the slot lock: shutdown() detaches the slot and writes `_STOP` to the
        # same pipe, and two concurrent writers would interleave their bytes.
        with slot.lock:
            proc, conn = slot.process, slot.conn
            if proc is None or conn is None:
                raise WorkerCrashed(f"{self.name} {slot.index} has no running process")
            pid = proc.pid  # read before the process object is closed
            try:
                conn.send((fn, args))
                sent = None
            except OSError as e:  # broken pipe: the worker is already gone
                sent = e
        if sent is not None:
            self._stop_process(slot, kill=True)
            raise WorkerCrashed(
                f"{self.name} {slot.index} (pid={pid}) is gone: {sent!r}"
            ) from sent
        ready = wait([conn, proc.sentinel], timeout=timeout_s or None)
        if conn in ready:  # a reply wins over a simultaneous exit
            try:
                return conn.recv()
            except EOFError:
                pass
        if not ready:
            self._stop_process(slot, kill=True)
            raise WorkerTaskTimeout(
                f"{self.name} {slot.index} (pid={pid}) task exceeded {timeout_s:.0f}s — worker killed"
            )
        proc.join(1)
        exitcode = proc.exitcode
        self._stop_process(slot, kill=True)
        raise WorkerCrashed(
            f"{self.name} {slot.index} (pid={pid}) died during the task (exitcode={exitcode})"
        )

    def _stop_process(self, slot: _Slot, kill: bool = False) -> None:
        proc, conn = slot.detach()
        _terminate(proc, conn, kill=kill)


def _retrieve_exception(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


def _terminate(
    proc: BaseProcess | None,
    conn: Connection | None,
    kill: bool = False,
    graceful_timeout_s: float | None = 30,
) -> None:
    """Stop a detached worker: `_STOP` and wait (graceful), or kill; close its pipe.

    `graceful_timeout_s=None` waits as long as the current task runs.
    """
    if proc is not None:
        if not kill and proc.is_alive() and conn is not None:
            try:
                conn.send(_STOP)
            except OSError:
                pass
            proc.join(graceful_timeout_s)
        if proc.is_alive():
            proc.kill()
        proc.join(10)
        if not proc.is_alive():
            proc.close()
    if conn is not None:
        conn.close()
