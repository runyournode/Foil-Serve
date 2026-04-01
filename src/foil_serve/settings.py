from typing import List, Annotated, Dict, Literal
from enum import StrEnum
import asyncio
import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource
from pydantic import Field, model_validator
from fastapi import HTTPException, Query
from openai import AsyncOpenAI

from schemas import VLMModelConfig


ExcelOutputFormat = Literal["human", "llm"]
PaperFormat = Literal["A3", "A4", "A2", "Letter", "Legal", "Tabloid"]


class Settings(BaseSettings):
    requires_auth: bool
    app_api_keys: List[str] = Field(default_factory=list)
    check_vlm_endpoints: bool
    image_min_size: tuple[int, int]
    prompts: Dict[str, str] = Field(default_factory=dict)
    vlm_models: List[VLMModelConfig] = Field(default_factory=list)
    max_tasks_between_pipeline_reload: int
    max_concurrent_libreoffice: int
    max_concurrent_excel: int
    max_file_size_mb: int
    max_pdf_file_size_mb: int
    max_image_file_size_mb: int
    max_office_file_size_mb: int
    max_excel_file_size_mb: int
    max_unzip_size_mb: int
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    libs_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    log_file: str | None = None
    artifact_dir: str = "/var/log/foil/artifacts"
    save_failed_artifacts: bool = False
    failed_artifacts_subdir: str = "failed"
    save_table_conversion_artifacts: bool = False
    table_conversion_artifacts_subdir: str = "xls2pdf"
    temp_dir: str = "/tmp/foil-runtime"

    # OCR output control
    output_paddle_ocr: bool
    output_paddle_ocr_no_img_desc: bool

    # Spreadsheet processing
    excel_output_format: ExcelOutputFormat
    excel_mask_cell_errors: bool
    save_cell_error_artifacts: bool = False
    cell_error_artifacts_subdir: str = "cell_errors"
    excel_max_output_ratio: float
    excel_pdf_fallback_enabled: bool
    excel_min_input_for_fallback_mb: float
    excel_min_output_ratio: float
    excel_pdf_paper_format: PaperFormat
    excel_pdf_landscape: bool

    model_config = SettingsConfigDict(
        toml_file="config/server_config.toml", extra="ignore"
    )

    @model_validator(mode="after")
    def validate_auth_consistency(self) -> "Settings":
        if self.requires_auth and not self.app_api_keys:
            raise ValueError(
                "Invalid `server_config.toml`: 'requires_auth' is `true` but 'app_api_keys' is empty."
            )
        return self

    @model_validator(mode="after")
    def keep_enabled__resolve_prompts(self) -> "Settings":
        """
        Resolve prompts for each VLM model.
        If the prompt value matches a key in self.prompts, replace it with the value from self.prompts.
        Otherwise, keep the value as is (allows direct prompt string).
        Also filter out disabled models.
        """
        self.vlm_models = [m for m in self.vlm_models if m.enabled]

        for model in self.vlm_models:
            if model.prompt in self.prompts:
                model.prompt = self.prompts[model.prompt]
        return self

    # Enable toml file since pydantic v2
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            TomlConfigSettingsSource(
                settings_cls,
                toml_file=Path(__file__).parent / "config" / "server_config.toml",
            ),
            env_settings,
        )


settings = Settings()


_CONSOLE_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_CONSOLE_DATE_FMT = "%m-%d %H:%M:%S"
_FILE_FMT = "%(asctime)s,%(msecs)03d | %(levelname)-8s | %(name)s | %(message)s"
_FILE_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> logging.FileHandler | None:
    """Configure the root logger. Safe to call from both main and worker processes."""
    root = logging.getLogger()
    root.setLevel(logging.getLevelName(settings.log_level))

    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_CONSOLE_DATE_FMT))
        root.addHandler(sh)

    fh = None
    if settings.log_file and not any(
        isinstance(h, logging.FileHandler) for h in root.handlers
    ):
        log_path = Path(settings.log_file)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(settings.log_file)
        except OSError as e:
            raise OSError(
                f"Cannot open log file '{settings.log_file}': {e}. "
                f"Create the directory with: sudo mkdir -p {log_path.parent} "
                f"&& sudo chown $(whoami) {log_path.parent}"
            ) from e
        fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_FILE_DATE_FMT))
        root.addHandler(fh)

    libs_level = logging.getLevelName(settings.libs_log_level)
    for lib in ("httpx", "openai", "httpcore"):
        logging.getLogger(lib).setLevel(libs_level)

    return fh


def align_uvicorn_logging(file_handler: logging.FileHandler | None) -> None:
    """Reformat uvicorn loggers to match our style. Call after uvicorn has started."""
    console_formatter = logging.Formatter(_CONSOLE_FMT, datefmt=_CONSOLE_DATE_FMT)
    for name in ("uvicorn", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        for h in uv_logger.handlers:
            h.setFormatter(console_formatter)
        if file_handler and file_handler not in uv_logger.handlers:
            uv_logger.addHandler(file_handler)

# Dynamic loading of vlm config (from .toml) at startup
vlm_registry: dict[str, VLMModelConfig] = {m.name: m for m in settings.vlm_models}

# Enum of the client allowed vlm (used for FastAPI doc),
VLMModelEnum = StrEnum("VLMModelEnum", list(vlm_registry.keys()))

# VLMModelEnum = Enum(
#     'VLMModelEnum',
#     {name: name for name in vlm_registry.keys()},
#     type=str
# )
# VLMModelEnum = Literal[tuple(vlm_registry.keys())]


class AsyncOpenAIWithInfo(AsyncOpenAI):
    """Simply add more info to the client"""

    def __init__(
        self,
        endpoint_name: str,
        prompt: str,
        temperature: float,
        max_output_tokens: int,
        max_input_ocr_length: int,
        extra_body: dict | None,
        *args,
        **kwargs,
    ) -> None:
        self.endpoint_name = endpoint_name
        self.promt = prompt
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.max_input_ocr_length = max_input_ocr_length
        self.extra_body = extra_body
        super().__init__(*args, **kwargs)


def get_vlm_client(model_name: str) -> AsyncOpenAIWithInfo:
    """
    Get the openai client associated to the vlm
    """
    cfg: VLMModelConfig | None = vlm_registry.get(model_name)

    if cfg is None or not cfg.enabled:
        raise HTTPException(
            status_code=400,
            detail=f"image_description_model_name: '{model_name}' is not supported.",
        )

    return AsyncOpenAIWithInfo(
        endpoint_name=cfg.endpoint_name,
        prompt=cfg.prompt,
        temperature=cfg.temperature,
        max_output_tokens=cfg.max_output_tokens,
        max_input_ocr_length=cfg.max_input_ocr_length,
        extra_body=cfg.extra_body,
        api_key=cfg.endpoint_api_key,
        base_url=str(cfg.url),
    )


async def validate_endpoint(
    model_name: Annotated[
        str | None, Query(alias="image_description_model_name")
    ] = None,
) -> AsyncOpenAIWithInfo | None:
    """
    Check if vlm is properly served by the openai endpoint
    If successful, return the async client
    If image_description_model_name is None then return None
    """
    if model_name is None:
        return None

    client = get_vlm_client(model_name)
    try:
        response = await asyncio.wait_for(client.models.list(), timeout=10.0)
        served_models = [m.id for m in response.data]
        if client.endpoint_name not in served_models:
            raise ValueError(
                f"Model '{client.endpoint_name}' not served by openai endpoint. Check `server_config.toml`"
            )
    except Exception as e:
        # Propagate the error so that the lifespan handles it
        raise RuntimeError(f"Error with openai endpoint [{model_name}]: {e}")

    return client
