"""HTTP surface: every route, and nothing else.

Two response shapes (JSON and tar.zst archive) × two ways of choosing the
spreadsheet strategy (a query param, or a route that pins it). The fixed-mode
routes are generated from one factory rather than written out six times — they
differ only by the mode they pass and the text of their docstring.
"""

import asyncio
import time
from typing import Annotated, Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Security
from fastapi.responses import Response

from external import check_external_processor
from processing import ProcessedResult, process_document
from schemas import ProcessedDocument, SpreadsheetMode
from security import verify_api_key
from settings import AsyncOpenAIWithInfo, settings, validate_endpoint, vlm_registry
from utils import build_tar_zst

router = APIRouter()

_ARCHIVE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/zstd": {}}, "description": "tar.zst archive"}
}

# Every processing route runs the same pipeline; only the packaging differs.
Responder = Callable[[ProcessedResult], Any]


async def _run(
    request: Request,
    file_content: bytes,
    image_description_model_name: Optional[str],
    client: Optional[AsyncOpenAIWithInfo],
    spreadsheet_mode: SpreadsheetMode,
    excel_min_output_ratio: Optional[float] = None,
) -> ProcessedResult:
    return await process_document(
        request,
        file_content,
        image_description_model_name,
        client,
        t0_wall=time.perf_counter(),
        spreadsheet_mode=spreadsheet_mode,
        excel_min_output_ratio=excel_min_output_ratio,
    )


def _as_json(result: ProcessedResult) -> ProcessedDocument:
    return ProcessedDocument(
        page_content=result.page_content,
        images=result.images,
        metadata=result.metadata,
    )


async def _as_archive(result: ProcessedResult) -> Response:
    archive = await asyncio.to_thread(
        build_tar_zst,
        page_content=result.page_content,
        imgs_b64=result.images,
        metadata=result.metadata,
        mime_ext=result.mime_ext,
        raw_mime=result.raw_mime,
    )
    return Response(
        content=archive,
        media_type="application/zstd",
        headers={"Content-Disposition": 'attachment; filename="result.tar.zst"'},
    )


# ── Routes with a selectable spreadsheet mode ─────────────────────────────────


@router.post(
    "/v1/process",
    dependencies=[Security(verify_api_key)],
    response_model=ProcessedDocument,
)
async def foil_process(
    request: Request,
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    image_description_model_name: Optional[str] = Query(None),
    spreadsheet_mode: SpreadsheetMode = Query(SpreadsheetMode.AUTO),
    excel_min_output_ratio: Optional[float] = Query(None, ge=0.0),
    client: Annotated[AsyncOpenAIWithInfo | None, Depends(validate_endpoint)] = None,
):
    """Process a document and return its Markdown, images and metadata.

    `spreadsheet_mode` selects how spreadsheets are converted (ignored for other
    file types). `excel_min_output_ratio` overrides the configured
    sparse-detection threshold and only applies to the default `auto` mode.
    """
    return _as_json(
        await _run(
            request,
            file_content,
            image_description_model_name,
            client,
            spreadsheet_mode,
            excel_min_output_ratio,
        )
    )


@router.post(
    "/v1/md/process",
    dependencies=[Security(verify_api_key)],
    response_model=ProcessedDocument,
)
async def foil_process_md(
    request: Request,
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    image_description_model_name: Optional[str] = Query(None),
    spreadsheet_mode: SpreadsheetMode = Query(SpreadsheetMode.AUTO),
    excel_min_output_ratio: Optional[float] = Query(None, ge=0.0),
    client: Annotated[AsyncOpenAIWithInfo | None, Depends(validate_endpoint)] = None,
):
    """Same as /v1/process but returns only the Markdown (no images, no metadata)."""
    result = await _run(
        request,
        file_content,
        image_description_model_name,
        client,
        spreadsheet_mode,
        excel_min_output_ratio,
    )
    return ProcessedDocument(page_content=result.page_content, images={}, metadata=None)


@router.post(
    "/v1/process/download",
    dependencies=[Security(verify_api_key)],
    response_class=Response,
    responses=_ARCHIVE_RESPONSES,
)
async def foil_process_download(
    request: Request,
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    image_description_model_name: Optional[str] = Query(None),
    spreadsheet_mode: SpreadsheetMode = Query(SpreadsheetMode.AUTO),
    excel_min_output_ratio: Optional[float] = Query(None, ge=0.0),
    client: Annotated[AsyncOpenAIWithInfo | None, Depends(validate_endpoint)] = None,
):
    """Process a document and return a downloadable .tar.zst archive containing
    the Markdown output, images, metadata and MIME information."""
    return await _as_archive(
        await _run(
            request,
            file_content,
            image_description_model_name,
            client,
            spreadsheet_mode,
            excel_min_output_ratio,
        )
    )


# ── Fixed-mode routes ─────────────────────────────────────────────────────────
# Same pipeline, with the spreadsheet strategy pinned by the path. Every file
# type is accepted; the mode is a no-op outside .xls/.xlsx/.ods.
# `excel_min_output_ratio` is deliberately not exposed here: it only drives the
# `auto` mode's sparse-detection fallback, which these routes bypass.

_MODE_BLURBS: dict[SpreadsheetMode, str] = {
    SpreadsheetMode.PANDAS: "cell values only, no OCR fallback",
    SpreadsheetMode.OCR: "LibreOffice → PDF → PaddleOCR, pandas is not run",
    SpreadsheetMode.BOTH: "pandas and OCR in a single Markdown document",
}


def _make_fixed_mode_endpoint(
    mode: SpreadsheetMode, respond: Responder, archive: bool
) -> Callable[..., Awaitable[Any]]:
    """Build one fixed-mode endpoint. FastAPI reads the signature and the
    docstring below, so both have to be real — hence a closure rather than
    functools.partial."""

    async def endpoint(
        request: Request,
        file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
        image_description_model_name: Optional[str] = Query(None),
        client: Annotated[
            AsyncOpenAIWithInfo | None, Depends(validate_endpoint)
        ] = None,
    ):
        result = await _run(
            request, file_content, image_description_model_name, client, mode
        )
        return await respond(result) if archive else respond(result)

    endpoint.__name__ = f"foil_process_spreadsheet_{mode.value}" + (
        "_download" if archive else ""
    )
    endpoint.__doc__ = (
        f"Force the **{mode.value}** spreadsheet strategy "
        f"({_MODE_BLURBS[mode]})"
        + (" and return a .tar.zst archive.\n\n" if archive else ".\n\n")
        + "Accepts every supported file type — the mode is a no-op outside "
        ".xls/.xlsx/.ods."
    )
    return endpoint


for _mode in (SpreadsheetMode.PANDAS, SpreadsheetMode.OCR, SpreadsheetMode.BOTH):
    router.post(
        f"/v1/process/spreadsheet_{_mode.value}",
        dependencies=[Security(verify_api_key)],
        response_model=ProcessedDocument,
    )(_make_fixed_mode_endpoint(_mode, _as_json, archive=False))

    router.post(
        f"/v1/process/spreadsheet_{_mode.value}/download",
        dependencies=[Security(verify_api_key)],
        response_class=Response,
        responses=_ARCHIVE_RESPONSES,
    )(_make_fixed_mode_endpoint(_mode, _as_archive, archive=True))


# ── Service routes ────────────────────────────────────────────────────────────


@router.get("/v1/vlm_models", dependencies=[Security(verify_api_key)])
def list_models() -> list[str]:
    """List available VLM models for (optional) image description."""
    return sorted((m.name for m in vlm_registry.values()), key=str.lower)


@router.get("/health")
async def health_check(
    request: Request,
    image_description_model_name: Optional[str] = Query(None),
    external_processor_name: Optional[str] = Query(None),
):
    """
    Check if server is up (doesn't check if vllm serving PaddleOCR-VL-1.5 is reachable).
    Optionally also check if vlm endpoints and/or external processors are available
    (both query params support the special value "all").
    """
    if image_description_model_name:
        try:
            names = (
                list(vlm_registry)
                if image_description_model_name == "all"
                else [image_description_model_name]
            )
            for name in names:
                await validate_endpoint(name)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "status": "error",
                    "message": str(e),
                    "failed_at": image_description_model_name,
                },
            )

    if external_processor_name:
        processors = {p.name: p for p in settings.external_processors}
        try:
            if external_processor_name == "all":
                targets = list(processors.values())
            elif external_processor_name in processors:
                targets = [processors[external_processor_name]]
            else:
                raise ValueError(
                    f"external_processor_name: '{external_processor_name}' is not configured."
                )
            for proc in targets:
                await check_external_processor(
                    request.app.state.external_http_client, proc
                )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "status": "error",
                    "message": str(e),
                    "failed_at": external_processor_name,
                },
            )

    return {"status": "ok"}
