# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI-based document-to-Markdown conversion server using PaddleOCR-VL-1.5 (0.9B vision-language model served via vLLM). Converts PDF, DOCX, PPTX, RTF, images, Excel/ODS, and other formats to structured Markdown with extracted images. Optionally describes images via external OpenAI-compatible VLM endpoints.

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
              + survey_drawings() — cheap: object bytes from the ZIP directory / BIFF
                headers, and (.xlsx/.ods) pictures + charts read from the drawing XML
              → no cell data AND no picture/chart: `auto` falls back to PDF+OCR (if
                enabled), `both` keeps going with an empty pandas section, else HTTP 422
              → Sparse output (ratio check on file size minus picture/chart bytes,
                `auto` only) → fallback to PDF+OCR
          → pandas section kept: _extract_objects() — libreoffice_sem →
            render_with_libreoffice() only for charts / EMF-WMF / .xls, then
            decode_objects() (pictures → RGB, dedupe, `excel_max_images` cap)
          → OCR needed (`ocr`, `both`, or the `auto` fallback) → continue to Phase 2
          → otherwise (pandas only) → skip Phases 2–4
      → .tiff/.webp: image_to_pdf() via Pillow (handles multipage TIFF)
  → Phase 2: libreoffice_sem → convert_to_pdf() via LibreOfficeServer (DOCX, PPTX, DOC, PPT, ODT, ODP, RTF)
             (spreadsheets also get back a sheet → page-count map for the table of contents)
  → Phase 3: PaddlePipelineWrapper.run() — multiprocessing.Pool worker (1 process, maxtasksperchild=N)
             returns Markdown per PDF page + JPEG crops renamed after their content
             (name_by_content, page by page)
  → Phase 4: parallel post-processing — extract_raw_ocr() + prune_tables() (page by page)
  → Small images (`small_image_threshold`): dropped or kept per `ignore_small_images`,
    never described; base64 — images are already `EncodedImage`s, one per distinct content (Paddle crops ∪ spreadsheet objects: a shared name means identical bytes)
  → Phase 5 (optional): describe images via VLM endpoint (async TaskGroup, semaphore-limited per model)
             — Paddle figures with their OCR text, spreadsheet pictures/charts with their
             alt text / chart summary as context
  → Phase 6: reformat_md() — inject VLM descriptions + OCR into markdown figure blocks,
             collapse runs of blank lines (collapse_blank_lines, fenced code untouched)
  → Non-spreadsheets: join_pages(), or join_marked_pages() when `add_pdf_page`
  → Spreadsheets: pandas section = render_figures() under each sheet's table
                  (merge_sheet_bodies → assemble_sections, line numbers counted after captions)
                  + build_ocr_body() (## anchors per sheet or per page)
                  + build_spreadsheet_document() (header + ToC, per-section size budget)
  → return JSON or tar.zst
```

### Key Components

**`src/foil_serve/main.py`** — Application assembly only: lifespan (runtime dirs, semaphores for LibreOffice / Excel / per-VLM-model / per-external-processor, LibreOffice server, PaddleOCR pipeline + startup smoke test), the `FastAPIOffline` app, and the router mount. No request handling and no conversion logic.

**`src/foil_serve/api.py`** — Every route, and nothing else. Two response shapes (`_as_json` / `_as_archive`) x two ways of picking the spreadsheet strategy. The 6 fixed-mode routes are generated by `_make_fixed_mode_endpoint()` — a closure factory, so FastAPI still sees a real signature and docstring in the OpenAPI schema.

**`src/foil_serve/processing.py`** — `process_document()`: the conversion orchestration, returning a `ProcessedResult`. Owns the two things the domain modules deliberately do not — mapping domain errors to HTTP status codes, and accounting *active* time. `ActiveTimer` accumulates it (open `track()` **inside** an `async with semaphore` block so waits are excluded) and builds the response `Metadata`. `_Job` carries per-request state; its `failing_with()` context manager turns any unexpected error into an HTTPException after saving a failure artifact. Phases are one function each: `_prepare_input`, `_apply_size_limits`, `_run_external`, `_read_plain_text`, `_convert_with_pandas`, `_extract_objects`, `_to_pipeline_input`, `_run_pipeline`, `_post_process`, `_encode_images`, `_pandas_section`, `_finalize_spreadsheet`. Picture/chart extraction is best effort: its failures are logged, never fatal.

**`src/foil_serve/spreadsheet.py`** — Excel/ODS to Markdown conversion **and** final document assembly. Public API: `excel2txt()` (returns a `SpreadsheetConversion`: per-sheet `(name, table)` pairs + pre-clean size; `.markdown` / `.anchors` are derived), `EmptySpreadsheetError`, `assemble_sections()` (`## <name>` headings + exact 1-based anchors, shared by both sections), `render_figures()` (`<figure>` blocks for a sheet's pictures/charts: `<img alt>`, `<chart>` summary, `<figcaption>` VLM description), `merge_sheet_bodies()` (table then figures, workbook sheet order incl. chart sheets), `build_ocr_body()` (groups Paddle page markdown under `## <sheet>` — or `## Page N` when the sheet→page map is unusable), `build_spreadsheet_document()` (header + table of contents with exact line numbers, per-section size budget → `SpreadsheetDocument.kept` / `.dropped`), and the two pure decision rules `resolve_strategy()` (mode → run pandas? run OCR?) and `is_sparse()` (takes `object_bytes`, excluded from the file size). Features: cell error detection and masking/labeling, whitespace normalization, empty row/column stripping, two table formats (`"human"` aligned / `"llm"` compact), encrypted XLS detection via `is_encrypted_xls_error()`. Supports .xls (xlrd), .xlsx (openpyxl via pandas), .ods (odfpy via pandas).

**`src/foil_serve/spreadsheet_drawings.py`** — Pictures and charts embedded in spreadsheets, for the pandas section. Split by cost so the OCR fallback is settled before anything heavy: `survey_drawings()` (never raises → `DrawingSurvey`: `object_bytes`, `has_objects`, `has_charts`, and for .xlsx/.ods the `WorkbookDrawings`), `needs_libreoffice()` / `render_with_libreoffice()`, `decode_objects()` (→ `ExtractedObjects`: per-sheet `Figure`s, content-named `EncodedImage`s — a picture stored once per use is decoded once, identical pixels share one image and one slot of `excel_max_images` — VLM contexts, `omitted` counts). `.xlsx`: OPC relationship chain workbook → sheet → drawing → media / chart part, namespace-agnostic (transitional + strict), two/one/absolute anchors, groups, `mc:AlternateContent`, chartex; hidden, linked, SmartArt and OLE objects are skipped. `.ods`: streaming `iterparse` of `content.xml` (row/col counters honor repeats) + `Object N/content.xml` charts. `.xls`: BIFF scan via `xlrd.compdoc` (OBJ records typed chart/picture, MsoDrawingGroup bytes), then LibreOffice → .xlsx. Chart summaries (`format_chart_summary`) give type, title, axes, source sheet, series names, category span — **never cell references**, and values only for series not backed by workbook cells (literals, external links). Cell anchors only order objects on their sheet. Renderings are matched to objects by (sheet, kind, shape name), falling back to the drawing-order index.

**`src/foil_serve/pipeline.py`** — `PaddlePipelineWrapper`: multiprocessing pool (processes=1, maxtasksperchild=N) with automatic worker recycling to address PaddleOCR memory leaks. `_worker_predict()` runs pipeline.predict() in the worker process. `run()` accepts `use_ocr_for_image_block` flag to skip OCR when not needed, and returns **one Markdown string per PDF page, empty pages included** (index n = page n+1; join with `"  \n"`) so output line numbers can be mapped back to pages, plus the crops as `EncodedImage`s. PaddleX names crops by their box only (no page number — two different pictures at the same place on two pages would collide), so the worker renames them after their content page by page (`postprocessing.name_by_content`); JPEG bytes, not raw pixels, cross the process boundary.

**`src/foil_serve/vlm.py`** — `describe_image()` / `describe_image_sem()`: async image description via external OpenAI-compatible VLM. OCR text is injected into the prompt and truncated to `client.max_input_ocr_length` chars. `describe_images()`: TaskGroup fan-out over every image that has a key in the context dict (OCR text or alt text / chart summary, possibly empty — images not referenced by a figure block have no key and are skipped); any failure becomes a 502 rather than a document with missing captions.

**`src/foil_serve/utils.py`** — Supported extensions, **listed once**: one `Literal` per family (`OfficeDocExt`, `SpreadsheetExt`, `ImageExt`, `TextExt`), from which `MimeExt` and the runtime sets (`OFFICE_EXTS`, `SPREADSHEET_EXTS`, `IMAGE_EXTS`, `TEXT_EXTS`, `PAGED_EXTS`) are derived and which `libreoffice.py` imports — a new format is added there and to `mime_def` only (a test checks both agree). MIME detection (`prepare_input_file()`), image utilities (`pil_to_jpeg()` — the single JPEG encoding, `EncodedImage` — JPEG bytes + size, named `imgs/<sha256(jpeg)[:16]>.jpg`), `build_tar_zst()` (in-memory tar.zst archive creation), `read_text_smart()` (auto-detect encoding via chardet), `image_to_pdf()` (TIFF/WebP → PDF via Pillow), `check_zip_uncompressed_size()` (two-phase zip bomb detection), `check_file_size()`, `check_input_size()` (type-specific limits), `_detect_ooxml()` (OOXML fallback from ZIP central directory).

**`src/foil_serve/postprocessing.py`** — `name_by_content()`: encodes one page's crops and rewrites their quoted references to content names (called in the Paddle worker). `extract_raw_ocr()`: extracts per-image OCR text from raw Paddle markdown. `reformat_md()`: injects VLM descriptions and OCR into figure blocks (accepts `include_ocr` flag). Per-page variants `prune_pages()` / `reformat_pages()` plus `join_pages()` (separator `"  \n"`) keep page boundaries locatable for the spreadsheet table of contents. (`prune_tables()` itself lives in `table_utils.py`.)

**`src/foil_serve/external.py`** — Async client for external foil-compatible processing backends (e.g. video). `process_external()`: POST file bytes to the configured route with Bearer auth, configurable timeout and retry (transient failures only: timeouts, transport errors, HTTP 5xx — exponential backoff; 4xx forwarded as-is). `check_external_processor()`: health check used at startup (`check_on_startup`) and by `/health`. `merge_external_metadata()`: external metadata passed through, foil overrides `wall_clock_time` and fills timing fields if absent (`Metadata` allows extra fields). `external_mime_ext()` / `EXTERNAL_MIME_EXT`: MIME → extension for externally-routed types.

**`src/foil_serve/libreoffice.py`** — `LibreOfficeServer`: persistent LibreOffice headless server (soffice --headless --accept). Reached over a UNO **pipe** (Unix domain socket, osl creates it under `/tmp` and ignores `$TMPDIR`) — **no TCP port**; requires the app to be able to write to `/tmp`. Pipe name is unique per start and namespaced with `PIPE_PREFIX` (`foil_soffice_`); the real socket path is resolved from the kernel via `/proc/net/unix` (`_resolve_socket_path()`), not hard-coded. `_sweep_dead_pipes()` removes residual sockets (no live listener) from crashed runs at startup, leaving live ones untouched. `convert_to_pdf()`: converts DOCX, PPTX, DOC, PPT, ODT, ODP, RTF to PDF without revision marks. `convert_spreadsheet()`: dedicated spreadsheet → PDF conversion with paper format (A2–Tabloid), landscape/portrait, fit-to-page-width; also returns a `SheetPageMap` (sheet name → page count, via `XRenderable.getRendererCount`, written by the UNO script as a JSON sidecar and validated on read — `None` when unusable). `convert_xls_to_xlsx()`: legacy encrypted XLS → XLSX conversion. `render_drawings()`: one UNO call exporting a spreadsheet's charts (OLE2Shape with the chart CLSID) and non-raster pictures (EMF/WMF/SVM) to PNG via `GraphicExportFilter` (150 dpi, ≤ 2000 px), optionally saving an .xlsx copy first (used for .xls); returns `RenderedShape`s from a validated JSON sidecar (PNG paths confined to the output dir). `OfficeMimeExt` / `is_office_mime_ext()`: the types this module converts (`Literal[OfficeDocExt, SpreadsheetExt]`, imported from `utils.py`), with a `TypeIs` narrower.

**`src/foil_serve/security.py`** — `verify_api_key()`: FastAPI dependency for `Authorization: Bearer` header validation via HTTPBearer.

**`src/foil_serve/debug.py`** — Artifact saving system for debugging. `ArtifactContext`: per-request state tracking. `save_failed_artifacts()`: persist failed processing artifacts with timing/resource info. `save_cell_error_artifacts()`: spreadsheet error debugging. `save_table_conversion_artifacts()`: sparse spreadsheet fallback artifacts. Collects system metrics (RAM, CPU, VRAM, app version).

**`src/foil_serve/schemas.py`** — Pydantic models: `ProcessedDocument`, `Metadata` (API response), `VLMModelConfig` (internal config). See *Gateway Integration* for the `Metadata` contract.

**`src/foil_serve/settings.py`** — TOML config loader (Pydantic v2; `Settings`, `VLMModelConfig` and `ExternalProcessorConfig` have **no default values** and `extra="forbid"`: a missing, unknown or obsolete key aborts startup — nothing is ever silently defaulted or ignored. A new setting must be added to every `server_config.toml` in the repo), dynamic VLM registry, `AsyncOpenAIWithInfo` (OpenAI client extended with model metadata), `validate_endpoint()` FastAPI dependency. Defines `TableOutputFormat = Literal["human", "llm"]` and `PaperFormat = Literal["A3", "A4", "A2", "Letter", "Legal", "Tabloid"]`.

**`src/foil_serve/config/server_config.toml`** — Runtime configuration: API keys, VLM model definitions, prompts, pipeline reload settings, spreadsheet processing options (output format, error handling, PDF fallback), artifact saving, OCR output control, concurrency limits.

**`src/foil_serve/config/pipeline_config.yaml`** — PaddleOCR-VL-1.5 pipeline config (batch sizes, thresholds, vLLM URL).

**`docker/compose.yaml`** — 3 services: `foil_app` (port 8081), vLLM for PaddleOCR-VL-1.5 (port 8088, dev only), vLLM for Ministral-3-3B (internal to the docker network, no published port).

**`docker/rtx4090/`** — Same stack tuned for a single RTX 4090, latency over VRAM: vLLM v0.32.0 (needs `trust-request-mm-kwargs`), fixed KV caches, 32 / 8 sequences, worker never recycled. Its own `mount_config/` and `mount_runtime/`.

### Memory Management Strategy
PaddleOCR is not thread-safe and leaks GPU/CPU memory. The current solution:
1. `processes=1` pool with `maxtasksperchild=N` (`max_tasks_between_pipeline_reload` in config) — worker recycled every N documents
2. `spawn` multiprocessing context — clean worker initialization
3. Worker recycle costs a few seconds per reload

### External Processor Configuration
External foil-compatible backends (e.g. video → markdown) are defined in `server_config.toml` under `[[external_processors]]`. Each processor has:
- `name`: identifier (logs, semaphore, `external_processor_name` query param on `/health`)
- `url`, `process_route` (e.g. `/v1/process`), `health_route` (e.g. `/health`; `""` when the server has none — then `check_on_startup` must be false, `/health?external_processor_name=<it>` answers 400 and `all` lists it under `unchecked_external_processors`), `endpoint_api_key` (`""` = no auth header)
- `mime_types`: MIME types routed to this processor — each type may be claimed by at most one enabled processor; types overlapping native support are delegated externally (warning logged at startup)
- `max_concurrent_requests`: asyncio.Semaphore size (waits excluded from active time)
- `timeout_s`, `max_retries`, `retry_backoff_s` (doubles per retry)
- `max_file_size_mb`: per-type size limit
- `check_on_startup`: health-check the processor during lifespan (server aborts startup on failure)
- `enabled`: enable/disable without removing the config

Calls go through a shared `httpx.AsyncClient` (`app.state.external_http_client`) — fully async, the event loop is never blocked.

### VLM Configuration
VLM endpoints are defined in `server_config.toml` under `[[vlm_models]]`. Each model has:
- `name`: value used as `image_description_model_name` query param
- `url`, `endpoint_name`, `endpoint_api_key`: OpenAI-compatible endpoint
- `temperature`, `max_output_tokens`: generation parameters
- `max_input_ocr_length`: max chars of OCR text injected into the prompt
- `prompt`: key from `[prompts]` section or a direct prompt string
- `extra_body`: provider-specific parameters forwarded as-is (`{}` for none)
- `enabled`: enable/disable without removing the config

Small images (width OR height below the global `small_image_threshold`) are never sent to the VLM. `ignore_small_images = true` drops them from the Markdown and from `images`; `false` returns them and references them with a bare `<img>` (no `<figure>`/`<figcaption>`; `<ocr>` only when OCR output is on and the text is < `SMALL_IMAGE_MAX_OCR_CHARS` = 40).

### Spreadsheet Configuration

Four conversion strategies, selected per request via `spreadsheet_mode` (or a `/v1/process/spreadsheet_*` route):

| Mode | pandas | PDF+OCR | Notes |
|---|---|---|---|
| `auto` (default) | yes | when empty or sparse | the only mode driven by `excel_pdf_fallback_enabled` / `excel_min_*` |
| `pandas` | yes | never | 422 when the file yields no cell data, picture or chart |
| `ocr` | no | yes | `excel2txt()` is not called at all |
| `both` | yes | yes | two sections in one document; an empty pandas section does not fail the request |

Every spreadsheet response starts with a header (source type + method used) and a table of contents giving the exact line number of each sheet, assembled by `build_spreadsheet_document()`. In `ocr`/`both`, sheet anchors come from the LibreOffice sheet→page map; when that map is missing or disagrees with the page count Paddle returned, the ToC degrades to one entry per page (`## Page N`) rather than guessing.

**Pictures and charts (pandas section — `auto`, `pandas`, `both`).** Under each sheet's table, its objects in on-sheet order (top-to-bottom, left-to-right), as `<figure>` blocks: pictures keep their original bytes (re-encoded JPEG in `images`), charts are rendered by LibreOffice and carry a `<chart>` text summary (kept even without a rendering). No cell reference is ever written. The VLM (when requested) describes them with the alt text / chart summary as context, in the same Phase 5 fan-out as Paddle figures — in `both`, native objects and Paddle crops are all returned. In `auto`, the sparse check subtracts the natively handled object bytes from the file size (a small table next to a big picture stays pandas), and a workbook with pictures but no cells is not "empty"; decoding, rendering and VLM calls only start once the pandas section is known to be kept.

Key settings in `server_config.toml`:
- `table_output_format`: `"human"` (aligned tables) or `"llm"` (compact, minimal formatting) — applies to all pure-MD table rendering (Excel/ODS sheets and HTML tables from the OCR pipeline)
- `excel_mask_cell_errors`: mask error cells (#REF!, #N/A, etc.) with empty string (true) or label (false)
- `excel_max_output_ratio`: **per-section** size budget as a ratio of the input size. A section above it is dropped from the Markdown and replaced by a note; HTTP 413 only when no section survives
- `excel_pdf_fallback_enabled` *(auto only)*: fall back to PDF+OCR for empty or sparse spreadsheets
- `excel_min_input_for_fallback_mb` *(auto only)*: minimum file size to trigger sparse fallback check
- `excel_min_output_ratio` *(auto only)*: if `md_bytes / (file_bytes - picture_and_chart_bytes)` is below this, trigger PDF fallback — overridable per request with the query param of the same name
- `excel_extract_images`: pictures and charts in the pandas section; false restores cell-only output
- `excel_render_charts`: LibreOffice renders charts to PNG; false keeps the text summary only
- `excel_rasterize_metafiles`: LibreOffice renders EMF/WMF pictures; false skips them
- `excel_max_images`: distinct images per document; beyond, pictures are dropped with a note and charts keep their summary
- `excel_pdf_paper_format`: paper format for the PDF rendering (A2, A3, A4, Letter, Legal, Tabloid)
- `excel_pdf_landscape`: landscape orientation for the PDF rendering
- `output_paddle_ocr`: include raw OCR text in final markdown
- `output_paddle_ocr_no_img_desc`: include OCR text when no VLM image description is requested
- `add_pdf_page`: start each page of a paginated input (`PAGED_EXTS`: PDF, office documents, TIFF/WebP) with `<page number="n/n_max" />` (`postprocessing.join_marked_pages`); never for spreadsheets or single images

## Gateway Integration

This server is called by a **gateway** that owns the API contract. Understanding the contract:

### Response format (`ProcessedDocument`)
```
{
  "page_content": str,           # Markdown content
  "images": {                    # Extracted images as base64 JPEG
    "imgs/<sha256[:16]>.jpg": "b64..."   # Gateway converts these to PIL.Image internally
  },
  "metadata": { ... }            # Arbitrary dict — see Metadata below
}
```

**Image names are content-addressed:** `imgs/` + the first 16 hex chars of the sha256 of the returned JPEG bytes. Each distinct image appears once (and is described by the VLM once), however many times the Markdown references it; the name is a stable identifier the gateway can use to store and serve images later (a store shared across documents should keep the full sha256 or compare bytes on insert).

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