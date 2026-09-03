# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI-based document-to-Markdown conversion server using PaddleOCR-VL-1.5 (0.9B vision-language model served via vLLM). Converts PDF, DOCX, PPTX, images, Excel/ODS, and other formats to structured Markdown with extracted images. Optionally describes images via external OpenAI-compatible VLM endpoints.

This server is designed to be called by a **gateway** server that acts as the source of truth for the API contract (see *Gateway Integration* section).

## Development Commands

### Local Development
```bash
uv sync                        # Install dependencies from lock file
uv run uvicorn src.foil_serve.main:app --reload --port 8080
```

### Docker (Recommended)
```bash
# Build image
docker build -t ghcr.io/runyournode/foil-serve:<tag> .

# Run full stack (server + 2x vLLM services)
cd docker/
docker compose up -d
```

### Code Quality
```bash
uv ruff check src/          # Lint
uv ruff format src/         # Format
uv ty check src/            # Type check
```

### Tests
```bash
uv run pytest tests/         # Run all tests
```

## Architecture

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/v1/process` | API key | Process file → JSON (`ProcessedDocument`) |
| `POST` | `/v1/md/process` | API key | Same, but returns only `page_content` (no images, no metadata) |
| `POST` | `/v1/process/download` | API key | Process file → tar.zst archive (markdown + images + metadata.json) |
| `POST` | `/v1/process/spreadsheet_{pandas,ocr,both}` | API key | Same as `/v1/process` with `spreadsheet_mode` pinned by the path |
| `POST` | `/v1/process/spreadsheet_{pandas,ocr,both}/download` | API key | Same, tar.zst archive |
| `GET` | `/v1/vlm_models` | API key | List available VLM model names |
| `GET` | `/health` | none | Health check (optional VLM endpoint check via `image_description_model_name` query param and external processor check via `external_processor_name`; both support `"all"`) |

All authenticated endpoints use HTTP Bearer token (`Authorization: Bearer <key>`), validated by `security.py`.

Optional query params on the processing endpoints:

| Param | Routes | Description |
|---|---|---|
| `image_description_model_name` | all | VLM used to describe extracted figures |
| `spreadsheet_mode` | `/v1/process`, `/v1/md/process`, `/v1/process/download` | `auto` (default), `pandas`, `ocr`, `both` — see *Spreadsheet Configuration*. Ignored for non-spreadsheet inputs |
| `excel_min_output_ratio` | same three | Per-request override of the sparse-detection threshold; `auto` mode only |

The `spreadsheet_*` routes accept every supported file type — the pinned mode is simply a no-op outside `.xls/.xlsx/.ods`.

### Request Flow
```
POST /v1/process or /v1/process/download (file upload)   [api.py → processing.process_document]
  → File size checks (global + type-specific, zip bomb detection for ZIP-based formats)
  → Phase 1: prepare_input_file() — write to tmpdir, MIME detection
      → MIME claimed by [[external_processors]] (e.g. video/mp4): async POST to the
        external foil-compatible server (per-processor semaphore + timeout/retry),
        merge returned metadata with foil timing → return immediately
      → .txt/.json/.csv/.xml: read_text_smart() (auto-detect encoding) → return immediately
      → .xls/.xlsx/.ods: dispatch on `spreadsheet_mode` (auto | pandas | ocr | both)
          → pandas run (all modes but `ocr`): excel_sem → excel2txt() → SpreadsheetConversion
              → EmptySpreadsheetError: `auto` falls back to PDF+OCR (if enabled),
                `both` keeps going with an empty pandas section, else HTTP 422
              → Sparse output (ratio check, `auto` only) → fallback to PDF+OCR
          → OCR needed (`ocr`, `both`, or the `auto` fallback) → continue to Phase 2
          → otherwise → build_spreadsheet_document() → return immediately
      → .tiff/.webp: image_to_pdf() via Pillow (handles multipage TIFF)
  → Phase 2: libreoffice_sem → convert_to_pdf() via LibreOfficeServer (DOCX, PPTX, DOC, PPT, ODT, ODP)
             (spreadsheets also get back a sheet → page-count map for the table of contents)
  → Phase 3: PaddlePipelineWrapper.run() — multiprocessing.Pool worker (1 process, maxtasksperchild=N)
             returns Markdown per PDF page
  → Phase 4: parallel post-processing — extract_raw_ocr() + prune_tables() (page by page)
  → Image filtering + PIL → base64 conversion
  → Phase 5 (optional): describe images via VLM endpoint (async TaskGroup, semaphore-limited per model)
  → Phase 6: reformat_md() — inject VLM descriptions + OCR into markdown figure blocks
  → Spreadsheets: build_ocr_body() (## anchors per sheet or per page)
                  + build_spreadsheet_document() (header + ToC, per-section size budget)
  → return JSON or tar.zst
```

### Key Components

**`src/foil_serve/main.py`** — Application assembly only: lifespan (runtime dirs, semaphores for LibreOffice / Excel / per-VLM-model / per-external-processor, LibreOffice server, PaddleOCR pipeline + startup smoke test), the `FastAPIOffline` app, and the router mount. No request handling and no conversion logic.

**`src/foil_serve/api.py`** — Every route, and nothing else. Two response shapes (`_as_json` / `_as_archive`) x two ways of picking the spreadsheet strategy. The 6 fixed-mode routes are generated by `_make_fixed_mode_endpoint()` — a closure factory, so FastAPI still sees a real signature and docstring in the OpenAPI schema.

**`src/foil_serve/processing.py`** — `process_document()`: the conversion orchestration, returning a `ProcessedResult`. Owns the two things the domain modules deliberately do not — mapping domain errors to HTTP status codes, and accounting *active* time. `ActiveTimer` accumulates it (open `track()` **inside** an `async with semaphore` block so waits are excluded) and builds the response `Metadata`. `_Job` carries per-request state; its `failing_with()` context manager turns any unexpected error into an HTTPException after saving a failure artifact. Phases are one function each: `_prepare_input`, `_apply_size_limits`, `_run_external`, `_read_plain_text`, `_convert_with_pandas`, `_to_pipeline_input`, `_run_pipeline`, `_post_process`, `_encode_images`, `_finalize_spreadsheet`.

**`src/foil_serve/spreadsheet.py`** — Excel/ODS to Markdown conversion **and** final document assembly. Public API: `excel2txt()` (returns a `SpreadsheetConversion`: markdown, pre-clean size, per-sheet `SheetAnchor`s), `EmptySpreadsheetError`, `build_ocr_body()` (groups Paddle page markdown under `## <sheet>` — or `## Page N` when the sheet→page map is unusable), `build_spreadsheet_document()` (header + table of contents with exact line numbers, per-section size budget → `SpreadsheetDocument.kept` / `.dropped`), and the two pure decision rules `resolve_strategy()` (mode → run pandas? run OCR?) and `is_sparse()`. Features: cell error detection and masking/labeling, whitespace normalization, empty row/column stripping, two table formats (`"human"` aligned / `"llm"` compact), encrypted XLS detection via `is_encrypted_xls_error()`. Supports .xls (xlrd), .xlsx (openpyxl via pandas), .ods (odfpy via pandas).

**`src/foil_serve/pipeline.py`** — `PaddlePipelineWrapper`: multiprocessing pool (processes=1, maxtasksperchild=N) with automatic worker recycling to address PaddleOCR memory leaks. `_worker_predict()` runs pipeline.predict() in the worker process. `run()` accepts `use_ocr_for_image_block` flag to skip OCR when not needed, and returns **one Markdown string per PDF page** (join with `"  \n"`) so output line numbers can be mapped back to pages.

**`src/foil_serve/vlm.py`** — `describe_image()` / `describe_image_sem()`: async image description via external OpenAI-compatible VLM. OCR text is injected into the prompt and truncated to `client.max_input_ocr_length` chars. `describe_images()`: TaskGroup fan-out over every figure that has OCR context; any failure becomes a 502 rather than a document with missing captions.

**`src/foil_serve/utils.py`** — MIME detection (`prepare_input_file()`), image utilities (`batch_pil_to_b64`), `build_tar_zst()` (in-memory tar.zst archive creation), `read_text_smart()` (auto-detect encoding via chardet), `image_to_pdf()` (TIFF/WebP → PDF via Pillow), `check_zip_uncompressed_size()` (two-phase zip bomb detection), `check_file_size()`, `check_input_size()` (type-specific limits), `_detect_ooxml()` (OOXML fallback from ZIP central directory).

**`src/foil_serve/postprocessing.py`** — `extract_raw_ocr()`: extracts per-image OCR text from raw Paddle markdown. `reformat_md()`: injects VLM descriptions and OCR into figure blocks (accepts `include_ocr` flag). Per-page variants `prune_pages()` / `reformat_pages()` plus `join_pages()` (separator `"  \n"`) keep page boundaries locatable for the spreadsheet table of contents. (`prune_tables()` itself lives in `table_utils.py`.)

**`src/foil_serve/external.py`** — Async client for external foil-compatible processing backends (e.g. video). `process_external()`: POST file bytes to the configured route with Bearer auth, configurable timeout and retry (transient failures only: timeouts, transport errors, HTTP 5xx — exponential backoff; 4xx forwarded as-is). `check_external_processor()`: health check used at startup (`check_on_startup`) and by `/health`. `merge_external_metadata()`: external metadata passed through, foil overrides `wall_clock_time` and fills timing fields if absent (`Metadata` allows extra fields). `external_mime_ext()` / `EXTERNAL_MIME_EXT`: MIME → extension for externally-routed types.

**`src/foil_serve/libreoffice.py`** — `LibreOfficeServer`: persistent LibreOffice headless server (soffice --headless --accept). Reached over a UNO **pipe** (Unix domain socket, osl creates it under `/tmp` and ignores `$TMPDIR`) — **no TCP port**; requires the app to be able to write to `/tmp`. Pipe name is unique per start and namespaced with `PIPE_PREFIX` (`foil_soffice_`); the real socket path is resolved from the kernel via `/proc/net/unix` (`_resolve_socket_path()`), not hard-coded. `_sweep_dead_pipes()` removes residual sockets (no live listener) from crashed runs at startup, leaving live ones untouched. `convert_to_pdf()`: converts DOCX, PPTX, DOC, PPT, ODT, ODP to PDF without revision marks. `convert_spreadsheet()`: dedicated spreadsheet → PDF conversion with paper format (A2–Tabloid), landscape/portrait, fit-to-page-width; also returns a `SheetPageMap` (sheet name → page count, via `XRenderable.getRendererCount`, written by the UNO script as a JSON sidecar and validated on read — `None` when unusable). `convert_xls_to_xlsx()`: legacy encrypted XLS → XLSX conversion. `OfficeMimeExt` / `is_office_mime_ext()`: the types this module converts, with a `TypeIs` narrower (same pattern as `MimeExt` in `utils.py`).

**`src/foil_serve/security.py`** — `verify_api_key()`: FastAPI dependency for `Authorization: Bearer` header validation via HTTPBearer.

**`src/foil_serve/debug.py`** — Artifact saving system for debugging. `ArtifactContext`: per-request state tracking. `save_failed_artifacts()`: persist failed processing artifacts with timing/resource info. `save_cell_error_artifacts()`: spreadsheet error debugging. `save_table_conversion_artifacts()`: sparse spreadsheet fallback artifacts. Collects system metrics (RAM, CPU, VRAM, app version).

**`src/foil_serve/schemas.py`** — Pydantic models: `ProcessedDocument`, `Metadata` (API response), `VLMModelConfig` (internal config). See *Gateway Integration* for the `Metadata` contract.

**`src/foil_serve/settings.py`** — TOML config loader (Pydantic v2), dynamic VLM registry, `AsyncOpenAIWithInfo` (OpenAI client extended with model metadata), `validate_endpoint()` FastAPI dependency. Defines `TableOutputFormat = Literal["human", "llm"]` and `PaperFormat = Literal["A3", "A4", "A2", "Letter", "Legal", "Tabloid"]`.

**`src/foil_serve/config/server_config.toml`** — Runtime configuration: API keys, VLM model definitions, prompts, pipeline reload settings, spreadsheet processing options (output format, error handling, PDF fallback), artifact saving, OCR output control, concurrency limits.

**`src/foil_serve/config/pipeline_config.yaml`** — PaddleOCR-VL-1.5 pipeline config (batch sizes, thresholds, vLLM URL).

**`docker/compose.yaml`** — 3 services: `foil_app` (port 8081), vLLM for PaddleOCR-VL-1.5 (port 8088, dev only), vLLM for Ministral-3-3B (internal to the docker network, no published port).

### Memory Management Strategy
PaddleOCR is not thread-safe and leaks GPU/CPU memory. The current solution:
1. `processes=1` pool with `maxtasksperchild=N` (`max_tasks_between_pipeline_reload` in config, default 5) — worker recycled every N documents
2. `spawn` multiprocessing context — clean worker initialization
3. Worker recycle costs a few seconds per reload

### External Processor Configuration
External foil-compatible backends (e.g. video → markdown) are defined in `server_config.toml` under `[[external_processors]]`. Each processor has:
- `name`: identifier (logs, semaphore, `external_processor_name` query param on `/health`)
- `url`, `process_route` (default `/v1/process`), `health_route` (default `/health`), `endpoint_api_key`
- `mime_types`: MIME types routed to this processor — each type may be claimed by at most one enabled processor; types overlapping native support are delegated externally (warning logged at startup)
- `max_concurrent_requests`: asyncio.Semaphore size (waits excluded from active time)
- `timeout_s` (default 600), `max_retries` (default 2), `retry_backoff_s` (default 1.0, doubles per retry)
- `max_file_size_mb`: per-type size limit
- `check_on_startup`: health-check the processor during lifespan (server aborts startup on failure)
- `enabled`: enable/disable without removing the config

Calls go through a shared `httpx.AsyncClient` (`app.state.external_http_client`) — fully async, the event loop is never blocked.

### VLM Configuration
VLM endpoints are defined in `server_config.toml` under `[[vlm_models]]`. Each model has:
- `name`: value used as `image_description_model_name` query param
- `url`, `endpoint_name`, `endpoint_api_key`: OpenAI-compatible endpoint
- `temperature`, `max_output_tokens`: generation parameters
- `max_input_ocr_length` (default 700): max chars of OCR text injected into the prompt
- `min_size` (default [64,64]): minimum image dimensions to process
- `prompt`: key from `[prompts]` section or a direct prompt string
- `enabled`: enable/disable without removing the config

### Spreadsheet Configuration

Four conversion strategies, selected per request via `spreadsheet_mode` (or a `/v1/process/spreadsheet_*` route):

| Mode | pandas | PDF+OCR | Notes |
|---|---|---|---|
| `auto` (default) | yes | when empty or sparse | the only mode driven by `excel_pdf_fallback_enabled` / `excel_min_*` |
| `pandas` | yes | never | 422 when the file yields no cell data |
| `ocr` | no | yes | `excel2txt()` is not called at all |
| `both` | yes | yes | two sections in one document; an empty pandas section does not fail the request |

Every spreadsheet response starts with a header (source type + method used) and a table of contents giving the exact line number of each sheet, assembled by `build_spreadsheet_document()`. In `ocr`/`both`, sheet anchors come from the LibreOffice sheet→page map; when that map is missing or disagrees with the page count Paddle returned, the ToC degrades to one entry per page (`## Page N`) rather than guessing.

Key settings in `server_config.toml`:
- `table_output_format`: `"human"` (aligned tables) or `"llm"` (compact, minimal formatting) — applies to all pure-MD table rendering (Excel/ODS sheets and HTML tables from the OCR pipeline)
- `excel_mask_cell_errors`: mask error cells (#REF!, #N/A, etc.) with empty string (true) or label (false)
- `excel_max_output_ratio`: **per-section** size budget as a ratio of the input size. A section above it is dropped from the Markdown and replaced by a note; HTTP 413 only when no section survives
- `excel_pdf_fallback_enabled` *(auto only)*: fall back to PDF+OCR for empty or sparse spreadsheets
- `excel_min_input_for_fallback_mb` *(auto only)*: minimum file size to trigger sparse fallback check
- `excel_min_output_ratio` *(auto only)*: if `md_bytes / file_bytes` is below this, trigger PDF fallback — overridable per request with the query param of the same name
- `excel_pdf_paper_format`: paper format for the PDF rendering (A2, A3, A4, Letter, Legal, Tabloid)
- `excel_pdf_landscape`: landscape orientation for the PDF rendering
- `output_paddle_ocr`: include raw OCR text in final markdown
- `output_paddle_ocr_no_img_desc`: include OCR text when no VLM image description is requested

## Gateway Integration

This server is called by a **gateway** that owns the API contract. Understanding the contract:

### Response format (`ProcessedDocument`)
```
{
  "page_content": str,           # Markdown content
  "images": {                    # Extracted images as base64 JPEG
    "image_filename": "b64..."   # Gateway converts these to PIL.Image internally
  },
  "metadata": { ... }            # Arbitrary dict — see Metadata below
}
```

The `/v1/process/download` endpoint returns the same data as a tar.zst archive containing: `output.md`, images as files, `metadata.json`, and `mime.txt`.

### Metadata contract
The gateway treats `metadata` as `dict[str, Any]` and forwards it transparently to the final client. The fields defined in `Metadata` (schemas.py) are therefore **directly visible to the end client**:

| Field | Type | Description |
|---|---|---|
| `active_conversion_time_no_img_desc` | `int` (seconds) | Accumulated active CPU/GPU time for document→Markdown conversion. Excludes semaphore wait times and VLM image description. |
| `img_desc_time` | `int` (seconds) | Accumulated active time for VLM image description calls. Does not account for concurrency (sum of individual call durations). |
| `wall_clock_time` | `int` (seconds) | Wall-clock latency as seen by the client (includes all waits). |

**Convention for future backends:** adopt these same field names when possible. Additional backend-specific fields are allowed — the gateway passes the full dict through unchanged.

## Known Constraints

- Only one document processed at a time (Paddle not thread-safe)
- Requires NVIDIA GPU with CUDA 13.0
- Requires LibreOffice installed for non-image/non-Excel document conversion
- Python 3.13, dependency management via `uv`

## Additional Instructions
- Always use English to comment the code