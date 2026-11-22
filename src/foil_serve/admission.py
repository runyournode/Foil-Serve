"""Admission control: a RAM budget for the uploads of the requests in progress.

A request waiting for a pipeline worker or a LibreOffice instance holds almost nothing
but its upload, in RAM (FastAPI reads the whole body). Under a sustained overload the
waiting queue would grow until the container runs out of memory. This ASGI middleware
admits a processing request only while the uploads of every request in progress fit
in `max_inflight_upload_mb`, and answers 503 + Retry-After otherwise. A refused body is
read and thrown away chunk by chunk, never held in memory: replying while the client is
still sending would close the connection under it, and most HTTP clients would then
report a connection error instead of the 503. The budget is released when the request
ends, whatever the outcome (response, error, cancelled client).

It is the one setting that bounds the waiting queue; the file-descriptor limit is
raised generously at startup (`raise_open_files_limit`) so that it is never reached
first (when it is, clients get their connection dropped without any HTTP answer).
"""

import json
import logging
import resource

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Seconds a refused client is told to wait before retrying
RETRY_AFTER_S = 30

# Soft limit of open files set at startup (capped by the hard limit)
OPEN_FILES_TARGET = 65536


class UploadBudget:
    """Refuse processing requests (POST /v1/...) beyond `max_bytes` of uploads in
    progress (0 = no budget). Runs in the event loop only: no lock needed."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.in_use = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.max_bytes <= 0
            or scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith("/v1/")
        ):
            await self.app(scope, receive, send)
            return

        length = _content_length(scope)
        if length is None:
            await _reply(
                receive,
                send,
                411,
                "Content-Length is required (the upload budget counts it)",
            )
            return
        if length > self.max_bytes:
            await _reply(
                receive,
                send,
                413,
                f"Upload of {length / 2**20:.0f} MB exceeds the server's whole budget",
            )
            return
        if self.in_use + length > self.max_bytes:
            logger.warning(
                f"Upload budget full ({self.in_use / 2**20:.0f} of "
                f"{self.max_bytes / 2**20:.0f} MB in progress): 503 for {length / 2**20:.1f} MB"
            )
            await _reply(
                receive,
                send,
                503,
                "Server busy: too many uploads in progress, retry later",
                headers=[(b"retry-after", str(RETRY_AFTER_S).encode())],
            )
            return

        self.in_use += length
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_use -= length


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reply(
    receive: Receive,
    send: Send,
    status: int,
    detail: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    # Drain the upload first (discarded as it arrives): the client then reads the answer
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return  # the client is gone: nobody to answer
        if not message.get("more_body", False):
            break
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def raise_open_files_limit(target: int = OPEN_FILES_TARGET) -> tuple[int, int]:
    """Raise the soft limit of open files towards `target` (never above the hard limit).

    Each connection waiting for its turn holds a file descriptor; a container usually
    starts with a soft limit of 1024. Deliberately large: the waiting queue is bounded
    by the upload budget (`max_inflight_upload_mb`), not by this limit.
    Returns (before, after).
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    ceiling = target if hard == resource.RLIM_INFINITY else min(hard, target)
    if soft == resource.RLIM_INFINITY or soft >= ceiling:
        return soft, soft
    resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
    return soft, ceiling
