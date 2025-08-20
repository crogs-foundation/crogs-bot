import asyncio
from datetime import datetime
from typing import Any, Callable, Optional

from telebot.apihelper import ApiTelegramException
from telebot.types import Message

from src.bot_modules.base import BotModule
from src.config_management import ConfigManager
from src.llm import generate_image


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
        dev,
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
            dev,
        )

        self._image_placeholder = module_config.get("llm", {}).get(
            "image_placeholder", ""
        )

        self.logger.info(f"ImageGeneratorModule '{self.name}' initialized.")

    async def _handle_image_request(
        self, message: Message, prompt: str, target_lang: str
    ):
        """
        Handles the long-running process of generating and sending an image
        in a background task to avoid blocking the main bot loop.
        """
        try:
            data = await self._generate_image(prompt, target_lang)
            if data:
                image_url, caption = data
                await self._post_image(
                    image_url, caption=caption, target_chat_id=message.chat.id
                )
            else:
                await self.sign_reply(
                    message,
                    "Sorry, I couldn't generate the image. The image service might be down or the request was rejected.",
                    utility=True,
                    target_lang=target_lang,
                )
        except Exception as e:
            self.logger.error(f"Error in background image generation task: {e}")

    def register_handlers(self):
        """Register the /img command handler."""

        @self.bot.message_handler(commands=["img"])
        async def send_image(message):
            target_lang = ConfigManager.get_language_for_chat(
                message.chat.id, self.global_config
            )

            if not self.is_enabled_for_chat(message.chat.id):
                self.logger.debug(
                    f"Image command ignored in chat {message.chat.id} because module is disabled."
                )
                return

            parts = message.text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await self.sign_reply(
                    message,
                    "Please provide a description. \nUsage: `/img a cat sitting on a moon`",
                    utility=True,
                    target_lang=target_lang,
                )
                return

            prompt = parts[1].strip()
            self.logger.info(
                f"Received /img command in chat {message.chat.id} with prompt: '{prompt}'"
            )

            await self.sign_reply(
                message,
                f'🎨 Generating an image for: "{prompt}"...',
                utility=True,
                target_lang=target_lang,
            )

            # Schedule the image generation and posting to run in the background
            asyncio.create_task(self._handle_image_request(message, prompt, target_lang))

    async def _generate_image(
        self, prompt: str, target_lang: str
    ) -> Optional[tuple[str, str]]:
        """Generates an image using the configured provider and returns the URL."""
        llm_cfg = self.module_config.get("llm", {})
        model = llm_cfg.get("image_model", self._base_image_model)
        prompt_template = llm_cfg.get("image_prompt_template", "{prompt}")
        final_prompt: str = prompt_template.format(prompt=prompt)

        self.logger.debug(
            f"Generating image with model '{model}' and prompt: '{final_prompt}'"
        )
        try:
            image_url, _caption = await generate_image(
                final_prompt,
                model,
                self.client,
                max_caption_size=1000,
                translator_options=(self.translator, "en")
                if target_lang != "en"
                else None,
            )
            caption = final_prompt.capitalize()
            if image_url and image_url.startswith("http"):
                return (image_url, caption)
            self.logger.error(f"Image generation returned invalid URL: {image_url}")
            return (
                self._image_placeholder,
                caption,
            )
        except Exception as e:
            self.logger.error(f"Error generating image for prompt '{prompt}': {e}")
            return None

    async def _post_image(self, image_url: str, caption: str, target_chat_id: int):
        """Sends the generated image to the specified chat."""
        try:
            # Telegram captions have a 1024 character limit.
            if len(caption) > 1000:
                caption = caption[:997] + "..."
            caption = self._sign_response(caption)
            await self.bot.send_photo(target_chat_id, image_url, caption=caption)
        except ApiTelegramException as e:
            self.logger.error(
                f"Telegram API Error sending photo to {target_chat_id}: {e}"
            )

    def get_commands(self) -> list[dict[str, Any]]:
        return [
            {
                "command": "img",
                "description": "Generates an image from a text description (e.g., /img a cat in space).",
                "admin_only": False,
            }
        ]

    # ----- Abstract Methods -----

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        pass

    @property
    def has_pending_posts(self) -> bool:
        return False

    @property
    def next_scheduled_event_time(self) -> Optional[datetime]:
        return None

    async def process_due_event(self):
        pass  # No scheduled events
