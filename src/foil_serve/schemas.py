from pydantic import BaseModel, HttpUrl


class VLMModelConfig(BaseModel):
    """Used internally"""

    name: str  # == `image_description_model_name` in route query
    enabled: bool
    min_size: tuple[int, int] = (64, 64)
    temperature: float
    max_output_tokens: int
    url: HttpUrl
    max_concurrent_requests: int
    endpoint_name: str  # exact model name to pass to the openai endpoint
    endpoint_api_key: str | None = None
    prompt: str  # Default prompt key or full prompt string
    max_input_ocr_length: int = (
        700  # Max chars of OCR text injected into the VLM prompt
    )


class Metadata(BaseModel):
    """
    Metadata returned by the server
    """

    active_conversion_time_no_img_desc: int  # accumulated active (CPU/GPU) time for document-to-markdown conversion, excluding semaphore waits and VLM image description
    img_desc_time: int = 0  # total 'cpu' time for image description  (doesn't take into account concurency or waiting semaphore)
    wall_clock_time: int  # wall-clock time (latency as seen by the client)


class ProcessedDocument(BaseModel):
    """Output of main route /v1/process"""

    page_content: str
    images: dict[str, str]  # [relative_filename, img in base64]
    metadata: Metadata | None = None
