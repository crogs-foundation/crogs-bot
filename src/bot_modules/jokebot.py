# src/bot_modules/joke_generator.py
import asyncio
from typing import Optional

import telebot  # Import for ApiTelegramException

from src.bot_modules.base import BotModule


class JokeGeneratorModule(BotModule):
    """
    A BotModule responsible for generating and posting jokes, with an optional topic.
    """

    def __init__(self, bot, client, module_config, global_config, logger):
        super().__init__(bot, client, module_config, global_config, logger)
        self.logger.info(f"JokeGeneratorModule '{self.name}' initialized.")

    def register_handlers(self):
        """
        Registers command handlers specific to the JokeGeneratorModule.
        """

        @self.bot.message_handler(commands=["joke"])
        async def send_joke(message):
            if message.from_user.id not in self.global_config["telegram"]["admin_ids"]:
                await self.bot.reply_to(
                    message, "Sorry, you are not authorized to request jokes."
                )
                return

            # Parse message for topic: "/joke <topic>"
            command_parts = message.text.split(maxsplit=1)
            topic = None
            if len(command_parts) > 1:
                topic = command_parts[1].strip()
                self.logger.debug(f"Received /joke command with topic: '{topic}'")

            await self.bot.reply_to(
                message, f"Generating a joke {'about ' + topic if topic else '...'}"
            )

            joke = await self._generate_joke(topic=topic)

            await self.bot.reply_to(message, joke)

    async def _generate_joke(self, topic: Optional[str] = None) -> str:
        """
        Generates a joke using the configured LLM, optionally on a specific topic.
        """
        llm_cfg = self.module_config.get("llm", {})

        # Determine the prompt template based on whether a topic is provided
        if topic:
            prompt_template = llm_cfg.get(
                "joke_prompt_with_topic", "Tell me a short, funny joke about {topic}."
            )
            prompt = prompt_template.format(topic=topic)
        else:
            prompt_template = llm_cfg.get("joke_prompt", "Tell me a short, funny joke.")
            prompt = prompt_template

        model = llm_cfg.get("text_model", "gpt-3.5-turbo")

        try:
            self.logger.debug(f"Generating joke with prompt: '{prompt}' (model: {model})")
            response = await self.client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        except Exception as e:
            self.logger.error(f"Error generating joke ({self.name}): {e}")
            return "Why don't scientists trust atoms? Because they make up everything! (Joke generation failed, so here's a classic!)"

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        """
        Main scheduled job for JokeGeneratorModule: generates a joke
        (using the default topic from config if available) and posts it.
        """
        self.logger.info(f"'{self.name}' scheduled job started.")

        default_topic = self.module_config.get("default_joke_topic", "anything")

        joke = await self._generate_joke(topic=default_topic)

        post_to_chats = (
            target_chat_ids
            if target_chat_ids is not None
            else self.global_config["telegram"]["chat_ids"]
        )
        if not post_to_chats:
            self.logger.warning(f"No chats configured for '{self.name}' job. Aborting.")
            return

        telegram_cfg = self.module_config.get("telegram_settings", {})
        post_delay = telegram_cfg.get("post_delay_seconds", 1)

        self.logger.info(
            f"Posting joke from '{self.name}' to {len(post_to_chats)} chat(s)."
        )
        for chat_id in post_to_chats:
            try:
                await self.bot.send_message(
                    chat_id, f"Here's a joke from {self.name}:\n\n{joke}"
                )
                await asyncio.sleep(post_delay)
            except telebot.apihelper.ApiTelegramException as e:
                self.logger.error(
                    f"Telegram API Error sending joke from '{self.name}' to {chat_id}: {e}"
                )
        self.logger.info(f"'{self.name}' job finished.")
