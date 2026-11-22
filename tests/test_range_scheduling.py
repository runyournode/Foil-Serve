"""Page ranges of a document on the pipeline workers: fair and work-conserving.

Real WorkerPool, fake predict (`worker_pool_helpers.fake_predict`) that records on
which worker and when each range ran.
"""

import asyncio
import time
from pathlib import Path

import pypdfium2 as pdfium
import pytest

import pipeline
import worker_pool_helpers as h
from settings import settings
from worker_pool import WorkerPool


def _pdf(path: Path, pages: int) -> str:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(100, 100)
    pdf.save(path)
    pdf.close()
    return str(path)


def _wrapper(pool: WorkerPool) -> pipeline.PaddlePipelineWrapper:
    wrapper = pipeline.PaddlePipelineWrapper.__new__(pipeline.PaddlePipelineWrapper)
    wrapper._pool = pool
    return wrapper


def _ranges(pages: list[str]) -> list[tuple[str, int, int, float, float]]:
    """One (doc, first page, pid, t0, t1) per range."""
    seen = {}
    for page in pages:
        name, pid, t0, t1 = page.split("@")
        doc, index = name.split(":")
        seen.setdefault((doc, t0), (doc, int(index), int(pid), float(t0), float(t1)))
    return sorted(seen.values(), key=lambda r: r[3])


@pytest.fixture(autouse=True)
def fake_predict(monkeypatch):
    monkeypatch.setattr(pipeline, "_worker_predict", h.fake_predict)
    monkeypatch.setattr(settings, "pipeline_chunk_pages", 2)


def test_pages_come_back_in_order(tmp_path):
    async def scenario():
        pool = WorkerPool(3, h.init_ok, name="test")
        await pool.start()
        try:
            pages, _, _ = await _wrapper(pool).run(_pdf(tmp_path / "doc.pdf", 7))
        finally:
            pool.shutdown()
        return pages

    pages = asyncio.run(scenario())
    assert [p.split("@")[0] for p in pages] == [f"doc:{i}" for i in range(7)]


def test_big_document_alone_uses_every_idle_worker(tmp_path):
    async def scenario():
        pool = WorkerPool(3, h.init_ok, name="test")
        await pool.start()
        try:
            pages, _, _ = await _wrapper(pool).run(_pdf(tmp_path / "big.pdf", 6))
        finally:
            pool.shutdown()
        return _ranges(pages)

    ranges = asyncio.run(scenario())
    assert len({pid for _, _, pid, _, _ in ranges}) == 3  # 3 ranges, 3 workers
    first_end = min(t1 for *_, t1 in ranges)
    assert all(t0 < first_end for *_, t0, _ in ranges)  # all started together


def test_one_worker_serves_a_small_document_between_two_ranges(tmp_path):
    big = _pdf(tmp_path / "big.pdf", 6)
    small = _pdf(tmp_path / "small.pdf", 1)

    async def scenario():
        pool = WorkerPool(1, h.init_ok, name="test")
        await pool.start()
        wrapper = _wrapper(pool)
        try:
            big_task = asyncio.create_task(wrapper.run(big))
            await asyncio.sleep(0.05)  # the big document holds the worker
            small_pages, _, _ = await wrapper.run(small)
            big_pages, _, _ = await big_task
        finally:
            pool.shutdown()
        return _ranges(big_pages + small_pages)

    order = [(doc, first) for doc, first, *_ in asyncio.run(scenario())]
    assert order == [("big", 0), ("small", 0), ("big", 2), ("big", 4)]


def test_small_document_waits_one_range_on_a_busy_pool(tmp_path):
    big = _pdf(tmp_path / "big.pdf", 20)  # 10 ranges for 2 workers
    small = _pdf(tmp_path / "small.pdf", 1)

    async def scenario():
        pool = WorkerPool(2, h.init_ok, name="test")
        await pool.start()
        wrapper = _wrapper(pool)
        try:
            big_task = asyncio.create_task(wrapper.run(big))
            await asyncio.sleep(0.05)
            small_pages, _, _ = await wrapper.run(small)
            big_pages, _, _ = await big_task
        finally:
            pool.shutdown()
        return _ranges(big_pages), _ranges(small_pages)

    big_ranges, (small_range,) = asyncio.run(scenario())
    started_before_small = [r for r in big_ranges if r[3] < small_range[3]]
    assert len(started_before_small) <= 3  # 2 already running + at most 1 more


def test_cancelled_document_starts_no_further_range(tmp_path):
    big = _pdf(tmp_path / "big.pdf", 20)

    async def scenario():
        pool = WorkerPool(1, h.init_ok, name="test")
        await pool.start()
        try:
            task = asyncio.create_task(_wrapper(pool).run(big))
            await asyncio.sleep(0.25)  # first range running, second one possibly too
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            t_cancel = time.monotonic()
            # the worker comes back: another document is served after the range in
            # flight (0.15 s), not after the 8-9 ranges left (> 1.2 s)
            pages, _, _ = await _wrapper(pool).run(_pdf(tmp_path / "next.pdf", 1))
            waited = time.monotonic() - t_cancel
        finally:
            pool.shutdown()
        return pages, waited

    pages, waited = asyncio.run(scenario())
    assert pages[0].startswith("next:0@")
    assert waited < 0.6
