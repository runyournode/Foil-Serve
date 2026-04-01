import asyncio
import time
import logging

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

    # Inject ocr in prompt, truncate if too long
    if len(ocr) > client.max_input_ocr_length:
        ocr = ocr[: client.max_input_ocr_length]

    input_text = client.promt.format(raw_text=ocr)

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
) -> tuple[str, str, float]:
    """
    Simple wrapper of `describe_image` with a semaphore.
    Also returns img_name and elapsed time.
    """
    async with semaphore:
        t0 = time.perf_counter()
        description = await describe_image(client, img_name, img_b64, ocr)
        elapsed = time.perf_counter() - t0
    return img_name, description, elapsed
