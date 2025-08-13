# src/bot_modules/holibot.py
import asyncio

import requests
import telebot  # Import for ApiTelegramException
from bs4 import BeautifulSoup

from src.bot_modules.base import BotModule


class HoliBotModule(BotModule):
    """
    A BotModule responsible for fetching and posting daily holidays.
    """

    def __init__(self, bot, client, module_config, global_config, logger):
        super().__init__(bot, client, module_config, global_config, logger)
        self.logger.info(f"HoliBotModule '{self.name}' initialized.")

    def register_handlers(self):
        """
        HoliBotModule currently does not have its own specific command handlers
        beyond what's triggered by the main app's /postnow and /posttome.
        If it had commands like /holiday_settings, they would be registered here.
        """

    def _get_todays_holidays(self) -> list[str]:
        """Synchronous part of holiday scraping, run in a separate thread."""
        try:
            scraper_cfg = self.module_config.get("scraper", {})
            url = scraper_cfg.get("holiday_url", "https://www.checkiday.com/")
            limit = scraper_cfg.get("holiday_limit", 0)

            response = requests.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            elements = [
                h.text.strip() for h in soup.find_all("h2", class_="mdl-card__title-text")
            ]
            return elements[:limit] if limit > 0 else elements
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error fetching holidays for '{self.name}': {e}")
            return []

    async def _generate_caption(self, holiday_name: str) -> str:
        """Generates a caption for a holiday using the configured LLM."""
        llm_cfg = self.module_config.get("llm", {})
        prompt_template = llm_cfg.get(
            "text_prompt", "Generate a short, funny caption for '{holiday_name}'."
        )
        model = llm_cfg.get("text_model", "qwen-3-32b")

        try:
            prompt = prompt_template.format(holiday_name=holiday_name)
            response = await self.client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        except Exception as e:
            self.logger.error(
                f"Error generating caption for {holiday_name} ({self.name}): {e}"
            )
            return f"Happy {holiday_name}!"

    async def _generate_image(self, holiday_name: str) -> str | None:
        """Generates an image URL for a holiday using the configured LLM."""
        llm_cfg = self.module_config.get("llm", {})
        prompt_template = llm_cfg.get(
            "image_prompt", "A humorous image for '{holiday_name}'."
        )
        model = llm_cfg.get("image_model", "flux")

        try:
            prompt = prompt_template.format(holiday_name=holiday_name)
            response = await self.client.images.generate(
                model=model, prompt=prompt, response_format="url"
            )
            image_url = response.data[0].url
            if image_url and image_url.startswith("http"):
                return image_url
            self.logger.warning(
                f"Image generation returned invalid URL for {holiday_name} ({self.name}): {image_url}"
            )
            return None
        except Exception as e:
            self.logger.error(
                f"Error generating image for {holiday_name} ({self.name}): {e}"
            )
            return None

    async def _generate_holiday_content(
        self, holiday_name: str, semaphore: asyncio.Semaphore
    ):
        """Generates both caption and image concurrently for a single holiday."""
        async with semaphore:
            self.logger.debug(f"Generating content for '{holiday_name}' ({self.name})...")
            caption, image_url = await asyncio.gather(
                self._generate_caption(holiday_name), self._generate_image(holiday_name)
            )
            self.logger.debug(f"Finished content for '{holiday_name}' ({self.name}).")
            return holiday_name, caption, image_url

    async def run_scheduled_job(self, target_chat_ids: list[int] = None):
        """
        Main job for HoliBotModule: fetches holidays, generates content, and posts.
        """
        self.logger.info(f"'{self.name}' job started.")

        holidays = await asyncio.to_thread(self._get_todays_holidays)

        # Determine target chats: use provided list (for /posttome) or global config's chat_ids
        post_to_chats = (
            target_chat_ids
            if target_chat_ids is not None
            else self.global_config["telegram"]["chat_ids"]
        )

        if not holidays or not post_to_chats:
            self.logger.warning(
                f"No holidays or target chats found for '{self.name}'. Job aborted."
            )
            return

        llm_cfg = self.module_config.get("llm", {})
        telegram_cfg = self.module_config.get("telegram_settings", {})

        concurrency = llm_cfg.get("concurrency_limit", 4)
        caption_limit = telegram_cfg.get("caption_character_limit", 1024)
        post_delay = telegram_cfg.get("post_delay_seconds", 1)

        self.logger.info(
            f"Found {len(holidays)} holidays for '{self.name}'. Generating content with concurrency {concurrency}."
        )
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [self._generate_holiday_content(h, semaphore) for h in holidays]
        generated_content = await asyncio.gather(*tasks)
        self.logger.info(
            f"Content generation complete for '{self.name}'. Posting to {len(post_to_chats)} chat(s)."
        )

        for holiday, caption, image_url in generated_content:
            if len(caption) > caption_limit:
                caption = caption[: caption_limit - 3] + "..."
            for chat_id in post_to_chats:
                try:
                    if image_url:
                        await self.bot.send_photo(chat_id, image_url, caption=caption)
                    else:
                        await self.bot.send_message(chat_id, caption)
                    await asyncio.sleep(post_delay)
                except telebot.apihelper.ApiTelegramException as e:
                    self.logger.error(
                        f"Telegram API Error sending from '{self.name}' to {chat_id} for {holiday}: {e}"
                    )
        self.logger.info(f"'{self.name}' job finished.")
