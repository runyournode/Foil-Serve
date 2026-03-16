# Foil Serve 🏄‍♂️

> Document → Markdown conversion server, built on PaddleOCR — with meaningful extras.

**Foil Serve** is a [FastAPI](https://fastapi.tiangolo.com/) server that converts common document formats to structured Markdown. It uses [PaddleOCR-VL-1.5](https://github.com/PaddlePaddle/PaddleOCR) as its OCR backbone, served via vLLM, and adds several practical improvements for LLM/RAG workflows.

---

## ✨ What's on top of PaddleOCR

### 🖼️ External VLM image description
Extracted figures can be described by any OpenAI-compatible VLM of your choice. The description is injected directly into the Markdown inside a proper `<figcaption>` tag, keeping the output clean and semantically structured.

### 📊 HTML table simplification
PaddleOCR outputs verbose HTML tables. foil-serve strips redundant formatting attributes and flattens the structure — reducing token count by 3–5 × without losing semantic content. This matters when feeding documents into a RAG pipeline or an LLM with limited context.

### 📁 Extended input format support

| Format | Conversion | Post-processing |
|---|---|---|
| `.txt`, `.json`, `.csv`, `.xml` | Pass-through (returned as-is) | — |
| `.pdf`, `.png`, `.jpg`, `.bmp` | PaddleOCR-VL natively | HTML table simplification, optional VLM image description |
| `.tiff` (incl. multi-page), `.webp` | Pillow → PDF → PaddleOCR-VL | HTML table simplification, optional VLM image description |
| `.xls`, `.xlsx`, `.ods` | Pandas + Tabulate → Markdown tables | — |
| `.docx`, `.doc`, `.pptx`, `.ppt`, `.odt`, `.odp` | LibreOffice → PDF → PaddleOCR-VL | HTML table simplification, optional VLM image description |

**PDF conversion:** when converting `.docx` and `.doc` files to PDF via LibreOffice, tracked changes (revisions) are automatically accepted, so the output reflects the final state of the document. Inline comments are **not** captured in the conversion.

Only MIME types listed in [`utils.mime_def`](src/foil_serve/utils.py) are accepted.

Support for additional formats is welcome — contributions are open 🙌


---

## 🔌 API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/docs` | `GET` | Interactive Swagger documentation (offline-enabled) |
| `/v1/process` | `POST` | Convert document to Markdown + extract images (JSON response) |
| `/v1/process/download` | `POST` | Same as above but returns a `tar.zst` archive |
| `/v1/vlm_models` | `GET` | List available VLM models for image description |
| `/health` | `GET` | Server health check, optionally validates VLM endpoints |

### `POST /v1/process`

**Request:** raw file bytes (`application/octet-stream`) + optional query params:
- `image_description_model_name`: name of the VLM model to use for image description (see `/v1/vlm_models`)

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
- Python 3.13
- [uv](https://github.com/astral-sh/uv) for dependency management
- System packages (for Ubuntu 22.04):
  ```bash
  apt install -y --no-install-recommends \
      libgl1 \
      libreoffice \
      libmagic1 \
      fontconfig \
      fonts-dejavu-core \
      fonts-liberation \
      fonts-noto-cjk \
      fonts-wqy-microhei \
      fonts-freefont-ttf
  ```
- **PaddleOCR-VL-1.5** served via an OpenAI-compatible endpoint (vLLM recommended — see `./docker`)

### Option 1 — Native (tested on Ubuntu)

```bash
uv sync --no-dev

uv run --no-sync src/foil_serve/main.py  # default listening on 0.0.0.0:8081
# or
cd src/foil_serve && uv run --no-sync uvicorn main:app --host 0.0.0.0 --port 8081

```


`uvicorn` is included in the project dependencies — no separate installation needed.

### Option 2 — Docker (air-gapped)

A ready-to-use stack (foil-serve + 2× vLLM services) is available in `./docker`.  
Paddle native models are included in the foil-serve image, but you will still need to download the PaddleOCR-VL-1.5 model for the vllm server.  
To be released: a smaller image without any model.

```bash
cd docker/
docker compose up -d
```

---

## ⚙️ Configuration

- **`pipeline_config.yaml`** — PaddleOCR-VL-1.5 pipeline settings. Update the vLLM URL to point to your model endpoint. Other pipeline settings can be changed at your own risk 😉.
- **`server_config.toml`** — API keys, VLM model definitions, prompts, and resource limits.

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
```

---

## ⚠️ Known limitations

### Images without text
If an image-only document contains no text, the Paddle pipeline may produce sparse Markdown (without even referencing the image), causing the VLM description step to be skipped. This server is not recommended for pure image description use cases.

### Embedded objects in spreadsheets
Only cell content is extracted from spreadsheet files. Embedded images, charts, and text boxes are ignored.

### Upside-down images
Extracted images may be rotated relative to the original document.

### PDF conversion
Office documents (excluding spreadsheets) are converted to PDF before OCR. This can occasionally cause rendering issues (e.g., overlapping text).

---

## 🛠️ Developer notes

### Memory management & worker recycling
PaddleOCR is not thread-safe and leaks GPU/CPU memory over time. The current workaround uses a `spawn`-based multiprocessing pool (`processes=1`) that recycles the worker process every N documents (`max_tasks_between_pipeline_reload` in `server_config.toml`, default 5). Each recycle takes a few seconds while the pipeline reloads.

The root cause of the memory leak has not been fully identified — feel free to investigate 🔍

### Why vLLM for PaddleOCR-VL-1.5
Running PaddleOCR-VL-1.5 natively proved problematic (*Exception from the 'vlm' worker: only 0-dimensional arrays can be converted to Python scalars*).
The model is instead served via vLLM through an OpenAI-compatible endpoint. This also benefits from faster inference compared to the native Paddle runtime, and greatly reduces wasted time during [worker recycling](#memory-management--worker-recycling).

### Debug artifact saving
When `save_failed_artifacts = true` in `server_config.toml`, any processing failure creates a timestamped subdirectory under `failed_artifacts_dir` (default `/tmp/paddleocr_failed`):

### Single uvicorn worker
Foil Serve is not compatible with multiple uvicorn worker (because of the way we spawn and kill the LibreOffice server). 
This should not be too hard to solve, but I don't expect uvicorn worker to be the bottleneck.
If extra ressources are available and scaling is required, it would probably be better to try increasing the number of paddle pipeline.

```
<yy-mm-dd_hh-mm>_<mime-type>/
  input_file.<ext>     — copy of the input file
  converted.pdf        — intermediate PDF (if conversion happened before the error)
  partial_output.md    — last markdown state (if the pipeline ran before the error)
  meta.txt             — app version, mime, timing, sha256, RAM/VRAM/CPU state
  trace.txt            — full exception traceback
```

This directory is not automatically cleaned up — manage disk space manually.

---

## 🙏 Acknowledgements

foil-serve would not exist without:

- **[PaddleOCR / PaddlePaddle](https://github.com/PaddlePaddle/PaddleOCR)** — the OCR backbone powering document understanding.
- **[LibreOffice](https://www.libreoffice.org/)** — the quiet workhorse handling Office format conversion. Running headless, doing its job without complaint.