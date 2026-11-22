"""Log file rotation, written by the API process only (workers send their records)."""

import asyncio
import logging
import logging.handlers
import multiprocessing

import worker_pool_helpers as h
from settings import settings, setup_logging
from worker_pool import WorkerPool


def test_log_file_rotates(tmp_path, monkeypatch):
    log = tmp_path / "foil.log"
    monkeypatch.setattr(settings, "log_file", str(log))
    monkeypatch.setattr(settings, "log_max_mb", 1)
    monkeypatch.setattr(settings, "log_backup_count", 2)
    root = logging.getLogger()
    saved = root.handlers[:]
    for handler in saved:
        root.removeHandler(handler)
    try:
        fh = setup_logging()
        assert isinstance(fh, logging.handlers.RotatingFileHandler)
        line = "x" * 1000
        for _ in range(4000):  # ~4 MB
            logging.getLogger("rot").warning(line)
        fh.flush()
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["foil.log", "foil.log.1", "foil.log.2"]  # 2 backups kept
        assert all(p.stat().st_size <= 1024 * 1024 + 2000 for p in tmp_path.iterdir())
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in saved:
            root.addHandler(handler)


def test_worker_records_reach_the_api_process():
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    queue = multiprocessing.get_context("spawn").Queue()
    listener = logging.handlers.QueueListener(queue, Collect())
    listener.start()

    async def scenario():
        pool = WorkerPool(1, h.init_queue_logging, (queue,), name="test")
        await pool.start()
        try:
            return await pool.submit(h.log_message, "hello from the worker")
        finally:
            pool.shutdown()

    try:
        pid = asyncio.run(scenario())
    finally:
        listener.stop()
    messages = [(r.getMessage(), r.process) for r in records if r.name == "worker_test"]
    assert messages == [("hello from the worker", pid)]
