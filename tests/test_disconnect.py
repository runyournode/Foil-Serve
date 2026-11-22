"""A client that disconnects cancels its processing (real uvicorn server)."""

import asyncio
import socket
from typing import Annotated

import httpx
import uvicorn
from fastapi import Body, FastAPI, Request

import api


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_disconnect_cancels_the_work():
    state = {
        "started": asyncio.Event(),
        "cancelled": asyncio.Event(),
        "finished": False,
    }
    app = FastAPI()

    async def slow_work():
        state["started"].set()
        try:
            await asyncio.sleep(30)
            state["finished"] = True
        except asyncio.CancelledError:
            state["cancelled"].set()
            raise

    @app.post("/slow")
    async def slow(
        request: Request,
        body: Annotated[bytes, Body(media_type="application/octet-stream")],
    ):
        return await api._cancel_on_disconnect(request, slow_work())

    @app.post("/fast")
    async def fast(
        request: Request,
        body: Annotated[bytes, Body(media_type="application/octet-stream")],
    ):
        async def work():
            return {"n": len(body)}

        return await api._cancel_on_disconnect(request, work())

    async def scenario():
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        try:
            async with httpx.AsyncClient() as client:
                # a normal request is not disturbed by the watcher
                r = await client.post(f"http://127.0.0.1:{port}/fast", content=b"abc")
                assert r.status_code == 200 and r.json() == {"n": 3}
                # the client gives up after 0.3 s
                try:
                    await client.post(
                        f"http://127.0.0.1:{port}/slow",
                        content=b"x" * 1000,
                        timeout=0.3,
                    )
                except httpx.ReadTimeout:
                    pass
            await asyncio.wait_for(state["cancelled"].wait(), timeout=5)
        finally:
            server.should_exit = True
            await serving

    asyncio.run(scenario())
    assert state["started"].is_set() and state["cancelled"].is_set()
    assert not state["finished"]
