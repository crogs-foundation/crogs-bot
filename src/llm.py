import re
from typing import Optional

from g4f.client import AsyncClient

from src.translators.base import Translator


async def _generate_text_inner(
    prompt: str,
    model: str,
    client: AsyncClient,
    max_size: Optional[int] = None,
    remove_thinking: bool = True,
    **kwargs,
) -> str:
    response = await client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], **kwargs
    )

    content: str = response.choices[0].message.content

    if remove_thinking:
        content = re.sub(
            r"<[Tt]hink>.*?</[Tt]hink>", "", content, flags=re.DOTALL
        ).strip()

    if max_size:
        return content[:max_size]
    return content


async def generate_text(
    prompt: str,
    model: str,
    client: Optional[AsyncClient] = None,
    max_size: Optional[int] = None,
    translator_options: Optional[tuple[Translator, str]] = None,
    **kwargs,
) -> str:
    if client is None:
        client = AsyncClient()

    if translator_options is None or translator_options[1].lower() in ["en", "en-us"]:
        return await _generate_text_inner(
            prompt, model, client, max_size=max_size, **kwargs
        )

    translator, target_lang = translator_options

    final_prompt = prompt
    if translator.strategy == "prompt":
        final_prompt = await translator.translate(prompt, target_lang)

    response = await _generate_text_inner(
        final_prompt, model, client, max_size=None, **kwargs
    )

    if translator.strategy == "response":
        response = await translator.translate(response, target_lang)

    return response[:max_size]
