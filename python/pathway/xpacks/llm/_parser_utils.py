# Copyright © 2024 Pathway

import asyncio
import base64
import io
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from typing import Literal

import PIL.Image
from pydantic import BaseModel

import pathway as pw
from pathway.internals.udfs import coerce_async
from pathway.optional_import import optional_imports
from pathway.xpacks.llm.constants import DEFAULT_VISION_MODEL

logger = logging.getLogger(__name__)


def maybe_downscale(
    img: PIL.Image.Image, max_image_size: int, downsize_horizontal_width: int
) -> PIL.Image.Image:
    """Downscale an image if it exceeds `max_image_size` limit, while maintaining the aspect ratio.

    Args:
        img: The image to be downscaled.
        max_image_size: The maximum allowable size of the image in bytes.
        downsize_horizontal_width: The target width for the downscaled image if resizing is needed.
    """
    img_size = img.size[0] * img.size[1] * 3

    if img_size > max_image_size:
        logging.info(
            f"Image size exceeds the limit. Size: `{img_size/(1024 * 1024)}MBs`. Resizing."
        )
        ratio = img.size[1] / img.size[0]  # keep the ratio
        img = img.resize(
            (downsize_horizontal_width, int(downsize_horizontal_width * ratio))
        )

    return img


def img_to_b64(img: PIL.Image.Image) -> str:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    img_bytes = buffer.getbuffer()
    return base64.b64encode(img_bytes).decode("utf-8")


async def parse_images(
    images: list[PIL.Image.Image],
    llm: pw.UDF,
    parse_prompt: str,
    *,
    run_mode: Literal["sequential", "parallel"] = "parallel",
    parse_details: bool = False,
    detail_parse_schema: type[BaseModel] | None = None,
    parse_fn: Callable,
    parse_image_details_fn: Callable | None,
) -> tuple[list[str], list[BaseModel]]:
    """
    Parse images and optional Pydantic model with a multi-modal LLM.
    `parse_prompt` will be only used for the regular parsing.

    Args:
        images: Image list to be parsed. Images are expected to be `PIL.Image.Image`.
        llm: LLM model to be used for parsing. Needs to support image input.
        parse_details: Whether to make second LLM call to parse specific Pydantic
            model from the image.
        run_mode: Mode of execution,
            either ``"sequential"`` or ``"parallel"``. Default is ``"parallel"``.
            ``"parallel"`` mode is suggested for speed, but if timeouts or memory usage in local LLMs are concern,
            ``"sequential"`` may be better.
        parse_details: Whether a schema should be parsed.
        detail_parse_schema: Pydantic model for schema to be parsed.
        parse_fn: Awaitable image parsing function.
        parse_image_details_fn: Awaitable image schema parsing function.

    """
    logger.info("`parse_images` converting images to base64.")

    b64_images = [img_to_b64(image) for image in images]

    return await _parse_b64_images(
        b64_images,
        llm,
        parse_prompt,
        run_mode=run_mode,
        parse_details=parse_details,
        detail_parse_schema=detail_parse_schema,
        parse_fn=parse_fn,
        parse_image_details_fn=parse_image_details_fn,
    )


async def _parse_b64_images(
    b64_images: list[str],
    llm: pw.UDF,
    parse_prompt: str,
    *,
    run_mode: Literal["sequential", "parallel"],
    parse_details: bool,
    detail_parse_schema: type[BaseModel] | None,
    parse_fn: Callable,
    parse_image_details_fn: Callable | None,
) -> tuple[list[str], list[BaseModel]]:
    total_images = len(b64_images)

    if parse_details:
        assert detail_parse_schema is not None and issubclass(
            detail_parse_schema, BaseModel
        ), "`detail_parse_schema` must be valid Pydantic Model class when `parse_details` is True"

    logger.info(f"`parse_images` parsing descriptions for {total_images} images.")

    parsed_details: list[BaseModel] = []

    if run_mode == "sequential":
        parsed_content = []

        for img in b64_images:
            parsed_txt = await parse_fn(img, llm, parse_prompt)
            parsed_content.append(parsed_txt)

        if parse_details:
            assert parse_image_details_fn is not None
            parsed_details = []
            for img in b64_images:
                parsed_detail = await parse_image_details_fn(
                    img,
                    parse_schema=detail_parse_schema,
                )
                parsed_details.append(parsed_detail)

    else:
        parse_tasks = [parse_fn(img, llm, parse_prompt) for img in b64_images]

        if parse_details:
            assert parse_image_details_fn is not None
            detail_tasks = [
                parse_image_details_fn(
                    img,
                    parse_schema=detail_parse_schema,
                )
                for img in b64_images
            ]
        else:
            detail_tasks = []

        results = await asyncio.gather(*parse_tasks, *detail_tasks)

        parsed_content = results[: len(b64_images)]
        parsed_details = results[len(b64_images) :]

    return parsed_content, parsed_details


async def parse_image(
    b_64_img,
    llm: pw.UDF,
    prompt: str,
    model: str | None = None,
    **kwargs,
) -> str:
    """
    Parse base64 image with the LLM. `model` will be set to llm's default if not provided.
    If llm's `model` is also not set, ``OpenAI`` ``gpt-4o-mini`` will be used.

    Args:
        b_64_img: Image in base64 format to be parsed. See `img_to_b64` for the conversion utility.
        llm: LLM instance to be called with image.
        prompt: Instructions for image parsing.
        model: Optional LLM model name. Defaults to ``OpenAI`` ``gpt-4o-mini``,
            if neither `model` nor `llm.model` is set.
        kwargs: Additional arguments to be sent to the LLM inference.
            Refer to the specific provider's API for available options.
            Examples include `temperature`, `max_tokens`, etc.
    """
    model = model or llm.kwargs.get("model") or DEFAULT_VISION_MODEL  # type:ignore

    content = [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b_64_img}"},
        },
    ]

    messages = [
        {
            "role": "user",
            "content": content,
        }
    ]

    logger.info(f"Parsing table, model: {model}\nmessages: {str(content)[:350]}...")

    response = await coerce_async(llm.func)(model=model, messages=messages, **kwargs)

    logger.info(f"Parsed table, model: {model}\nmessages: {str(response)}...")

    return response


async def parse_image_details(
    b_64_img,
    parse_schema: type[BaseModel],
    model: str = DEFAULT_VISION_MODEL,
    openai_client_args: dict = {},
    **kwargs,
) -> BaseModel:
    """Parse Pydantic schema from given base64 image."""
    with optional_imports("xpack-llm"):
        import instructor
        import openai

    client = instructor.from_openai(openai.AsyncOpenAI(**openai_client_args))

    content = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b_64_img}"},
        },
    ]

    messages = [
        {
            "role": "user",
            "content": content,
        }
    ]

    logger.info(
        f"Parsing slide details, schema: {parse_schema}, model: {model}\nmessages: {str(content)[:350]}..."
    )

    user_info = await client.chat.completions.create(
        model=model,
        response_model=parse_schema,
        messages=messages,
        **kwargs,
    )

    return user_info


def _convert_pptx_to_pdf(contents: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as pptx_temp:
        pptx_temp.write(contents)
        pptx_temp_path = pptx_temp.name

    pdf_temp_path = pptx_temp_path.replace(".pptx", ".pdf").split(os.path.sep)[-1]

    try:
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", pptx_temp_path],
            check=True,
            capture_output=True,
            text=True,
        )

        logger.info(f"`_convert_pptx_to_pdf` result: {str(result)}")

        with open(pdf_temp_path, "rb") as pdf_temp:
            pdf_contents = pdf_temp.read()

    except FileNotFoundError:
        raise Exception(
            "`LibreOffice` is not installed or `soffice` command is not found. Please install LibreOffice."
        )

    finally:
        os.remove(pptx_temp_path)
        if os.path.exists(pdf_temp_path):
            os.remove(pdf_temp_path)

    return pdf_contents
