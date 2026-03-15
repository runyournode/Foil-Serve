import asyncio
import hashlib
import importlib.metadata
import logging
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

try:
    _APP_VERSION = importlib.metadata.version("foil_serve")
except importlib.metadata.PackageNotFoundError:
    _APP_VERSION = "unknown"


@dataclass
class ArtifactContext:
    """
    Tracks per-request state for debug artifact saving.

    Fields are updated progressively as processing phases complete.
    Call save() on error to persist all available artifacts to disk.
    """

    artifacts_dir: Path
    t0_wall: float
    image_description_model_name: str | None = None
    # Updated as phases complete:
    raw_mime: str = "unknown"
    prepared_path: Path | None = None
    converted_pdf: Path | None = None
    partial_md: str | None = None

    async def save(
        self,
        exc: BaseException,
        t_active: float,
        img_desc_time: float = 0.0,
    ) -> None:
        """Persist artifacts asynchronously (non-blocking via asyncio.to_thread).

        wall_clock_time is computed at call time (moment of the error) for accuracy.
        """
        wall_clock_time = time.perf_counter() - self.t0_wall
        await asyncio.to_thread(
            save_failed_artifacts,
            artifacts_dir=self.artifacts_dir,
            raw_mime=self.raw_mime,
            exc=exc,
            prepared_path=self.prepared_path,
            converted_pdf=self.converted_pdf,
            partial_md=self.partial_md,
            t_active=t_active,
            wall_clock_time=wall_clock_time,
            img_desc_time=img_desc_time,
            image_description_model_name=self.image_description_model_name,
        )


def save_failed_artifacts(
    artifacts_dir: Path,
    raw_mime: str,
    exc: BaseException,
    prepared_path: Path | None = None,
    converted_pdf: Path | None = None,
    partial_md: str | None = None,
    t_active: float = 0.0,
    wall_clock_time: float = 0.0,
    img_desc_time: float = 0.0,
    image_description_model_name: str | None = None,
) -> None:
    """
    Save failed processing artifacts to a timestamped subdirectory for debugging.

    Called on processing errors (conversion failure, pipeline error, etc.).
    All internal errors are silently logged to avoid masking the original exception.

    Directory structure:
      <artifacts_dir>/<yy-mm-dd_hh-mm>_<safe-mime>/
        input_file.<ext>      — copy of the input file (if still on disk)
        converted.pdf         — intermediate PDF (if conversion happened)
        partial_output.md     — last Markdown state (if available)
        meta.txt              — timing, sha256, RAM/VRAM, CPU, version
        trace.txt             — full exception traceback
    """
    try:
        timestamp = datetime.now().strftime("%y-%m-%d_%H-%M")
        # Replace characters not safe in directory names
        safe_mime = raw_mime.replace("/", "-").replace("+", "-").replace(":", "-")
        subdir = artifacts_dir / f"{timestamp}_{safe_mime}"
        subdir.mkdir(parents=True, exist_ok=True)

        # Copy input file (if it is still on disk — inside tmpdir)
        file_sha256: str | None = None
        if prepared_path is not None and prepared_path.exists():
            shutil.copy2(prepared_path, subdir / prepared_path.name)
            file_sha256 = hashlib.sha256(prepared_path.read_bytes()).hexdigest()

        # Copy intermediate PDF (if produced before the error)
        if converted_pdf is not None and converted_pdf.exists():
            shutil.copy2(converted_pdf, subdir / "converted.pdf")

        # Save partial Markdown output
        if partial_md:
            (subdir / "partial_output.md").write_text(partial_md, encoding="utf-8")

        # ── meta.txt ──────────────────────────────────────────────────────────
        meta_lines: list[str] = [
            f"app_version: {_APP_VERSION}",
            f"timestamp: {datetime.now().isoformat()}",
            f"mime: {raw_mime}",
            f"image_description_model_name: {image_description_model_name or 'none'}",
            f"wall_clock_time_s: {wall_clock_time:.2f}",
            f"active_time_s: {t_active:.2f}",
            f"img_desc_time_s: {img_desc_time:.2f}",
        ]
        if file_sha256:
            meta_lines.append(f"sha256: {file_sha256}")

        # RAM + CPU
        mem = psutil.virtual_memory()
        meta_lines += [
            f"ram_total_mb: {mem.total // (1024 * 1024)}",
            f"ram_used_mb: {mem.used // (1024 * 1024)}",
            f"ram_available_mb: {mem.available // (1024 * 1024)}",
            f"cpu_percent: {psutil.cpu_percent(interval=0.1)}",
            f"cpu_count_logical: {psutil.cpu_count(logical=True)}",
        ]

        # VRAM (nvidia-smi)
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.used,memory.free,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) == 5:
                        idx, name, used, free, total = parts
                        meta_lines += [
                            f"gpu_{idx}_name: {name}",
                            f"gpu_{idx}_vram_used_mb: {used}",
                            f"gpu_{idx}_vram_free_mb: {free}",
                            f"gpu_{idx}_vram_total_mb: {total}",
                        ]
        except Exception as e:
            meta_lines.append(f"vram_error: {e}")

        (subdir / "meta.txt").write_text("\n".join(meta_lines), encoding="utf-8")

        # ── trace.txt ─────────────────────────────────────────────────────────
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        (subdir / "trace.txt").write_text(tb, encoding="utf-8")

        logger.info(f"Failed artifacts saved to {subdir}")

    except Exception as save_exc:
        logger.error(f"Failed to save debug artifacts: {save_exc}")
