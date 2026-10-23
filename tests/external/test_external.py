"""
Tests for external.py — retry policy, error mapping, response parsing and
metadata merge for external foil-compatible processors.

No network involved: httpx.MockTransport simulates the external server.
Async functions are driven with asyncio.run() inside sync tests.
"""

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from external import (
    check_external_processor,
    external_mime_ext,
    merge_external_metadata,
    process_external,
)
from schemas import ExternalProcessorConfig


def make_cfg(**overrides) -> ExternalProcessorConfig:
    defaults = dict(
        name="video",
        enabled=True,
        url="http://processor.test",
        process_route="/v1/process",
        health_route="/health",
        endpoint_api_key="secret-key",
        mime_types=["video/mp4"],
        max_concurrent_requests=2,
        timeout_s=5.0,
        max_retries=2,
        retry_backoff_s=0.0,  # no sleep between attempts in tests
        max_file_size_mb=500,
        check_on_startup=False,
    )
    defaults.update(overrides)
    return ExternalProcessorConfig(**defaults)


def run_process(handler, cfg: ExternalProcessorConfig):
    """Run process_external against a mocked transport."""
    transport = httpx.MockTransport(handler)

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await process_external(
                http_client=client,
                cfg=cfg,
                file_content=b"fake video bytes",
                raw_mime="video/mp4",
            )

    return asyncio.run(_run())


def ok_payload(**metadata) -> dict:
    return {
        "page_content": "# Video transcript",
        "images": {"frame_1.jpg": "b64data"},
        "metadata": metadata,
    }


class TestProcessExternal:
    def test_success_first_attempt(self):
        seen_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return httpx.Response(200, json=ok_payload(frames=12))

        page_content, images, metadata = run_process(handler, make_cfg())

        assert page_content == "# Video transcript"
        assert images == {"frame_1.jpg": "b64data"}
        assert metadata == {"frames": 12}
        assert len(seen_requests) == 1
        request = seen_requests[0]
        assert str(request.url) == "http://processor.test/v1/process"
        assert request.headers["Authorization"] == "Bearer secret-key"
        assert request.headers["Content-Type"] == "video/mp4"
        assert request.content == b"fake video bytes"

    def test_custom_process_route(self):
        seen_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, json=ok_payload())

        run_process(handler, make_cfg(process_route="/v1/teaching_video"))
        assert seen_urls == ["http://processor.test/v1/teaching_video"]

    def test_no_auth_header_without_api_key(self):
        seen_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return httpx.Response(200, json=ok_payload())

        run_process(handler, make_cfg(endpoint_api_key=""))
        assert "Authorization" not in seen_requests[0].headers

    def test_retries_on_5xx_then_succeeds(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503, json={"detail": "warming up"})
            return httpx.Response(200, json=ok_payload())

        page_content, _, _ = run_process(handler, make_cfg(max_retries=2))
        assert page_content == "# Video transcript"
        assert len(calls) == 3

    def test_5xx_exhaustion_maps_to_502(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(500, json={"detail": "internal error"})

        with pytest.raises(HTTPException) as exc_info:
            run_process(handler, make_cfg(max_retries=1))
        assert exc_info.value.status_code == 502
        assert "internal error" in exc_info.value.detail
        assert len(calls) == 2  # first attempt + 1 retry

    def test_4xx_is_forwarded_without_retry(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(415, json={"detail": "codec not supported"})

        with pytest.raises(HTTPException) as exc_info:
            run_process(handler, make_cfg(max_retries=3))
        assert exc_info.value.status_code == 415
        assert "codec not supported" in exc_info.value.detail
        assert len(calls) == 1  # never retried

    def test_timeout_exhaustion_maps_to_504(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectTimeout("connection timed out")

        with pytest.raises(HTTPException) as exc_info:
            run_process(handler, make_cfg(max_retries=1))
        assert exc_info.value.status_code == 504
        assert len(calls) == 2

    def test_connection_error_exhaustion_maps_to_502(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(HTTPException) as exc_info:
            run_process(handler, make_cfg(max_retries=0))
        assert exc_info.value.status_code == 502

    def test_invalid_payload_maps_to_502(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        with pytest.raises(HTTPException) as exc_info:
            run_process(handler, make_cfg())
        assert exc_info.value.status_code == 502
        assert "invalid response" in exc_info.value.detail

    def test_missing_images_and_metadata_default_to_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"page_content": "# md"})

        page_content, images, metadata = run_process(handler, make_cfg())
        assert page_content == "# md"
        assert images == {}
        assert metadata == {}

    def test_error_detail_falls_back_to_raw_text(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="not json at all")

        with pytest.raises(HTTPException) as exc_info:
            run_process(handler, make_cfg())
        assert exc_info.value.status_code == 400
        assert "not json at all" in exc_info.value.detail


class TestCheckExternalProcessor:
    def run_check(self, handler, cfg):
        transport = httpx.MockTransport(handler)

        async def _run():
            async with httpx.AsyncClient(transport=transport) as client:
                await check_external_processor(client, cfg)

        return asyncio.run(_run())

    def test_healthy(self):
        seen_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, json={"status": "ok"})

        self.run_check(handler, make_cfg())
        assert seen_urls == ["http://processor.test/health"]

    def test_no_health_route_is_never_requested(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        with pytest.raises(ValueError, match="no health_route"):
            self.run_check(handler, make_cfg(health_route=""))

    def test_custom_health_route(self):
        seen_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, json={"status": "ok"})

        self.run_check(handler, make_cfg(health_route="/v1/alive"))
        assert seen_urls == ["http://processor.test/v1/alive"]

    def test_unhealthy_raises_runtime_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with pytest.raises(RuntimeError, match="health check failed"):
            self.run_check(handler, make_cfg())


class TestMergeExternalMetadata:
    def test_foil_fields_filled_and_extras_kept(self):
        metadata = merge_external_metadata(
            external_metadata={"frames_extracted": 120, "audio_duration_s": 33.5},
            active_time_s=12.7,
            wall_clock_s=45.2,
        )
        dumped = metadata.model_dump()
        # foil timing contract fields
        assert dumped["active_conversion_time_no_img_desc"] == 12
        assert dumped["img_desc_time"] == 0
        assert dumped["wall_clock_time"] == 45
        # arbitrary external fields are preserved (Metadata has extra="allow")
        assert dumped["frames_extracted"] == 120
        assert dumped["audio_duration_s"] == 33.5
        # pydantic class config never leaks into the output
        assert "model_config" not in dumped

    def test_external_active_time_takes_precedence(self):
        metadata = merge_external_metadata(
            external_metadata={"active_conversion_time_no_img_desc": 99},
            active_time_s=12.7,
            wall_clock_s=45.2,
        )
        assert metadata.active_conversion_time_no_img_desc == 99

    def test_wall_clock_is_always_overridden(self):
        metadata = merge_external_metadata(
            external_metadata={"wall_clock_time": 1},
            active_time_s=0.0,
            wall_clock_s=45.2,
        )
        assert metadata.wall_clock_time == 45

    def test_none_metadata(self):
        metadata = merge_external_metadata(
            external_metadata=None, active_time_s=3.9, wall_clock_s=8.1
        )
        assert metadata.active_conversion_time_no_img_desc == 3
        assert metadata.wall_clock_time == 8

    def test_serialized_json_round_trip(self):
        metadata = merge_external_metadata(
            external_metadata={"language": "fr"}, active_time_s=1, wall_clock_s=2
        )
        payload = json.loads(metadata.model_dump_json())
        assert payload["language"] == "fr"


class TestExternalMimeExt:
    @pytest.mark.parametrize(
        "raw_mime, expected",
        [
            ("video/mp4", ".mp4"),
            ("video/webm", ".webm"),
            ("application/x-totally-unknown", ".bin"),
        ],
    )
    def test_known_and_unknown_types(self, raw_mime, expected):
        assert external_mime_ext(raw_mime) == expected


class TestHealthRouteConfig:
    def test_processor_without_health_route_is_valid(self):
        assert make_cfg(health_route="").health_route == ""

    def test_startup_check_requires_a_health_route(self):
        with pytest.raises(ValidationError, match="no `health_route`"):
            make_cfg(health_route="", check_on_startup=True)


class TestHealthEndpoint:
    """`/health?external_processor_name=...` with processors lacking a health route."""

    @pytest.fixture
    def api(self, monkeypatch):
        api = pytest.importorskip("api", reason="PaddleOCR (api.py) not importable")
        monkeypatch.setattr(
            api.settings,
            "external_processors",
            [make_cfg(name="checked"), make_cfg(name="blind", health_route="")],
        )
        return api

    def call(self, api, name, handler):
        async def _run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                state = type("S", (), {"external_http_client": client})
                request = type("R", (), {"app": type("A", (), {"state": state})})
                return await api.health_check(
                    request,
                    image_description_model_name=None,
                    external_processor_name=name,
                )

        return asyncio.run(_run())

    def test_all_lists_unchecked_processors(self, api):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200)

        assert self.call(api, "all", handler) == {
            "status": "ok",
            "unchecked_external_processors": ["blind"],
        }
        assert seen == ["http://processor.test/health"]

    def test_named_processor_without_health_route_is_400(self, api):
        with pytest.raises(HTTPException) as exc_info:
            self.call(api, "blind", lambda request: httpx.Response(200))
        assert exc_info.value.status_code == 400


class TestThroughProcessDocument:
    """A MIME type claimed by an external processor is accepted and routed to it.

    Covers the acceptance path end to end (MIME detection with `extra_mimes`,
    routing before the native checks): a type unknown to foil, and a native type
    (RTF) that an external processor claims and therefore takes over.
    """

    MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
    RTF = b"{\\rtf1\\ansi Hello {\\b world}.}"

    @pytest.fixture
    def processing(self, tmp_path, monkeypatch):
        processing = pytest.importorskip(
            "processing", reason="PaddleOCR (processing.py) not importable"
        )
        from settings import settings

        monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
        monkeypatch.setattr(settings, "save_failed_artifacts", False)
        return processing

    def run(self, processing, monkeypatch, content: bytes, claimed: str):
        import time
        import types
        from unittest.mock import MagicMock

        cfg = make_cfg(mime_types=[claimed])
        monkeypatch.setattr(processing, "external_registry", {claimed: cfg})
        monkeypatch.setattr(
            processing, "EXTERNAL_MIME_EXT", {claimed: external_mime_ext(claimed)}
        )
        calls = []

        async def _process_external(http_client, cfg, file_content, raw_mime):
            calls.append(raw_mime)
            return "# From the external processor", {}, {"language": "fr"}

        monkeypatch.setattr(processing, "process_external", _process_external)
        state = types.SimpleNamespace(
            external_sem={cfg.name: asyncio.Semaphore(1)},
            external_http_client=MagicMock(),
            pipeline_wrapper=MagicMock(),
        )
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
        result = asyncio.run(
            processing.process_document(
                request, content, None, None, time.perf_counter()
            )
        )
        return result, calls

    def test_new_mime_is_accepted(self, processing, monkeypatch):
        result, calls = self.run(processing, monkeypatch, self.MP4, "video/mp4")
        assert calls == ["video/mp4"]
        assert result.page_content == "# From the external processor"
        assert result.mime_ext == ".mp4"
        assert result.metadata.language == "fr"

    def test_claimed_native_mime_goes_external(self, processing, monkeypatch):
        result, calls = self.run(processing, monkeypatch, self.RTF, "text/rtf")
        assert calls == ["text/rtf"], "the external processor takes precedence"
        assert result.page_content == "# From the external processor"
