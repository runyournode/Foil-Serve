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

### 📁 Extended input format support

| Format | Conversion | Post-processing |
|---|---|---|
| `.txt`, `.json`, `.csv`, `.xml`, `.md` | Pass-through (smart encoding detection) | — |
| `.pdf`, `.png`, `.jpg`, `.bmp` | PaddleOCR-VL natively | HTML tables → Markdown or simplified HTML, optional VLM image description |
| `.tiff` (incl. multi-page), `.webp` | Pillow → PDF → PaddleOCR-VL | HTML tables → Markdown or simplified HTML, optional VLM image description |
| `.xls`, `.xlsx`, `.ods` | Pandas → Markdown tables, LibreOffice → PDF → PaddleOCR-VL, or both — see *Spreadsheet conversion strategies* | Cell error masking, header + table of contents, per-section size budget |
| `.docx`, `.doc`, `.pptx`, `.ppt`, `.odt`, `.odp` | LibreOffice → PDF → PaddleOCR-VL | HTML tables → Markdown or simplified HTML, optional VLM image description |


**PDF conversion:** when converting `.docx` and `.doc` files to PDF via LibreOffice, tracked changes (revisions) are automatically accepted, so the output reflects the final state of the document. Inline comments are **not** captured in the conversion.

**Spreadsheet processing:** cell errors (`#REF!`, `#N/A`, `nan`, etc.) are detected and masked by default. Legacy-encrypted `.xls` files (empty password) are automatically converted to `.xlsx` via LibreOffice before processing. The Markdown table format (`table_output_format`: `"llm"` compact or `"human"` aligned) applies to both spreadsheet tables and HTML tables converted from the OCR pipeline.

In `auto` mode, a spreadsheet that produces very little cell data (content mostly in text boxes, images or shapes) falls back to PDF+OCR via LibreOffice — configurable paper format and orientation, default A3 landscape, fit-to-width. The threshold is `excel_min_output_ratio`, which can be **overridden per request** with the query param of the same name; the check itself only runs above `excel_min_input_for_fallback_mb`, and the whole fallback can be turned off with `excel_pdf_fallback_enabled`. An empty spreadsheet returns HTTP 422 when no OCR conversion is going to run.

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
    "page_1_figure_1.jpg": "<base64 JPEG>"
  },
  "metadata": {
    "active_conversion_time_no_img_desc": 12,
    "img_desc_time": 20,
    "wall_clock_time": 18
  }
}
```

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

A ready-to-use stack (foil-serve + 2× vLLM services) is available in `./docker`.  
Paddle native models are included in the foil-serve `-offline` image, but you will still need to download the models for the vllm servers (PaddleOCR-VL-1.5 and any model used for image description).  

#### Run the docker stack
```bash
cd docker/
docker compose up -d
```

---

## ⚙️ Configuration

- **`pipeline_config.yaml`** — PaddleOCR-VL-1.5 pipeline settings. Update the vLLM URL to point to your model endpoint. Other pipeline settings can be changed at your own risk 😉.
- **`server_config.toml`** — API keys, VLM model definitions, prompts, resource limits, spreadsheet processing options, and OCR output control.

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
min_size = [64, 64]             # minimum image dimensions to process (pixels)
max_concurrent_requests = 10
prompt = "default"              # key from [prompts] section or a direct prompt string
extra_body = {chat_template_kwargs = {enable_thinking = false}} # optional extra body parameter (e.g. disable thinking on vllm)
```

### Spreadsheet processing (`server_config.toml`)

| Setting | Default | Applies to | Description |
|---|---|---|---|
| `table_output_format` | `"llm"` | all | `"llm"` compact or `"human"` aligned — also used for HTML tables from the OCR pipeline |
| `excel_mask_cell_errors` | `true` | pandas | Mask error cells with an empty string, or label them (`#ref`, `#n/a`, …) |
| `excel_max_output_ratio` | `5.0` | all | Size budget **per conversion section**, as a ratio of the input size. An oversized section is dropped with a note; HTTP 413 only when none survives |
| `excel_pdf_fallback_enabled` | `true` | `auto` only | Fall back to PDF+OCR for empty or sparse spreadsheets |
| `excel_min_input_for_fallback_mb` | `0.5` | `auto` only | Skip the sparse check below this input size |
| `excel_min_output_ratio` | `0.01` | `auto` only | Below this output/input ratio the file is considered sparse. Overridable per request |
| `excel_pdf_paper_format` | `"A3"` | `ocr`, `both`, `auto` fallback | `A2`, `A3`, `A4`, `Letter`, `Legal` or `Tabloid` |
| `excel_pdf_landscape` | `true` | `ocr`, `both`, `auto` fallback | Landscape suits wide sheets; portrait suits narrow ones |

### OCR output control (`server_config.toml`)

Controls whether `<ocr>` tags (Paddle OCR text on images) appear in the final Markdown:

| Setting | Default | Description |
|---|---|---|
| `output_paddle_ocr` | `false` | When VLM image description **is** requested: keep `<ocr>` tags alongside `<figcaption>`. |
| `output_paddle_ocr_no_img_desc` | `true` | When **no** VLM is requested: keep `<ocr>` tags. If `false`, the OCR-on-images pipeline step is skipped entirely (performance optimization). |

When both OCR output and VLM are disabled, `<figcaption>` tags are omitted, and the image-block OCR step is skipped, reducing processing time.

---

## ⚠️ Known limitations

### Images without text
If an image-only document contains no text, the Paddle pipeline may produce sparse Markdown (without even referencing the image), causing the VLM description step to be skipped. This server is not recommended for pure image description use cases.

### Embedded objects in spreadsheets
The pandas conversion extracts cell content only — embedded images, charts and text boxes are ignored. The `ocr` and `both` modes (and the `auto` fallback) render the sheets to PDF first, which does capture those visual elements, but with OCR fidelity rather than exact values.

### OCR cost on large spreadsheets
A dense spreadsheet renders to as many PDF pages as it needs, and each page goes through the model. A file with tens of thousands of rows can therefore occupy the single Paddle worker for a long time in `ocr` or `both` mode. Something to keep in mind before lowering `excel_min_output_ratio` or routing large files to a fixed OCR mode.

### Upside-down images
Extracted images may be rotated relative to the original document.

### PDF conversion
Office documents (excluding spreadsheets) are converted to PDF before OCR. This can occasionally cause rendering issues (e.g., overlapping text).

---

## 🛠️ Developer notes

### Project layout

| Module | Role |
|---|---|
| `main.py` | Application assembly only — lifespan (runtime dirs, semaphores, LibreOffice server, PaddleOCR pipeline + startup smoke test) and the router mount |
| `api.py` | The HTTP surface. The six fixed-mode routes are generated by one closure factory rather than written out |
| `processing.py` | `process_document()` — the conversion orchestration. Owns what the domain modules deliberately do not: mapping domain errors to HTTP status codes, and accounting *active* time |
| `spreadsheet.py` | Cell extraction, header + table-of-contents assembly, and the pure decision rules (`resolve_strategy`, `is_sparse`) |
| `pipeline.py` | The PaddleOCR worker pool. Returns **one Markdown string per PDF page** |
| `libreoffice.py` | Persistent UNO server, Office/spreadsheet → PDF, and the per-sheet page counts |
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
PaddleOCR is not thread-safe and leaks GPU/CPU memory over time. The current workaround uses a `spawn`-based multiprocessing pool (`processes=1`) that recycles the worker process every N documents (`max_tasks_between_pipeline_reload` in `server_config.toml`, default 5). Each recycle takes a few seconds while the pipeline reloads.

The root cause of the memory leak has not been fully identified — feel free to investigate 🔍

### Why vLLM for PaddleOCR-VL-1.5
Running PaddleOCR-VL-1.5 natively proved problematic (*Exception from the 'vlm' worker: only 0-dimensional arrays can be converted to Python scalars*).
The model is instead served via vLLM through an OpenAI-compatible endpoint. This also benefits from faster inference compared to the native Paddle runtime, and greatly reduces wasted time during [worker recycling](#memory-management--worker-recycling).

### Debug artifact saving
When `save_failed_artifacts = true` in `server_config.toml`, any processing failure creates a timestamped subdirectory under `failed_artifacts_dir` (default `/foil-log/failed`):

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
The persistent LibreOffice server is reached over a UNO **pipe**, which on Linux is a Unix domain socket — **no TCP port is exposed**. The pipe name is unique per start and namespaced with `foil_soffice_`; the osl library creates the socket under `/tmp` (`OSL_PIPE_<euid>_foil_soffice_<pid>_<rnd>`). foil-serve does **not** hard-code that location: the real socket path is resolved from the kernel via `/proc/net/unix`, matched by the unique pipe name (see `_resolve_socket_path()` in `libreoffice.py`).

Deployment notes (relevant for non-root containers):
- The app must be able to **write to `/tmp`** (the default `1777` permissions are fine) — that's where osl places the socket, and it ignores `$TMPDIR`.
- The soffice server and the UNO client run under the **same UID** (same process tree), so the socket's owner-only write bit is sufficient — no cross-user access is involved.
- Residual sockets left by a hard crash are cleaned up automatically at startup: within the resolved socket directory, any `foil_soffice_*` socket with no live listener is unlinked (a socket still accepting connections — e.g. another instance — is left untouched). See `_sweep_dead_pipes()` in `libreoffice.py`.

### Single uvicorn worker
Foil Serve is not compatible with multiple uvicorn worker (because of the way we spawn and kill the LibreOffice server). 
This should not be too hard to solve, but I don't expect uvicorn worker to be the bottleneck.

The UNO pipe name is already unique per process. Making it multi-worker-safe would additionally require **namespacing each worker**:
- a per-worker LibreOffice profile (`-env:UserInstallation=file://<per-worker dir>`) — otherwise LibreOffice's `SingleOfficeIPC` mechanism routes every worker to a single soffice instance;
- a per-worker PID file (the current `soffice.pid` path is shared).

If extra ressources are available and scaling is required, it would probably be better to try increasing the number of paddle pipeline.


---

## 🙏 Acknowledgements

foil-serve would not exist without:

- **[PaddleOCR / PaddlePaddle](https://github.com/PaddlePaddle/PaddleOCR)** — the OCR backbone powering document understanding.
- **[LibreOffice](https://www.libreoffice.org/)** — handling Office format conversion. Running headless, doing its job without complaint.