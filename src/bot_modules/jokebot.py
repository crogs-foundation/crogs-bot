# src/bot_modules/joke_generator.py
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable  # Import Callable

import telebot

from src.bot_modules.base import BotModule


class JokeGeneratorModule(BotModule):
    """
    A BotModule responsible for generating and posting jokes, with an optional topic.
    It supports a daily scheduled joke and on-demand joke generation via command.
    """

    def __init__(
        self,
        bot,
        client,
        module_config,
        global_config,
        logger,
        save_state_callback: Callable[[str, str], None],
    ):
        super().__init__(
            bot, client, module_config, global_config, logger, save_state_callback
        )
        self.logger.info(f"JokeGeneratorModule '{self.name}' initialized.")

        # Load last scheduled joke date from config, default to a very old date
        last_joke_date_str = self.module_config.get(
            "_last_scheduled_joke_date", "1970-01-01"
        )
        try:
            self._last_scheduled_joke_date: datetime.date = datetime.fromisoformat(
                last_joke_date_str
            ).date()
        except ValueError:
            self.logger.warning(
                f"Invalid _last_scheduled_joke_date '{last_joke_date_str}' in config. Resetting to 1970-01-01."
            )
            self._last_scheduled_joke_date: datetime.date = datetime(1970, 1, 1).date()

    def register_handlers(self):
        @self.bot.message_handler(commands=["joke"])
        async def send_joke(message):
            if message.from_user.id not in self.global_config["telegram"]["admin_ids"]:
                await self.bot.reply_to(
                    message, "Sorry, you are not authorized to request jokes."
                )
                return

            command_parts = message.text.split(maxsplit=1)
            topic = None
            if len(command_parts) > 1:
                topic = command_parts[1].strip()

            await self.bot.reply_to(
                message, f"Generating a joke {'about ' + topic if topic else '...'}"
            )

            joke = await self._generate_joke(topic=topic)
            await self._post_joke(
                joke, target_chat_ids=[message.chat.id]
            )  # Post only to the sender's chat for manual command

    async def _generate_joke(self, topic: str = None) -> str:
        llm_cfg = self.module_config.get("llm", {})

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

    async def _post_joke(self, joke: str, target_chat_ids: list[int] = None):
        """
        Internal method to post a given joke to Telegram chats.
        Handles selecting target chats and applying delays.
        """
        post_to_chats = (
            target_chat_ids
            if target_chat_ids is not None
            else self.global_config["telegram"]["chat_ids"]
        )
        if not post_to_chats:
            self.logger.warning(
                f"No chats configured for '{self.name}'. Joke will not be posted."
            )
            return

        telegram_cfg = self.module_config.get("telegram_settings", {})
        post_delay = telegram_cfg.get("post_delay_seconds", 1)

        self.logger.info(
            f"Posting joke from '{self.name}' to {len(post_to_chats)} chat(s)."
        )
        for chat_id in post_to_chats:
            try:
                await self.bot.send_message(
                    chat_id, f"Here's a laugh from {self.name}:\n\n{joke}"
                )
                await asyncio.sleep(post_delay)
            except telebot.apihelper.ApiTelegramException as e:
                self.logger.error(
                    f"Telegram API Error sending joke from '{self.name}' to {chat_id}: {e}"
                )
        self.logger.info(f"'{self.name}' joke posting finished.")

    async def run_scheduled_job(self, target_chat_ids: list[int] = None):
        """
        For JokeGenerator, this is primarily called by manual triggers (/postnow, /posttome).
        It generates a joke and posts it immediately.
        """
        self.logger.info(
            f"'{self.name}' received manual trigger for chat_ids: {target_chat_ids}."
        )

        default_topic = self.module_config.get("default_joke_topic")
        joke = await self._generate_joke(topic=default_topic)

        await self._post_joke(joke, target_chat_ids)

    @property
    def has_pending_posts(self) -> bool:
        """
        JokeGenerator does not have a queue for staggered posts, so this is always False.
        Its scheduled action is a single generation and post.
        """
        return False

    @property
    def next_scheduled_event_time(self) -> datetime | None:
        """
        Returns the next UTC datetime when this module needs attention from the scheduler
        for its daily scheduled joke.
        """
        now = datetime.now(timezone.utc)
        scheduler_cfg = self.module_config.get("scheduler", {})
        joke_time_str = scheduler_cfg.get("post_time_utc")

        if not joke_time_str:
            return None

        try:
            joke_hour, joke_minute = map(int, joke_time_str.split(":"))
            today_joke_time = now.replace(
                hour=joke_hour, minute=joke_minute, second=0, microsecond=0
            )

            # If joke not yet sent for today AND today_joke_time is past or current
            if (
                self._last_scheduled_joke_date != now.date()
                and now >= today_joke_time - timedelta(seconds=2)
            ):
                return now  # Signal immediate execution for today's joke
            elif (
                self._last_scheduled_joke_date != now.date()
            ):  # Joke not sent for today, but time is in future
                return today_joke_time
            else:  # Joke already sent for today, schedule for tomorrow
                return today_joke_time + timedelta(days=1)
        except (ValueError, KeyError) as e:
            self.logger.error(
                f"Invalid 'post_time_utc' for '{self.name}' joke scheduling: {e}"
            )
            return None

    async def process_due_event(self):
        """
        Called by the main scheduler when next_scheduled_event_time is due.
        For JokeGenerator, this means generating and posting a single daily joke.
        """
        now = datetime.now(timezone.utc)
        scheduler_cfg = self.module_config.get("scheduler", {})
        joke_time_str = scheduler_cfg.get("post_time_utc")

        is_joke_due = False
        if joke_time_str:
            try:
                joke_hour, joke_minute = map(int, joke_time_str.split(":"))
                today_joke_time = now.replace(
                    hour=joke_hour, minute=joke_minute, second=0, microsecond=0
                )

                # Check if joke for today is due and not already sent
                if (
                    self._last_scheduled_joke_date != now.date()
                    and now >= today_joke_time - timedelta(seconds=2)
                ):
                    is_joke_due = True
            except (ValueError, KeyError) as e:
                self.logger.error(
                    f"Error parsing joke time for '{self.name}' in process_due_event: {e}"
                )

        if is_joke_due:
            self.logger.info(
                f"'{self.name}': Scheduled joke generation and posting is due."
            )
            default_topic = self.module_config.get("default_joke_topic")
            joke = await self._generate_joke(topic=default_topic)

            await self._post_joke(joke)  # Post joke to globally configured chats

            self._last_scheduled_joke_date = now.date()  # Mark as sent for today
            self._save_state_callback(
                "_last_scheduled_joke_date", now.date().isoformat()
            )  # Persist state
            self.logger.info(f"'{self.name}' scheduled joke job finished.")
        else:
            self.logger.debug(
                f"'{self.name}': process_due_event called, but scheduled joke not due at {now.strftime('%H:%M:%S UTC')}. Last sent: {self._last_scheduled_joke_date}. Next: {self.next_scheduled_event_time.strftime('%H:%M:%S UTC') if self.next_scheduled_event_time else 'N/A'}"
            )
