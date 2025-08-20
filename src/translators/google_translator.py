import asyncio

from googletrans import Translator as GoogleApiTranslator

from src.logger import Logger
from src.translators.base import Translator


def _run_google_translation_in_thread(text_or_texts, dest: str):
    """
    Creates a new translator instance and a new event loop to run a
    translation task. This function is designed to be self-contained,
    blocking, and run in a separate thread to not interfere with the main
    application's event loop.
    """
    # 1. Create the translator instance INSIDE the thread. This is crucial.
    translator = GoogleApiTranslator()

    # 2. Run its async method in a new event loop created just for this thread.
    return asyncio.run(translator.translate(text_or_texts, dest=dest))


class GoogleTranslator(Translator):
    """
    A service to handle text translation using the googletrans library.
    This implementation correctly isolates the blocking and event-loop-conflicting
    library by creating a new instance for each operation in a separate thread.
    """

    def __init__(self, config: dict, logger: Logger):
        super().__init__(config)
        self.logger = logger.get_child("GoogleTranslator")
        self.is_ready = False

    async def check_api(self) -> bool:
        """
        Performs a non-blocking health check by running the entire task in a thread.
        """
        self.logger.info("Checking googletrans API connection...")
        try:
            # We call our new, isolated function in a thread.
            await asyncio.to_thread(_run_google_translation_in_thread, "hello", "es")
            self.logger.info("googletrans API seems to be working.")
            self.is_ready = True
            return True
        except Exception as e:
            self.logger.error(
                f"Failed to initialize googletrans. Translations will be disabled: {e}"
            )
            self.is_ready = False
            return False

    async def translate(self, text: str, target_lang: str) -> str:
        """
        Translates text by running the isolated translation task in a separate thread.
        """
        if not self.is_ready:
            self.logger.warning("Translation skipped; translator is not ready.")
            return text
        if not text or not target_lang or target_lang.lower() in ["en", "en-us"]:
            return text

        try:
            self.logger.debug(
                f"Translating '{text[:30]}...' to '{target_lang}' in isolated thread."
            )
            result = await asyncio.to_thread(
                _run_google_translation_in_thread, text, target_lang
            )
            return result.text
        except Exception as e:
            self.logger.error(f"Failed to translate text to '{target_lang}': {e}")
            return text

    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]:
        """
        Translates a batch of texts by running the isolated translation task in a separate thread.
        """
        if not self.is_ready:
            self.logger.warning("Batch translation skipped; translator is not ready.")
            return texts
        if not texts or not target_lang or target_lang.lower() in ["en", "en-us"]:
            return texts

        try:
            self.logger.debug(
                f"Batch translating {len(texts)} texts to '{target_lang}' in isolated thread."
            )
            results = await asyncio.to_thread(
                _run_google_translation_in_thread, texts, target_lang
            )
            return [result.text for result in results]
        except Exception as e:
            self.logger.error(f"Failed to batch translate texts to '{target_lang}': {e}")
            return texts
