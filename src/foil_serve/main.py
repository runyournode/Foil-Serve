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
from PIL.Image import Image

os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
# os.environ["FLAGS_allocator_strategy"] = "naive_best_fit"        # ← returns memory to the system
warnings.filterwarnings("ignore", message="No ccache found", category=UserWarning)
# requests hardcodes chardet < 6.0.0 but paddlex requires chardet 7.x — both work fine together
warnings.filterwarnings(
    "ignore", message="urllib3.*chardet.*doesn't match", category=Warning
)

from api import router
from external import EXTERNAL_MIME_EXT, check_external_processor
from libreoffice import LibreOfficeServer
from pipeline import PaddlePipelineWrapper
from postprocessing import join_pages
from settings import (
    validate_endpoint,
    settings,
    vlm_registry,
    external_registry,
    setup_logging,
    align_uvicorn_logging,
)
from utils import MimeExt, mime_def

_log_file_handler = setup_logging()

logger = logging.getLogger(__name__)


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

    # Semaphores per VLM model
    _app.state.img_desc_sem = {
        model_name: asyncio.Semaphore(model_config.max_concurrent_requests)
        for model_name, model_config in vlm_registry.items()
    }

    # Semaphores for resource-intensive pre-processing steps.
    # asyncio.Semaphore suspends the coroutine (not the thread) when full,
    # so the event loop stays free → /health always responds, /process queues without rejecting.
    _app.state.libreoffice_sem = asyncio.Semaphore(settings.max_concurrent_libreoffice)
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

    # ── LibreOffice persistent server ─────────────────────────────────────────
    libreoffice_server = LibreOfficeServer(runtime_dir=settings.temp_dir)
    await asyncio.to_thread(libreoffice_server.start)
    _app.state.libreoffice_server = libreoffice_server

    # ── PaddleOCR pipeline wrapper ────────────────────────────────────────────
    config_path = (Path(__file__).parent / "config" / "pipeline_config.yaml").as_posix()
    pipeline_wrapper = PaddlePipelineWrapper(config_path)
    _app.state.pipeline_wrapper = pipeline_wrapper

    # ── Pipeline connection test (runs once at startup) ───────────────────────
    test_img = Path(__file__).parent / "init" / "test_pipeline.jpeg"
    logger.info("Testing pipeline connection with test_pipeline.jpeg ...")
    try:
        pages: list[str]
        imgs: dict[str, Image]
        pages, imgs, _ = await pipeline_wrapper.run(test_img.as_posix())
        txt = join_pages(pages)
        if "know about magic" not in txt.lower():
            raise RuntimeError(f"Pipeline was executed but OCR was way too bad: {txt=}")
        if not isinstance(next(iter(imgs.values()), None), Image):
            raise RuntimeError(
                f"Pipeline was executed but no image was extracted: {imgs=}"
            )
        del pages, txt, imgs
        logger.info("Pipeline connection test passed.")
    except Exception as e:
        logger.error(f"Pipeline connection test FAILED: {e}")
        pipeline_wrapper.shutdown(wait=False)
        libreoffice_server.stop()
        raise RuntimeError(f"Pipeline connection test failed at startup: {e}") from e

    yield

    # ── Clean shutdown ────────────────────────────────────────────────────────
    pipeline_wrapper.shutdown(wait=True)
    libreoffice_server.stop()
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
