# src/bot_modules/holibot.py
import asyncio
from asyncio import QueueEmpty
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup
from telebot.apihelper import ApiTelegramException

from src.bot_modules.base import BotModule


class HoliBotModule(BotModule):
    """
    BotModule responsible for fetching and posting daily holidays.
    Content generation and posting are separated; posting is distributed over a period.
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
        self.logger.info(f"HoliBotModule '{self.name}' initialized.")

        self._generated_content_queue: asyncio.Queue = asyncio.Queue()
        self._next_post_time: datetime | None = None
        self._post_interval_seconds: float = 0.0

        # Load last generation date (default to epoch if missing/invalid)
        last_gen_date_str = self.module_config.get("_last_generation_date", "1970-01-01")
        try:
            self._last_generation_date = datetime.fromisoformat(last_gen_date_str).date()
        except ValueError:
            self.logger.warning(
                f"Invalid _last_generation_date '{last_gen_date_str}', resetting to 1970-01-01."
            )
            self._last_generation_date = datetime(1970, 1, 1).date()

    # ----- Required API -----

    def register_handlers(self):
        """No Telegram command handlers for this module."""
        pass

    @property
    def has_pending_posts(self) -> bool:
        return not self._generated_content_queue.empty()

    @property
    def next_scheduled_event_time(self) -> datetime | None:
        """
        Returns the next UTC datetime when this module needs attention.
        Prioritizes pending posts if any. Otherwise, looks at next generation time.
        """
        now = datetime.now(timezone.utc)
        scheduler_cfg = self.module_config.get("scheduler", {})
        generation_time_str = scheduler_cfg.get("post_time_utc")

        next_event: datetime | None = None

        # 1) Next relevant generation time
        if generation_time_str:
            try:
                gen_hour, gen_minute = self._parse_hhmm(generation_time_str)
                today_gen_time = now.replace(
                    hour=gen_hour, minute=gen_minute, second=0, microsecond=0
                )

                if self._last_generation_date != now.date():
                    # If generation not yet done for today...
                    if now >= today_gen_time - timedelta(seconds=2):
                        next_event = now  # due immediately
                    else:
                        next_event = today_gen_time
                else:
                    # Already generated today -> schedule tomorrow
                    next_event = today_gen_time + timedelta(days=1)
            except (ValueError, KeyError) as e:
                self.logger.error(
                    f"Invalid 'post_time_utc' for '{self.name}' generation: {e}"
                )

        # 2) Next post time (if queue has items)
        if self.has_pending_posts and self._next_post_time:
            post_event = (
                now
                if self._next_post_time <= now + timedelta(seconds=2)
                else self._next_post_time
            )
            if next_event is None or post_event < next_event:
                next_event = post_event

        return next_event

    async def process_due_event(self):
        """
        Called by the main scheduler when next_scheduled_event_time is due.
        Determines whether to generate content or post a queued item.
        """
        now = datetime.now(timezone.utc)
        scheduler_cfg = self.module_config.get("scheduler", {})
        generation_time_str = scheduler_cfg.get("post_time_utc")

        is_generation_due = False
        if generation_time_str:
            try:
                gen_hour, gen_minute = self._parse_hhmm(generation_time_str)
                today_gen_time = now.replace(
                    hour=gen_hour, minute=gen_minute, second=0, microsecond=0
                )

                if (
                    self._last_generation_date != now.date()
                    and now >= today_gen_time - timedelta(seconds=2)
                ):
                    is_generation_due = True
            except (ValueError, KeyError) as e:
                self.logger.error(
                    f"Error parsing generation time for '{self.name}' in process_due_event: {e}"
                )

        if is_generation_due:
            await self._do_generate_and_queue_content()
        elif (
            self.has_pending_posts
            and self._next_post_time
            and now >= (self._next_post_time - timedelta(seconds=2))
        ):
            await self._do_post_next_item()
        else:
            self.logger.debug(
                f"'{self.name}': process_due_event called, but no action at {now.strftime('%H:%M:%S UTC')}."
                f" Last gen: {self._last_generation_date}."
                f" Next: {self.next_scheduled_event_time.strftime('%H:%M:%S UTC') if self.next_scheduled_event_time else 'N/A'}"
            )

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        """
        Triggered by manual commands (/postnow, /posttome).
        Always (re)generates content and then posts all pending items to the specified chats.
        """
        self.logger.info(
            f"'{self.name}' received manual trigger for chat_ids: {target_chat_ids}."
        )
        await self._do_generate_and_queue_content()

        posts_made = 0
        while self.has_pending_posts:
            posted = await self._do_post_next_item(target_chat_ids=target_chat_ids)
            if posted:
                posts_made += 1
            else:
                break
            await asyncio.sleep(0.5)

        self.logger.info(
            f"'{self.name}' manual posting finished. Posted {posts_made} items."
        )

    # ----- Internal helpers -----

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        hour, minute = map(int, value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("HH:MM out of range")
        return hour, minute

    @staticmethod
    def _within_same_or_next_day_window(
        start: datetime, end: datetime
    ) -> tuple[datetime, datetime]:
        """Ensure end >= start, rolling end by +1 day if the window crosses midnight."""
        if end < start:
            end += timedelta(days=1)
        return start, end

    def _clear_queue(self):
        """Remove all items from the async queue without awaiting."""
        try:
            while True:
                self._generated_content_queue.get_nowait()
        except QueueEmpty:
            pass

    # ----- Scraping & generation -----

    def _get_todays_holidays(self) -> list[str]:
        """Scrape holiday names from the configured URL."""
        try:
            cfg = self.module_config.get("scraper", {})
            url = cfg.get("holiday_url", "https://www.checkiday.com/")
            limit = cfg.get("holiday_limit", 0)

            response = requests.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            holidays = [
                h.text.strip() for h in soup.find_all("h2", class_="mdl-card__title-text")
            ]

            return holidays[:limit] if limit > 0 else holidays
        except requests.RequestException as e:
            self.logger.error(f"Error fetching holidays for '{self.name}': {e}")
            return []

    async def _generate_caption(self, holiday_name: str) -> str:
        """Generate a short caption for a holiday using the LLM client."""
        llm_cfg = self.module_config.get("llm", {})
        prompt_template = llm_cfg.get(
            "text_prompt", "Generate a short, funny caption for '{holiday_name}'."
        )
        model = llm_cfg.get("text_model", "gpt-3.5-turbo")

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
        """Generate an image URL for a holiday using the LLM client."""
        llm_cfg = self.module_config.get("llm", {})
        prompt_template = llm_cfg.get(
            "image_prompt", "A humorous image for '{holiday_name}'."
        )
        model = llm_cfg.get("image_model", "dall-e-3")

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
        """Generate caption and image concurrently for a single holiday."""
        async with semaphore:
            self.logger.debug(f"Generating content for '{holiday_name}' ({self.name})...")
            caption, image_url = await asyncio.gather(
                self._generate_caption(holiday_name),
                self._generate_image(holiday_name),
            )
            self.logger.debug(f"Finished content for '{holiday_name}' ({self.name}).")
            return holiday_name, caption, image_url

    async def _do_generate_and_queue_content(self):
        """
        Generate content for today's holidays and queue it for posting.
        Also updates _last_generation_date and persists it.
        """
        now = datetime.now(timezone.utc)
        today = now.date()

        self.logger.info(f"'{self.name}': Starting content generation for {today}.")
        holidays = await asyncio.to_thread(self._get_todays_holidays)

        # Persist generation date regardless of results (prevents repeated attempts).
        self._last_generation_date = today
        self._save_state_callback("_last_generation_date", today.isoformat())

        if not holidays:
            self.logger.warning(
                f"No holidays found for '{self.name}'. No content to queue."
            )
            self._next_post_time = None
            return False

        # Clear any previous queue
        self._clear_queue()

        # Generate with concurrency
        concurrency = self.module_config.get("llm", {}).get("concurrency_limit", 4)
        self.logger.info(
            f"Found {len(holidays)} holidays for '{self.name}'. Generating content with concurrency {concurrency}."
        )
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [self._generate_holiday_content(h, semaphore) for h in holidays]
        generated_content = await asyncio.gather(*tasks)
        self.logger.info(
            f"Content generation complete for '{self.name}'. Queueing {len(generated_content)} items."
        )

        for item in generated_content:
            await self._generated_content_queue.put(item)

        self._calculate_post_schedule(now)

        self.logger.info(
            f"'{self.name}': Content generation and queueing complete for {today}. "
            f"Next post scheduled for: "
            f"{self._next_post_time.strftime('%H:%M:%S UTC') if self._next_post_time else 'N/A'}"
        )
        return True

    def _calculate_post_schedule(self, now: datetime):
        """Calculate posting intervals and next post time based on scheduler config."""
        scheduler_cfg = self.module_config.get("scheduler", {})
        start_str = scheduler_cfg.get("post_start_time_utc")
        end_str = scheduler_cfg.get("post_end_time_utc")

        try:
            start_hour, start_minute = self._parse_hhmm(start_str)
            end_hour, end_minute = self._parse_hhmm(end_str)

            start_time = now.replace(
                hour=start_hour, minute=start_minute, second=0, microsecond=0
            )
            end_time = now.replace(
                hour=end_hour, minute=end_minute, second=0, microsecond=0
            )
            start_time, end_time = self._within_same_or_next_day_window(
                start_time, end_time
            )

            num_posts = self._generated_content_queue.qsize()
            total_window = (end_time - start_time).total_seconds()

            if num_posts > 0 and total_window > 0:
                # Spread N posts uniformly across the window (first at start, last at end)
                self._post_interval_seconds = total_window / max(1, num_posts - 1)
            else:
                self._post_interval_seconds = 0.0

            # First post time: tomorrow's start if we're past today's window; otherwise now or start.
            self._next_post_time = (
                start_time + timedelta(days=1) if now > end_time else max(now, start_time)
            )

            self.logger.info(
                f"Calculated post interval for '{self.name}': {self._post_interval_seconds:.2f} seconds "
                f"({num_posts} posts over {total_window / 3600:.2f} hours). "
                f"First post at {self._next_post_time.strftime('%H:%M:%S UTC')}."
            )
        except Exception as e:
            self.logger.error(
                f"Invalid scheduler times (start/end) for '{self.name}': {e}. "
                f"Posting might not be uniformly distributed."
            )
            self._post_interval_seconds = 0.0
            self._next_post_time = now  # Fallback: post immediately

    async def _do_post_next_item(
        self, target_chat_ids: Optional[list[int]] = None
    ) -> bool:
        """
        Post the next item from the queue to Telegram.
        Returns True if a post was attempted/sent, False if no item was available.
        """
        if self._generated_content_queue.empty():
            self.logger.debug(
                f"'{self.name}': Post queue is empty. No more items to post."
            )
            self._next_post_time = None
            return False

        post_to_chats = target_chat_ids or self.global_config["telegram"]["chat_ids"]
        if not post_to_chats:
            self.logger.warning(
                f"No target chats for '{self.name}' to post to. Dropping queued item."
            )
            await self._generated_content_queue.get()
            self._next_post_time = datetime.now(timezone.utc) + timedelta(
                seconds=self._post_interval_seconds
            )
            return False

        try:
            holiday, caption, image_url = await self._generated_content_queue.get()

            telegram_cfg = self.module_config.get("telegram_settings", {})
            caption_limit = telegram_cfg.get("caption_character_limit", 1024)
            if len(caption) > caption_limit:
                caption = caption[: caption_limit - 3] + "..."

            self.logger.info(
                f"'{self.name}': Posting '{holiday}' to {len(post_to_chats)} chat(s)."
            )

            for chat_id in post_to_chats:
                try:
                    if image_url:
                        await self.bot.send_photo(chat_id, image_url, caption=caption)
                    else:
                        await self.bot.send_message(chat_id, caption)
                except ApiTelegramException as e:
                    self.logger.error(
                        f"Telegram API Error sending from '{self.name}' to {chat_id} for {holiday}: {e}"
                    )

            # Schedule time for the next item
            if not self._generated_content_queue.empty():
                self._next_post_time = datetime.now(timezone.utc) + timedelta(
                    seconds=self._post_interval_seconds
                )
            else:
                self.logger.info(
                    f"'{self.name}': Last item posted for today. Queue is now empty."
                )
                self._next_post_time = None

            # Optional (kept for parity with original): parse window, no enforcement
            scheduler_cfg = self.module_config.get("scheduler", {})
            post_start_str = scheduler_cfg.get("post_start_time_utc")
            post_end_str = scheduler_cfg.get("post_end_time_utc")
            if (
                post_start_str
                and post_end_str
                and self._next_post_time
                and not self._generated_content_queue.empty()
            ):
                try:
                    # Calculate today's start and end times (no dropping behavior)
                    start_hour, start_minute = self._parse_hhmm(post_start_str)
                    end_hour, end_minute = self._parse_hhmm(post_end_str)
                    _ = datetime.now(timezone.utc).replace(
                        hour=start_hour, minute=start_minute, second=0, microsecond=0
                    )
                    _ = datetime.now(timezone.utc).replace(
                        hour=end_hour, minute=end_minute, second=0, microsecond=0
                    )
                except (ValueError, KeyError) as e:
                    self.logger.error(
                        f"Error parsing post_start/end_time_utc for '{self.name}': {e}. Uniform distribution check failed."
                    )

            return True

        except Exception as e:
            self.logger.error(f"Error while processing next post for '{self.name}': {e}")
            self._next_post_time = datetime.now(timezone.utc) + timedelta(
                seconds=self._post_interval_seconds
            )
            return False
