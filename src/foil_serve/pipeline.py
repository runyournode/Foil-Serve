import asyncio
import contextlib
import logging
import multiprocessing
import multiprocessing.pool
import os
import time
from collections.abc import Generator

from PIL.Image import Image
from paddleocr import PaddleOCRVL
from paddlex.inference.pipelines.paddleocr_vl.result import PaddleOCRVLResult

from settings import settings, setup_logging

logger = logging.getLogger(__name__)


# --------------------------------------------------------
# Worker process for PaddlePipelineWrapper
# --------------------------------------------------------

# Global worker variables — initialized only once per process via _init_worker
_worker_pipeline: PaddleOCRVL | None = None


def _init_worker(config_path: str) -> None:
    global _worker_pipeline
    # os.environ["FLAGS_allocator_strategy"] = "naive_best_fit"

    # Worker is a spawned process — configure logging independently
    setup_logging()

    _worker_pipeline = PaddleOCRVL(paddlex_config=config_path)
    logger.info(f"[WORKER: pid={os.getpid()}] Pipeline initialized")


def _worker_predict(
    file_path: str, use_ocr_for_image_block: bool = True
) -> tuple[str, dict[str, Image], float]:
    """
    Worker process entry point — only pipeline.predict() and image extraction.

    Pre-processing (MIME detection, LibreOffice conversion) and post-processing
    (extract_raw_ocr, prune_tables) are handled in the main process via asyncio.to_thread,
    so they don't consume maxtasksperchild slots and can run in parallel across requests.

    Parameters
    ----------
    use_ocr_for_image_block : bool
        Passed to pipeline.predict(). When False, the pipeline skips OCR
        inside image blocks (performance optimization when OCR output is not needed).
    """
    t0_worker = time.perf_counter()
    mds: list[str] = []
    imgs: dict[str, Image] = {}

    # Retry on vLLM "Already borrowed" race condition.
    # SUSPECTED Root cause: PaddleOCR's _parallel.py sends all pages of a document concurrently
    # to vLLM, which triggers a race condition in vLLM's block allocator (400 "Already
    # borrowed"). The error is transient: retrying the full predict() call usually
    # succeeds because vLLM has finished settling its block state by then.
    # Preferred fix: set max-num-seqs: 1 in the vLLM config (serializes requests
    # server-side without touching this code). Enable this block only as a fallback.
    #
    # _ALREADY_BORROWED_MAX_RETRIES = 3
    # _ALREADY_BORROWED_DELAY_S = 2.0
    # for _attempt in range(1, _ALREADY_BORROWED_MAX_RETRIES + 1):
    #     try:
    #         output = _worker_pipeline.predict(file_path)
    #         break
    #     except RuntimeError as _exc:
    #         if "Already borrowed" not in str(_exc):
    #             raise
    #         if _attempt >= _ALREADY_BORROWED_MAX_RETRIES:
    #             logger.error(
    #                 f"[WORKER: pid={os.getpid()}] 'Already borrowed' still failing "
    #                 f"after {_ALREADY_BORROWED_MAX_RETRIES} retries — giving up"
    #             )
    #             raise
    #         logger.warning(
    #             f"[WORKER: pid={os.getpid()}] vLLM 'Already borrowed' on attempt "
    #             f"{_attempt}/{_ALREADY_BORROWED_MAX_RETRIES}, retrying in "
    #             f"{_ALREADY_BORROWED_DELAY_S}s…"
    #         )
    #         time.sleep(_ALREADY_BORROWED_DELAY_S)

    assert (
        _worker_pipeline is not None
    )  # set by _init_worker before the pool submits any task
    output = _worker_pipeline.predict(
        file_path, use_ocr_for_image_block=use_ocr_for_image_block
    )
    ctx = (
        contextlib.closing(output)
        if isinstance(output, Generator)
        else contextlib.nullcontext(output)
    )

    page_count = 0
    with ctx as pages:
        assert pages is not None  # predict() always returns a generator or iterable
        paddle_page: PaddleOCRVLResult
        for paddle_page in pages:
            page_count += 1
            paddle_page_md: dict = paddle_page.markdown
            md: str = paddle_page_md.get("markdown_texts", "")
            if md:
                mds.append(md)
            img: dict[str, Image] = paddle_page_md.get("markdown_images", {})
            for img_name, pil_img in img.items():
                imgs[img_name] = pil_img.copy()  # detached from PaddleX buffers
                pil_img.close()
            img.clear()
            del img
            paddle_page.clear()
            del paddle_page
            # gc.collect()

    # If the pipeline yielded no pages at all, it silently rejected the input
    # (e.g. unsupported file type). Raise so the caller gets a proper error
    # instead of an empty 200 response.
    if page_count == 0:
        raise RuntimeError(
            f"Pipeline produced no output for '{file_path}' — "
            "unsupported file type or corrupted file"
        )

    return "  \n".join(mds), imgs, time.perf_counter() - t0_worker


class PaddlePipelineWrapper:
    """
    Serializes PaddleOCR calls in a dedicated single worker process.

    Context
    -------
    PaddleOCR is not thread-safe. Previous tests showed that:

    - asyncio.to_thread with concurrent requests  → memory leak (progressive OOM)
    - direct blocking call in the event loop      → freezes all FastAPI requests
    - home-made thread worker with queue.Queue    → seems to work (not tested in hpc) but:
        * queue.put() blocks the event loop if the queue is full
        * manual loop and future management is fragile
    - ThreadPoolExecutor(max_workers=1)           → works but residual memory drift:
        * PaddlePaddle's native C++ pool accumulates Python weakrefs (PaddleX internals)
        * malloc_trim(0) reclaims native memory but not PaddleX Python objects

    Chosen solution
    ---------------
    multiprocessing.Pool(processes=1, maxtasksperchild=N) with apply_async :

    - processes=1       → only one document processed at a time, thread-safety guarantee
    - maxtasksperchild  → recycles the worker after N documents; the OS reclaims all
                          native AND Python memory without exception (including
                          PaddleX weakrefs and PaddlePaddle C++ pool)
    - spawn context     → worker starts cleanly without inheriting parent state
    - apply_async       → the coroutine is suspended (await fut) without blocking the event
                          loop nor requiring an intermediate thread
    - Additional requests accumulate in the Pool's internal queue automatically

    Only pipeline.predict() calls count toward maxtasksperchild. Pre-processing
    (MIME detection, LibreOffice, Excel) and post-processing (OCR extraction,
    table pruning) run in the main process via asyncio.to_thread and can therefore
    execute in parallel across concurrent requests.

    """

    MAXTASKS: int = settings.max_tasks_between_pipeline_reload

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path
        self._pool: multiprocessing.pool.Pool = self._make_pool()

    def _make_pool(self) -> multiprocessing.pool.Pool:
        ctx = multiprocessing.get_context("spawn")
        return ctx.Pool(
            processes=1,
            initializer=_init_worker,
            initargs=(self._config_path,),
            maxtasksperchild=self.MAXTASKS,
        )

    async def run(
        self, file_path: str, use_ocr_for_image_block: bool = True
    ) -> tuple[str, dict[str, Image], float]:
        """
        Runs pipeline.predict() in the dedicated worker process for the given file path.

        The tmpdir holding file_path must remain alive in the caller until this coroutine
        returns. The calling coroutine is suspended (await) without blocking the event loop.
        Concurrent calls are automatically queued by the Pool.

        The worker is automatically recycled after MAXTASKS calls (maxtasksperchild):
        the OS reclaims all native memory, the new worker reloads the model via _init_worker.

        Parameters
        ----------
        file_path: str
        use_ocr_for_image_block : bool
            When False, the pipeline skips OCR inside image blocks.

        Returns
        -------
        md_raw : str
            Raw Markdown from paddle (pages joined, before prune_tables).
        imgs : dict[str, Image]
            Extracted images as PIL Images {name: PIL.Image}.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def _on_success(result):
            loop.call_soon_threadsafe(fut.set_result, result)

        def _on_error(exc):
            loop.call_soon_threadsafe(fut.set_exception, exc)

        self._pool.apply_async(
            _worker_predict,
            args=(file_path, use_ocr_for_image_block),
            callback=_on_success,
            error_callback=_on_error,
        )
        return await fut

    def shutdown(self, wait: bool = True) -> None:
        """
        Clean shutdown of the Pool.

        wait=True  : waits for the currently processing document to finish before
                     stopping (recommended in lifespan to avoid corruption).
        wait=False : immediate return, the worker is killed immediately.
        """
        if wait:
            self._pool.close()
            self._pool.join()
        else:
            self._pool.terminate()
