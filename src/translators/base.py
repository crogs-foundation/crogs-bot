# src/translators/base.py
from abc import ABC, abstractmethod
from typing import Literal


class Translator(ABC):
    """Abstract base class for all translator implementations."""

    def __init__(
        self,
        config: dict,
    ):
        self.config = config.get("translation", {})
        self.strategy: Literal["prompt", "response"] = self.config["strategy"]
        self.translate_utility: bool = self.config.get("translate_utility", False)
        self.only_english_models: set[str] = set(
            self.config.get("only_english_models", [])
        )

    @abstractmethod
    async def check_api(self) -> bool:
        """Performs a health check on the translation service."""

    @abstractmethod
    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: list[str] = ["en", "en-us"],
        raise_exception: bool = False,
    ) -> str:
        """Translates a single string of text."""

    @abstractmethod
    async def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        source_lang: list[str] = ["en", "en-us"],
        raise_exception: bool = False,
    ) -> list[str]:
        """Translates a list of strings."""
