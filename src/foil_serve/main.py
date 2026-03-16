import os
import warnings
import gc
import tempfile
import time
from datetime import datetime
import asyncio
import logging
from typing import Annotated, Optional
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi_offline import FastAPIOffline
from fastapi import FastAPI, Body, Depends, Query, Security, Request, HTTPException
from fastapi.responses import Response
from PIL.Image import Image

os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
# os.environ["FLAGS_allocator_strategy"] = "naive_best_fit"        # ← returns memory to the system
warnings.filterwarnings("ignore", message="No ccache found", category=UserWarning)

from schemas import ProcessedDocument, Metadata
from utils import (
    batch_pil_to_b64,
    build_tar_zst,
    check_file_size,
    check_zip_uncompressed_size,
    excel2txt,
    prepare_input_file,
    image_to_pdf,
    UnsupportedMimeTypeError,
    MimeExt
)
from debug import ArtifactContext
from libreoffice import LibreOfficeServer, convert_to_pdf
from pipeline import PaddlePipelineWrapper
from vlm import describe_image_sem
from postprocessing import extract_raw_ocr, prune_tables, reformat_md
from security import verify_api_key
from settings import validate_endpoint, settings, vlm_registry, AsyncOpenAIWithInfo


def _setup_logging() -> logging.FileHandler | None:
    level = logging.getLevelName(settings.log_level)

    root = logging.getLogger()
    root.setLevel(level)

    # Add StreamHandler only if none exists yet (avoids duplicates on --reload)
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)-8s %(name)s - %(message)s"))
        root.addHandler(sh)

    fh = None
    if settings.log_file and not any(
        isinstance(h, logging.FileHandler) for h in root.handlers
    ):
        fh = logging.FileHandler(settings.log_file)
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s - %(message)s")
        )
        root.addHandler(fh)

    # Silence noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return fh


_log_file_handler = _setup_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # ── Forward uvicorn logs to file (uvicorn uses propagate=False) ───────────
    if _log_file_handler:
        for _name in ("uvicorn", "uvicorn.access"):
            _uvicorn_logger = logging.getLogger(_name)
            if _log_file_handler not in _uvicorn_logger.handlers:
                _uvicorn_logger.addHandler(_log_file_handler)

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

    # ── LibreOffice persistent server ─────────────────────────────────────────
    libreoffice_server = LibreOfficeServer()
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
        txt: str
        imgs: dict[str, Image]
        txt, imgs, _ = await pipeline_wrapper.run(test_img.as_posix())
        if "know about magic" not in txt.lower():
            raise RuntimeError(f"Pipeline was executed but OCR was way too bad: {txt=}")
        if not isinstance(next(iter(imgs.values()), None), Image):
            raise RuntimeError(
                f"Pipeline was executed but no image was extracted: {imgs=}"
            )
        del txt, imgs
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


app = FastAPIOffline(
    title="Foil-Serve 🏄‍",
    description=f"""
<div align="center">
  <h3>Document → Markdown conversion server, built on PaddleOCR — with meaningful extras.</h3>
  <p><b>Supported formats:</b> {' : '.join(x.split('.')[1].upper() for x in MimeExt.__args__)}</p>
  <p><b>Extras:</b></p>
  <p>
    - Extracted figures can be described by any OpenAI-compatible VLM — description injected as &lt;figcaption&gt; in the Markdown output.<br>
    - HTML tables are simplified (3–5× token reduction) for LLM/RAG workflows.<br>
    - Writter documents (Open and Word) tracked changes are accepted automatically.
  </p>
</div>
    """,
    lifespan=lifespan,
)


# ── Shared processing logic ───────────────────────────────────────────────────


async def _process_document(
    request: Request,
    file_content: bytes,
    image_description_model_name: Optional[str],
    client: Optional[AsyncOpenAIWithInfo],
    t0_wall: float,
    artifact_ctx: ArtifactContext | None,
) -> tuple[str, dict[str, str], Metadata, str, str]:
    """
    Core document processing pipeline shared by JSON and download endpoints.

    Returns
    -------
    page_content : str
    imgs_b64     : dict[str, str]  — images as base64-encoded JPEG strings
    metadata     : Metadata
    mime_ext     : str             — file extension mapped from MIME (.pdf, .docx, …)
    raw_mime     : str             — raw MIME type string from libmagic
    """
    t_active = 0.0
    wrapper: PaddlePipelineWrapper = request.app.state.pipeline_wrapper

    # ── Global size check — before writing to disk ────────────────────────────
    try:
        _t = time.perf_counter()
        check_file_size(len(file_content), settings.max_file_size_mb, "File")
        t_active += time.perf_counter() - _t
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))

    timestamp = datetime.now().strftime("%m-%d_%H-%M")
    with tempfile.TemporaryDirectory(prefix=f"foil-serve_{timestamp}") as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        # ── Phase 1 : write file + detect MIME (thread, fast) ────────────────
        _t = time.perf_counter()
        try:
            prepared_path, mime, raw_mime = await asyncio.to_thread(
                prepare_input_file, file_content, tmpdir
            )
        except UnsupportedMimeTypeError as e:
            if artifact_ctx is not None:
                artifact_ctx.raw_mime = e.raw_mime
                artifact_ctx.prepared_path = tmpdir / "input_file.bin"
                await artifact_ctx.save(e, t_active=t_active)
            raise HTTPException(status_code=415, detail=str(e))
        if artifact_ctx is not None:
            artifact_ctx.raw_mime = raw_mime
            artifact_ctx.prepared_path = prepared_path
        t_active += time.perf_counter() - _t
        del file_content

        # ── Type-specific size checks (after MIME detection) ──────────────────
        try:
            _t = time.perf_counter()
            _file_size = prepared_path.stat().st_size
            if mime == ".pdf":
                check_file_size(_file_size, settings.max_pdf_file_size_mb, "PDF file")
            elif mime in (".png", ".jpg", ".bmp", ".webp", ".tiff"):
                check_file_size(
                    _file_size, settings.max_image_file_size_mb, "Image file"
                )
            elif mime in (".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp"):
                check_file_size(
                    _file_size, settings.max_office_file_size_mb, "Office file"
                )
                # ZIP-based formats (.docx, .pptx, .odt, .odp): check decompressed size
                # to prevent zip bomb attacks before handing off to LibreOffice.
                if mime in (".docx", ".pptx", ".odt", ".odp"):
                    await asyncio.to_thread(
                        check_zip_uncompressed_size,
                        prepared_path,
                        settings.max_office_file_size_mb * 1024 * 1024,
                    )
            elif mime in (".xls", ".xlsx", ".ods"):
                # Check on-disk (compressed) size — reliable for all spreadsheet formats
                check_file_size(
                    _file_size, settings.max_excel_file_size_mb, "Spreadsheet file"
                )
                # ZIP-based formats (.xlsx, .ods): check actual decompressed size to prevent zip bombs.
                # The ZIP central directory 'file_size' field is not used — it is attacker-controlled.
                if mime in (".xlsx", ".ods"):
                    await asyncio.to_thread(
                        check_zip_uncompressed_size,
                        prepared_path,
                        settings.max_excel_file_size_mb * 1024 * 1024,
                    )
            elif mime in (".txt", ".json", ".csv", ".xml"):
                pass
            else:
                raise NotImplementedError(
                    f"File size check not implemented for this type: {mime}"
                )
            t_active += time.perf_counter() - _t
        except (ValueError, NotImplementedError) as e:
            raise HTTPException(status_code=413, detail=str(e))

        # Short-circuit: plain text — no pipeline needed
        if mime in (".txt", ".json", ".csv", ".xml"):
            _t = time.perf_counter()
            md = prepared_path.read_text()
            t_active += time.perf_counter() - _t
            return (
                md,
                {},
                Metadata(
                    active_conversion_time_no_img_desc=int(t_active),
                    img_desc_time=0,
                    wall_clock_time=int(time.perf_counter() - t0_wall),
                ),
                mime,
                raw_mime,
            )

        # Short-circuit: spreadsheets — semaphored, no pipeline needed
        if mime in (".xls", ".xlsx", ".ods"):
            async with request.app.state.excel_sem:
                _t = time.perf_counter()
                try:
                    md = await asyncio.to_thread(excel2txt, prepared_path)
                except Exception as e:
                    if artifact_ctx is not None:
                        await artifact_ctx.save(e, t_active=t_active)
                    raise HTTPException(
                        status_code=500,
                        detail=f"Spreadsheet conversion error ({mime}): {e}",
                    )
                t_active += time.perf_counter() - _t
            return (
                md,
                {},
                Metadata(
                    active_conversion_time_no_img_desc=int(t_active),
                    img_desc_time=0,
                    wall_clock_time=int(time.perf_counter() - t0_wall),
                ),
                mime,
                raw_mime,
            )

        # ── Phase 2 : conversion to PDF if needed ────────────────────────────
        # TIFF/WebP: converted via Pillow (TIFF may be multipage; WebP not supported by Paddle).
        # Office/ODT/ODP: converted via LibreOffice (semaphored).
        # PDF and direct images (.png, .jpg, .bmp): passed as-is to the pipeline.
        # Semaphore wait is excluded from active time; only the conversion itself counts.
        if mime in (".tiff", ".webp"):
            _t = time.perf_counter()
            try:
                pipeline_input = await asyncio.to_thread(
                    image_to_pdf, prepared_path, tmpdir
                )
            except Exception as e:
                if artifact_ctx is not None:
                    await artifact_ctx.save(e, t_active=t_active)
                raise HTTPException(
                    status_code=500,
                    detail=f"Image to PDF conversion error ({mime}): {e}",
                )
            t_active += time.perf_counter() - _t
        elif mime in (".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp"):
            lo_server: LibreOfficeServer = request.app.state.libreoffice_server
            async with request.app.state.libreoffice_sem:
                _t = time.perf_counter()
                try:
                    pipeline_input = await asyncio.to_thread(
                        convert_to_pdf, prepared_path, mime, lo_server
                    )
                except Exception as e:
                    if artifact_ctx is not None:
                        await artifact_ctx.save(e, t_active=t_active)
                    raise HTTPException(
                        status_code=500,
                        detail=f"Office to PDF conversion error ({mime}): {e}",
                    )
                t_active += time.perf_counter() - _t
        else:
            # Direct pipeline: .pdf, .png, .jpg, .bmp
            pipeline_input = prepared_path

        # Track converted PDF for artifact saving (only when a conversion actually happened)
        if artifact_ctx is not None and pipeline_input != prepared_path:
            artifact_ctx.converted_pdf = pipeline_input

        # ── Phase 3 : pipeline.predict() — serialized in dedicated worker ────
        md_raw: str
        imgs: dict[str, Image]
        pipeline_duration: float
        try:
            md_raw, imgs, pipeline_duration = await wrapper.run(
                pipeline_input.as_posix()
            )
        except Exception as e:
            if artifact_ctx is not None:
                await artifact_ctx.save(e, t_active=t_active)
            raise HTTPException(status_code=500, detail=f"Pipeline error: {e}")
        t_active += pipeline_duration

    # tmpdir cleaned up; worker is done and no longer needs the file.

    # ── Phase 4 : post-processing — parallel threads ──────────────────────────
    # extract_raw_ocr and prune_tables both read md_raw independently.
    _t = time.perf_counter()
    if artifact_ctx is not None:
        artifact_ctx.partial_md = md_raw
    try:
        ocrs, md_full = await asyncio.gather(
            asyncio.to_thread(extract_raw_ocr, md_raw),
            asyncio.to_thread(prune_tables, md_raw),
        )
    except Exception as e:
        if artifact_ctx is not None:
            await artifact_ctx.save(e, t_active=t_active)
        raise HTTPException(
            status_code=500, detail=f"Post-processing error ({mime}): {e}"
        )
    t_active += time.perf_counter() - _t
    del md_raw

    # ── Image filtering + PIL → base64 ───────────────────────────────────────
    if imgs:
        vlm_config = vlm_registry.get(image_description_model_name or "")
        min_size = (
            vlm_config.min_size if vlm_config is not None else settings.image_min_size
        )
        imgs_accepted: dict[str, Image] = {}
        for path, img in imgs.items():
            if img.size[0] >= min_size[0] and img.size[1] >= min_size[1]:
                imgs_accepted[path] = img
            else:
                img.close()
        _t = time.perf_counter()
        imgs_b64: dict[str, str] = await asyncio.to_thread(
            batch_pil_to_b64, imgs_accepted
        )
        t_active += time.perf_counter() - _t
        gc.collect()
    else:
        imgs_b64: dict[str, str] = {}

    # ── Phase 5 : image description (async, external VLM) ────────────────────
    # Not blocked by the pipeline — runs fully async via TaskGroup.
    if image_description_model_name and client and imgs_b64:
        total_description_time: float = 0.0
        descriptions: dict[str, str] = {}
        sem = request.app.state.img_desc_sem.get(image_description_model_name)
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(
                        describe_image_sem(
                            client=client,
                            img_name=img_name,
                            img_b64=img_b64,
                            ocr=ocrs[img_name],
                            semaphore=sem,
                        )
                    )
                    for img_name, img_b64 in imgs_b64.items()
                    if img_name in ocrs.keys()
                ]
            for t in tasks:
                img_name, description, elapsed = t.result()
                descriptions[img_name] = description
                total_description_time += elapsed
            total_description_time = int(total_description_time)
        except ExceptionGroup as e:
            raise HTTPException(
                status_code=502,
                detail=f"Error in image description with {image_description_model_name}:\n{e}",
            )
    else:
        descriptions: None = None
        total_description_time: int = 0

    # ── Phase 6 : reformat Markdown (inject descriptions + OCR into figures) ──
    _t = time.perf_counter()
    md_full = await asyncio.to_thread(reformat_md, md_full, descriptions, ocrs)
    t_active += time.perf_counter() - _t

    return (
        md_full,
        imgs_b64,
        Metadata(
            active_conversion_time_no_img_desc=int(t_active),
            img_desc_time=total_description_time,
            wall_clock_time=int(time.perf_counter() - t0_wall),
        ),
        mime,
        raw_mime,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.post(
    "/v1/process",
    dependencies=[Security(verify_api_key)],
    response_model=ProcessedDocument,
)
async def paddleocr_process(
    request: Request,
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    image_description_model_name: Optional[str] = Query(None),
    client: Annotated[AsyncOpenAIWithInfo | None, Depends(validate_endpoint)] = None,
):
    t0_wall = time.perf_counter()
    artifact_ctx = (
        ArtifactContext(
            artifacts_dir=Path(settings.failed_artifacts_dir),
            t0_wall=t0_wall,
            image_description_model_name=image_description_model_name,
        )
        if settings.save_failed_artifacts
        else None
    )

    page_content, imgs_b64, metadata, _mime_ext, _raw_mime = await _process_document(
        request,
        file_content,
        image_description_model_name,
        client,
        t0_wall,
        artifact_ctx,
    )
    return ProcessedDocument(
        page_content=page_content, images=imgs_b64, metadata=metadata
    )


@app.post(
    "/v1/process/download",
    dependencies=[Security(verify_api_key)],
    response_class=Response,
    responses={
        200: {"content": {"application/zstd": {}}, "description": "tar.zst archive"}
    },
)
async def paddleocr_process_download(
    request: Request,
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    image_description_model_name: Optional[str] = Query(None),
    client: Annotated[AsyncOpenAIWithInfo | None, Depends(validate_endpoint)] = None,
):
    """Process a document and return a downloadable .tar.zst archive containing
    the Markdown output, images, metadata and MIME information."""
    t0_wall = time.perf_counter()
    artifact_ctx = (
        ArtifactContext(
            artifacts_dir=Path(settings.failed_artifacts_dir),
            t0_wall=t0_wall,
            image_description_model_name=image_description_model_name,
        )
        if settings.save_failed_artifacts
        else None
    )

    page_content, imgs_b64, metadata, mime_ext, raw_mime = await _process_document(
        request,
        file_content,
        image_description_model_name,
        client,
        t0_wall,
        artifact_ctx,
    )
    archive = await asyncio.to_thread(
        build_tar_zst, page_content, imgs_b64, metadata, mime_ext, raw_mime
    )
    return Response(
        content=archive,
        media_type="application/zstd",
        headers={"Content-Disposition": 'attachment; filename="result.tar.zst"'},
    )


@app.get(
    "/v1/vlm_models",
    dependencies=[Security(verify_api_key)],
)
def list_models():
    """List available VLM models for (optional) image description"""
    models = sorted(vlm_registry.values(), key=lambda m: m.name.lower())
    return [m.name for m in models]


@app.get("/health")
async def health_check(image_description_model_name: Optional[str] = Query(None)):
    """
    Check if server is up (doesn't check if vllm serving PaddleOCR-VL-1.5 is reachable).
    Optionally also check if vlm endpoints are available.
    """
    if image_description_model_name:
        try:
            if image_description_model_name == "all":
                for model_name in vlm_registry.keys():
                    await validate_endpoint(model_name)
            else:
                await validate_endpoint(image_description_model_name)

        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "status": "error",
                    "message": str(e),
                    "failed_at": image_description_model_name,
                },
            )
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
