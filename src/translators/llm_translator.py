# src/translators/llm_translator.py
import asyncio

from g4f.client import AsyncClient

from src.llm import generate_text
from src.logger import Logger
from src.translators.base import Translator


class LLMTranslator(Translator):
    """Translator implementation using a Large Language Model."""

    def __init__(self, config: dict, logger: Logger, client: AsyncClient):
        super().__init__(config)

        self.logger = logger.get_child("LLMTranslator")
        self.client = client
        self.llm_config = self.config.get("llm_translator_settings", {})
        self.model = self.llm_config.get("model", "gpt-3.5-turbo")
        self.prompt_template = self.llm_config.get(
            "prompt_template",
            "Translate the following text to {target_lang}. Return only the translated text, without any additional comments or explanations:\n\n---\n\n{text}",
        )

    async def check_api(self) -> bool:
        self.logger.info(
            f"Checking LLM API connection for translation using model '{self.model}'..."
        )
        try:
            await self.translate("hello", "es")
            self.logger.info("LLM Translator seems to be working.")
            return True
        except Exception as e:
            self.logger.error(f"Failed to initialize LLM Translator: {e}")
            return False

    async def translate(
        self, text: str, target_lang: str, source_lang: list[str] = ["en", "en-us"]
    ) -> str:
        if not text or not target_lang or target_lang.lower() in source_lang:
            return text

        prompt = self.prompt_template.format(target_lang=target_lang, text=text)
        try:
            self.logger.debug(
                f"Translating '{text[:30]}...' to '{target_lang}' using LLM."
            )
            return await generate_text(prompt, self.model, self.client)
        except Exception as e:
            self.logger.error(f"LLM translation failed for text to '{target_lang}': {e}")
            return text

    async def translate_batch(
        self, texts: list[str], target_lang: str, source_lang: list[str] = ["en", "en-us"]
    ) -> list[str]:
        if not texts or not target_lang or target_lang.lower() in source_lang:
            return texts

        self.logger.debug(
            f"Batch translating {len(texts)} texts to '{target_lang}' using LLM."
        )
        tasks = [self.translate(text, target_lang) for text in texts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_texts = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                self.logger.error(f"Error in batch translation for item {i}: {res}")
                final_texts.append(texts[i])
            else:
                final_texts.append(res)
        return final_texts
