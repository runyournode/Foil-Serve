# Foil Serve 🏄‍♂️

> Document → Markdown conversion server, built on PaddleOCR — with meaningful extras.

**Foil Serve** is a [FastAPI](https://fastapi.tiangolo.com/) server that converts common document formats to structured Markdown. It uses [PaddleOCR-VL-1.5](https://github.com/PaddlePaddle/PaddleOCR) as its OCR backbone, served via vLLM, and adds several practical improvements for LLM/RAG workflows.

---

## ✨ What's on top of PaddleOCR

### 🖼️ External VLM image description
Extracted figures can be described by any OpenAI-compatible VLM of your choice. The description is injected directly into the Markdown inside a proper `<figcaption>` tag, keeping the output clean and semantically structured.

### 📊 HTML table simplification and Markdown conversion
PaddleOCR outputs verbose HTML tables. foil-serve post-processes each table in two steps:

1. **Clean** — strip redundant formatting attributes and normalize whitespace (3–5× token reduction).
2. **Convert to Markdown** — if the table structure is simple enough (no merged cells, no nested tables, no multi-level header, consistent column count), it is rendered as a plain Markdown pipe table. Complex tables that cannot be represented without semantic loss stay as cleaned HTML.

The output format for Markdown tables is controlled by `table_output_format` in `server_config.toml` (`"llm"` compact or `"human"` aligned — same setting as for spreadsheet tables).

### 📑 Spreadsheet conversion strategies

Spreadsheets are converted in one of four ways, chosen **per request** — either with the `spreadsheet_mode` query param, or with a route that pins it:

| Mode | Cell extraction (pandas) | PDF + OCR | Dedicated route |
|---|---|---|---|
| `auto` *(default)* | yes | only when the file is empty or sparse | — (`/v1/process`) |
| `pandas` | yes | never | `/v1/process/spreadsheet_pandas` |
| `ocr` | no | yes | `/v1/process/spreadsheet_ocr` |
| `both` | yes | yes — two sections in one document | `/v1/process/spreadsheet_both` |

`auto` reproduces the historical behaviour and is the **only** mode driven by the `excel_pdf_fallback_enabled` / `excel_min_*` settings; the explicit modes are a direct client decision and ignore them. Each dedicated route also has a `/download` variant, and all of them accept every supported file type — the mode is simply a no-op outside `.xls`, `.xlsx` and `.ods`.

### 🧭 Spreadsheet header and table of contents

Every spreadsheet output is prefixed with a header stating the method actually used, and a table of contents giving the **exact line number** of each sheet — so an agentic RAG pipeline can cite a position instead of re-scanning the whole document:

````markdown
# Spreadsheet converted to Markdown

Source file type: `.xlsx` — conversion method: **both** (cell values read with pandas, then PaddleOCR over a PDF rendering of the sheets).

## Table of contents

Line numbers refer to this document, first line included.

|Section|Line|
|---|---|
|pandas — Budget|22|
|pandas — Summary|287|
|pandas — Notes|296|
|ocr — Budget|306|
|ocr — Summary|582|
|ocr — Notes|593|

---
````

(Real output for a three-sheet workbook whose `Budget` sheet spans six PDF pages — hence the 276-line gap before `Summary` in the OCR section.)

In `ocr` and `both`, the sheet anchors come from the per-sheet page count LibreOffice reports, so a sheet spanning several PDF pages still gets a single anchor with the next sheet placed after all of them. When that map is unavailable or disagrees with what the pipeline returned, the table of contents degrades to one entry per page (`## Page N`) instead of guessing — the line numbers stay exact either way.

### 🖼️ Pictures and charts in spreadsheets

The pandas section (`auto`, `pandas`, `both`) also extracts the **pictures and charts** of each sheet, listed under the sheet's table in on-sheet order (top-to-bottom, left-to-right) and returned in `images` — described by the VLM when `image_description_model_name` is set:

````markdown
## Data

|Month|Revenue|Cost|
|---|---|---|
|Jan|100|80|
…

<figure>
<img src="imgs/3f9c2a71d04be85e.jpg">
<chart>  
Column chart "Revenue 2025" — data from sheet Data  
Axes: Month / EUR  
Series: Revenue, Cost  
Categories: Jan … Jun (6 points)  
</chart>
<figcaption>  
…VLM description…  
</figcaption>
</figure>
````

- **Pictures** are read from the file itself (original bytes, no OCR round-trip), with their alt text when it is meaningful. EMF/WMF pictures are rendered by LibreOffice.
- **Charts** are stored as data, not as a bitmap: LibreOffice renders them, and a text summary (type, title, axes, series, category span) is always included. Values are not repeated when the chart plots cells that are already in the table — only literal values or links to other workbooks are listed.
- **No cell references** are written: row and column coordinates do not survive in a Markdown table.
- `.xlsx` and `.ods` are read natively; `.xls` goes through LibreOffice, but only when the file actually contains pictures or charts.
- In `auto`, the size of these objects does not count in the sparse check: a small table next to a large picture stays a pandas conversion, with the picture extracted. A workbook with pictures but no cell data is not considered empty.

### 📁 Extended input format support

| Format | Conversion | Post-processing |
|---|---|---|
| `.txt`, `.json`, `.csv`, `.xml`, `.md` | Pass-through (smart encoding detection) | — |
| `.pdf`, `.png`, `.jpg`, `.bmp`, `.tiff` (incl. multi-page), `.webp` | Pages rendered by foil (PDF at 144 dpi, images at native size, `pipeline_max_page_megapixels` cap) → PaddleOCR-VL | HTML tables → Markdown or simplified HTML, optional VLM image description |
| `.xls`, `.xlsx`, `.ods` | Pandas → Markdown tables, LibreOffice → PDF → PaddleOCR-VL, or both — see *Spreadsheet conversion strategies* | Cell error masking, pictures and charts extraction, header + table of contents, per-section size budget, optional VLM image description |
| `.docx`, `.doc`, `.pptx`, `.ppt`, `.odt`, `.odp`, `.rtf` | LibreOffice → PDF → PaddleOCR-VL | HTML tables → Markdown or simplified HTML, optional VLM image description |


**PDF conversion:** when converting `.docx` and `.doc` files to PDF via LibreOffice, tracked changes (revisions) are automatically accepted, so the output reflects the final state of the document. Inline comments are **not** captured in the conversion.

**Spreadsheet processing:** cell errors (`#REF!`, `#N/A`, `nan`, etc.) are detected and masked by default. Legacy-encrypted `.xls` files (empty password) are automatically converted to `.xlsx` via LibreOffice before processing. The Markdown table format (`table_output_format`: `"llm"` compact or `"human"` aligned) applies to both spreadsheet tables and HTML tables converted from the OCR pipeline.

In `auto` mode, a spreadsheet that produces very little cell data (content mostly in text boxes, shapes or embedded objects — pictures and charts are extracted natively and do not count) falls back to PDF+OCR via LibreOffice — configurable paper format and orientation, default A3 landscape, fit-to-width. The threshold is `excel_min_output_ratio`, which can be **overridden per request** with the query param of the same name; the check itself only runs above `excel_min_input_for_fallback_mb`, and the whole fallback can be turned off with `excel_pdf_fallback_enabled`. An empty spreadsheet returns HTTP 422 when no OCR conversion is going to run.

**Output size budget:** `excel_max_output_ratio` caps each conversion **section** relative to the input file size. A section that exceeds it is removed from the Markdown and replaced by a short note explaining why — so in `both` mode an oversized pandas section is dropped while the OCR section is still returned, correctly indexed. HTTP 413 is returned only when no section survives.

**Markdown files with HTML tables:** some Markdown files (e.g. outputs from other conversion tools) contain embedded HTML tables and are misdetected as `text/html` by libmagic. foil-serve applies a heuristic (`_detect_md`) to distinguish these from real HTML pages: if no HTML document root markers (`<!DOCTYPE html>`, `<html>`, `<head>`, `<body>`) are found but Markdown structural elements are present (headings, lists, links), the file is treated as Markdown and passed through unchanged. Real HTML pages remain unsupported and are rejected with HTTP 422.

**OOXML detection fallback:** some `.docx`, `.xlsx`, and `.pptx` files are misidentified as `application/octet-stream` by libmagic. foil-serve inspects the ZIP structure (`[Content_Types].xml`) to resolve the actual OOXML type automatically.

Only MIME types listed in [`utils.mime_def`](src/foil_serve/utils.py) are accepted.

Support for additional formats is welcome — contributions are open 🙌


---

## 🔌 API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/docs` | `GET` | Interactive Swagger documentation (offline-enabled) |
| `/v1/process` | `POST` | Convert document to Markdown + extract images (JSON response) |
| `/v1/md/process` | `POST` | Same, but returns only `page_content` (no images, no metadata) |
| `/v1/process/download` | `POST` | Same as `/v1/process` but returns a `tar.zst` archive |
| `/v1/process/spreadsheet_pandas` | `POST` | `/v1/process` with the spreadsheet strategy pinned to `pandas` |
| `/v1/process/spreadsheet_ocr` | `POST` | … pinned to `ocr` |
| `/v1/process/spreadsheet_both` | `POST` | … pinned to `both` |
| `/v1/process/spreadsheet_{mode}/download` | `POST` | Same three, returning a `tar.zst` archive |
| `/v1/vlm_models` | `GET` | List available VLM models for image description |
| `/health` | `GET` | Server health check, optionally validates VLM endpoints and external processors |

The `spreadsheet_*` routes accept every supported file type — the pinned mode only affects `.xls`, `.xlsx` and `.ods`, and is ignored for PDF, Office, image and text inputs.

### `POST /v1/process`

**Request:** raw file bytes (`application/octet-stream`) + optional query params:

| Param | Applies to | Description |
|---|---|---|
| `image_description_model_name` | all routes | VLM used to describe extracted figures (see `/v1/vlm_models`) |
| `spreadsheet_mode` | `/v1/process`, `/v1/md/process`, `/v1/process/download` | `auto` (default), `pandas`, `ocr` or `both` — see *Spreadsheet conversion strategies*. Ignored for non-spreadsheet inputs |
| `excel_min_output_ratio` | same three | Per-request override of the sparse-detection threshold. `auto` mode only |

```bash
curl -X POST "http://localhost:8081/v1/process?spreadsheet_mode=both" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @report.xlsx
```

**Response (`ProcessedDocument`):**
```json
{
  "page_content": "# Markdown content ...",
  "images": {
    "imgs/3f9c2a71d04be85e.jpg": "<base64 JPEG>"
  },
  "metadata": {
    "active_conversion_time_no_img_desc": 12,
    "img_desc_time": 20,
    "wall_clock_time": 18
  }
}
```

**Image names are content-addressed:** `imgs/` + the first 16 hex characters of the sha256 of the JPEG bytes returned. An image repeated in the document (a logo on every page or sheet) is returned once and described by the VLM once, and every `<img src>` referencing it uses the same name. Names are stable identifiers: a client can store and serve images by name, and check a file with `sha256sum`.

### Error codes

| Status | When |
|---|---|
| `422` | Unsupported, empty or unreadable document (corrupted PDF, file LibreOffice cannot open) |
| `413` | File above a size limit, or no spreadsheet section within `excel_max_output_ratio` |
| `411` | Upload without `Content-Length` |
| `503` + `Retry-After` | Upload budget full (`max_inflight_upload_mb`), or a pipeline worker / LibreOffice instance crashed: retry later |
| `504` | Page range or LibreOffice conversion over its timeout |
| `499` | Client disconnected: the processing was cancelled |

### Response metadata fields

| Field | Type | Description |
|---|---|---|
| `active_conversion_time_no_img_desc` | `int` (seconds) | Accumulated active CPU/GPU time for document→Markdown conversion. Excludes semaphore wait times and VLM image description time. |
| `img_desc_time` | `int` (seconds) | Accumulated active time for VLM image description calls. Sum of individual call durations (does not account for concurrency). |
| `wall_clock_time` | `int` (seconds) | Total wall-clock latency as seen by the client (includes all waits). |

---

## 🚀 Installation & running

### Requirements

- NVIDIA GPU with CUDA 13.0 (may work on other devices but untested)

- **PaddleOCR-VL-1.5** served via an OpenAI-compatible endpoint (vLLM recommended — see `./docker`)

### Option 1 — Native (tested on Ubuntu)

#### Install system dependancies:
  - Python 3.13 (can be automatically installed by uv )
- [uv](https://github.com/astral-sh/uv) for python dependency management
- System packages (for Ubuntu 24.04):
  ```bash
  apt install -y --no-install-recommends \
      libgl1 \
      libreoffice \
      libmagic1t64 \
      fontconfig \
      fonts-dejavu-core \
      fonts-liberation \
      fonts-noto-cjk \
      fonts-wqy-microhei \
      fonts-freefont-ttf
  # On Ubuntu <= 22.04 use 'libmagic1' instead of 'libmagic1t64'
  # Not tested on non-Debian based linux.
  ```
#### Install and run the app:
```bash
uv sync --no-dev

# Make sure the user running the app can write to the log/artifact files (defaults are /var/log/foil/)
# sudo mkdir -p /var/log/foil && sudo chown <user> /var/log/foil

uv run --no-sync src/foil_serve/main.py  # default listening on 0.0.0.0:8081
# or
cd src/foil_serve && uv run --no-sync uvicorn main:app --host 0.0.0.0 --port 8081

```
`uvicorn` is included in the project dependencies — no separate installation needed.

### Option 2 — Docker (air-gapped)

Paddle native models are included in the foil-serve `-offline` image; the models served by vLLM (PaddleOCR-VL-1.5, and any model used for image description) must be in the host's Hugging Face cache.

| Directory | Target | Stack |
|---|---|---|
| `docker/` | any GPU | foil + PaddleOCR-VL vLLM + Ministral vLLM |
| `docker/rtx3070/` | RTX 3070, 8 GB (measured) | foil + PaddleOCR-VL vLLM, VLM on another server |
| `docker/rtx4090/` | RTX 4090, 24 GB | foil + PaddleOCR-VL vLLM + Ministral vLLM |
| `docker/a100/` | A100 with 65 GB available | foil + PaddleOCR-VL vLLM, VLM on another server |
| `docker/h100/` | H100 with 20 GB available | foil + PaddleOCR-VL vLLM, VLM on another server |

```bash
cd docker/rtx3070 && docker compose up -d --wait   # → http://localhost:8081/docs
```

Sizing rules and measured unit costs (worker, vLLM, LibreOffice): end of `pipeline_config.yaml`.

---

## ⚙️ Configuration

- **`pipeline_config.yaml`** — PaddleOCR-VL-1.5 pipeline settings. Update the vLLM URL to point to your model endpoint. Other pipeline settings can be changed at your own risk 😉.
- **`server_config.toml`** — API keys, VLM model definitions, prompts, resource limits, spreadsheet processing options, and OCR output control.

Every `server_config.toml` key is required and unknown keys are rejected: the server refuses to start rather than fall back to a hidden default.

### VLM model config (`server_config.toml`)

```toml
[[vlm_models]]
enabled = true
name = "my-model"               # used as `image_description_model_name` query param
url = "http://host:port/v1"
endpoint_name = "org/model-id"  # exact model name for the OpenAI endpoint
endpoint_api_key = "sk-..."
temperature = 0.0
max_output_tokens = 4000
max_input_ocr_length = 700      # max chars of OCR text injected into the VLM prompt
max_concurrent_requests = 10
prompt = "default"              # key from [prompts] section or a direct prompt string
extra_body = {chat_template_kwargs = {enable_thinking = false}} # optional extra body parameter (e.g. disable thinking on vllm)
```

### Small images (`server_config.toml`)

```toml
small_image_threshold = [64, 64]  # small = width OR height below this (pixels); never described by the VLM
ignore_small_images = true        # true: dropped from the Markdown and `images`
                                  # false: returned, referenced by a bare <img> (OCR kept if < 40 chars)
```

### Spreadsheet processing (`server_config.toml`)


| Setting | Shipped value | Applies to | Description |
|---|---|---|---|
| `table_output_format` | `"llm"` | all | `"llm"` compact or `"human"` aligned — also used for HTML tables from the OCR pipeline |
| `excel_mask_cell_errors` | `true` | pandas | Mask error cells with an empty string, or label them (`#ref`, `#n/a`, …) |
| `excel_max_output_ratio` | `7.0` | all | Size budget **per conversion section**, as a ratio of the input size. An oversized section is dropped with a note; HTTP 413 only when none survives |
| `excel_pdf_fallback_enabled` | `true` | `auto` only | Fall back to PDF+OCR for empty or sparse spreadsheets |
| `excel_min_input_for_fallback_mb` | `0.25` | `auto` only | Skip the sparse check below this input size |
| `excel_min_output_ratio` | `0.01` | `auto` only | Below this output/input ratio the file is considered sparse (picture and chart bytes excluded from the input size). Overridable per request |
| `excel_extract_images` | `true` | `auto`, `pandas`, `both` | Extract pictures and charts under each sheet's table. `false`: cell values only |
| `excel_render_charts` | `true` | `auto`, `pandas`, `both` | Render charts to images with LibreOffice. `false`: text summary only |
| `excel_rasterize_metafiles` | `true` | `auto`, `pandas`, `both` | Render EMF/WMF pictures with LibreOffice. `false`: they are skipped |
| `excel_max_images` | `200` | `auto`, `pandas`, `both` | Distinct images per document. Beyond it, pictures are dropped with a note, charts keep their summary |
| `excel_pdf_paper_format` | `"A3"` | `ocr`, `both`, `auto` fallback | `A2`, `A3`, `A4`, `Letter`, `Legal` or `Tabloid` |
| `excel_pdf_landscape` | `true` | `ocr`, `both`, `auto` fallback | Landscape suits wide sheets; portrait suits narrow ones |

### OCR output control (`server_config.toml`)

Controls whether `<ocr>` tags (Paddle OCR text on images) appear in the final Markdown:

| Setting | Shipped value | Description |
|---|---|---|
| `output_paddle_ocr` | `false` | When VLM image description **is** requested: keep `<ocr>` tags alongside `<figcaption>`. |
| `output_paddle_ocr_no_img_desc` | `false` | When **no** VLM is requested: keep `<ocr>` tags. If `false`, the OCR-on-images pipeline step is skipped entirely (performance optimization). |
| `add_pdf_page` | `true` | Start each page of a paginated document (PDF, office documents, multipage TIFF/WebP) with `<page number="n/n_max" />`. Spreadsheets and single images are never marked. |

When both OCR output and VLM are disabled, `<figcaption>` tags are omitted, and the image-block OCR step is skipped, reducing processing time.

---

## ⚠️ Known limitations

### Images without text
When the pipeline finds no block at all (a photo, a drawing without text) and an image description is requested, the whole pages are sent to the VLM (documents up to `empty_document_vlm_max_pages` pages). Without a VLM, such a document answers 422.

### Embedded objects in spreadsheets
The pandas conversion extracts cell content, pictures and charts. Text boxes, shapes, SmartArt, embedded OLE objects (Word, PDF, Visio…), cell comments and Excel 365 "place in cell" pictures are ignored. The `ocr` and `both` modes (and the `auto` fallback) render the sheets to PDF first, which does capture those visual elements, but with OCR fidelity rather than exact values. Excel 2016+ chart types (waterfall, treemap, sunburst…) get a best-effort summary and may not render. Charts written by tools that store no cached values (e.g. openpyxl) have unnamed series in their summary.

### OCR cost on large spreadsheets
A dense spreadsheet renders to as many PDF pages as it needs, and each page goes through the model. A file with tens of thousands of rows can therefore keep the Paddle workers busy for a long time in `ocr` or `both` mode. Something to keep in mind before lowering `excel_min_output_ratio` or routing large files to a fixed OCR mode.

### Upside-down images
Extracted images may be rotated relative to the original document.

### PDF conversion
Office documents (excluding spreadsheets) are converted to PDF before OCR. This can occasionally cause rendering issues (e.g., overlapping text).

---

## 🛠️ Developer notes

### Project layout

| Module | Role |
|---|---|
| `main.py` | Application assembly only — lifespan (runtime dirs, semaphores, LibreOffice pool, PaddleOCR workers + startup smoke test), the upload budget middleware and the router mount |
| `admission.py` | Upload budget (`max_inflight_upload_mb`: 503 + Retry-After beyond) and the open-files limit |
| `api.py` | The HTTP surface. The six fixed-mode routes are generated by one closure factory rather than written out |
| `processing.py` | `process_document()` — the conversion orchestration. Owns what the domain modules deliberately do not: mapping domain errors to HTTP status codes, and accounting *active* time |
| `spreadsheet.py` | Cell extraction, figure blocks, header + table-of-contents assembly, and the pure decision rules (`resolve_strategy`, `is_sparse`) |
| `spreadsheet_drawings.py` | Pictures and charts of a workbook: cheap survey (before the fallback decision), native `.xlsx` / `.ods` parsing, chart summaries, decoding |
| `pipeline.py` / `worker_pool.py` | PaddleOCR in `pipeline_workers` processes, documents split in page ranges spread over idle workers; crash → 503, timeout → 504. Returns **one Markdown string per page** |
| `page_images.py` | Pages rendered for the pipeline (pypdfium2 + Pillow, pixel cap) |
| `libreoffice.py` | Pool of `max_concurrent_libreoffice` instances (one conversion each, restarted after every use), Office/spreadsheet → PDF, the per-sheet page counts, chart / metafile rendering |
| `postprocessing.py` / `table_utils.py` | Figure blocks, per-page helpers, HTML table simplification |
| `external.py`, `vlm.py`, `utils.py`, `debug.py`, `settings.py`, `schemas.py`, `security.py` | Remote backends, VLM fan-out, MIME/size/archive helpers, failure artifacts, config, models, auth |

Two conventions worth knowing before editing:

- **Active time excludes semaphore waits.** `ActiveTimer.track()` is always opened *inside* an `async with <semaphore>` block, never around it.
- **The pipeline output stays split per page** until the very end. Post-processing runs page by page so output line numbers remain mappable to PDF pages — that is what makes the spreadsheet table of contents exact. Join with `postprocessing.join_pages()`.

### Tests

```bash
uv run pytest tests/          # offline suite
uv run pytest tests/live/ -v  # end-to-end, needs a running server (skipped otherwise)
```

`tests/live/` talks to an actual instance and is skipped automatically when none answers on `$FOIL_SERVE_URL` (default `http://127.0.0.1:8081`). It covers what the offline suite structurally cannot — chiefly that the sheet → page map LibreOffice reports lines up with the pages PaddleOCR returns. See `tests/README.md` for the full layout and for the fixture generator.

### Memory management & worker recycling
PaddleOCR is not thread-safe: each pipeline runs in its own `spawn`ed worker process. A worker's RSS grows with the largest page range it processed (memory Paddle frees stays in glibc's arenas, no leak measured) and `pipeline_malloc_trim` gives it back after each task: ~2.3 GB per worker, flat. Recycling (`max_tasks_between_pipeline_reload`, `max_worker_rss_mb`) is only a safety net, staggered so that the service never stops. `pipeline_worker_vram_limit_mb` caps each worker's VRAM.

### Why vLLM for PaddleOCR-VL-1.5
Running PaddleOCR-VL-1.5 natively proved problematic (*Exception from the 'vlm' worker: only 0-dimensional arrays can be converted to Python scalars*).
The model is instead served via vLLM through an OpenAI-compatible endpoint. This also benefits from faster inference compared to the native Paddle runtime, and keeps the 0.9B model out of the worker processes.

### Debug artifact saving
When `save_failed_artifacts = true` in `server_config.toml`, any processing failure creates a timestamped subdirectory under `artifact_dir/failed_artifacts_subdir` (default `/var/log/foil/artifacts/failed`):

```
<yy-mm-dd_hh-mm>_<mime-type>/
  input_file.<ext>     — copy of the input file
  converted.pdf        — intermediate PDF (if conversion happened before the error)
  partial_output.md    — last markdown state (if the pipeline ran before the error)
  meta.txt             — app version, mime, timing, sha256, RAM/VRAM/CPU state
  trace.txt            — full exception traceback
```

This directory is not automatically cleaned up — manage disk space manually.

Additional artifact types can be enabled independently:
- `save_table_conversion_artifacts`: saves the input spreadsheet + the generated PDF whenever a spreadsheet is rendered to PDF (`ocr`, `both`, or the `auto` fallback).
- `save_cell_error_artifacts`: saves Markdown before/after error masking when cell errors are detected.

### LibreOffice UNO transport (Unix domain socket)
Each LibreOffice instance is reached over a UNO **pipe**, which on Linux is a Unix domain socket — **no TCP port is exposed**. The pipe name is unique per start and namespaced with `foil_soffice_`; the osl library creates the socket under `/tmp` (`OSL_PIPE_<euid>_foil_soffice_<pid>_<rnd>`). foil-serve does **not** hard-code that location: the real socket path is resolved from the kernel via `/proc/net/unix`, matched by the unique pipe name (see `_resolve_socket_path()` in `libreoffice.py`).

Deployment notes (relevant for non-root containers):
- The app must be able to **write to `/tmp`** (the default `1777` permissions are fine) — that's where osl places the socket, and it ignores `$TMPDIR`.
- soffice and the UNO client run under the **same UID** (same process tree), so the socket's owner-only write bit is sufficient — no cross-user access is involved.
- Residual sockets left by a hard crash are cleaned up automatically at startup: within the resolved socket directory, any `foil_soffice_*` socket with no live listener is unlinked (a socket still accepting connections — e.g. another instance — is left untouched). See `_sweep_dead_pipes()` in `libreoffice.py`.

### Single uvicorn worker
Run foil with **one** uvicorn worker. The heavy work already runs outside the event loop (pipeline worker processes, LibreOffice instances, threads), so a second uvicorn worker adds no throughput; it would also start its own pipeline workers (VRAM ×2) and its own LibreOffice pool on the same profile directories (`lo_profile_<slot>`, whose stale processes are killed at start), and split the upload budget and the description cache. To scale, raise `pipeline_workers` and `max_concurrent_libreoffice`.

### Architecture: from the upload to the Markdown

Each request is one coroutine in the API process; it hands the heavy work to pools of processes or threads and waits for them. A request only holds a worker while that worker is working for it.

```
client ── POST /v1/process
  │
  ├─ 1. admission          API process · RAM: the upload           max_inflight_upload_mb → 503 beyond
  ├─ 2. type + size checks  thread · temp_dir on disk
  │    ├─ external MIME ──► remote foil-compatible server           [[external_processors]].max_concurrent_requests
  │    ├─ text ───────────► returned as is
  │    └─ spreadsheet ────► 3. pandas: thread · CPU, RAM             max_concurrent_excel
  │                            (pictures / charts: LibreOffice, 4.)
  ├─ 4. LibreOffice         soffice processes · CPU, RAM            max_concurrent_libreoffice (FIFO queue)
  │      office → PDF, spreadsheet → PDF, charts, EMF, .xls          libreoffice_conversion_timeout_s → 504
  ├─ 5. pipeline            Paddle worker processes · CPU, RAM, VRAM pipeline_workers (fair queue)
  │      document split in ranges of pipeline_chunk_pages pages      pipeline_task_timeout_s → 504
  │      per range: render pages (CPU) → orientation + layout (GPU) → crops
  │      └─ 6. PaddleOCR-VL vLLM   separate container · GPU          max_concurrency per worker (pipeline_config.yaml)
  │                                                                  max_num_seqs, kv-cache-memory-bytes (vLLM)
  ├─ 7. post-processing     threads · CPU (tables, OCR text)
  ├─ 8. image descriptions  async calls to the VLM server             [[vlm_models]].max_concurrent_requests
  │                         exact-match cache · RAM                  vlm_description_cache_mb
  └─ 9. Markdown + images   API process · RAM (base64 images)  ──► JSON or tar.zst
```

A client that disconnects cancels its request at whatever stage it is in (499): it leaves the queues, its LibreOffice conversion is killed, no further page range starts.

| Stage | Runs in | Uses | Unit cost (measured, RTX 3070) | Becomes the bottleneck when |
|---|---|---|---|---|
| 1. Admission | API event loop | RAM | the size of each upload in progress | uploads pile up faster than they are processed: 503 |
| 3. pandas | thread | CPU, RAM | not measured: grows with the sheets | many big spreadsheets at once |
| 4. LibreOffice | `max_concurrent_libreoffice` soffice processes, restarted after each use (~1 s) | ~1 CPU core each, RAM | 0.1-0.35 GB, up to ~2 GB on a heavy docx; seconds to 20 min | long conversions occupy every instance: the others wait in the queue |
| 5. Pipeline worker | `pipeline_workers` processes, one page range each at a time | CPU (rendering), VRAM (layout models), RAM | VRAM 0.8 GB (layout batch 1) to 2.5 GB (batch 4); RAM ~2.3 GB, peak ~2.6 GB | too few workers to keep vLLM busy (`num_requests_running` well below `max_num_seqs`) |
| 6. PaddleOCR-VL vLLM | its own container | GPU compute, VRAM | ~2 GB + KV 18.4 KB/token; 0.5-2k tokens per crop | usually: requests waiting in vLLM, GPU at 100 % |
| 8. VLM | the VLM server (local or remote) | its GPU | Ministral-3-3B: 3.4 GB + 107 KB/token; 0.7-2.7k tokens per description | descriptions requested on figure-heavy documents |

Where a request waits: the LibreOffice queue (FIFO), the pipeline queue (an idle worker is taken at once; with several documents the workers go round-robin between them), vLLM's queue (FIFO), and the VLM semaphore. `active_conversion_time_no_img_desc` excludes these waits, `wall_clock_time` includes them.

**Setting the concurrency parameters**

1. **VRAM** = PaddleOCR-VL vLLM (base + KV) + `pipeline_workers` × worker VRAM (+ the VLM if it shares the GPU). Cap each worker with `pipeline_worker_vram_limit_mb`, a bit above its layout batch's need. Small layout batches cost little speed: prefer more workers.
2. **Workers vs vLLM**: on a 3070, 3 workers keep a 10-sequence vLLM busy (1.36 pages/s, a 4th adds nothing). On a faster GPU, scale both by its speed. Then keep `pipeline_workers` × `max_concurrency` ≈ 3 × `max_num_seqs`, and the KV ≥ `max_num_seqs` × ~3k tokens.
3. **LibreOffice**: about half the workers (at least 3), more if long conversions are common. RAM ≈ instances × 2 GB in the worst case.
4. **VLM**: `max_concurrent_requests` = the parallel sequences of the VLM server.
5. **Host RAM** ≈ `pipeline_workers` × 2.6 GB + LibreOffice instances × 2 GB + vLLM (~3-6 GB) + `max_inflight_upload_mb` + ~2 GB for the API.
6. Check under load with vLLM's `/metrics` and the table at the end of `pipeline_config.yaml`.


---

## 🙏 Acknowledgements

foil-serve would not exist without:

- **[PaddleOCR / PaddlePaddle](https://github.com/PaddlePaddle/PaddleOCR)** — the OCR backbone powering document understanding.
- **[LibreOffice](https://www.libreoffice.org/)** — handling Office format conversion. Running headless, doing its job without complaint.