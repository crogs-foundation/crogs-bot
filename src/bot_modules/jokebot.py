# src/bot_modules/jokebot.py
import re
from datetime import datetime
from typing import Callable, Optional

from telebot.apihelper import ApiTelegramException

from src.bot_modules.base import BotModule


# Helper function to get the language for a chat
def get_language_for_chat(chat_id: int, global_config: dict) -> str:
    """Gets the configured language for a chat, defaulting to 'en'."""
    chat_settings = global_config.get("chat_module_settings", {}).get(str(chat_id), {})
    return chat_settings.get("language", "en")


class JokeGeneratorModule(BotModule):
    """
    BotModule for generating and posting jokes on demand via the /joke command.
    Scheduled posting is disabled for this module.
    """

    def __init__(
        self,
        bot,
        client,
        translator,
        module_config,
        global_config,
        logger,
        save_state_callback: Callable[[str, str], None],
        is_module_enabled_for_chat_callback: Callable[[int], bool],
    ):
        super().__init__(
            bot,
            client,
            translator,
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
            target_lang = get_language_for_chat(message.chat.id, self.global_config)

            # --- Authorization Check with Translation ---
            if user_id not in self.global_config["telegram"]["admin_ids"]:
                auth_err_text = "Sorry, you are not authorized to request jokes."
                # 1. Await the translation FIRST
                translated_err = await self.translator.translate(
                    auth_err_text, target_lang
                )
                # 2. Then await the bot action with the result
                await self.bot.reply_to(message, translated_err)
                return

            # --- Module Enablement Check with Translation ---
            if not self.is_enabled_for_chat(message.chat.id):
                enabled_err_text = f"The '{self.name}' module is disabled for this chat. An admin can enable it in the settings."
                # 1. Await the translation FIRST
                translated_err = await self.translator.translate(
                    enabled_err_text, target_lang
                )
                # 2. Then await the bot action with the result
                await self.bot.reply_to(message, translated_err)
                return

            topic = None
            if (parts := message.text.split(maxsplit=1)) and len(parts) > 1:
                topic = parts[1].strip()
                self.logger.debug(f"Received /joke command with topic: '{topic}'")

            # --- Generating Message with Translation ---
            reply_text = f"Generating a joke{' about ' + topic if topic else '...'}"
            # 1. Await the translation FIRST
            translated_reply = await self.translator.translate(reply_text, target_lang)
            # 2. Then await the bot action with the result
            await self.bot.reply_to(message, translated_reply)

            joke = await self._generate_joke(topic, target_lang)
            await self._post_joke(
                joke, target_chat_ids=[message.chat.id], target_lang=target_lang
            )

    async def _generate_joke(
        self, topic: Optional[str] = None, target_lang: str = "en"
    ) -> str:
        llm_cfg = self.module_config.get("llm", {})
        strategy = self.global_config.get("translation", {}).get("strategy", "response")

        prompt_key = "joke_prompt_with_topic" if topic else "joke_prompt"
        default_prompt = (
            "Tell me a short, funny joke about {topic}."
            if topic
            else "Tell me a short, funny joke."
        )
        prompt_template = llm_cfg.get(prompt_key, default_prompt)
        prompt = prompt_template.format(topic=topic) if topic else prompt_template
        model = llm_cfg.get("text_model", "gpt-3.5-turbo")

        final_prompt = prompt
        if strategy == "prompt" and target_lang.lower() not in ["en", "en-us"]:
            # Correctly awaiting the translation
            final_prompt = await self.translator.translate(prompt, target_lang)

        try:
            response = await self.client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": final_prompt}]
            )
            raw_content = response.choices[0].message.content
            pattern = r"<think>.*?</think>"
            cleaned_content = re.sub(pattern, "", raw_content, flags=re.DOTALL).strip()
            final_content = cleaned_content[:2048]

            if strategy == "response" and target_lang.lower() not in ["en", "en-us"]:
                # Correctly awaiting and returning the translation
                return await self.translator.translate(final_content, target_lang)
            return final_content
        except Exception as e:
            self.logger.error(f"Error generating joke ({self.name}): {e}")
            fallback_joke = "Why don't scientists trust atoms? Because they make up everything! (Joke generation failed.)"
            # Correctly awaiting and returning the translation for the fallback message
            return await self.translator.translate(fallback_joke, target_lang)

    async def _post_joke(
        self,
        joke: str,
        target_chat_ids: Optional[list[int]] = None,
        target_lang: str = "en",
    ):
        all_chats = target_chat_ids or self.global_config["telegram"]["chat_ids"]
        enabled_chats = [
            chat_id for chat_id in all_chats if self.is_enabled_for_chat(chat_id)
        ]

        if not enabled_chats:
            return

        header_text = f"Here's a laugh from {self.name}:"
        # 1. Await the translation FIRST
        translated_header = await self.translator.translate(header_text, target_lang)

        for chat_id in enabled_chats:
            try:
                # 2. Then await the bot action with the result
                await self.bot.send_message(chat_id, f"{translated_header}\n\n{joke}")
            except ApiTelegramException as e:
                self.logger.error(f"Telegram API Error sending to {chat_id}: {e}")

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        target_lang = "en"
        if target_chat_ids and len(target_chat_ids) == 1:
            target_lang = get_language_for_chat(target_chat_ids[0], self.global_config)
        joke = await self._generate_joke(target_lang=target_lang)
        await self._post_joke(
            joke, target_chat_ids=target_chat_ids, target_lang=target_lang
        )

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
