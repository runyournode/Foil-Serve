"""
Async client for external foil-compatible processing backends (e.g. video).

An external processor exposes the same API contract as foil-serve
(routes are configurable per processor, defaults shown):
  POST /v1/process  →  {"page_content": str, "images": {name: b64}, "metadata": {...}}
  GET  /health      →  200 when alive

Calls are fully async (httpx) so the event loop is never blocked, and the
caller limits concurrency with a per-processor asyncio.Semaphore.
"""

import asyncio
import logging
import mimetypes

import httpx
from fastapi import HTTPException

from schemas import ExternalProcessorConfig, Metadata

logger = logging.getLogger(__name__)


def external_mime_ext(raw_mime: str) -> str:
    """Best-effort file extension for an externally-routed MIME type (video/mp4 → .mp4)."""
    return mimetypes.guess_extension(raw_mime) or ".bin"


def merge_external_metadata(
    external_metadata: dict | None,
    active_time_s: float,
    wall_clock_s: float,
) -> Metadata:
    """
    Merge external processor metadata with foil timing fields.

    External metadata is passed through unchanged, except:
    - `wall_clock_time` is always overridden — latency as seen by *this* server's
      client includes foil's own queueing, which the external processor cannot know.
    - `active_conversion_time_no_img_desc` and `img_desc_time` are filled only if
      absent: the processor's own measurement is more truthful than our HTTP call
      duration, so it takes precedence when provided.
    """
    merged = dict(external_metadata or {})
    merged.setdefault("active_conversion_time_no_img_desc", int(active_time_s))
    merged.setdefault("img_desc_time", 0)
    merged["wall_clock_time"] = int(wall_clock_s)
    return Metadata(**merged)


def _error_detail(response: httpx.Response) -> str:
    """Extract the FastAPI-style `detail` from an error response, fallback to raw text."""
    try:
        detail = response.json().get("detail")
    except Exception:
        detail = None
    return str(detail) if detail else response.text[:500]


def _parse_response(
    response: httpx.Response, cfg: ExternalProcessorConfig
) -> tuple[str, dict[str, str], dict]:
    """Validate the external response shape and return (page_content, images_b64, metadata)."""
    try:
        payload = response.json()
        page_content = payload["page_content"]
        images = payload.get("images") or {}
        metadata = payload.get("metadata") or {}
        if (
            not isinstance(page_content, str)
            or not isinstance(images, dict)
            or not isinstance(metadata, dict)
        ):
            raise TypeError("unexpected field types in response payload")
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"External processor '{cfg.name}' returned an invalid response: {e}",
        )
    return page_content, images, metadata


async def process_external(
    http_client: httpx.AsyncClient,
    cfg: ExternalProcessorConfig,
    file_content: bytes,
    raw_mime: str,
) -> tuple[str, dict[str, str], dict]:
    """
    POST `file_content` to the external processor and return
    (page_content, images_b64, metadata_dict).

    `raw_mime` is the libmagic-detected MIME type that routed the file here;
    it is forwarded as Content-Type so the external server doesn't have to
    re-detect the type blindly.

    Retry policy — transient failures only:
    - retried: timeouts, connection/transport errors, HTTP 5xx
    - not retried: HTTP 4xx — forwarded with the original status code so the
      client sees the real reason (413 too large, 415 unsupported, 422 empty, …)
    - backoff: retry_backoff_s doubling after each failed attempt
    On exhaustion: 504 if the last failure was a timeout, 502 otherwise.
    """
    url = str(cfg.url).rstrip("/") + "/" + cfg.process_route.lstrip("/")
    headers = {"Content-Type": raw_mime}
    if cfg.endpoint_api_key:
        headers["Authorization"] = f"Bearer {cfg.endpoint_api_key}"

    last_error = ""
    timed_out = False
    for attempt in range(cfg.max_retries + 1):
        if attempt:
            await asyncio.sleep(cfg.retry_backoff_s * 2 ** (attempt - 1))
            logger.warning(
                f"External processor '{cfg.name}': retry {attempt}/{cfg.max_retries} after: {last_error}"
            )
        try:
            response = await http_client.post(
                url, content=file_content, headers=headers, timeout=cfg.timeout_s
            )
        except httpx.TimeoutException as e:
            timed_out = True
            last_error = f"timeout after {cfg.timeout_s}s ({e!r})"
            continue
        except httpx.TransportError as e:
            timed_out = False
            last_error = f"{e!r}"
            continue

        if response.status_code < 400:
            return _parse_response(response, cfg)

        detail = _error_detail(response)
        if response.status_code < 500:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"External processor '{cfg.name}': {detail}",
            )
        timed_out = False
        last_error = f"HTTP {response.status_code}: {detail}"

    raise HTTPException(
        status_code=504 if timed_out else 502,
        detail=(
            f"External processor '{cfg.name}' failed after "
            f"{cfg.max_retries + 1} attempt(s): {last_error}"
        ),
    )


async def check_external_processor(
    http_client: httpx.AsyncClient, cfg: ExternalProcessorConfig
) -> None:
    """Check the external processor health endpoint. Raises RuntimeError on failure."""
    url = str(cfg.url).rstrip("/") + "/" + cfg.health_route.lstrip("/")
    try:
        response = await http_client.get(url, timeout=10.0)
        response.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            f"External processor '{cfg.name}' health check failed ({url}): {e}"
        )
