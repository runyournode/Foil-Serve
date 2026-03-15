# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI-based document-to-Markdown conversion server using PaddleOCR-VL-1.5 (0.9B vision-language model served via vLLM). Converts PDF, DOCX, PPTX, images, Excel, and other formats to structured Markdown with extracted images. Optionally describes images via external OpenAI-compatible VLM endpoints.

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

## Architecture

### Request Flow
```
POST /v1/process (file upload)
  → Phase 1: prepare_input_file() — write to tmpdir, MIME detection
      → .txt: read_text() → return immediately
      → .xls/.xlsx: excel_sem → excel2txt() → return immediately
  → Phase 2: libreoffice_sem → convert_to_pdf() via LibreOfficeServer (non-image/PDF files only)
  → Phase 3: PaddlePipelineWrapper.run() — multiprocessing.Pool worker (1 process, maxtasksperchild=N)
  → Phase 4: parallel post-processing — extract_raw_ocr() + prune_tables()
  → Image filtering + PIL → base64 conversion
  → Phase 5 (optional): describe images via VLM endpoint (async TaskGroup, semaphore-limited per model)
  → Phase 6: reformat_md() — inject VLM descriptions + OCR into markdown figure blocks
  → return JSON: {page_content, images, metadata}
```

### Key Components

**`src/foil_serve/main.py`** — FastAPI app, lifespan (pipeline init + LibreOffice server), 3 endpoints (`/v1/process`, `/v1/vlm_models`, `/health`). Timing instrumentation: `t0_wall` (wall-clock) and `t_active` (accumulated active time, semaphore waits excluded).

**`src/foil_serve/pipeline.py`** — `PaddlePipelineWrapper`: multiprocessing pool (processes=1, maxtasksperchild=N) with automatic worker recycling to address PaddleOCR memory leaks. `_worker_predict()` runs pipeline.predict() in the worker process.

**`src/foil_serve/vlm.py`** — `describe_image()` / `describe_image_sem()`: async image description via external OpenAI-compatible VLM. OCR text is injected into the prompt and truncated to `client.max_input_ocr_length` chars.

**`src/foil_serve/utils.py`** — MIME detection, image utilities (PIL↔base64), `excel2txt()`, `prepare_input_file()`.

**`src/foil_serve/postprocessing.py`** — `prune_tables()`: HTML table simplification (3–5x size reduction). `extract_raw_ocr()`: extracts per-image OCR text from raw Paddle markdown. `reformat_md()`: injects VLM descriptions and OCR into figure blocks.

**`src/foil_serve/libreoffice.py`** — `LibreOfficeServer`: persistent LibreOffice headless server (soffice --headless --accept). `convert_to_pdf()`: converts DOCX, PPTX, DOC, PPT to PDF without revision marks.

**`src/foil_serve/schemas.py`** — Pydantic models: `ProcessedDocument`, `Metadata` (API response), `VLMModelConfig` (internal config). See *Gateway Integration* for the `Metadata` contract.

**`src/foil_serve/settings.py`** — TOML config loader (Pydantic v2), dynamic VLM registry, `AsyncOpenAIWithInfo` (OpenAI client extended with model metadata), `validate_endpoint()` FastAPI dependency.

**`src/foil_serve/config/server_config.toml`** — Runtime configuration: API keys, VLM model definitions, prompts, `max_tasks_between_pipeline_reload`.

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

### Metadata contract
The gateway treats `metadata` as `dict[str, Any]` and forwards it transparently to the final client. The fields defined in `Metadata` (schemas.py) are therefore **directly visible to the end client**:

| Field | Type | Description |
|---|---|---|
| `active_conversion_time_no_img_desc` | `int` (seconds) | Accumulated active CPU/GPU time for document→Markdown conversion. Excludes semaphore wait times and VLM image description. |
| `img_desc_time` | `int` (seconds) | Accumulated active time for VLM image description calls. Does not account for concurrency (sum of individual call durations). |
| `wall_clock_time` | `int` (seconds) | Wall-clock latency as seen by the client (includes all waits). |

**Convention for future backends:** adopt these same field names when possible. Additional backend-specific fields are allowed — the gateway passes the full dict through unchanged.

## Active Work: OOM Debug Branch

The `oom-debug` branch is actively debugging memory leaks. Debug artifacts present (tracemalloc snapshots, psutil RSS logging) should be cleaned before merging to `main`. The `src/foil_serve/init/warmup.py.disable` file is intentionally disabled (warmup increased latency unnecessarily).

## Known Constraints

- Only one document processed at a time (Paddle not thread-safe)
- Requires NVIDIA GPU with CUDA 13.0
- Requires LibreOffice installed for non-image/non-Excel document conversion
- Python 3.13, dependency management via `uv`

## Additional Instructions
- Always use English to comment the code
