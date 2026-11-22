"""Application assembly: lifespan, shared state, and the router mount.

The HTTP surface lives in `api.py` and the conversion pipeline in
`processing.py` — this module only wires them to their runtime resources.
"""

import os
import warnings
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi_offline import FastAPIOffline
from fastapi import FastAPI

os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
# os.environ["FLAGS_allocator_strategy"] = "naive_best_fit"        # ← returns memory to the system
warnings.filterwarnings("ignore", message="No ccache found", category=UserWarning)
# requests hardcodes chardet < 6.0.0 but paddlex requires chardet 7.x — both work fine together
warnings.filterwarnings(
    "ignore", message="urllib3.*chardet.*doesn't match", category=Warning
)

from admission import UploadBudget, raise_open_files_limit
from api import router
from external import EXTERNAL_MIME_EXT, check_external_processor
from libreoffice import LibreOfficePool
from pipeline import PaddlePipelineWrapper
from settings import (
    validate_endpoint,
    settings,
    vlm_registry,
    external_registry,
    setup_logging,
    align_uvicorn_logging,
)
from utils import MimeExt, mime_def
from vlm import DescriptionCache

_log_file_handler = setup_logging()

logger = logging.getLogger(__name__)

# Each waiting connection holds a file descriptor: a generous limit, never reached first
# (the queue is bounded by the upload budget below).
_fd_before, _fd_after = raise_open_files_limit()
if _fd_after != _fd_before:
    logger.info(f"Open files limit raised from {_fd_before} to {_fd_after}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    align_uvicorn_logging(_log_file_handler)

    # ── Ensure runtime directories exist ─────────────────────────────────────
    Path(settings.temp_dir).mkdir(parents=True, exist_ok=True)
    try:
        Path(settings.artifact_dir).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise OSError(
            f"Cannot create artifact directory '{settings.artifact_dir}': {e}. "
            f"Create it with: sudo mkdir -p {settings.artifact_dir} && sudo chown $(whoami) {settings.artifact_dir}"
        ) from e

    # ── VLM endpoints check ───────────────────────────────────────────────────
    if settings.check_vlm_endpoints:
        for model_name in vlm_registry.keys():
            await validate_endpoint(model_name)

    # Descriptions reused across documents, for byte-identical VLM requests only
    _app.state.vlm_description_cache = (
        DescriptionCache(settings.vlm_description_cache_mb)
        if settings.vlm_description_cache_mb > 0
        else None
    )

    # Semaphores per VLM model
    _app.state.img_desc_sem = {
        model_name: asyncio.Semaphore(model_config.max_concurrent_requests)
        for model_name, model_config in vlm_registry.items()
    }

    # Semaphores for resource-intensive pre-processing steps.
    # asyncio.Semaphore suspends the coroutine (not the thread) when full,
    # so the event loop stays free → /health always responds, /process queues without rejecting.
    _app.state.excel_sem = asyncio.Semaphore(settings.max_concurrent_excel)

    # ── External processors (e.g. video) ──────────────────────────────────────
    # Shared async HTTP client (connection pooling); per-request timeouts come
    # from each processor config, so no default timeout is set here.
    _app.state.external_http_client = httpx.AsyncClient()
    _app.state.external_sem = {
        proc.name: asyncio.Semaphore(proc.max_concurrent_requests)
        for proc in settings.external_processors
    }
    for raw_mime in external_registry:
        if raw_mime in mime_def:
            logger.warning(
                f"MIME type '{raw_mime}' is natively supported but claimed by external "
                f"processor '{external_registry[raw_mime].name}' — external processing takes precedence."
            )
    for proc in settings.external_processors:
        if proc.check_on_startup:
            # Raises RuntimeError (and aborts startup) if the processor is unreachable
            await check_external_processor(_app.state.external_http_client, proc)
            logger.info(f"External processor '{proc.name}' health check passed.")

    # ── LibreOffice instances ─────────────────────────────────────────────────
    # One conversion per instance, each instance restarted after every use.
    libreoffice_pool = LibreOfficePool(
        settings.max_concurrent_libreoffice,
        runtime_dir=settings.temp_dir,
        conversion_timeout_s=settings.libreoffice_conversion_timeout_s,
    )
    await libreoffice_pool.start()
    _app.state.libreoffice_pool = libreoffice_pool

    # ── PaddleOCR pipeline workers ────────────────────────────────────────────
    # Workers start one by one; the first one runs the connection test once (OCR +
    # image extraction on test_pipeline.jpeg: model weights + PaddleOCR-VL vLLM).
    config_path = (Path(__file__).parent / "config" / "pipeline_config.yaml").as_posix()
    pipeline_wrapper = PaddlePipelineWrapper(config_path)
    _app.state.pipeline_wrapper = pipeline_wrapper
    test_img = Path(__file__).parent / "init" / "test_pipeline.jpeg"
    logger.info(
        f"Starting {settings.pipeline_workers} pipeline worker(s), "
        "connection test on test_pipeline.jpeg ..."
    )
    try:
        await pipeline_wrapper.start(test_img.as_posix())
        logger.info("Pipeline connection test passed.")
    except Exception as e:
        logger.error(f"Pipeline connection test FAILED: {e}")
        libreoffice_pool.stop()
        raise RuntimeError(f"Pipeline connection test failed at startup: {e}") from e

    yield

    # ── Clean shutdown ────────────────────────────────────────────────────────
    pipeline_wrapper.shutdown(wait=True)
    libreoffice_pool.stop()
    await _app.state.external_http_client.aclose()


_EXTERNAL_FORMATS = sorted(
    {ext.lstrip(".").upper() for ext in EXTERNAL_MIME_EXT.values()}
)

app = FastAPIOffline(
    title="Foil-Serve 🏄‍",
    description=f"""
<div align="center">
  <h3>Document → Markdown conversion server, built on PaddleOCR — with meaningful extras.</h3>
  <p><b>Supported formats:</b> {" : ".join(x.split(".")[1].upper() for x in MimeExt.__args__)}</p>
  {f"<p><b>External formats:</b> {' : '.join(_EXTERNAL_FORMATS)} (delegated to external processors)</p>" if _EXTERNAL_FORMATS else ""}
  <p><b>Extras:</b></p>
  <p>
    - Extracted figures can be described by any OpenAI-compatible VLM — description injected as &lt;figcaption&gt; in the Markdown output.<br>
    - HTML tables are simplified (3–5× token reduction) for LLM/RAG workflows.<br>
    - Writter documents (Open and Word) tracked changes are accepted automatically.<br>
    - Spreadsheets get a header and a table of contents, and four conversion strategies (auto / pandas / ocr / both).
  </p>
</div>
    """,
    lifespan=lifespan,
)

app.include_router(router)

# Admission: 503 + Retry-After once the uploads in progress would exceed the budget
app.add_middleware(
    UploadBudget,  # ty: ignore[invalid-argument-type]
    max_bytes=settings.max_inflight_upload_mb * 1024 * 1024,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
