"""WorkerPool: dispatch, crash detection, recycling, timeout — with dummy workers.

These tests spawn real processes (the `spawn` start method, as in production) but
never load Paddle.
"""

import asyncio

import psutil
import pytest

import worker_pool_helpers as h
from worker_pool import WorkerCrashed, WorkerPool, WorkerTaskTimeout


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


async def _started(size: int, **kwargs) -> WorkerPool:
    pool = WorkerPool(size, h.init_ok, ("t",), name="test", **kwargs)
    await pool.start()
    return pool


async def _wait_until(predicate, timeout_s: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


def test_parallel_tasks_use_distinct_workers():
    async def scenario():
        pool = await _started(3)
        try:
            pids = await asyncio.gather(
                *(pool.submit(h.pid_after, 0.5) for _ in range(3))
            )
            # three overlapping tasks, three different processes: no slot shared
            assert len(set(pids)) == 3
            # more tasks than workers: they wait for a free slot, none is lost
            more = await asyncio.gather(*(pool.submit(h.echo, i) for i in range(10)))
            assert more == list(range(10))
        finally:
            pool.shutdown()

    _run(scenario())


def test_task_exception_is_reraised_and_worker_kept():
    async def scenario():
        pool = await _started(1)
        try:
            pid = await pool.submit(h.pid_after)
            with pytest.raises(ValueError, match="nope"):
                await pool.submit(h.raise_value_error, "nope")
            # an exception in the task is not a crash: same worker afterwards
            assert await pool.submit(h.pid_after) == pid
        finally:
            pool.shutdown()

    _run(scenario())


def test_unrebuildable_exception_becomes_runtime_error():
    async def scenario():
        pool = await _started(1)
        try:
            with pytest.raises(RuntimeError, match="_Unrebuildable"):
                await pool.submit(h.raise_unrebuildable)
            assert isinstance(await pool.submit(h.pid_after), int)
        finally:
            pool.shutdown()

    _run(scenario())


def test_worker_death_fails_the_task_and_slot_is_replaced():
    async def scenario():
        pool = await _started(1)
        try:
            pid = await pool.submit(h.pid_after)
            with pytest.raises(WorkerCrashed, match="exitcode=7"):
                await pool.submit(h.die)
            # the slot comes back with a new process; nothing hangs
            new_pid = await pool.submit(h.pid_after)
            assert new_pid != pid
        finally:
            pool.shutdown()

    _run(scenario())


def test_crash_does_not_block_other_workers():
    async def scenario():
        pool = await _started(2)
        try:
            results = await asyncio.gather(
                pool.submit(h.die),
                pool.submit(h.pid_after, 0.3),
                return_exceptions=True,
            )
            assert isinstance(results[0], WorkerCrashed)
            assert isinstance(results[1], int)
        finally:
            pool.shutdown()

    _run(scenario())


def test_timeout_kills_worker():
    async def scenario():
        pool = await _started(1, task_timeout_s=1)
        try:
            pid = await pool.submit(h.pid_after)
            with pytest.raises(WorkerTaskTimeout):
                await pool.submit(h.pid_after, 30)
            assert await pool.submit(h.pid_after) != pid
        finally:
            pool.shutdown()

    _run(scenario())


def test_recycle_after_max_tasks():
    async def scenario():
        pool = await _started(1, max_tasks=2)
        try:
            p1 = await pool.submit(h.pid_after)
            p2 = await pool.submit(h.pid_after)
            p3 = await pool.submit(h.pid_after)  # after 2 tasks the worker is replaced
            assert p1 == p2 != p3
        finally:
            pool.shutdown()

    _run(scenario())


def test_recycle_on_rss_threshold():
    async def scenario():
        pool = await _started(1, max_rss_mb=150)
        try:
            p1 = await pool.submit(h.grow_rss, 10)
            p2 = await pool.submit(h.pid_after)
            assert p1 == p2  # still below the threshold
            await pool.submit(h.grow_rss, 200)  # crosses it
            assert await pool.submit(h.pid_after) != p1
        finally:
            pool.shutdown()

    _run(scenario())


def test_staggered_recycling_keeps_one_worker_serving():
    async def scenario():
        # every task makes its worker due for recycling
        pool = await _started(2, max_tasks=1)
        try:
            await asyncio.gather(
                pool.submit(h.pid_after, 0.2), pool.submit(h.pid_after, 0.2)
            )
            # both are due, but only one may reload: the other keeps serving
            assert pool._reloading <= 1
            await _wait_until(
                lambda: pool._idle is not None and pool._idle.qsize() >= 1
            )
            assert isinstance(await pool.submit(h.pid_after), int)
        finally:
            pool.shutdown()

    _run(scenario())


def test_cancelled_caller_does_not_release_a_busy_slot():
    async def scenario():
        pool = await _started(1)
        try:
            task = asyncio.create_task(pool.submit(h.pid_after, 1.0))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # the slot is still busy with the cancelled task: it must not be lent out
            assert pool._idle is not None and pool._idle.qsize() == 0
            # it comes back once the worker answered, and serves the next task correctly
            assert await pool.submit(h.echo, "next") == "next"
        finally:
            pool.shutdown()

    _run(scenario())


def test_init_failure_aborts_start():
    async def scenario():
        pool = WorkerPool(1, h.init_fail, name="test")
        with pytest.raises(WorkerCrashed, match="init boom"):
            await pool.start()

    _run(scenario())


def test_init_crash_aborts_start():
    async def scenario():
        pool = WorkerPool(1, h.init_crash, name="test")
        with pytest.raises(WorkerCrashed, match="exitcode=3"):
            await pool.start()

    _run(scenario())


def test_smoke_check_runs_once_at_start():
    async def scenario():
        seen: list[int] = []
        # max_tasks=1: every task respawns its worker — respawns are not re-tested
        pool = WorkerPool(2, h.init_ok, max_tasks=1, name="test")
        await pool.start(smoke=(h.pid_after, (), seen.append))
        try:
            assert len(seen) == 1
            await asyncio.gather(*(pool.submit(h.pid_after) for _ in range(4)))
            assert len(seen) == 1
        finally:
            pool.shutdown()

    _run(scenario())


def test_cancelled_caller_failure_is_retrieved():
    async def scenario():
        pool = WorkerPool(1, h.init_ok, name="test")
        await pool.start()
        loop = asyncio.get_running_loop()
        unretrieved: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: unretrieved.append(ctx))
        try:
            caller = asyncio.create_task(pool.submit(h.raise_after, 0.3, "late"))
            await asyncio.sleep(0.05)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            # the worker still answers: the slot comes back once the reply is read
            assert await pool.submit(h.echo, 1) == 1
            import gc

            gc.collect()
            await asyncio.sleep(0)
            assert not unretrieved
        finally:
            pool.shutdown()

    _run(scenario())


def test_failed_smoke_check_aborts_start():
    async def scenario():
        def check(_):
            raise RuntimeError("bad output")

        pool = WorkerPool(1, h.init_ok, name="test")
        with pytest.raises(RuntimeError, match="bad output"):
            await pool.start(smoke=(h.pid_after, (), check))

    _run(scenario())


def test_submit_before_start_raises():
    async def scenario():
        pool = WorkerPool(1, h.init_ok, name="test")
        with pytest.raises(RuntimeError, match="not running"):
            await pool.submit(h.echo, 1)

    _run(scenario())


def _spawned_children() -> list[psutil.Process]:
    return [
        c for c in psutil.Process().children() if "spawn_main" in " ".join(c.cmdline())
    ]


def test_shutdown_during_respawn_does_not_fail():
    async def scenario():
        # every task makes its worker due: the slot respawns right after the reply
        pool = WorkerPool(1, h.init_slow, (0.5,), max_tasks=1, name="test")
        await pool.start()
        await pool.submit(h.pid_after)
        await asyncio.sleep(0.2)  # the respawn thread is stopping / starting the worker
        # shutdown() and the respawn thread touch the same slot: no double close
        pool.shutdown(wait=False)
        await asyncio.sleep(1.0)

    _run(scenario())
    assert not _spawned_children()


def test_spawn_finishing_after_shutdown_stops_its_worker():
    # The respawn thread can create a worker after shutdown() emptied the slots:
    # that worker must not survive, unseen by the pool.
    pool = WorkerPool(1, h.init_ok, name="test")
    pool._closed = True
    with pytest.raises(WorkerCrashed, match="shut down"):
        pool._spawn(pool._slots[0])
    assert pool._slots[0].process is None
    assert not _spawned_children()
