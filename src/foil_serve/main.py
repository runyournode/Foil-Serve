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
# requests hardcodes chardet < 6.0.0 but paddlex requires chardet 7.x — both work fine together
warnings.filterwarnings(
    "ignore", message="urllib3.*chardet.*doesn't match", category=Warning
)

from schemas import ProcessedDocument, Metadata
from spreadsheet import excel2txt, EmptySpreadsheetError
from utils import (
    batch_pil_to_b64,
    build_tar_zst,
    check_file_size,
    check_zip_uncompressed_size,
    prepare_input_file,
    read_text_smart,
    image_to_pdf,
    UnsupportedMimeTypeError,
    MimeExt,
)
from debug import ArtifactContext, save_table_conversion_artifacts
from libreoffice import LibreOfficeServer, convert_to_pdf
from pipeline import PaddlePipelineWrapper
from vlm import describe_image_sem
from postprocessing import extract_raw_ocr, prune_tables, reformat_md
from security import verify_api_key
from settings import validate_endpoint, settings, vlm_registry, AsyncOpenAIWithInfo, setup_logging, align_uvicorn_logging


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
  <p><b>Supported formats:</b> {" : ".join(x.split(".")[1].upper() for x in MimeExt.__args__)}</p>
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
    artifact_ctx = (
        ArtifactContext(
            artifacts_dir=Path(settings.artifact_dir) / settings.failed_artifacts_subdir,
            t0_wall=t0_wall,
            image_description_model_name=image_description_model_name,
        )
        if settings.save_failed_artifacts
        else None
    )
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
    with tempfile.TemporaryDirectory(prefix=f"foil-serve_{timestamp}_", dir=settings.temp_dir) as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        # ── Phase 1 : write file + detect MIME (thread, fast) ────────────────
        _t = time.perf_counter()
        try:
            prepared_path, mime, raw_mime = await asyncio.to_thread(
                prepare_input_file, file_content=file_content, tmpdir=tmpdir
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
                # ZIP-based formats (.docx, .pptx, .odt, .odp): two-phase zip bomb check
                if mime in (".docx", ".pptx", ".odt", ".odp"):
                    await asyncio.to_thread(
                        check_zip_uncompressed_size,
                        path=prepared_path,
                        max_bytes=settings.max_unzip_size_mb * 1024**2,
                    )
            elif mime in (".xls", ".xlsx", ".ods"):
                # Check on-disk (compressed) size — reliable for all spreadsheet formats
                check_file_size(
                    _file_size, settings.max_excel_file_size_mb, "Spreadsheet file"
                )
                # ZIP-based formats (.xlsx, .ods): two-phase zip bomb check
                if mime in (".xlsx", ".ods"):
                    await asyncio.to_thread(
                        check_zip_uncompressed_size,
                        path=prepared_path,
                        max_bytes=settings.max_unzip_size_mb * 1024**2,
                    )
            elif mime in (".txt", ".json", ".csv", ".xml", ".md"):
                pass
            else:
                raise NotImplementedError(
                    f"File size check not implemented for this type: {mime}"
                )
            t_active += time.perf_counter() - _t
        except (ValueError, NotImplementedError) as e:
            raise HTTPException(status_code=413, detail=str(e))

        # Short-circuit: plain text — no pipeline needed
        if mime in (".txt", ".json", ".csv", ".xml", ".md"):
            _t = time.perf_counter()
            md = await asyncio.to_thread(read_text_smart, path=prepared_path)
            t_active += time.perf_counter() - _t
            if not md.strip():
                raise HTTPException(
                    status_code=422,
                    detail=f"File is empty or contains only whitespace ({mime})",
                )
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

        # Spreadsheets — try pandas first, fallback to PDF+Paddle if sparse
        fallback_to_pdf = False
        if mime in (".xls", ".xlsx", ".ods"):
            async with request.app.state.excel_sem:
                _t = time.perf_counter()
                try:
                    md, pre_clean_md_bytes = await asyncio.to_thread(
                        excel2txt,
                        path=prepared_path,
                        table_format=settings.excel_output_format,
                        raw_mime=mime,
                        lo_server=request.app.state.libreoffice_server,
                    )
                except EmptySpreadsheetError:
                    if settings.excel_pdf_fallback_enabled:
                        logger.warning(
                            "Empty spreadsheet detected (%s) — falling back to PDF+OCR pipeline",
                            mime,
                        )
                        fallback_to_pdf = True
                    else:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Spreadsheet is empty (no cell data found) and PDF fallback is disabled ({mime})",
                        )
                except Exception as e:
                    if artifact_ctx is not None:
                        await artifact_ctx.save(e, t_active=t_active)
                    raise HTTPException(
                        status_code=500,
                        detail=f"Spreadsheet conversion error ({mime}): {e}",
                    )
                t_active += time.perf_counter() - _t

            if not fallback_to_pdf:
                md_bytes = len(md.encode("utf-8"))

                # Guard: reject if output is disproportionately large (bloated / NaN-filled)
                if md_bytes > _file_size * settings.excel_max_output_ratio:
                    ratio = md_bytes / _file_size
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Spreadsheet output too large: {md_bytes / 1e6:.0f} MB markdown "
                            f"from {_file_size / 1e6:.0f} MB input "
                            f"(ratio {ratio:.1f}x, max {settings.excel_max_output_ratio}x)"
                        ),
                    )

                # Detect sparse spreadsheets (text boxes / images only, no real cell data).
                # Use pre-clean Markdown size (before error masking and empty-row stripping)
                # so that files heavy with NaN/errors are not incorrectly treated as sparse:
                # a NaN-filled file has real cell data and should not fall back to PDF+OCR.
                if (
                    settings.excel_pdf_fallback_enabled
                    and _file_size >= settings.excel_min_input_for_fallback_mb * 1024 * 1024
                    and pre_clean_md_bytes < _file_size * settings.excel_min_output_ratio
                ):
                    logger.warning(
                        "Sparse spreadsheet detected: %d bytes pre-clean markdown from %d bytes input "
                        "(ratio %.4f, threshold %.4f) — falling back to PDF+OCR pipeline",
                        pre_clean_md_bytes,
                        _file_size,
                        pre_clean_md_bytes / _file_size,
                        settings.excel_min_output_ratio,
                    )
                    fallback_to_pdf = True
                    del md

            if not fallback_to_pdf:
                if not md.strip():
                    # Pre-clean had content (no EmptySpreadsheetError) but cleaning
                    # erased everything (e.g. all cells were errors and mask_errors=True).
                    # Falling back to PDF would reproduce the same errors via OCR — return
                    # 422 instead so the caller knows the file yielded no usable content.
                    raise HTTPException(
                        status_code=422,
                        detail=f"Spreadsheet produced no content after cell-error masking ({mime})",
                    )
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
                    image_to_pdf, path=prepared_path, output_dir=tmpdir
                )
            except Exception as e:
                if artifact_ctx is not None:
                    await artifact_ctx.save(e, t_active=t_active)
                raise HTTPException(
                    status_code=500,
                    detail=f"Image to PDF conversion error ({mime}): {e}",
                )
            t_active += time.perf_counter() - _t
        elif mime in (
            ".docx",
            ".doc",
            ".pptx",
            ".ppt",
            ".odt",
            ".odp",
            ".xls",
            ".xlsx",
            ".ods",
        ) and (fallback_to_pdf or mime not in (".xls", ".xlsx", ".ods")):
            lo_server: LibreOfficeServer = request.app.state.libreoffice_server
            async with request.app.state.libreoffice_sem:
                _t = time.perf_counter()
                try:
                    pipeline_input = await asyncio.to_thread(
                        convert_to_pdf,
                        file_path=prepared_path,
                        mime=mime,
                        lo_server=lo_server,
                        paper_format=settings.excel_pdf_paper_format if fallback_to_pdf else None,
                    )
                except Exception as e:
                    if artifact_ctx is not None:
                        await artifact_ctx.save(e, t_active=t_active)
                    raise HTTPException(
                        status_code=500,
                        detail=f"{'Spreadsheet' if fallback_to_pdf else 'Office'} to PDF conversion error ({mime}): {e}",
                    )
                t_active += time.perf_counter() - _t

            # Save spreadsheet fallback artifacts if configured
            if fallback_to_pdf and settings.save_table_conversion_artifacts:
                await asyncio.to_thread(
                    save_table_conversion_artifacts,
                    input_path=prepared_path,
                    pdf_path=pipeline_input,
                    artifacts_dir=Path(settings.artifact_dir) / settings.table_conversion_artifacts_subdir,
                    raw_mime=mime,
                )
        else:
            # Direct pipeline: .pdf, .png, .jpg, .bmp
            pipeline_input = prepared_path

        # Track converted PDF for artifact saving (only when a conversion actually happened)
        if artifact_ctx is not None and pipeline_input != prepared_path:
            artifact_ctx.converted_pdf = pipeline_input

        # ── Phase 3 : pipeline.predict() — serialized in dedicated worker ────
        # Determine whether the pipeline should run OCR on image blocks.
        # OCR is needed when: (a) VLM description is requested (OCR feeds the VLM prompt),
        # or (b) OCR output is requested in the final Markdown without VLM.
        has_vlm = image_description_model_name is not None and client is not None
        need_image_ocr = has_vlm or settings.output_paddle_ocr_no_img_desc

        md_raw: str
        imgs: dict[str, Image]
        pipeline_duration: float
        try:
            md_raw, imgs, pipeline_duration = await wrapper.run(
                pipeline_input.as_posix(),
                use_ocr_for_image_block=need_image_ocr,
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
        if need_image_ocr:
            ocrs, md_full = await asyncio.gather(
                asyncio.to_thread(extract_raw_ocr, md_with_html=md_raw),
                asyncio.to_thread(prune_tables, md_with_html=md_raw),
            )
        else:
            ocrs: dict[str, str] = {}
            md_full = await asyncio.to_thread(prune_tables, md_with_html=md_raw)
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
            batch_pil_to_b64, images_dict=imgs_accepted
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
    # Decide whether to include <ocr> tags in the final output
    include_ocr = (
        (has_vlm and settings.output_paddle_ocr)
        or (not has_vlm and settings.output_paddle_ocr_no_img_desc)
    )
    _t = time.perf_counter()
    md_full = await asyncio.to_thread(
        reformat_md,
        md=md_full,
        descriptions_dict=descriptions,
        ocr_dict=ocrs,
        include_ocr=include_ocr,
    )
    t_active += time.perf_counter() - _t

    # Guard: never return empty markdown — pipeline produced nothing useful
    if not md_full.strip():
        raise HTTPException(
            status_code=422,
            detail=f"Processing produced no content ({mime})",
        )

    # Guard: reject if spreadsheet OCR fallback output is disproportionately large
    if fallback_to_pdf and settings.excel_max_output_ratio:
        md_bytes = len(md_full.encode("utf-8"))
        if md_bytes > _file_size * settings.excel_max_output_ratio:
            ratio = md_bytes / _file_size
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Spreadsheet OCR fallback output too large: {md_bytes / 1e6:.0f} MB markdown "
                    f"from {_file_size / 1e6:.0f} MB input "
                    f"(ratio {ratio:.1f}x, max {settings.excel_max_output_ratio}x)"
                ),
            )

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
async def foil_process(
    request: Request,
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    image_description_model_name: Optional[str] = Query(None),
    client: Annotated[AsyncOpenAIWithInfo | None, Depends(validate_endpoint)] = None,
):
    t0_wall = time.perf_counter()
    page_content, imgs_b64, metadata, _mime_ext, _raw_mime = await _process_document(
        request,
        file_content,
        image_description_model_name,
        client,
        t0_wall,
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
async def foil_process_download(
    request: Request,
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    image_description_model_name: Optional[str] = Query(None),
    client: Annotated[AsyncOpenAIWithInfo | None, Depends(validate_endpoint)] = None,
):
    """Process a document and return a downloadable .tar.zst archive containing
    the Markdown output, images, metadata and MIME information."""
    t0_wall = time.perf_counter()
    page_content, imgs_b64, metadata, mime_ext, raw_mime = await _process_document(
        request,
        file_content,
        image_description_model_name,
        client,
        t0_wall,
    )
    archive = await asyncio.to_thread(
        build_tar_zst,
        page_content=page_content,
        imgs_b64=imgs_b64,
        metadata=metadata,
        mime_ext=mime_ext,
        raw_mime=raw_mime,
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
