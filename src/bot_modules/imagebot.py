# src/bot_modules/imagebot.py
import asyncio
from datetime import datetime
from typing import Callable, Optional

from telebot.apihelper import ApiTelegramException

from src.bot_modules.base import BotModule


class ImageGeneratorModule(BotModule):
    """
    BotModule for generating images on demand via the /img <description> command.
    """

    def __init__(
        self,
        name,
        bot,
        client,
        translator,
        module_config,
        global_config,
        logger,
        is_module_enabled_for_chat_callback: Callable[[int], bool],
    ):
        super().__init__(
            name,
            bot,
            client,
            translator,
            module_config,
            global_config,
            logger,
            is_module_enabled_for_chat_callback,
        )
        self.logger.info(f"ImageGeneratorModule '{self.name}' initialized.")

    async def _handle_image_request(self, message, prompt: str):
        """
        Handles the long-running process of generating and sending an image
        in a background task to avoid blocking the main bot loop.
        """
        try:
            image_url = await self._generate_image(prompt)
            if image_url:
                await self._post_image(
                    image_url, caption=prompt, target_chat_id=message.chat.id
                )
            else:
                await self.bot.reply_to(
                    message,
                    "Sorry, I couldn't generate the image. The image service might be down or the request was rejected.",
                )
        except Exception as e:
            self.logger.error(f"Error in background image generation task: {e}")
            await self.bot.reply_to(
                message, "An unexpected error occurred. Please try again later."
            )

    def register_handlers(self):
        """Register the /img command handler."""

        @self.bot.message_handler(commands=["img"])
        async def send_image(message):
            if not self.is_enabled_for_chat(message.chat.id):
                # This module is command-driven, so we can ignore this check if we want anyone to use it
                # or keep it to restrict usage to certain chats.
                self.logger.debug(
                    f"Image command ignored in chat {message.chat.id} because module is disabled."
                )
                return

            # Extract the prompt
            parts = message.text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await self.bot.reply_to(
                    message,
                    "Please provide a description. \nUsage: `/img a cat sitting on a moon`",
                )
                return

            prompt = parts[1].strip()
            self.logger.info(
                f"Received /img command in chat {message.chat.id} with prompt: '{prompt}'"
            )

            # Acknowledge the request immediately
            await self.bot.reply_to(message, f'🎨 Generating an image for: "{prompt}"...')

            # Schedule the image generation and posting to run in the background
            asyncio.create_task(self._handle_image_request(message, prompt))

    async def _generate_image(self, prompt: str) -> Optional[str]:
        """Generates an image using the configured provider and returns the URL."""
        llm_cfg = self.module_config.get("llm", {})
        model = llm_cfg.get("image_model", "dall-e-3")
        prompt_template = llm_cfg.get("image_prompt_template", "{prompt}")
        final_prompt = prompt_template.format(prompt=prompt)

        self.logger.debug(
            f"Generating image with model '{model}' and prompt: '{final_prompt}'"
        )
        try:
            response = await self.client.images.generate(
                model=model, prompt=final_prompt, response_format="url"
            )
            image_url = response.data[0].url
            if image_url and image_url.startswith("http"):
                return image_url
            self.logger.warning(f"Image generation returned invalid URL: {image_url}")
            return None
        except Exception as e:
            self.logger.error(f"Error generating image for prompt '{prompt}': {e}")
            return None

    async def _post_image(self, image_url: str, caption: str, target_chat_id: int):
        """Sends the generated image to the specified chat."""
        try:
            # Telegram captions have a 1024 character limit.
            if len(caption) > 1024:
                caption = caption[:1021] + "..."
            await self.bot.send_photo(target_chat_id, image_url, caption=caption)
        except ApiTelegramException as e:
            self.logger.error(
                f"Telegram API Error sending photo to {target_chat_id}: {e}"
            )

    # ----- Abstract Methods (Not used for this command-driven module) -----

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        pass  # Not a scheduled module

    @property
    def has_pending_posts(self) -> bool:
        return False

    @property
    def next_scheduled_event_time(self) -> Optional[datetime]:
        return None

    async def process_due_event(self):
        pass  # No scheduled events
