# src/bot_modules/jokebot.py
import asyncio
import re
from datetime import datetime
from typing import Callable, Optional

from telebot.apihelper import ApiTelegramException

from src.bot_modules.base import BotModule


class JokeGeneratorModule(BotModule):
    """
    BotModule for generating and posting jokes on demand via the /joke command.
    Scheduled posting is disabled for this module.
    """

    def __init__(
        self,
        bot,
        client,
        module_config,
        global_config,
        logger,
        save_state_callback: Callable[[str, str], None],
        is_module_enabled_for_chat_callback: Callable[[int], bool],
    ):
        super().__init__(
            bot,
            client,
            module_config,
            global_config,
            logger,
            save_state_callback,
            is_module_enabled_for_chat_callback,
        )
        self.logger.info(
            f"JokeGeneratorModule '{self.name}' initialized (scheduled posting disabled)."
        )

    def register_handlers(self):
        """Register the /joke command handler."""

        @self.bot.message_handler(commands=["joke"])
        async def send_joke(message):
            user_id = message.from_user.id
            if user_id not in self.global_config["telegram"]["admin_ids"]:
                await self.bot.reply_to(
                    message, "Sorry, you are not authorized to request jokes."
                )
                return

            # Check if the module is enabled for the chat where the command was issued
            if not self.is_enabled_for_chat(message.chat.id):
                await self.bot.reply_to(
                    message,
                    f"The '{self.name}' module is disabled for this chat. An admin can enable it in the settings.",
                )
                return

            topic = None
            if (parts := message.text.split(maxsplit=1)) and len(parts) > 1:
                topic = parts[1].strip()
                self.logger.debug(f"Received /joke command with topic: '{topic}'")

            await self.bot.reply_to(
                message, f"Generating a joke{' about ' + topic if topic else '...'}"
            )
            joke = await self._generate_joke(topic)
            await self._post_joke(joke, target_chat_ids=[message.chat.id])

    async def _generate_joke(self, topic: Optional[str] = None) -> str:
        """Generate a joke using the configured LLM, optionally about a specific topic."""
        llm_cfg = self.module_config.get("llm", {})
        prompt_key = "joke_prompt_with_topic" if topic else "joke_prompt"
        default_prompt = (
            "Tell me a short, funny joke about {topic}."
            if topic
            else "Tell me a short, funny joke."
        )
        prompt_template = llm_cfg.get(prompt_key, default_prompt)
        prompt = prompt_template.format(topic=topic) if topic else prompt_template
        model = llm_cfg.get("text_model", "gpt-3.5-turbo")

        try:
            self.logger.debug(f"Generating joke with prompt: '{prompt}' (model: {model})")
            response = await self.client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}]
            )
            raw_content = response.choices[0].message.content

            pattern = r"<think>.*?</think>"
            # Replace all occurrences of the pattern with an empty string and trim whitespace.
            cleaned_content = re.sub(pattern, "", raw_content, flags=re.DOTALL).strip()

            return cleaned_content[:2048]  # TODO: Fix
        except Exception as e:
            self.logger.error(f"Error generating joke ({self.name}): {e}")
            return (
                "Why don't scientists trust atoms? Because they make up everything! "
                "(Joke generation failed.)"
            )

    async def _post_joke(self, joke: str, target_chat_ids: Optional[list[int]] = None):
        """
        Post a joke to Telegram chats in batches, respecting per-chat settings.
        """
        all_chats = target_chat_ids or self.global_config["telegram"]["chat_ids"]

        # Filter for chats where this module is enabled
        enabled_chats = [
            chat_id for chat_id in all_chats if self.is_enabled_for_chat(chat_id)
        ]

        if not enabled_chats:
            self.logger.warning(
                f"No enabled chats found for '{self.name}'. Joke will not be posted."
            )
            return

        telegram_cfg = self.module_config.get("telegram_settings", {})
        batch_size = telegram_cfg.get("batch_size", 5)
        batch_delay = telegram_cfg.get("batch_delay_seconds", 2)

        self.logger.info(
            f"Posting joke from '{self.name}' to {len(enabled_chats)} enabled chat(s)."
        )

        for batch_start in range(0, len(enabled_chats), batch_size):
            batch = enabled_chats[batch_start : batch_start + batch_size]
            self.logger.debug(f"Sending batch: {batch}")

            async def send_to_chat(chat_id: int):
                try:
                    await self.bot.send_message(
                        chat_id, f"Here's a laugh from {self.name}:\n\n{joke}"
                    )
                except ApiTelegramException as e:
                    self.logger.error(f"Telegram API Error sending to {chat_id}: {e}")

            await asyncio.gather(*(send_to_chat(chat_id) for chat_id in batch))

            if batch_start + batch_size < len(enabled_chats):
                await asyncio.sleep(batch_delay)

        self.logger.info(f"'{self.name}' joke posting finished.")

    # ----- Scheduling API -----
    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        """Manually trigger a joke post."""
        joke = await self._generate_joke()
        # The _post_joke function already contains the necessary enablement checks
        await self._post_joke(joke, target_chat_ids=target_chat_ids)

    @property
    def has_pending_posts(self) -> bool:
        return False

    @property
    def next_scheduled_event_time(self) -> Optional[datetime]:
        return None

    async def process_due_event(self):
        self.logger.debug(
            f"'{self.name}': process_due_event called, but no scheduled events to process."
        )
