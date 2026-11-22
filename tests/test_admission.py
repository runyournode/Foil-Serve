"""Upload budget (503 before the body is read) and the open-files limit."""

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from admission import RETRY_AFTER_S, UploadBudget


class _HoldingApp:
    """ASGI app that keeps each request open until `release` is set."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        while (await receive()).get("more_body"):
            pass
        self.started += 1
        if scope["path"] == "/v1/fail":
            raise RuntimeError("boom")
        await self.release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def test_refuses_beyond_budget_then_admits_again():
    async def scenario():
        inner = _HoldingApp()
        budget = UploadBudget(inner, max_bytes=100)
        async with _client(budget) as client:
            first = asyncio.create_task(client.post("/v1/process", content=b"x" * 60))
            await asyncio.sleep(0.05)
            assert budget.in_use == 60
            refused = await client.post("/v1/process", content=b"x" * 60)
            assert refused.status_code == 503
            assert refused.headers["retry-after"] == str(RETRY_AFTER_S)
            fits = asyncio.create_task(
                client.post("/v1/process", content=b"x" * 40)
            )  # 60 + 40 = 100
            await asyncio.sleep(0.05)
            assert budget.in_use == 100
            inner.release.set()
            assert (await first).status_code == 200 and (await fits).status_code == 200
            assert budget.in_use == 0
            assert (
                await client.post("/v1/process", content=b"x" * 60)
            ).status_code == 200
        return inner.started

    assert asyncio.run(scenario()) == 3  # the refused request never reached the app


def test_single_upload_above_the_whole_budget_is_413():
    async def scenario():
        async with _client(UploadBudget(_HoldingApp(), max_bytes=10)) as client:
            return (await client.post("/v1/process", content=b"x" * 11)).status_code

    assert asyncio.run(scenario()) == 413


def test_missing_content_length_is_411():
    async def scenario():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"abc", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/process", "headers": []}
        await UploadBudget(_HoldingApp(), max_bytes=100)(scope, receive, send)
        return sent[0]["status"]

    assert asyncio.run(scenario()) == 411


def test_only_processing_posts_are_counted():
    async def scenario():
        inner = _HoldingApp()
        inner.release.set()
        budget = UploadBudget(inner, max_bytes=10)
        async with _client(budget) as client:
            other = await client.post("/other", content=b"x" * 50)  # not under /v1/
            health = await client.get("/health")
        return other.status_code, health.status_code, budget.in_use

    assert asyncio.run(scenario()) == (200, 200, 0)


def test_budget_released_after_error_and_cancellation():
    async def scenario():
        inner = _HoldingApp()
        budget = UploadBudget(inner, max_bytes=100)
        async with _client(budget) as client:
            with pytest.raises(RuntimeError):
                await client.post("/v1/fail", content=b"x" * 50)
            assert budget.in_use == 0
            pending = asyncio.create_task(client.post("/v1/process", content=b"x" * 50))
            await asyncio.sleep(0.05)
            assert budget.in_use == 50
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.sleep(0.05)
        return budget.in_use

    assert asyncio.run(scenario()) == 0


def test_zero_disables_the_budget():
    async def scenario():
        inner = _HoldingApp()
        inner.release.set()
        async with _client(UploadBudget(inner, max_bytes=0)) as client:
            return (await client.post("/v1/process", content=b"x" * 10_000)).status_code

    assert asyncio.run(scenario()) == 200


def test_open_files_limit_is_raised(tmp_path):
    src = Path(__file__).resolve().parents[1] / "src" / "foil_serve"
    code = (
        f"import sys, resource; sys.path.insert(0, {str(src)!r})\n"
        "from admission import raise_open_files_limit\n"
        "before, after = raise_open_files_limit()\n"
        "hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]\n"
        "print(before, after, resource.getrlimit(resource.RLIMIT_NOFILE)[0], hard)\n"
    )
    out = subprocess.run(
        ["bash", "-c", f'ulimit -S -n 256; exec {sys.executable} -c "$0"', code],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    before, after, now, hard = map(int, out)
    assert before == 256
    assert now == after == min(hard, 65536)
