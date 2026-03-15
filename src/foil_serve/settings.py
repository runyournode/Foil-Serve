from typing import List, Annotated, Dict, Literal
from enum import StrEnum
import asyncio
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource
from pydantic import Field, model_validator
from fastapi import HTTPException, Query
from openai import AsyncOpenAI

from schemas import VLMModelConfig


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
    save_failed_artifacts: bool = False
    failed_artifacts_dir: str = "/tmp/paddleocr_failed"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file: str | None = None

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
        *args,
        **kwargs,
    ) -> None:
        self.endpoint_name = endpoint_name
        self.promt = prompt
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.max_input_ocr_length = max_input_ocr_length
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
