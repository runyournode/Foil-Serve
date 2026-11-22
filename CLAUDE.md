# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI-based document-to-Markdown conversion server using PaddleOCR-VL-1.5 (0.9B vision-language model served via vLLM). Converts PDF, DOCX, PPTX, RTF, images, Excel/ODS, and other formats to structured Markdown with extracted images. Optionally describes images via external OpenAI-compatible VLM endpoints.

This server is designed to be called by a **gateway** server that acts as the source of truth for the API contract (see *Gateway Integration* section).

## Development Commands

### Local Development
```bash
uv sync                        # Install dependencies from lock file
cd src/foil_serve && uv run uvicorn main:app --reload --port 8080   # flat imports: run from src/foil_serve (as the Docker image does)
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
              (legacy-encrypted .xls: LegacyEncryptedXlsError → a LibreOffice instance
                converts it to .xlsx → excel2txt() again)
              + survey_drawings() — cheap: object bytes from the ZIP directory / BIFF
                headers, and (.xlsx/.ods) pictures + charts read from the drawing XML
              → no cell data AND no picture/chart: `auto` falls back to PDF+OCR (if
                enabled), `both` keeps going with an empty pandas section, else HTTP 422
              → Sparse output (ratio check on file size minus picture/chart bytes,
                `auto` only) → fallback to PDF+OCR
          → pandas section kept: _extract_objects() — LibreOfficePool.acquire() →
            render_with_libreoffice() only for charts / EMF-WMF / .xls, then
            decode_objects() (pictures → RGB, dedupe, `excel_max_images` cap)
          → OCR needed (`ocr`, `both`, or the `auto` fallback) → continue to Phase 2
          → otherwise (pandas only) → skip Phases 2–4
      → PDF and images (multipage TIFF, WebP included): passed through untouched
  → Phase 2: LibreOfficePool.acquire() → convert_to_pdf() on an instance of its own
             (DOCX, PPTX, DOC, PPT, ODT, ODP, RTF; the instance is restarted after use;
             timeout → 504, crash → 503); spreadsheets also get back a sheet →
             page-count map for the table of contents
  → Phase 3: PaddlePipelineWrapper.run() — page count, then consecutive page ranges of
             `pipeline_chunk_pages`, one WorkerPool task each (`pipeline_workers` spawned
             processes, one task each at a time; recycled on task count / RSS; crash →
             503, timeout → 504). The worker renders the pages itself (page_images:
             PDF via pypdfium2 at 144 dpi, images at native size, `pipeline_max_page_megapixels`
             cap), runs predict_iter() on the arrays, returns Markdown per page + JPEG
             crops, background-trimmed and renamed after their content (name_by_content)
      → nothing found at all + VLM requested + ≤ `empty_document_vlm_max_pages` pages:
        the whole pages become the figures (_whole_pages_as_figures)
  → Phase 4: parallel post-processing — extract_raw_ocr() + prune_tables() (page by page)
  → Small images (`small_image_threshold`): dropped or kept per `ignore_small_images`,
    never described; base64 — images are already `EncodedImage`s, one per distinct content (Paddle crops ∪ spreadsheet objects: a shared name means identical bytes)
  → Phase 5 (optional): describe images via VLM endpoint (async TaskGroup, semaphore-limited per model,
             exact-match DescriptionCache shared across documents)
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

**`src/foil_serve/main.py`** — Application assembly only: lifespan (runtime dirs, semaphores for Excel / per-VLM-model / per-external-processor, the VLM `DescriptionCache`, the `LibreOfficePool`, PaddleOCR pipeline + startup smoke test), the `FastAPIOffline` app, and the router mount. No request handling and no conversion logic.

**`src/foil_serve/admission.py`** — `UploadBudget`: ASGI middleware (added in `main.py`) admitting a processing request (POST /v1/...) only while the `Content-Length` of every request in progress fits in `max_inflight_upload_mb` — the setting that bounds the waiting queue and its RAM (a waiting request holds its upload). Beyond: 503 + Retry-After (411 without Content-Length, 413 above the whole budget); the refused body is drained and discarded (replying mid-upload would make clients see a connection error instead). `raise_open_files_limit()` raises the soft open-files limit to min(hard, 65536) at startup: deliberately large, never the limit reached first.

**`src/foil_serve/api.py`** — Every route, and nothing else. `_cancel_on_disconnect()` wraps every processing call: when the client disconnects (`http.disconnect` on the ASGI receive channel), the processing is cancelled — out of the worker / LibreOffice queues, a running LibreOffice conversion killed (instance replaced), descriptions stopped, no further page range started — and the request ends with 499. Two response shapes (`_as_json` / `_as_archive`) x two ways of picking the spreadsheet strategy. The 6 fixed-mode routes are generated by `_make_fixed_mode_endpoint()` — a closure factory, so FastAPI still sees a real signature and docstring in the OpenAPI schema.

**`src/foil_serve/processing.py`** — `process_document()`: the conversion orchestration, returning a `ProcessedResult`. Owns the two things the domain modules deliberately do not — mapping domain errors to HTTP status codes, and accounting *active* time. `ActiveTimer` accumulates it (open `track()` **inside** an `async with semaphore` block so waits are excluded) and builds the response `Metadata`. `_Job` carries per-request state; its `failing_with()` context manager turns any unexpected error into an HTTPException after saving a failure artifact (`PipelineUnavailable` / `LibreOfficeUnavailable` keep their 503 / 504; `utils.UnreadableDocument` — a supported type that pdfium / Pillow / LibreOffice cannot open — answers 422). Phases are one function each: `_prepare_input`, `_apply_size_limits`, `_run_external`, `_read_plain_text`, `_convert_with_pandas`, `_excel2txt`, `_extract_objects`, `_to_pipeline_input`, `_run_pipeline`, `_whole_pages_as_figures`, `_post_process`, `_encode_images`, `_pandas_section`, `_finalize_spreadsheet`. Picture/chart extraction is best effort: its failures are logged, never fatal.

**`src/foil_serve/spreadsheet.py`** — Excel/ODS to Markdown conversion **and** final document assembly. Public API: `excel2txt()` (returns a `SpreadsheetConversion`: per-sheet `(name, table)` pairs + pre-clean size; `.markdown` / `.anchors` are derived), `EmptySpreadsheetError`, `LegacyEncryptedXlsError` (the caller converts the .xls with a LibreOffice instance, then calls again), `assemble_sections()` (`## <name>` headings + exact 1-based anchors, shared by both sections), `render_figures()` (`<figure>` blocks for a sheet's pictures/charts: `<img alt>`, `<chart>` summary, `<figcaption>` VLM description), `merge_sheet_bodies()` (table then figures, workbook sheet order incl. chart sheets), `build_ocr_body()` (groups Paddle page markdown under `## <sheet>` — or `## Page N` when the sheet→page map is unusable), `build_spreadsheet_document()` (header + table of contents with exact line numbers, per-section size budget → `SpreadsheetDocument.kept` / `.dropped`), and the two pure decision rules `resolve_strategy()` (mode → run pandas? run OCR?) and `is_sparse()` (takes `object_bytes`, excluded from the file size). Features: cell error detection and masking/labeling, whitespace normalization, empty row/column stripping, two table formats (`"human"` aligned / `"llm"` compact), encrypted XLS detection via `is_encrypted_xls_error()`. Supports .xls (xlrd), .xlsx (openpyxl via pandas), .ods (odfpy via pandas).

**`src/foil_serve/spreadsheet_drawings.py`** — Pictures and charts embedded in spreadsheets, for the pandas section. Split by cost so the OCR fallback is settled before anything heavy: `survey_drawings()` (never raises → `DrawingSurvey`: `object_bytes`, `has_objects`, `has_charts`, and for .xlsx/.ods the `WorkbookDrawings`), `needs_libreoffice()` / `render_with_libreoffice()`, `decode_objects()` (→ `ExtractedObjects`: per-sheet `Figure`s, content-named `EncodedImage`s — a picture stored once per use is decoded once, identical pixels share one image and one slot of `excel_max_images` — VLM contexts, `omitted` counts). `.xlsx`: OPC relationship chain workbook → sheet → drawing → media / chart part, namespace-agnostic (transitional + strict), two/one/absolute anchors, groups, `mc:AlternateContent`, chartex; hidden, linked, SmartArt and OLE objects are skipped. `.ods`: streaming `iterparse` of `content.xml` (row/col counters honor repeats) + `Object N/content.xml` charts. `.xls`: BIFF scan via `xlrd.compdoc` (OBJ records typed chart/picture, MsoDrawingGroup bytes), then LibreOffice → .xlsx. Chart summaries (`format_chart_summary`) give type, title, axes, source sheet, series names, category span — **never cell references**, and values only for series not backed by workbook cells (literals, external links). Cell anchors only order objects on their sheet. Renderings are matched to objects by (sheet, kind, shape name), falling back to the drawing-order index.

**`src/foil_serve/pipeline.py`** — `PaddlePipelineWrapper`: runs PaddleOCR-VL in a `WorkerPool` of `pipeline_workers` processes (Paddle is imported in the workers only, never in the API process). `start()` starts the workers one by one; the first one runs the `init/test_pipeline.jpeg` smoke test **once** (model weights present, PaddleOCR-VL vLLM reachable) — other workers and respawns are not re-tested. `_init_worker()` loads the pipeline once per process, overriding the YAML's `genai_config.max_concurrency` with `pipeline_vlm_max_concurrency` when set. `_worker_predict(path, start, stop, …)` renders pages [start, stop) with `page_images.iter_pages()` (BGR arrays, in the worker: no pixels cross the pipe), runs `predict_iter()` on them and releases each page result as soon as it is converted (bounded retry of the range on vLLM `Already borrowed`), and logs one line per task (page range, Mpx, crops, render / predict+encode / trim time, RSS before→after and peak, threads), then hands freed heap memory back to the OS (`pipeline_malloc_trim`, also after a failure). A dead worker or a timeout surfaces as `PipelineUnavailable` (`status_code` 503 / 504), mapped to the HTTP status by `processing._run_pipeline`. `run()` counts the pages and schedules ranges of `pipeline_chunk_pages`, fair and work-conserving: an idle worker is taken at once (`WorkerPool.try_acquire`: a big document alone runs on every free worker), otherwise the document first waits for one of its own ranges to end, then queues (`acquire` + `start_on`) — a document with work in progress never stands ahead of one that has none, so workers go round-robin between documents (pages are independent for the pipeline: `restructure_pages()` is not used, the split changes nothing). It accepts `use_ocr_for_image_block` flag to skip OCR when not needed, and returns **one Markdown string per PDF page, empty pages included** (index n = page n+1; join with `"  \n"`) so output line numbers can be mapped back to pages, plus the crops as `EncodedImage`s. PaddleX names crops by their box only (no page number — two different pictures at the same place on two pages would collide), so the worker renames them after their content page by page (`postprocessing.name_by_content`); JPEG bytes, not raw pixels, cross the process boundary.

**`src/foil_serve/page_images.py`** — Pages handed to the pipeline, rendered by foil (pypdfium2 + Pillow + numpy, no Paddle). `page_count()`, `iter_pages(path, start, stop, max_megapixels)` → BGR uint8 arrays: PDF rendered exactly as PaddleX does (same pypdfium2 call, forms) at `PDF_RENDER_SCALE` = 2.0 (144 dpi), lowered per page above the budget; images decoded once by Pillow, every frame (multipage TIFF, WebP), EXIF orientation, transparency on white, JPEG `draft()` + LANCZOS only above the budget. Never an image → PDF → image round trip (Pillow writes PDFs at 72 dpi: rendered ×2, a 300-dpi scan used to get 4× its pixels and hit vLLM's 179-Mpx Pillow limit). `Image.MAX_IMAGE_PIXELS` = 500 M (refusal at 1 Gpx): a decompression-bomb guard only, everything below is downscaled. A module lock serializes pdfium (not thread-safe).

**`src/foil_serve/worker_pool.py`** — `WorkerPool`: generic crash-safe process pool (no Paddle inside, unit-tested with dummy workers). N `spawn`ed slots, each with its own `Pipe`; an `asyncio.Queue` of idle slots guarantees one task per slot (a slot whose caller was cancelled is only released once its worker answered). Waits run in a pool-owned thread executor on `wait([pipe, Process.sentinel])`, so a dead worker raises `WorkerCrashed` at once and is respawned in the background; `task_timeout_s` kills a stuck worker (`WorkerTaskTimeout`). Recycling after `max_tasks` tasks or when the RSS reported by the worker reaches `max_rss_mb`, **staggered** (at most N-1 slots reload at once; N=1 reloads inline). Worker starts are serialized (`_spawn_lock`) so model-loading VRAM peaks never stack. Exceptions that cannot round-trip through pickle are sent back as `RuntimeError(repr)`.

**`src/foil_serve/vlm.py`** — `describe_image()` / `describe_image_sem()`: async image description via external OpenAI-compatible VLM. OCR text is injected into the prompt and truncated to `client.max_input_ocr_length` chars. `DescriptionCache`: descriptions reused across documents **only for a byte-identical VLM request** — key = sha256 of endpoint, model, generation parameters, final prompt text (context included) and image bytes; LRU bounded in bytes (`vlm_description_cache_mb`), event loop only (no lock); a hit counts 0 in `img_desc_time`. Never approximate matching across documents (confidentiality). `describe_images()`: TaskGroup fan-out over every image that has a key in the context dict (OCR text or alt text / chart summary, possibly empty — images not referenced by a figure block have no key and are skipped); any failure becomes a 502 rather than a document with missing captions.

**`src/foil_serve/utils.py`** — Supported extensions, **listed once**: one `Literal` per family (`OfficeDocExt`, `SpreadsheetExt`, `ImageExt`, `TextExt`), from which `MimeExt` and the runtime sets (`OFFICE_EXTS`, `SPREADSHEET_EXTS`, `IMAGE_EXTS`, `TEXT_EXTS`, `PAGED_EXTS`) are derived and which `libreoffice.py` imports — a new format is added there and to `mime_def` only (a test checks both agree). MIME detection (`prepare_input_file()`), image utilities (`pil_to_jpeg()` — the single JPEG encoding, `EncodedImage` — JPEG bytes + size, named `imgs/<sha256(jpeg)[:16]>.jpg`), `build_tar_zst()` (in-memory tar.zst archive creation), `read_text_smart()` (auto-detect encoding via chardet), `check_zip_uncompressed_size()` (two-phase zip bomb detection), `check_file_size()`, `check_input_size()` (type-specific limits), `_detect_ooxml()` (OOXML fallback from ZIP central directory).

**`src/foil_serve/postprocessing.py`** — `name_by_content()`: trims the white margins of one page's crops (`trim_background()`: the same drawing boxed with more or less white gives the same pixels, hence one name), encodes them and rewrites their quoted references to content names (called in the Paddle worker). `extract_raw_ocr()`: extracts per-image OCR text from raw Paddle markdown. `reformat_md()`: injects VLM descriptions and OCR into figure blocks (accepts `include_ocr` flag). Per-page variants `prune_pages()` / `reformat_pages()` plus `join_pages()` (separator `"  \n"`) keep page boundaries locatable for the spreadsheet table of contents. (`prune_tables()` itself lives in `table_utils.py`.)

**`src/foil_serve/external.py`** — Async client for external foil-compatible processing backends (e.g. video). `process_external()`: POST file bytes to the configured route with Bearer auth, configurable timeout and retry (transient failures only: timeouts, transport errors, HTTP 5xx — exponential backoff; 4xx forwarded as-is). `check_external_processor()`: health check used at startup (`check_on_startup`) and by `/health`. `merge_external_metadata()`: external metadata passed through, foil overrides `wall_clock_time` and fills timing fields if absent (`Metadata` allows extra fields). `external_mime_ext()` / `EXTERNAL_MIME_EXT`: MIME → extension for externally-routed types.

**`src/foil_serve/libreoffice.py`** — `LibreOfficePool`: `max_concurrent_libreoffice` instances, each used by **one conversion at a time** (`async with pool.acquire() as lo`, FIFO wait) and restarted in the background after every use (~1 s; the first start on a new profile ~15 s) before going back to the queue — every conversion starts on a fresh soffice (no memory accumulated, no state left by a wedged or crashed conversion), a long conversion holds one instance only, and a restart never touches anyone else's work (no lock, no shared server). `LibreOfficeServer`: one instance (soffice --headless --accept), with its own profile (`-env:UserInstallation=<runtime_dir>/lo_profile_<slot>`), pipe and **process group** (`start_new_session`: `/usr/bin/soffice` execs oosplash, which forks soffice.bin — `_kill()` signals the whole group SIGTERM → SIGKILL and reaps our zombies, needed when uvicorn is PID 1). Stale processes of a slot (a previous app run) are found by their profile path in the command line and killed at start. Documents are untrusted: every UNO script first sets `BlockUntrustedRefererLinks` and `DisableMacrosExecution` (`_UNO_HARDENING`) and loads with `MacroExecutionMode` NEVER / `UpdateDocMode` NO_UPDATE — a linked image or section made soffice fetch the URL (SSRF, plus a stall); NOT `DisableActiveContent`, which drops every OLE object, charts included. soffice runs with `SAL_DISABLE_CUPS=1` (the first document load of an instance waited 30 s for the CUPS printer list where localhost:631 drops packets, i.e. every conversion), `--nodefault`, `--nolockcheck`; measured: no network socket at all while converting. `_run_uno_script()` is bounded by `libreoffice_conversion_timeout_s`: timeout → `LibreOfficeUnavailable` 504, UNO client killed by a signal or soffice dead → 503 (mapped by `processing._Job.failing_with`). Reached over a UNO **pipe** (Unix domain socket, osl creates it under `/tmp` and ignores `$TMPDIR`) — **no TCP port**; requires the app to be able to write to `/tmp`. Pipe name is unique per start and namespaced with `PIPE_PREFIX` (`foil_soffice_`); the real socket path is resolved from the kernel via `/proc/net/unix` (`_resolve_socket_path()`), not hard-coded. `_sweep_dead_pipes()` removes residual sockets (no live listener) from crashed runs, once at pool start (first instance, before the others start), leaving live ones untouched. `convert_to_pdf()`: converts DOCX, PPTX, DOC, PPT, ODT, ODP, RTF to PDF without revision marks. `convert_spreadsheet()`: dedicated spreadsheet → PDF conversion with paper format (A2–Tabloid), landscape/portrait, fit-to-page-width; also returns a `SheetPageMap` (sheet name → page count, via `XRenderable.getRendererCount`, written by the UNO script as a JSON sidecar and validated on read — `None` when unusable). `convert_xls_to_xlsx()`: legacy encrypted XLS → XLSX conversion (called by `processing._excel2txt` on `spreadsheet.LegacyEncryptedXlsError`). `render_drawings()`: one UNO call exporting a spreadsheet's charts (OLE2Shape with the chart CLSID) and non-raster pictures (EMF/WMF/SVM) to PNG via `GraphicExportFilter` (150 dpi, ≤ 2000 px), optionally saving an .xlsx copy first (used for .xls); returns `RenderedShape`s from a validated JSON sidecar (PNG paths confined to the output dir). `OfficeMimeExt` / `is_office_mime_ext()`: the types this module converts (`Literal[OfficeDocExt, SpreadsheetExt]`, imported from `utils.py`), with a `TypeIs` narrower.

**`src/foil_serve/security.py`** — `verify_api_key()`: FastAPI dependency for `Authorization: Bearer` header validation via HTTPBearer.

**`src/foil_serve/debug.py`** — Artifact saving system for debugging. `ArtifactContext`: per-request state tracking. `save_failed_artifacts()`: persist failed processing artifacts with timing/resource info. `save_cell_error_artifacts()`: spreadsheet error debugging. `save_table_conversion_artifacts()`: sparse spreadsheet fallback artifacts. Collects system metrics (RAM, CPU, VRAM, app version).

**`src/foil_serve/schemas.py`** — Pydantic models: `ProcessedDocument`, `Metadata` (API response), `VLMModelConfig` (internal config). See *Gateway Integration* for the `Metadata` contract.

**`src/foil_serve/settings.py`** — TOML config loader; `setup_logging()` (API process: console + `RotatingFileHandler`, `log_max_mb` / `log_backup_count`) and `setup_worker_logging()` (pipeline workers: a `QueueHandler` to the API process, which alone writes and rotates the file — the wrapper runs the `QueueListener`) (Pydantic v2; `Settings`, `VLMModelConfig` and `ExternalProcessorConfig` have **no default values** and `extra="forbid"`: a missing, unknown or obsolete key aborts startup — nothing is ever silently defaulted or ignored. A new setting must be added to every `server_config.toml` in the repo), dynamic VLM registry, `AsyncOpenAIWithInfo` (OpenAI client extended with model metadata), `validate_endpoint()` FastAPI dependency. Defines `TableOutputFormat = Literal["human", "llm"]` and `PaperFormat = Literal["A3", "A4", "A2", "Letter", "Legal", "Tabloid"]`.

**`src/foil_serve/config/server_config.toml`** — Runtime configuration: API keys, VLM model definitions, prompts, pipeline reload settings, spreadsheet processing options (output format, error handling, PDF fallback), artifact saving, OCR output control, concurrency limits.

**`src/foil_serve/config/pipeline_config.yaml`** — PaddleOCR-VL-1.5 pipeline config (batch sizes, thresholds, vLLM URL).

**`docker/compose.yaml`** — 3 services: `foil_app` (port 8081), vLLM v0.33.0 for PaddleOCR-VL-1.5 (port 8000, dev only), vLLM for Ministral-3-3B (internal to the docker network, no published port). vLLM >= 0.31 rejects the per-request `mm_processor_kwargs` PaddleX sends unless the server runs with `trust-request-mm-kwargs: True` (set in every PaddleOCR-VL vLLM config). vLLM 0.33 batches the PaddleOCR-VL vision encoder (vLLM PR #61458; RTX 3070: +11 % req/s, −37 % TTFT, +8-15 % pages/s end to end vs 0.32; 0.34 measured no better) but profiles it with a larger activation peak: every PaddleOCR-VL config uses a fixed `kv-cache-memory-bytes` (a `gpu-memory-utilization` fraction that worked on 0.32 leaves a negative KV budget). `docker/compose.dev.yaml`: the same stack on the `dev-offline` image, image descriptions served externally.

**`docker/rtx4090/`** — Same stack tuned for a single RTX 4090 (not measured on a 4090): vLLM v0.33.0, fixed KV caches, 32 / 8 sequences, 3 workers with layout batch 4 and a 3 GB VRAM cap, `pipeline_vlm_max_concurrency` 32, workers never recycled. Its own `mount_config/` and `mount_runtime/`.

**`docker/rtx3070/`** — Full stack on a single RTX 3070 (8 GB), measured: PaddleOCR-VL on vLLM 0.33 (8 sequences, 400 MB fixed KV, ~2.4 GB), Ministral-3-3B Q4_K_M + mmproj Q8_0 on **llama.cpp** (2 slots of 5120 tokens, q8_0 KV, ~3.4 GB) and 1 pipeline worker (layout batch 1, ~1.4 GB at peak) → 7.4 GB peak. Ministral does not fit with vLLM (AWQ keeps embeddings + vision tower in bf16: 4.1 GB loaded). OCR-only variant (no Ministral, 3 workers): 1.36 page/s at 4 concurrent requests, 6.2 GB.

**`docker/a100/`** — Proposal (not measured) for an A100 80 GB shared with another 12-14 GB vLLM: 8 workers (layout batch 4, 3 GB cap), PaddleOCR-VL vLLM with 128 sequences and 6 GB fixed KV, `pipeline_vlm_max_concurrency` 128, 6 LibreOffice instances, image descriptions on an external Qwen server.

### Memory Management Strategy
PaddleOCR is not thread-safe: one pipeline per `spawn`ed worker process (`WorkerPool`), never shared.

A worker's RSS grows with the **largest document it has processed**, then plateaus. Measured on a mixed corpus (RTX 3070, up to 120 documents, 100-page PDFs), no real leak was found: glibc's in-use bytes and the Python heap stay flat. What grows is memory Paddle freed but glibc keeps in its arenas (fragmentation, high-water mark). Mitigations, in order:
1. `pipeline_malloc_trim` (default on): `gc.collect()` + `malloc_trim(0)` after every document, failed ones included. After 100-page PDFs the RSS falls from 3.9 GB to 2.1 GB, for ~0.15 s per document (mostly the `gc.collect()`) and no model reload. It has no effect outside glibc (a warning is logged).
2. Safety net: recycle on `max_worker_rss_mb`, the RSS reported **after** the trim, and/or every `max_tasks_between_pipeline_reload` documents. Recycling is staggered (at most N-1 workers reload at once), and each reload costs a few seconds of model loading.
3. Each worker logs one line per task (page range): `rss before→after (peak …)`, where the peak is `VmHWM`, reset per task. Since documents are split into `pipeline_chunk_pages` ranges and each page result is released as soon as it is converted, the peak follows the largest *range*, not the largest document. Size host RAM as N × that peak, plus the API and the LibreOffice instances.

**VRAM.** Paddle's allocator (`auto_growth`) grows each worker into whatever VRAM is free (~1.2 GB idle; on an 8 GB RTX 3070 the GPU was full under load). `pipeline_worker_vram_limit_mb` caps it (`FLAGS_gpu_memory_limit_mb`, set before the models load). A request that exceeds the cap fails with HTTP 500 (Paddle out of memory) and the worker survives. The need is set by `SubModules.LayoutDetection.batch_size` of `pipeline_config.yaml` (measured per worker: batch 8 → 4.3 GB, 4 → 2.5, 2 → 1.4, 1 → 0.8 GB, speed per worker nearly unchanged): a smaller batch is what lets more workers fit (an 8 GB RTX 3070 holds 4 workers at batch 1). Set the cap a bit above the need, never below. The tuning guide at the end of `pipeline_config.yaml` maps every concurrency / batching knob. Cap every worker, and vLLM too, on a GPU shared with another service.

**Throughput.** The VLM stage (vLLM) is the bottleneck: workers spend most of their time waiting for it. One worker leaves vLLM idle ~20 % of the time; on an RTX 3070 (batch 1, vLLM 10 sequences) throughput under load went 0.84 → 1.09 → ~1.2 pages/s with 1 → 2 → 3-4 workers. A worker only helps when there are at least as many concurrent requests as workers. With 2+ workers, cap `pipeline_vlm_max_concurrency` at about `max_num_seqs` × vLLM replicas (not divided by the worker count): uncapped (YAML default 256), one worker floods vLLM's FIFO queue with a big document's crops and the other workers' documents wait behind them; lower caps slow a document processed alone without helping under load. Add workers only while vLLM still has idle time (`vllm:num_requests_running` often 0). Benchmark tooling and per-GPU results are not in the repo; re-measure on each GPU.

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

Small images (width OR height below the global `small_image_threshold`, measured after the background trim of Paddle crops) are never sent to the VLM. `ignore_small_images = true` drops them from the Markdown and from `images`; `false` returns them and references them with a bare `<img>` (no `<figure>`/`<figcaption>`; `<ocr>` only when OCR output is on and the text is < `SMALL_IMAGE_MAX_OCR_CHARS` = 40).

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

- One page range at a time **per pipeline worker** (Paddle not thread-safe: one pipeline per process, never shared); `pipeline_workers` sets the parallelism, each worker costs its own copy of the layout models in VRAM
- One conversion at a time **per LibreOffice instance**; `max_concurrent_libreoffice` instances, RAM = instances × the peak of the largest documents. The two stages are independent: converted documents wait in the pipeline queue (unbounded — the gateway owns admission)
- Requires NVIDIA GPU with CUDA 13.0
- Requires LibreOffice installed for non-image/non-Excel document conversion
- Python 3.13, dependency management via `uv`

## Additional Instructions
- Always use English to comment the code