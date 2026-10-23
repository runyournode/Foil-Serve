from enum import StrEnum

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator


class SpreadsheetMode(StrEnum):
    """Conversion strategy for spreadsheet inputs (.xls/.xlsx/.ods).

    Ignored for every other file type. Selected per request via the
    `spreadsheet_mode` query param or the dedicated /v1/process/spreadsheet_* routes.
    """

    AUTO = "auto"  # pandas, with PDF+OCR fallback when the file is empty or sparse
    PANDAS = "pandas"  # cell extraction only, never falls back
    OCR = "ocr"  # LibreOffice → PDF → PaddleOCR only, pandas is not run
    BOTH = "both"  # both conversions, concatenated in a single Markdown document


class ExternalProcessorConfig(BaseModel):
    """Used internally — external foil-compatible processing backend (e.g. video)"""

    model_config = ConfigDict(extra="forbid")  # unknown keys must fail loudly

    name: str  # identifier used in logs, semaphores and /health checks
    enabled: bool
    url: HttpUrl  # base URL of the external server
    process_route: str  # processing route on the external server
    # health-check route on the external server; "" = the server has none
    health_route: str
    endpoint_api_key: str  # sent as `Authorization: Bearer <key>`; "" = no auth header
    mime_types: list[str]  # MIME types routed to this processor (e.g. "video/mp4")
    max_concurrent_requests: int
    timeout_s: float  # total HTTP timeout per attempt
    max_retries: int  # extra attempts after the first one (transient errors only)
    retry_backoff_s: float  # base delay; doubles after each failed attempt
    max_file_size_mb: int
    check_on_startup: bool  # ping the processor /health during lifespan

    @model_validator(mode="after")
    def health_route_for_startup_check(self) -> "ExternalProcessorConfig":
        if self.check_on_startup and not self.health_route:
            raise ValueError(
                f"Invalid `server_config.toml`: external processor '{self.name}' has "
                "`check_on_startup = true` but no `health_route`."
            )
        return self


class VLMModelConfig(BaseModel):
    """Used internally"""

    model_config = ConfigDict(extra="forbid")  # unknown keys must fail loudly

    name: str  # == `image_description_model_name` in route query
    enabled: bool
    temperature: float
    max_output_tokens: int
    url: HttpUrl
    max_concurrent_requests: int
    endpoint_name: str  # exact model name to pass to the openai endpoint
    endpoint_api_key: str
    prompt: str  # Default prompt key or full prompt string
    max_input_ocr_length: int  # Max chars of OCR text injected into the VLM prompt
    # Provider-specific extra parameters passed as-is to the API call (e.g. thinking
    # level); {} = none
    extra_body: dict


class Metadata(BaseModel):
    """
    Metadata returned by the server
    """

    # Pydantic class-level config (not a data field, never serialized):
    # external processors may return arbitrary extra metadata fields — keep them
    model_config = ConfigDict(extra="allow")

    active_conversion_time_no_img_desc: int  # accumulated active (CPU/GPU) time for document-to-markdown conversion, excluding semaphore waits and VLM image description
    img_desc_time: int = 0  # total 'cpu' time for image description  (doesn't take into account concurency or waiting semaphore)
    wall_clock_time: int  # wall-clock time (latency as seen by the client)


class ProcessedDocument(BaseModel):
    """Output of main route /v1/process"""

    page_content: str
    images: dict[str, str]  # [relative_filename, img in base64]
    metadata: Metadata | None = None
