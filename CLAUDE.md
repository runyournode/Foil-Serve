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
| `POST` | `/v1/process/download` | API key | Process file → tar.zst archive (markdown + images + metadata.json) |
| `GET` | `/v1/vlm_models` | API key | List available VLM model names |
| `GET` | `/health` | none | Health check (optional VLM endpoint check via `image_description_model_name` query param, supports `"all"`) |

All authenticated endpoints use HTTP Bearer token (`Authorization: Bearer <key>`), validated by `security.py`.

### Request Flow
```
POST /v1/process or /v1/process/download (file upload)
  → File size checks (global + type-specific, zip bomb detection for ZIP-based formats)
  → Phase 1: prepare_input_file() — write to tmpdir, MIME detection
      → .txt/.json/.csv/.xml: read_text_smart() (auto-detect encoding) → return immediately
      → .xls/.xlsx/.ods: excel_sem → excel2txt()
          → EmptySpreadsheetError → fallback to PDF+OCR (if enabled)
          → Sparse output (ratio check) → fallback to PDF+OCR (if enabled)
          → Output too large → HTTP 413
          → Success → return immediately
      → .tiff/.webp: image_to_pdf() via Pillow (handles multipage TIFF)
  → Phase 2: libreoffice_sem → convert_to_pdf() via LibreOfficeServer (DOCX, PPTX, DOC, PPT, ODT, ODP)
  → Phase 3: PaddlePipelineWrapper.run() — multiprocessing.Pool worker (1 process, maxtasksperchild=N)
  → Phase 4: parallel post-processing — extract_raw_ocr() + prune_tables()
  → Image filtering + PIL → base64 conversion
  → Phase 5 (optional): describe images via VLM endpoint (async TaskGroup, semaphore-limited per model)
  → Phase 6: reformat_md() — inject VLM descriptions + OCR into markdown figure blocks
  → return JSON or tar.zst
```

### Key Components

**`src/foil_serve/main.py`** — FastAPI app, lifespan (pipeline init + LibreOffice server), 4 endpoints. Timing instrumentation: `t0_wall` (wall-clock) and `t_active` (accumulated active time, semaphore waits excluded). Manages semaphores for LibreOffice, Excel, and per-VLM-model concurrency.

**`src/foil_serve/spreadsheet.py`** — Excel/ODS to Markdown conversion. Public API: `excel2txt()`, `EmptySpreadsheetError`. Features: cell error detection and masking/labeling, whitespace normalization, empty row/column stripping, two table formats (`"human"` aligned / `"llm"` compact), encrypted XLS detection via `is_encrypted_xls_error()`. Supports .xls (xlrd), .xlsx (openpyxl via pandas), .ods (odfpy via pandas).

**`src/foil_serve/pipeline.py`** — `PaddlePipelineWrapper`: multiprocessing pool (processes=1, maxtasksperchild=N) with automatic worker recycling to address PaddleOCR memory leaks. `_worker_predict()` runs pipeline.predict() in the worker process. `run()` accepts `use_ocr_for_image_block` flag to skip OCR when not needed.

**`src/foil_serve/vlm.py`** — `describe_image()` / `describe_image_sem()`: async image description via external OpenAI-compatible VLM. OCR text is injected into the prompt and truncated to `client.max_input_ocr_length` chars.

**`src/foil_serve/utils.py`** — MIME detection (`prepare_input_file()`), image utilities (`batch_pil_to_b64`), `build_tar_zst()` (in-memory tar.zst archive creation), `read_text_smart()` (auto-detect encoding via chardet), `image_to_pdf()` (TIFF/WebP → PDF via Pillow), `check_zip_uncompressed_size()` (two-phase zip bomb detection), `check_file_size()`, `_detect_ooxml()` (OOXML fallback from ZIP central directory).

**`src/foil_serve/postprocessing.py`** — `prune_tables()`: HTML table simplification (3–5x size reduction). `extract_raw_ocr()`: extracts per-image OCR text from raw Paddle markdown. `reformat_md()`: injects VLM descriptions and OCR into figure blocks (accepts `include_ocr` flag).

**`src/foil_serve/libreoffice.py`** — `LibreOfficeServer`: persistent LibreOffice headless server (soffice --headless --accept). `convert_to_pdf()`: converts DOCX, PPTX, DOC, PPT, ODT, ODP to PDF without revision marks. `convert_spreadsheet()`: dedicated spreadsheet → PDF conversion with paper format (A2–Tabloid), landscape/portrait, fit-to-page-width. `convert_xls_to_xlsx()`: legacy encrypted XLS → XLSX conversion.

**`src/foil_serve/security.py`** — `verify_api_key()`: FastAPI dependency for `Authorization: Bearer` header validation via HTTPBearer.

**`src/foil_serve/debug.py`** — Artifact saving system for debugging. `ArtifactContext`: per-request state tracking. `save_failed_artifacts()`: persist failed processing artifacts with timing/resource info. `save_cell_error_artifacts()`: spreadsheet error debugging. `save_table_conversion_artifacts()`: sparse spreadsheet fallback artifacts. Collects system metrics (RAM, CPU, VRAM, app version).

**`src/foil_serve/schemas.py`** — Pydantic models: `ProcessedDocument`, `Metadata` (API response), `VLMModelConfig` (internal config). See *Gateway Integration* for the `Metadata` contract.

**`src/foil_serve/settings.py`** — TOML config loader (Pydantic v2), dynamic VLM registry, `AsyncOpenAIWithInfo` (OpenAI client extended with model metadata), `validate_endpoint()` FastAPI dependency. Defines `ExcelOutputFormat = Literal["human", "llm"]` and `PaperFormat = Literal["A3", "A4", "A2", "Letter", "Legal", "Tabloid"]`.

**`src/foil_serve/config/server_config.toml`** — Runtime configuration: API keys, VLM model definitions, prompts, pipeline reload settings, spreadsheet processing options (output format, error handling, PDF fallback), artifact saving, OCR output control, concurrency limits.

**`src/foil_serve/config/pipeline_config.yaml`** — PaddleOCR-VL-1.5 pipeline config (batch sizes, thresholds, vLLM URL).

**`docker/compose.yaml`** — 3 services: `paddle_app` (port 8081), vLLM for PaddleOCR-VL-1.5 (port 8088), vLLM for Ministral-3B (port 8089).

### Memory Management Strategy
PaddleOCR is not thread-safe and leaks GPU/CPU memory. The current solution:
1. `processes=1` pool with `maxtasksperchild=N` (`max_tasks_between_pipeline_reload` in config, default 5) — worker recycled every N documents
2. `spawn` multiprocessing context — clean worker initialization
3. Worker recycle costs a few seconds per reload

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
Key settings in `server_config.toml`:
- `excel_output_format`: `"human"` (aligned tables) or `"llm"` (compact, minimal formatting)
- `excel_mask_cell_errors`: mask error cells (#REF!, #N/A, etc.) with empty string (true) or label (false)
- `excel_pdf_fallback_enabled`: fall back to PDF+OCR for empty or sparse spreadsheets
- `excel_min_input_for_fallback_mb`: minimum file size to trigger sparse fallback check
- `excel_min_output_ratio`: if `md_bytes / file_bytes` is below this, trigger PDF fallback
- `excel_max_output_ratio`: if exceeded, return HTTP 413
- `excel_pdf_paper_format`: paper format for PDF fallback (A2, A3, A4, Letter, Legal, Tabloid)
- `excel_pdf_landscape`: landscape orientation for PDF fallback
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