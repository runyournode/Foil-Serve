"""Document → Markdown orchestration.

One coroutine, `process_document()`, drives every input type through the phases
described in CLAUDE.md. It owns two things the domain modules deliberately do
not: the mapping from domain errors to HTTP status codes, and the accounting of
*active* processing time (semaphore waits excluded).

Everything reusable lives elsewhere — MIME detection and size limits in
`utils`, spreadsheet rules in `spreadsheet`, per-page Markdown work in
`postprocessing`, VLM fan-out in `vlm`, remote backends in `external`.
"""

import asyncio
import logging
import tempfile
import time
from collections.abc import Collection, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from debug import ArtifactContext, save_table_conversion_artifacts
from external import EXTERNAL_MIME_EXT, merge_external_metadata, process_external
from libreoffice import (
    LibreOfficeServer,
    SheetPageMap,
    convert_to_pdf,
    is_office_mime_ext,
)
from pipeline import PaddlePipelineWrapper
from postprocessing import extract_raw_ocr, join_pages, prune_pages, reformat_pages
from schemas import Metadata, SpreadsheetMode
from settings import AsyncOpenAIWithInfo, external_registry, settings, vlm_registry
from spreadsheet import (
    EmptySpreadsheetError,
    SheetAnchor,
    SpreadsheetConversion,
    assemble_sections,
    build_ocr_body,
    build_spreadsheet_document,
    excel2txt,
    human_size,
    is_sparse,
    merge_sheet_bodies,
    render_figures,
    resolve_strategy,
)
from spreadsheet_drawings import (
    DrawingSurvey,
    ExtractedObjects,
    decode_objects,
    needs_libreoffice,
    render_with_libreoffice,
    survey_drawings,
)
from utils import (
    SPREADSHEET_EXTS,
    TEXT_EXTS,
    ZIP_BASED_EXTS,
    EncodedImage,
    UnsupportedMimeTypeError,
    check_file_size,
    check_input_size,
    check_zip_uncompressed_size,
    image_to_pdf,
    is_native_mime_ext,
    prepare_input_file,
    read_text_smart,
)
from vlm import describe_images

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Timing and error plumbing
# ---------------------------------------------------------------------------


class ActiveTimer:
    """Accumulates active processing time and builds the response Metadata.

    Semaphore waits must not count as active time; call sites get that for free
    by opening `track()` *inside* the `async with semaphore` block. Time spent in
    a step that then fails is still counted — it was really spent, and the
    failure artifact reports it.
    """

    def __init__(self, t0_wall: float) -> None:
        self.t0_wall = t0_wall
        self.active = 0.0

    @contextmanager
    def track(self):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.active += time.perf_counter() - start

    def add(self, seconds: float) -> None:
        """Add a duration measured elsewhere (the pipeline reports its own)."""
        self.active += seconds

    @property
    def wall_clock(self) -> float:
        return time.perf_counter() - self.t0_wall

    def metadata(self, img_desc_time: float = 0.0) -> Metadata:
        return Metadata(
            active_conversion_time_no_img_desc=int(self.active),
            img_desc_time=int(img_desc_time),
            wall_clock_time=int(self.wall_clock),
        )


@dataclass(frozen=True, slots=True)
class ProcessedResult:
    """What `process_document()` hands back to the endpoints."""

    page_content: str
    images: dict[
        str, str
    ]  # base64-encoded JPEG, keyed by the name used in the Markdown
    metadata: Metadata
    mime_ext: str  # extension mapped from the MIME type (.pdf, .docx, …)
    raw_mime: str  # raw MIME type string from libmagic


@dataclass
class _Job:
    """Per-request state threaded through the phases."""

    request: Request
    timer: ActiveTimer
    artifact_ctx: ArtifactContext | None
    mime: str = ""
    raw_mime: str = ""
    file_size: int = 0
    prepared_path: Path = field(default_factory=Path)

    @property
    def state(self):
        """The app-level shared objects (semaphores, servers, pipeline)."""
        return self.request.app.state

    @asynccontextmanager
    async def failing_with(self, detail: str, status: int = 500):
        """Turn an unexpected error into an HTTPException, saving a failure artifact.

        HTTPExceptions raised inside pass through untouched — they already carry
        the right status code.
        """
        try:
            yield
        except HTTPException:
            raise
        except Exception as e:
            await self.save_artifact(e)
            raise HTTPException(status_code=status, detail=f"{detail}: {e}") from e

    async def save_artifact(self, exc: BaseException) -> None:
        if self.artifact_ctx is not None:
            await self.artifact_ctx.save(exc, t_active=self.timer.active)


# ---------------------------------------------------------------------------
#  Phase 1 — write to disk, detect the MIME type, apply size limits
# ---------------------------------------------------------------------------


async def _prepare_input(job: _Job, file_content: bytes, tmpdir: Path) -> None:
    """Write the upload to `tmpdir` and detect its type. Fills `job` in place."""
    with job.timer.track():
        try:
            path, mime, raw_mime = await asyncio.to_thread(
                prepare_input_file,
                file_content=file_content,
                tmpdir=tmpdir,
                extra_mimes=EXTERNAL_MIME_EXT,
            )
        except UnsupportedMimeTypeError as e:
            if job.artifact_ctx is not None:
                job.artifact_ctx.raw_mime = e.raw_mime
                job.artifact_ctx.prepared_path = tmpdir / "input_file.bin"
            await job.save_artifact(e)
            raise HTTPException(status_code=415, detail=str(e))

    job.prepared_path, job.mime, job.raw_mime = path, mime, raw_mime
    if job.artifact_ctx is not None:
        job.artifact_ctx.raw_mime = raw_mime
        job.artifact_ctx.prepared_path = path


async def _apply_size_limits(job: _Job) -> None:
    """Type-specific limits, plus the zip-bomb check for ZIP containers."""
    try:
        with job.timer.track():
            job.file_size = job.prepared_path.stat().st_size
            check_input_size(
                job.mime,
                job.file_size,
                max_pdf_mb=settings.max_pdf_file_size_mb,
                max_image_mb=settings.max_image_file_size_mb,
                max_office_mb=settings.max_office_file_size_mb,
                max_excel_mb=settings.max_excel_file_size_mb,
            )
            if job.mime in ZIP_BASED_EXTS:
                await asyncio.to_thread(
                    check_zip_uncompressed_size,
                    path=job.prepared_path,
                    max_bytes=settings.max_unzip_size_mb * 1024**2,
                )
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(status_code=413, detail=str(e))


# ---------------------------------------------------------------------------
#  Short-circuit branches — no OCR pipeline involved
# ---------------------------------------------------------------------------


async def _run_external(job: _Job, file_content: bytes) -> ProcessedResult:
    """Delegate to a remote foil-compatible backend (e.g. video).

    Fully async, so the event loop stays free; concurrency is bounded by a
    per-processor semaphore whose wait time is excluded from active time.
    """
    cfg = external_registry[job.raw_mime]
    try:
        with job.timer.track():
            check_file_size(
                len(file_content),
                cfg.max_file_size_mb,
                f"{cfg.name.capitalize()} file",
            )
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))

    async with job.state.external_sem[cfg.name]:
        async with job.failing_with(f"External processor '{cfg.name}' error"):
            with job.timer.track():
                page_content, images, metadata = await process_external(
                    http_client=job.state.external_http_client,
                    cfg=cfg,
                    file_content=file_content,
                    raw_mime=job.raw_mime,
                )

    if not page_content.strip():
        raise HTTPException(
            status_code=422,
            detail=f"External processor '{cfg.name}' produced no content ({job.mime})",
        )
    return ProcessedResult(
        page_content=page_content,
        images=images,
        metadata=merge_external_metadata(
            external_metadata=metadata,
            active_time_s=job.timer.active,
            wall_clock_s=job.timer.wall_clock,
        ),
        mime_ext=job.mime,
        raw_mime=job.raw_mime,
    )


async def _read_plain_text(job: _Job) -> ProcessedResult:
    """Plain-text formats need no conversion at all."""
    with job.timer.track():
        md = await asyncio.to_thread(read_text_smart, path=job.prepared_path)
    if not md.strip():
        raise HTTPException(
            status_code=422,
            detail=f"File is empty or contains only whitespace ({job.mime})",
        )
    return ProcessedResult(
        page_content=md,
        images={},
        metadata=job.timer.metadata(),
        mime_ext=job.mime,
        raw_mime=job.raw_mime,
    )


# ---------------------------------------------------------------------------
#  Spreadsheets
# ---------------------------------------------------------------------------


@dataclass
class _SpreadsheetState:
    """What the pandas conversion produced, and whether OCR still has to run."""

    # The pandas section is part of the output.
    keep_pandas: bool = False
    # (sheet, Markdown table) pairs; empty when the file has no cell data.
    tables: tuple[tuple[str, str], ...] = ()
    survey: DrawingSurvey = field(default_factory=DrawingSurvey)
    # Pictures and charts of the pandas section, once decoded.
    objects: ExtractedObjects | None = None
    run_ocr: bool = False
    sheet_page_map: SheetPageMap = None


async def _convert_with_pandas(
    job: _Job, mode: SpreadsheetMode, min_output_ratio: float
) -> _SpreadsheetState:
    """Run the pandas cell extraction and decide whether OCR is still needed.

    The decision table (see CLAUDE.md):
      auto   — falls back to OCR when the file is empty or sparse
      pandas — never falls back; an empty file is a 422
      ocr    — pandas is not run at all
      both   — an empty pandas section is a warning, not a failure

    Pictures and charts count as content: they are surveyed here, cheaply (byte
    counts and drawing XML, nothing decoded), so that a workbook made of pictures
    is neither "empty" nor "sparse" because of them.
    """
    run_pandas, run_ocr = resolve_strategy(mode)
    state = _SpreadsheetState(run_ocr=run_ocr)
    if not run_pandas:
        return state

    conversion: SpreadsheetConversion | None = None
    async with job.state.excel_sem:
        async with job.failing_with(f"Spreadsheet conversion error ({job.mime})"):
            with job.timer.track():
                try:
                    conversion = await asyncio.to_thread(
                        excel2txt,
                        path=job.prepared_path,
                        table_format=settings.table_output_format,
                        raw_mime=job.mime,
                        lo_server=job.state.libreoffice_server,
                    )
                except EmptySpreadsheetError:
                    pass  # no cell data at all, before any cleaning — decided below
        if settings.excel_extract_images:
            with job.timer.track():
                state.survey = await asyncio.to_thread(
                    survey_drawings, path=job.prepared_path, mime=job.mime
                )

    if conversion is None and not state.survey.has_objects:
        # No cell data, no picture, no chart.
        if mode is SpreadsheetMode.BOTH:
            logger.warning(
                f"Empty spreadsheet ({job.mime}) — the pandas section will be empty"
            )
            state.keep_pandas = True
        elif mode is SpreadsheetMode.AUTO and settings.excel_pdf_fallback_enabled:
            logger.warning(
                f"Empty spreadsheet detected ({job.mime}) — falling back to PDF+OCR pipeline"
            )
            state.run_ocr = True
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Spreadsheet is empty (no cell data, picture or chart found) and "
                f"mode '{mode.value}' does not run the OCR conversion ({job.mime})",
            )
        return state

    # Sparse spreadsheets (text boxes / shapes / OLE objects) — `auto` only: an
    # explicit mode is the caller's decision, not ours.
    pre_clean_bytes = conversion.pre_clean_bytes if conversion is not None else 0
    object_bytes = state.survey.object_bytes
    if (
        mode is SpreadsheetMode.AUTO
        and settings.excel_pdf_fallback_enabled
        and is_sparse(
            pre_clean_bytes,
            job.file_size,
            min_input_mb=settings.excel_min_input_for_fallback_mb,
            min_output_ratio=min_output_ratio,
            object_bytes=object_bytes,
        )
    ):
        effective_size = max(job.file_size - object_bytes, 1)
        logger.warning(
            f"Sparse spreadsheet detected: {pre_clean_bytes} bytes pre-clean markdown "
            f"from {effective_size} bytes input ({job.file_size} bytes, "
            f"{object_bytes} of which are pictures/charts; "
            f"ratio {pre_clean_bytes / effective_size:.4f}, "
            f"threshold {min_output_ratio:.4f}) — falling back to PDF+OCR pipeline"
        )
        state.run_ocr = True
        state.survey = DrawingSurvey()
        return state

    state.keep_pandas = True
    state.tables = conversion.sheets if conversion is not None else ()
    return state


async def _extract_objects(
    job: _Job, state: _SpreadsheetState, mode: SpreadsheetMode, tmpdir: Path
) -> None:
    """Decode the pictures and charts of a kept pandas section.

    Runs once the OCR fallback is settled, so a discarded section costs nothing.
    LibreOffice is involved only where the file holds no usable bitmap (charts,
    EMF/WMF pictures) and for .xls.

    Best effort: failures are logged and the section keeps its tables — an
    optional extra must not sink a conversion that already succeeded. The one
    exception is `auto` on a workbook with no cell data whose objects all turned
    out unusable: it still gets its OCR fallback.
    """
    survey = state.survey
    if not (state.keep_pandas and survey.has_objects):
        return

    drawings, rendered = survey.drawings, None
    if needs_libreoffice(
        job.mime,
        survey,
        render_charts=settings.excel_render_charts,
        rasterize_metafiles=settings.excel_rasterize_metafiles,
    ):
        async with job.state.libreoffice_sem:
            with job.timer.track():
                try:
                    drawings, rendered = await asyncio.to_thread(
                        render_with_libreoffice,
                        path=job.prepared_path,
                        mime=job.mime,
                        survey=survey,
                        lo_server=job.state.libreoffice_server,
                        work_dir=tmpdir,
                        render_charts=settings.excel_render_charts,
                        rasterize_metafiles=settings.excel_rasterize_metafiles,
                    )
                except Exception as e:
                    logger.warning(
                        f"LibreOffice could not render the spreadsheet drawings "
                        f"({job.mime}): {e!r} — charts keep their text summary only"
                    )

    if drawings is not None:
        with job.timer.track():
            try:
                state.objects = await asyncio.to_thread(
                    decode_objects,
                    drawings=drawings,
                    rendered=rendered,
                    max_images=settings.excel_max_images,
                )
            except Exception as e:
                logger.warning(
                    f"Could not extract the spreadsheet pictures and charts "
                    f"({job.mime}): {e!r}"
                )

    nothing_extracted = state.objects is None or not state.objects.figures
    if (
        not state.tables
        and nothing_extracted
        and mode is SpreadsheetMode.AUTO
        and settings.excel_pdf_fallback_enabled
    ):
        logger.warning(
            f"Spreadsheet without cell data whose pictures/charts could not be "
            f"extracted ({job.mime}) — falling back to PDF+OCR pipeline"
        )
        state.keep_pandas = False
        state.run_ocr = True
        state.objects = None


def _pandas_section(
    state: _SpreadsheetState,
    kept_images: Collection[str],
    descriptions: Mapping[str, str] | None,
) -> tuple[str, tuple[SheetAnchor, ...]]:
    """The pandas section: each sheet's table, then its pictures and charts."""
    objects = state.objects
    figures_md: dict[str, str] = {}
    sheet_order: tuple[str, ...] = ()
    if objects is not None:
        sheet_order = objects.sheet_order
        for sheet in dict.fromkeys([*objects.figures, *objects.omitted]):
            figures_md[sheet] = render_figures(
                objects.figures.get(sheet, []),
                kept_images,
                descriptions,
                omitted=objects.omitted.get(sheet, 0),
            )
    return assemble_sections(merge_sheet_bodies(state.tables, figures_md, sheet_order))


def _finalize_spreadsheet(
    job: _Job,
    method: SpreadsheetMode,
    pandas_md: str | None = None,
    pandas_anchors: tuple[SheetAnchor, ...] = (),
    ocr_md: str | None = None,
    ocr_anchors: tuple[SheetAnchor, ...] = (),
) -> str:
    """Assemble the spreadsheet document, or fail with the right status code.

    A section whose Markdown blows past `excel_max_output_ratio` is dropped from
    the output rather than failing the whole request — 413 is raised only when
    nothing survives, 422 when no section produced any content in the first place.
    """
    doc = build_spreadsheet_document(
        mime_ext=job.mime,
        method=method,
        table_format=settings.table_output_format,
        pandas_md=pandas_md,
        pandas_anchors=pandas_anchors,
        ocr_md=ocr_md,
        ocr_anchors=ocr_anchors,
        input_bytes=job.file_size,
        max_output_ratio=settings.excel_max_output_ratio,
    )
    if doc.kept:
        if doc.dropped:
            logger.warning(
                f"Spreadsheet section(s) {', '.join(doc.dropped)} dropped ({job.mime}): "
                f"output above {settings.excel_max_output_ratio}x the input size"
            )
        return doc.markdown
    if doc.dropped:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Spreadsheet output too large: every conversion section "
                f"({', '.join(doc.dropped)}) exceeded "
                f"{settings.excel_max_output_ratio}x the "
                f"{human_size(job.file_size)} input ({job.mime})"
            ),
        )
    raise HTTPException(
        status_code=422,
        detail=f"Spreadsheet produced no usable content ({job.mime}, mode '{method.value}')",
    )


# ---------------------------------------------------------------------------
#  Phase 2 — anything the pipeline cannot read directly becomes a PDF
# ---------------------------------------------------------------------------


async def _to_pipeline_input(
    job: _Job, tmpdir: Path, is_spreadsheet: bool
) -> tuple[Path, SheetPageMap]:
    """Return the file to hand the pipeline, plus a sheet→page map for spreadsheets.

    TIFF/WebP go through Pillow (TIFF may be multipage, WebP is not supported by
    Paddle); Office and spreadsheet formats go through LibreOffice, whose
    semaphore wait is excluded from active time. PDFs and plain images are passed
    through untouched.
    """
    if job.mime in (".tiff", ".webp"):
        async with job.failing_with(f"Image to PDF conversion error ({job.mime})"):
            with job.timer.track():
                return await asyncio.to_thread(
                    image_to_pdf, path=job.prepared_path, output_dir=tmpdir
                ), None

    if not is_office_mime_ext(job.mime):
        return job.prepared_path, None  # .pdf, .png, .jpg, .bmp

    lo_server: LibreOfficeServer = job.state.libreoffice_server
    label = "Spreadsheet" if is_spreadsheet else "Office"
    async with job.state.libreoffice_sem:
        async with job.failing_with(f"{label} to PDF conversion error ({job.mime})"):
            with job.timer.track():
                pdf_path, sheet_page_map = await asyncio.to_thread(
                    convert_to_pdf,
                    file_path=job.prepared_path,
                    mime=job.mime,
                    lo_server=lo_server,
                    paper_format=settings.excel_pdf_paper_format
                    if is_spreadsheet
                    else None,
                    landscape=settings.excel_pdf_landscape,
                )

    if is_spreadsheet and settings.save_table_conversion_artifacts:
        await asyncio.to_thread(
            save_table_conversion_artifacts,
            input_path=job.prepared_path,
            pdf_path=pdf_path,
            artifacts_dir=Path(settings.artifact_dir)
            / settings.table_conversion_artifacts_subdir,
            raw_mime=job.mime,
        )
    return pdf_path, sheet_page_map


# ---------------------------------------------------------------------------
#  Phases 3–6 — OCR pipeline, post-processing, image description
# ---------------------------------------------------------------------------


async def _run_pipeline(
    job: _Job, pipeline_input: Path, need_image_ocr: bool
) -> tuple[list[str], dict[str, EncodedImage]]:
    """Run PaddleOCR in its dedicated worker. Returns one Markdown string per page."""
    wrapper: PaddlePipelineWrapper = job.state.pipeline_wrapper
    async with job.failing_with("Pipeline error"):
        pages, imgs, duration = await wrapper.run(
            pipeline_input.as_posix(), use_ocr_for_image_block=need_image_ocr
        )
    job.timer.add(duration)
    return pages, imgs


async def _post_process(
    job: _Job, pages: list[str], need_image_ocr: bool
) -> tuple[dict[str, str], list[str]]:
    """Extract per-figure OCR text and simplify tables, in parallel threads."""
    md_raw = join_pages(pages)
    if job.artifact_ctx is not None:
        job.artifact_ctx.partial_md = md_raw

    async with job.failing_with(f"Post-processing error ({job.mime})"):
        with job.timer.track():
            prune = asyncio.to_thread(
                prune_pages, pages=pages, table_format=settings.table_output_format
            )
            if not need_image_ocr:
                return {}, await prune
            ocrs, pruned = await asyncio.gather(
                asyncio.to_thread(extract_raw_ocr, md_with_html=md_raw), prune
            )
            return ocrs, pruned


def _encode_images(
    job: _Job, imgs: dict[str, EncodedImage], model_name: Optional[str]
) -> dict[str, str]:
    """Drop images below the minimum size, then base64 the rest.

    Images are already JPEG-encoded and content-named where they were produced, so
    each distinct image appears once here — and is described and returned once.
    """
    if not imgs:
        return {}
    vlm_config = vlm_registry.get(model_name or "")
    min_w, min_h = (
        vlm_config.min_size if vlm_config is not None else settings.image_min_size
    )
    with job.timer.track():
        return {
            name: image.b64
            for name, image in imgs.items()
            if image.width >= min_w and image.height >= min_h
        }


# ---------------------------------------------------------------------------
#  Orchestration
# ---------------------------------------------------------------------------


async def process_document(
    request: Request,
    file_content: bytes,
    image_description_model_name: Optional[str],
    client: Optional[AsyncOpenAIWithInfo],
    t0_wall: float,
    spreadsheet_mode: SpreadsheetMode = SpreadsheetMode.AUTO,
    excel_min_output_ratio: Optional[float] = None,
) -> ProcessedResult:
    """Convert an uploaded document to Markdown.

    Parameters
    ----------
    spreadsheet_mode
        Conversion strategy for spreadsheet inputs; ignored for every other type.
    excel_min_output_ratio
        Per-request override of `settings.excel_min_output_ratio`. Only the
        `auto` mode uses it — it is what decides the sparse-spreadsheet fallback.
    """
    job = _Job(
        request=request,
        timer=ActiveTimer(t0_wall),
        artifact_ctx=ArtifactContext(
            artifacts_dir=Path(settings.artifact_dir)
            / settings.failed_artifacts_subdir,
            t0_wall=t0_wall,
            image_description_model_name=image_description_model_name,
        )
        if settings.save_failed_artifacts
        else None,
    )

    # Global size check, before anything touches the disk.
    try:
        with job.timer.track():
            check_file_size(len(file_content), settings.max_file_size_mb, "File")
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))

    timestamp = datetime.now().strftime("%m-%d_%H-%M")
    with tempfile.TemporaryDirectory(
        prefix=f"foil-serve_{timestamp}_", dir=settings.temp_dir
    ) as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        # ── Phase 1 : write the file, detect its type ────────────────────────
        await _prepare_input(job, file_content, tmpdir)

        # MIME types claimed in [[external_processors]] never reach our pipeline.
        if job.raw_mime in external_registry:
            return await _run_external(job, file_content)
        del file_content

        # Everything left must be a native type (also narrows `mime` to MimeExt).
        if not is_native_mime_ext(job.mime):
            raise HTTPException(
                status_code=415, detail=f"File type not supported: {job.raw_mime}"
            )
        await _apply_size_limits(job)

        if job.mime in TEXT_EXTS:
            return await _read_plain_text(job)

        # ── Spreadsheets — strategy driven by `spreadsheet_mode` ─────────────
        is_spreadsheet = job.mime in SPREADSHEET_EXTS
        sheet = _SpreadsheetState()
        if is_spreadsheet:
            sheet = await _convert_with_pandas(
                job,
                spreadsheet_mode,
                min_output_ratio=excel_min_output_ratio
                if excel_min_output_ratio is not None
                else settings.excel_min_output_ratio,
            )
            await _extract_objects(job, sheet, spreadsheet_mode, tmpdir)
        # A pandas-only outcome never touches the OCR pipeline.
        run_pipeline = not is_spreadsheet or sheet.run_ocr

        # OCR on image blocks is needed when a VLM description is requested (it
        # feeds the prompt) or when OCR text is wanted in the Markdown without VLM.
        has_vlm = image_description_model_name is not None and client is not None
        need_image_ocr = has_vlm or settings.output_paddle_ocr_no_img_desc
        pages: list[str] = []
        imgs: dict[str, EncodedImage] = {}

        if run_pipeline:
            # ── Phase 2 : convert to PDF when the pipeline cannot read the input
            pipeline_input, sheet.sheet_page_map = await _to_pipeline_input(
                job, tmpdir, is_spreadsheet
            )
            if job.artifact_ctx is not None and pipeline_input != job.prepared_path:
                job.artifact_ctx.converted_pdf = pipeline_input

            # ── Phase 3 : pipeline.predict() — serialized in a dedicated worker
            pages, imgs = await _run_pipeline(job, pipeline_input, need_image_ocr)

    # tmpdir cleaned up; the worker is done and no longer needs the file.

    # ── Phase 4 : post-processing ────────────────────────────────────────────
    ocrs: dict[str, str] = {}
    if run_pipeline:
        ocrs, pages = await _post_process(job, pages, need_image_ocr)
    native = sheet.objects
    if native is not None:
        # A shared name means identical bytes: the union is also a dedup.
        imgs = imgs | native.images
    imgs_b64 = _encode_images(job, imgs, image_description_model_name)

    # ── Phase 5 : image description (async, external VLM) ────────────────────
    if has_vlm and imgs_b64:
        assert client is not None and image_description_model_name is not None
        # Paddle figures get their OCR text as context; pictures and charts read
        # from a spreadsheet get their alt text / chart summary.
        contexts = (ocrs | native.contexts) if native is not None else ocrs
        descriptions, img_desc_time = await describe_images(
            client=client,
            model_name=image_description_model_name,
            imgs_b64=imgs_b64,
            ocrs=contexts,
            semaphore=job.state.img_desc_sem.get(image_description_model_name),
        )
    else:
        descriptions, img_desc_time = None, 0.0

    # ── Phase 6 : inject descriptions and OCR into the figure blocks ─────────
    if run_pipeline:
        include_ocr = (has_vlm and settings.output_paddle_ocr) or (
            not has_vlm and settings.output_paddle_ocr_no_img_desc
        )
        with job.timer.track():
            pages = await asyncio.to_thread(
                reformat_pages,
                pages=pages,
                descriptions_dict=descriptions,
                ocr_dict=ocrs,
                include_ocr=include_ocr,
            )

    if is_spreadsheet:
        with job.timer.track():
            pandas_md, pandas_anchors = (
                _pandas_section(sheet, imgs_b64.keys(), descriptions)
                if sheet.keep_pandas
                else (None, ())
            )
            # Group the OCR pages per sheet (when LibreOffice could say how many
            # pages each sheet spans).
            ocr_md, ocr_anchors = (
                build_ocr_body(pages, sheet.sheet_page_map)
                if sheet.run_ocr
                else (None, ())
            )
        if not sheet.run_ocr:
            method = SpreadsheetMode.PANDAS
        elif spreadsheet_mode is SpreadsheetMode.BOTH:
            method = SpreadsheetMode.BOTH
        else:
            method = SpreadsheetMode.OCR
        # Prepend the header + table of contents.
        page_content = _finalize_spreadsheet(
            job, method, pandas_md, pandas_anchors, ocr_md, ocr_anchors
        )
    else:
        page_content = join_pages(pages)
        if not page_content.strip():
            raise HTTPException(
                status_code=422,
                detail=f"Processing produced no content ({job.mime})",
            )

    return ProcessedResult(
        page_content=page_content,
        images=imgs_b64,
        metadata=job.timer.metadata(img_desc_time),
        mime_ext=job.mime,
        raw_mime=job.raw_mime,
    )
