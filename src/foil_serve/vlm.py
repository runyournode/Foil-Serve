import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict

from fastapi import HTTPException
from openai.types.chat import ChatCompletionUserMessageParam
from openai.types.chat.chat_completion_content_part_image_param import (
    ChatCompletionContentPartImageParam,
    ImageURL,
)
from openai.types.chat.chat_completion_content_part_text_param import (
    ChatCompletionContentPartTextParam,
)
from PIL.Image import Image

from settings import AsyncOpenAIWithInfo
from utils import pil_to_b64

logger = logging.getLogger(__name__)


def build_prompt(client: AsyncOpenAIWithInfo, ocr: str) -> str:
    """The text part of the request: the prompt with the context (OCR text, alt text,
    chart summary) injected, truncated to `max_input_ocr_length`."""
    return client.promt.format(raw_text=ocr[: client.max_input_ocr_length])


class DescriptionCache:
    """Descriptions already obtained, reused only for a byte-identical VLM request.

    The key is the full sha256 of everything the VLM receives: endpoint, model,
    generation parameters, the final prompt text (context included) and the image
    bytes. A description is therefore reused only when the request would have been
    exactly the same — it carries nothing the current document does not already
    hold (the context of a spreadsheet picture comes from the document, not from
    its pixels: it is part of the key). No approximate matching, ever: documents of
    different clients are confidential.

    LRU bounded in bytes (UTF-8 description + a fixed per-entry overhead; images are
    not stored). Used from the event loop only: no lock.
    """

    _ENTRY_OVERHEAD = 150  # key, OrderedDict node, str header

    def __init__(self, max_mb: float) -> None:
        self._max_bytes = int(max_mb * 1024 * 1024)
        self._entries: OrderedDict[bytes, str] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(client: AsyncOpenAIWithInfo, prompt: str, img_b64: str) -> bytes:
        h = hashlib.sha256()
        parts = (
            str(client.base_url),
            client.endpoint_name,
            repr(client.temperature),
            repr(client.max_output_tokens),
            json.dumps(client.extra_body, sort_keys=True, default=str),
            prompt,
            img_b64,
        )
        for part in parts:
            data = part.encode()
            # length-prefixed: no two different tuples hash the same bytes
            h.update(len(data).to_bytes(8, "big"))
            h.update(data)
        return h.digest()

    def get(self, key: bytes) -> str | None:
        description = self._entries.get(key)
        if description is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return description

    def put(self, key: bytes, description: str) -> None:
        size = len(description.encode()) + self._ENTRY_OVERHEAD
        if self._max_bytes <= 0 or size > self._max_bytes:
            return
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= len(old.encode()) + self._ENTRY_OVERHEAD
        self._entries[key] = description
        self._bytes += size
        while self._bytes > self._max_bytes:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= len(evicted.encode()) + self._ENTRY_OVERHEAD

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def size_bytes(self) -> int:
        return self._bytes


async def describe_image(
    client: AsyncOpenAIWithInfo,
    img_name: str,
    img: Image
    | str,  # PIL.Image.Image or base64 encoded string (without `data:image/jpeg;base64`)
    ocr: str,  # raw ocr extracted by paddle
) -> str:
    """
    Request the VLM for an image description
    """
    if isinstance(img, Image):
        b64_img = pil_to_b64(img)
    elif isinstance(img, str):
        b64_img = img
    else:
        raise TypeError(
            f"img must be a PIL.Image.Image or a base64 encoded string, got {type(img)}."
        )

    input_text = build_prompt(client, ocr)

    message: ChatCompletionUserMessageParam = {
        "role": "user",
        "content": [
            ChatCompletionContentPartTextParam(type="text", text=input_text),
            ChatCompletionContentPartImageParam(
                type="image_url",
                image_url=ImageURL(
                    url=f"data:image/jpeg;base64,{b64_img}",
                    detail="auto",
                ),
            ),
        ],
    }
    try:
        response = await client.chat.completions.create(
            model=client.endpoint_name,
            messages=[message],
            temperature=client.temperature,
            max_completion_tokens=client.max_output_tokens,
            extra_body=client.extra_body,
        )

        description = response.choices[0].message.content
        if description is None:
            raise RuntimeError(f"VLM returned no content for image '{img_name}'")
        return description

    except Exception as e:
        logger.exception(
            "Failed to describe image",
            extra={
                "img_name": img_name,
                "model": client.endpoint_name,
                "error": f"{e}",
            },
        )
        raise e

    # BELOW IS RESPONSE API
    # WORKS WELL with (ministral-3-3b) lmstudio but broken with VLLM v0.15.1
    # # Build the request using OpenAI typing
    # from openai.types.responses import  EasyInputMessageParam, ResponseInputImageParam, ResponseInputTextParam
    # content = [
    # ResponseInputTextParam(type='input_text', text=input_text),
    # ResponseInputImageParam(type='input_image', image_url=f'data:image/jpeg;base64,{b64_img}', detail='auto'),
    # ]
    # easy_input = EasyInputMessageParam(
    # role='user',
    # content=content
    # )
    # try:
    #     response = await client.responses.create(
    #         model=client.endpoint_name,
    #         input=[easy_input],
    #         temperature=client.temperature,
    #         max_output_tokens=client.max_output_tokens,
    #     )
    #     description = response.output_text.strip()
    # return description


async def describe_image_sem(
    client: AsyncOpenAIWithInfo,
    img_name: str,
    img_b64: str,
    ocr: str,
    semaphore: asyncio.Semaphore,
    cache: DescriptionCache | None = None,
) -> tuple[str, str, float]:
    """
    Simple wrapper of `describe_image` with a semaphore and the exact-match cache.
    Also returns img_name and elapsed time (0 on a cache hit).
    """
    key = None
    if cache is not None:
        key = DescriptionCache.key(client, build_prompt(client, ocr), img_b64)
        cached = cache.get(key)
        if cached is not None:
            logger.info(
                f"VLM description cache hit ({len(cache)} entries, "
                f"{cache.size_bytes / 2**20:.1f} MB)"
            )
            return img_name, cached, 0.0
    async with semaphore:
        t0 = time.perf_counter()
        description = await describe_image(client, img_name, img_b64, ocr)
        elapsed = time.perf_counter() - t0
    if cache is not None and key is not None:
        cache.put(key, description)
    return img_name, description, elapsed


async def describe_images(
    client: AsyncOpenAIWithInfo,
    model_name: str,
    imgs_b64: dict[str, str],
    ocrs: dict[str, str],
    semaphore: asyncio.Semaphore,
    cache: DescriptionCache | None = None,
) -> tuple[dict[str, str], float]:
    """Describe, concurrently, every image that has a key in `ocrs`.

    `ocrs` maps each image referenced in the Markdown to its prompt context: the
    OCR text of a Paddle figure block, or the alt text / chart summary of a
    spreadsheet object. The value may be an empty string — such images are still
    described. Images with no key are not referenced by any figure block, so there
    is nowhere to put their description: they are skipped (but still returned).

    `cache`: descriptions reused for byte-identical requests (see DescriptionCache).

    Returns (descriptions, total_seconds) where total_seconds is the sum of the
    individual call durations — it does not account for concurrency (cache hits
    count 0).

    Raises HTTPException(502) if any description fails: a partial set would
    silently produce a document with missing captions.
    """
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(
                    describe_image_sem(
                        client=client,
                        img_name=img_name,
                        img_b64=img_b64,
                        ocr=ocrs[img_name],
                        semaphore=semaphore,
                        cache=cache,
                    )
                )
                for img_name, img_b64 in imgs_b64.items()
                if img_name in ocrs
            ]
    except ExceptionGroup as e:
        raise HTTPException(
            status_code=502,
            detail=f"Error in image description with {model_name}:\n{e}",
        )

    descriptions: dict[str, str] = {}
    total_seconds = 0.0
    for task in tasks:
        img_name, description, elapsed = task.result()
        descriptions[img_name] = description
        total_seconds += elapsed
    return descriptions, total_seconds
