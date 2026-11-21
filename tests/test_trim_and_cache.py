"""Crops lose their white margins; descriptions are reused for identical requests only.

- `trim_background`: the same drawing boxed with more or less white gives the same
  pixels, hence one content name (one image returned, described once).
- `DescriptionCache`: a description is reused across documents only when the VLM
  request would have been byte-identical — a few changed pixels, another context,
  model or prompt is a miss. Never an approximate match.
"""

import asyncio
import types

import pytest
from PIL import Image, ImageDraw

import vlm
from postprocessing import name_by_content, trim_background
from utils import EncodedImage
from vlm import DescriptionCache, describe_images


def _logo(margin: int, background=(255, 255, 255)) -> Image.Image:
    img = Image.new("RGB", (60 + 2 * margin, 40 + 2 * margin), background)
    draw = ImageDraw.Draw(img)
    draw.rectangle((margin, margin, margin + 59, margin + 39), fill=(200, 30, 30))
    draw.text((margin + 5, margin + 10), "ACME", fill=(255, 255, 255))
    return img


def _div(name: str) -> str:
    return f'<div style="text-align: center;"><img src="{name}" alt="Image" /></div>'


# ── trim ─────────────────────────────────────────────────────────────────────


def test_same_drawing_with_different_margins_is_one_image():
    md, images = name_by_content(
        f"{_div('box_a.jpg')}\n{_div('box_b.jpg')}",
        {"box_a.jpg": _logo(5), "box_b.jpg": _logo(40)},
    )
    (name,) = images
    assert md.count(f'"{name}"') == 2
    assert (images[name].width, images[name].height) == (64, 44)  # content + 2 px


def test_uniform_image_is_unchanged():
    white = Image.new("RGB", (50, 30), (255, 255, 255))
    assert trim_background(white).size == (50, 30)


def test_light_grey_scan_background_is_trimmed():
    assert trim_background(_logo(30, background=(248, 248, 248))).size == (64, 44)


def test_content_touching_the_edges_is_kept():
    assert trim_background(_logo(0)).size == (60, 40)


# ── cache ────────────────────────────────────────────────────────────────────


def _client(**overrides) -> types.SimpleNamespace:
    attrs = dict(
        base_url="http://vlm:8000/v1/",
        endpoint_name="model-a",
        temperature=0.0,
        max_output_tokens=500,
        extra_body=None,
        promt="Describe. OCR: {raw_text}",
        max_input_ocr_length=700,
    )
    attrs.update(overrides)
    return types.SimpleNamespace(**attrs)


@pytest.fixture
def vlm_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def _describe_image(client, img_name, img, ocr):
        calls.append(img_name)
        return f"description #{len(calls)}"

    monkeypatch.setattr(vlm, "describe_image", _describe_image)
    return calls


def _describe(cache, client, images: dict[str, str], contexts: dict[str, str]):
    return asyncio.run(
        describe_images(
            client=client,
            model_name="m",
            imgs_b64=images,
            ocrs=contexts,
            semaphore=asyncio.Semaphore(4),
            cache=cache,
        )
    )


def _b64(img: Image.Image) -> str:
    return EncodedImage.from_pil(img).b64


def test_identical_request_across_documents_is_described_once(vlm_calls):
    cache = DescriptionCache(max_mb=1)
    logo = _b64(_logo(0))
    first, t1 = _describe(cache, _client(), {"doc1.jpg": logo}, {"doc1.jpg": "ACME"})
    second, t2 = _describe(cache, _client(), {"doc2.jpg": logo}, {"doc2.jpg": "ACME"})
    assert len(vlm_calls) == 1
    assert second["doc2.jpg"] == first["doc1.jpg"]
    assert t2 == 0.0  # a hit costs no description time


def test_a_few_changed_pixels_are_a_miss(vlm_calls):
    cache = DescriptionCache(max_mb=1)
    original = _logo(0)
    altered = original.copy()
    for x in range(20, 24):
        altered.putpixel((x, 20), (0, 0, 0))  # inside the drawing, not background
    assert _b64(original) != _b64(altered)
    _describe(cache, _client(), {"a.jpg": _b64(original)}, {"a.jpg": ""})
    _describe(cache, _client(), {"b.jpg": _b64(altered)}, {"b.jpg": ""})
    assert len(vlm_calls) == 2


@pytest.mark.parametrize(
    "other_client, other_context",
    [
        (_client(), "ACME confidential"),  # another context (OCR / alt text)
        (_client(endpoint_name="model-b"), "ACME"),  # another model
        (_client(promt="Other prompt {raw_text}"), "ACME"),  # another prompt
        (_client(temperature=0.7), "ACME"),  # another generation parameter
        (_client(base_url="http://other:8000/v1/"), "ACME"),  # another endpoint
    ],
)
def test_any_other_request_part_is_a_miss(vlm_calls, other_client, other_context):
    cache = DescriptionCache(max_mb=1)
    logo = _b64(_logo(0))
    _describe(cache, _client(), {"a.jpg": logo}, {"a.jpg": "ACME"})
    _describe(cache, other_client, {"b.jpg": logo}, {"b.jpg": other_context})
    assert len(vlm_calls) == 2


def test_lru_eviction_by_bytes():
    cache = DescriptionCache(max_mb=1500 / 2**20)  # ~1500 bytes
    keys = [bytes([i]) * 32 for i in range(4)]
    for key in keys[:2]:
        cache.put(key, "x" * 500)
    assert cache.get(keys[0]) is not None  # keys[0] becomes the most recent
    cache.put(keys[2], "x" * 500)  # over budget: the least recent goes (keys[1])
    assert cache.get(keys[1]) is None
    assert cache.get(keys[0]) is not None and cache.get(keys[2]) is not None
    assert cache.size_bytes <= 1500


def test_zero_budget_stores_nothing():
    cache = DescriptionCache(max_mb=0)
    cache.put(b"k" * 32, "description")
    assert len(cache) == 0


def test_no_cache_always_calls(vlm_calls):
    logo = _b64(_logo(0))
    for name in ("a.jpg", "b.jpg"):
        _describe(None, _client(), {name: logo}, {name: ""})
    assert len(vlm_calls) == 2
