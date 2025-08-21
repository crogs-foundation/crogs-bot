import asyncio
from datetime import datetime
from typing import Any, Callable, Optional

from telebot.apihelper import ApiTelegramException
from telebot.types import Message

from src.bot_modules.base import BotModule
from src.config_management import ConfigManager
from src.llm import generate_text


class JokeGeneratorModule(BotModule):
    """
    BotModule for generating and posting jokes on demand via the /joke command.
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
        self.logger.info(f"JokeGeneratorModule '{self.name}' initialized.")

    async def _handle_joke_request(
        self, message: Message, topic: Optional[str], target_lang: str
    ):
        """
        Handles the long-running process of generating and sending a joke
        in a background task. This prevents blocking the main bot loop.
        """
        try:
            joke = await self._generate_joke(topic, target_lang)
            await self._post_joke(joke, target_message=message)
        except Exception as e:
            self.logger.error(f"Error in background joke generation task: {e}")
            try:
                # Try to inform the user about the failure
                error_text = "I tried to think of a joke, but my circuits fizzled. Please try again later."
                await self.sign_reply(
                    message, error_text, utility=True, target_lang=target_lang
                )
            except Exception as send_e:
                self.logger.error(f"Failed to send error message to user: {send_e}")

    def register_handlers(self):
        """Register the /joke command handler."""

        @self.bot.message_handler(commands=["joke"])
        async def send_joke(message: Message):
            target_lang = ConfigManager.get_language_for_chat(
                message.chat.id, self.global_config
            )

            if not self.is_enabled_for_chat(message.chat.id):
                enabled_err_text = f"The '{self.name}' module is disabled for this chat. An admin can enable it in the settings."
                await self.sign_reply(
                    message, enabled_err_text, utility=True, target_lang=target_lang
                )
                return

            topic = None
            # Priority 1: Check if the command is a reply to another message.
            # We also check if that replied-to message actually contains text.
            if message.reply_to_message and message.reply_to_message.text:
                topic = message.reply_to_message.text.strip()
                self.logger.debug(
                    f"Received /joke as a reply. Using replied message text as topic: '{topic}'"
                )
            # Priority 2: If not a reply, fall back to the original behavior.
            # Check for a topic provided directly after the command.
            else:
                parts = message.text.split(maxsplit=1) if message.text else ["", ""]
                if len(parts) > 1:
                    topic = parts[1].strip()
                    self.logger.debug(f"Received /joke command with topic: '{topic}'")

            # --- Generating Message with Translation ---
            reply_text = f"Generating a joke{' about ' + topic[:100] if topic else '...'}"
            await self.sign_reply(
                message, reply_text, utility=True, target_lang=target_lang
            )

            asyncio.create_task(self._handle_joke_request(message, topic, target_lang))

    async def _generate_joke(
        self, topic: Optional[str] = None, target_lang: str = "en"
    ) -> str:
        llm_cfg = self.module_config.get("llm", {})

        prompt_key = "joke_prompt_with_topic" if topic else "joke_prompt"
        default_prompt = (
            "Tell me a short, funny joke about {topic}."
            if topic
            else "Tell me a short, funny joke."
        )
        prompt_template = llm_cfg.get(prompt_key, default_prompt)
        prompt = prompt_template.format(topic=topic) if topic else prompt_template
        model = llm_cfg.get("text_model", self._base_text_model)

        try:
            return await generate_text(
                prompt,
                model,
                self.client,
                max_size=2000,
                translator_options=(self.translator, target_lang),
            )
        except Exception as e:
            self.logger.error(f"Error generating joke ({self.name}): {e}")
            fallback_joke = "Why don't scientists trust atoms? Because they make up everything! (Joke generation failed.)"
            return await self.translator.translate(fallback_joke, target_lang)

    async def _post_joke(
        self,
        joke: str,
        target_message: Message,
    ):
        try:
            await self.sign_reply(target_message, joke)
        except ApiTelegramException as e:
            self.logger.error(
                f"Telegram API Error sending to {target_message.chat.id}: {e}"
            )

    def get_commands(self) -> list[dict[str, Any]]:
        return [
            {
                "command": "joke",
                "description": "Tells a joke. You can specify a topic (e.g., /joke cats).",
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
        pass
