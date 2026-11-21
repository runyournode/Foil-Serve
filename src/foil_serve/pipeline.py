"""PaddleOCR-VL pipeline, run in a pool of dedicated worker processes.

Paddle is imported in the workers only: the API process never loads it.
"""

import asyncio
import contextlib
import ctypes
import ctypes.util
import gc
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import psutil

from page_images import iter_pages, page_count
from postprocessing import join_pages, name_by_content
from settings import settings, setup_logging
from utils import EncodedImage
from worker_pool import WorkerCrashed, WorkerPool, WorkerTaskTimeout

if TYPE_CHECKING:
    from paddleocr import PaddleOCRVL

logger = logging.getLogger(__name__)

PipelineOutput = tuple[list[str], dict[str, EncodedImage], float]


class PipelineUnavailable(RuntimeError):
    """The pipeline could not run the document (worker crash or timeout).

    Carries the HTTP status the API should answer with.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------
# Worker process
# --------------------------------------------------------

# Worker globals — set once per process by _init_worker
_worker_pipeline: "PaddleOCRVL | None" = None
_worker_tasks = 0
_worker_malloc_trim: "Callable[[int], int] | None" = None

# vLLM may answer 400 "Already borrowed" when it receives many concurrent requests
# (race in its tokenizer). The error is transient: retrying the document succeeds.
_ALREADY_BORROWED_MAX_ATTEMPTS = 3
_ALREADY_BORROWED_DELAY_S = 2.0


def _glibc_malloc_trim() -> "Callable[[int], int] | None":
    """glibc's malloc_trim, or None when the C library is not glibc (musl, macOS)."""
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    try:
        trim = ctypes.CDLL(name).malloc_trim
    except (OSError, AttributeError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def _release_free_memory() -> float:
    """Hand the heap memory freed by the last document back to the OS.

    Paddle frees what it allocates, but glibc keeps the freed pages in its arenas: the
    RSS would stay at the high-water mark of the largest document seen. Returns the
    seconds spent (0 when disabled).
    """
    if _worker_malloc_trim is None:
        return 0.0
    t0 = time.perf_counter()
    gc.collect()
    _worker_malloc_trim(0)
    return time.perf_counter() - t0


_PROC_STATUS = Path("/proc/self/status")
_PROC_CLEAR_REFS = Path("/proc/self/clear_refs")


def _reset_peak_rss() -> None:
    """Restart the kernel's peak-RSS counter (VmHWM), so it measures one document."""
    try:
        _PROC_CLEAR_REFS.write_text("5")
    except OSError:
        pass  # not Linux, or not allowed: the peak then covers the process lifetime


def _peak_rss_mb() -> float | None:
    try:
        for line in _PROC_STATUS.read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def _init_worker(
    config_path: str, vlm_max_concurrency: int, malloc_trim: bool, vram_limit_mb: int
) -> None:
    """Load the pipeline once in a new worker process."""
    global _worker_pipeline, _worker_malloc_trim
    # Worker is a spawned process — configure logging independently
    setup_logging()
    if malloc_trim:
        _worker_malloc_trim = _glibc_malloc_trim()
        if _worker_malloc_trim is None:
            logger.warning(
                f"[WORKER: pid={os.getpid()}] pipeline_malloc_trim is set but the C "
                "library is not glibc: no effect"
            )

    import paddle

    if vram_limit_mb > 0:
        # Must be set before the first GPU allocation (the models are not loaded yet).
        paddle.set_flags({"FLAGS_gpu_memory_limit_mb": vram_limit_mb})

    from paddleocr import PaddleOCRVL
    from paddlex.inference import load_pipeline_config

    config: dict[str, Any] = load_pipeline_config(config_path)
    if vlm_max_concurrency > 0:
        # Per-worker cap on in-flight VLM requests: N workers x this value is what
        # vLLM receives at most, so it should stay close to vLLM's max_num_seqs.
        genai = config["SubModules"]["VLRecognition"]["genai_config"]
        genai["max_concurrency"] = vlm_max_concurrency
    _worker_pipeline = PaddleOCRVL(paddlex_config=config)
    logger.info(
        f"[WORKER: pid={os.getpid()}] Pipeline initialized "
        f"(rss={psutil.Process().memory_info().rss / 2**20:.0f} MB)"
    )


def _collect_pages(
    pages: list["np.ndarray"], use_ocr_for_image_block: bool
) -> tuple[list[str], dict[str, EncodedImage], int, float]:
    """Run the pipeline on page images; returns (pages, images, crop count, predict s).

    `predict_iter` yields one result per page; each one is turned into Markdown and
    JPEG crops, then released at once (`predict()` would keep every page image of the
    call alive until the end). vLLM may answer 400 "Already borrowed" under load (race
    in its tokenizer): the call is replayed from the start, a few times.
    """
    assert _worker_pipeline is not None  # set by _init_worker before any task
    for attempt in range(1, _ALREADY_BORROWED_MAX_ATTEMPTS + 1):
        t0 = time.perf_counter()
        mds: list[str] = []
        imgs: dict[str, EncodedImage] = {}
        crop_count = 0
        try:
            results = _worker_pipeline.predict_iter(
                pages,
                use_ocr_for_image_block=use_ocr_for_image_block,
                layout_shape_mode="rect",
            )
            # closed explicitly so the pipeline threads stop if we leave early
            with contextlib.closing(results):
                for paddle_page in results:
                    paddle_page_md: dict = paddle_page.markdown
                    md: str = paddle_page_md.get("markdown_texts", "")
                    img: dict = paddle_page_md.get("markdown_images", {})
                    crop_count += len(img)
                    # Content names, page by page: PaddleX box-based names collide
                    # across pages. Encoding here also ships JPEG bytes, not raw
                    # pixels, to the parent.
                    md, encoded = name_by_content(md, img)
                    imgs.update(encoded)
                    # Empty pages are kept: list index == page index (page markers,
                    # spreadsheet sheet → page map).
                    mds.append(md)
                    for pil_img in img.values():
                        pil_img.close()
                    img.clear()
                    paddle_page.clear()
                    del paddle_page, img
        except RuntimeError as e:
            if (
                "Already borrowed" not in str(e)
                or attempt == _ALREADY_BORROWED_MAX_ATTEMPTS
            ):
                raise
            logger.warning(
                f"[WORKER: pid={os.getpid()}] vLLM 'Already borrowed' on attempt "
                f"{attempt}/{_ALREADY_BORROWED_MAX_ATTEMPTS}, retrying in "
                f"{_ALREADY_BORROWED_DELAY_S * attempt:.0f}s"
            )
            time.sleep(_ALREADY_BORROWED_DELAY_S * attempt)
            continue
        return mds, imgs, crop_count, time.perf_counter() - t0
    raise AssertionError("unreachable")


def _worker_predict(
    file_path: str,
    start: int,
    stop: int,
    use_ocr_for_image_block: bool,
    max_page_megapixels: float,
) -> PipelineOutput:
    """
    Worker task — pages [start, stop) of one file: render, pipeline, image extraction.

    The pages are rendered here, in the worker (`page_images.iter_pages`): pixels never
    cross the process boundary. Pre-processing (MIME detection, LibreOffice
    conversion) and post-processing (extract_raw_ocr, prune_tables) run in the API
    process, in parallel across requests.

    Parameters
    ----------
    use_ocr_for_image_block : bool
        Passed to the pipeline. When False, it skips OCR inside image blocks
        (performance optimization when OCR output is not needed).
    max_page_megapixels : float
        Pixel budget per page (0 = none): larger pages are rendered / downscaled to it.
    """
    global _worker_tasks
    _worker_tasks += 1
    proc = psutil.Process()
    rss_before = proc.memory_info().rss / 2**20
    _reset_peak_rss()
    t0 = time.perf_counter()
    pages: list[np.ndarray] = []
    try:
        pages = list(iter_pages(Path(file_path), start, stop, max_page_megapixels))
        if not pages:
            # Nothing to read: unsupported or corrupted file (or an empty range).
            raise RuntimeError(
                f"No page to process in '{file_path}' [{start}:{stop}] — "
                "unsupported file type or corrupted file"
            )
        t_render = time.perf_counter() - t0
        megapixels = sum(page.shape[0] * page.shape[1] for page in pages) / 1e6
        mds, imgs, crop_count, t_predict = _collect_pages(
            pages, use_ocr_for_image_block
        )
    finally:
        pages.clear()
        # Also after a failed document: its buffers were freed all the same.
        trim_s = _release_free_memory()
    elapsed = time.perf_counter() - t0

    peak = _peak_rss_mb()
    logger.info(
        f"[WORKER: pid={os.getpid()}] task #{_worker_tasks}: pages {start}-{stop - 1} "
        f"({len(mds)} page(s), {megapixels:.1f} Mpx), {crop_count} crop(s), "
        f"render {t_render:.2f}s, predict+encode {t_predict:.2f}s, trim {trim_s:.2f}s, "
        f"total {elapsed:.2f}s ({elapsed / len(mds):.2f}s/page), "
        f"rss {rss_before:.0f}→{proc.memory_info().rss / 2**20:.0f} MB "
        f"(peak {f'{peak:.0f}' if peak is not None else '?'} MB), "
        f"threads {threading.active_count()}/{proc.num_threads()} (py/os)"
    )
    # Pages are returned separately (not pre-joined) so the caller can map output
    # line numbers back to PDF pages — the spreadsheet table of contents needs it.
    return mds, imgs, elapsed


def _check_smoke_result(result: PipelineOutput) -> None:
    """Raise unless the startup test image was read correctly."""
    pages, imgs, _ = result
    txt = join_pages(pages)
    if "know about magic" not in txt.lower():
        raise RuntimeError(f"Pipeline was executed but OCR was way too bad: {txt=}")
    if not isinstance(next(iter(imgs.values()), None), EncodedImage):
        raise RuntimeError(f"Pipeline was executed but no image was extracted: {imgs=}")


class PaddlePipelineWrapper:
    """
    Runs PaddleOCR-VL in `pipeline_workers` dedicated worker processes.

    Context
    -------
    PaddleOCR is not thread-safe, and its RSS grows with the largest document seen
    (freed memory kept by glibc, handed back by `pipeline_malloc_trim`). Previous tests
    showed that:

    - asyncio.to_thread with concurrent requests  → memory leak (progressive OOM)
    - direct blocking call in the event loop      → freezes all FastAPI requests
    - ThreadPoolExecutor(max_workers=1)           → works but residual memory drift
    - multiprocessing.Pool(1, maxtasksperchild)   → works, but one document at a time,
      a crashed worker hangs its request forever, and recycling stops the service

    Chosen solution
    ---------------
    `WorkerPool` (worker_pool.py): one pipeline per process, never shared — N
    processes run N documents at once. Each worker is recycled after
    `max_tasks_between_pipeline_reload` documents or once its RSS reaches
    `max_worker_rss_mb`, never all at the same time; a worker that dies or exceeds
    `pipeline_task_timeout_s` fails its request with 503/504 and is replaced.

    The VLM stage is served by vLLM: the worker only runs the layout / orientation
    models and sends the crops to vLLM (at most `pipeline_vlm_max_concurrency`
    requests in flight per worker).
    """

    def __init__(self, config_path: str) -> None:
        self._pool = WorkerPool(
            settings.pipeline_workers,
            _init_worker,
            (
                config_path,
                settings.pipeline_vlm_max_concurrency,
                settings.pipeline_malloc_trim,
                settings.pipeline_worker_vram_limit_mb,
            ),
            max_tasks=settings.max_tasks_between_pipeline_reload,
            max_rss_mb=settings.max_worker_rss_mb,
            task_timeout_s=settings.pipeline_task_timeout_s,
            name="paddle",
        )

    async def start(self, smoke_image: str) -> None:
        """Start the workers one by one; the first one must read `smoke_image` correctly
        (checked once: model weights present, PaddleOCR-VL vLLM reachable)."""
        await self._pool.start(
            smoke=(
                _worker_predict,
                (smoke_image, 0, 1, True, settings.pipeline_max_page_megapixels),
                _check_smoke_result,
            )
        )

    async def run(
        self, file_path: str, use_ocr_for_image_block: bool = True
    ) -> PipelineOutput:
        """
        Runs the pipeline on every page of `file_path` (PDF or image), in free workers.

        A document longer than `pipeline_chunk_pages` is sent in consecutive page
        ranges, one task each, one after the other: each task goes back to the end of
        the queue, so a small document waiting behind a big one gets a worker between
        two ranges, even with a single worker. Pages are independent for the pipeline
        (no cross-page restructuring is used), so the output does not depend on the
        split.

        The tmpdir holding file_path must remain alive in the caller until this coroutine
        returns. The calling coroutine is suspended (await) without blocking the event loop.

        Returns
        -------
        md_pages : list[str]
            Raw Markdown from paddle, one entry per page, in order (before prune_tables).
            Join with "  \\n" to rebuild the whole-document Markdown.
        imgs : dict[str, EncodedImage]
            Extracted images, JPEG-encoded and named after their content (the
            Markdown references use the same names).
        elapsed : float
            Active seconds spent in the workers.

        Raises
        ------
        PipelineUnavailable
            503 when a worker died during the document, 504 on timeout (per range).
        """
        count = await asyncio.to_thread(page_count, Path(file_path))
        chunk = settings.pipeline_chunk_pages or count or 1
        md_pages: list[str] = []
        imgs: dict[str, EncodedImage] = {}
        elapsed = 0.0
        for start in range(0, max(count, 1), chunk):
            stop = min(start + chunk, count) if count else 1
            try:
                pages, chunk_imgs, duration = await self._pool.submit(
                    _worker_predict,
                    file_path,
                    start,
                    stop,
                    use_ocr_for_image_block,
                    settings.pipeline_max_page_megapixels,
                )
            except WorkerCrashed as e:
                raise PipelineUnavailable(str(e), status_code=503) from e
            except WorkerTaskTimeout as e:
                raise PipelineUnavailable(str(e), status_code=504) from e
            md_pages.extend(pages)
            imgs.update(chunk_imgs)  # content names: identical crops collapse
            elapsed += duration
        return md_pages, imgs, elapsed

    def shutdown(self, wait: bool = True) -> None:
        """
        wait=True  : each worker finishes its current document before stopping
                     (recommended in lifespan to avoid corruption).
        wait=False : workers are killed immediately.
        """
        self._pool.shutdown(wait=wait)
