# src/bot_modules/holibot.py
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable  # Import Callable

import requests
import telebot
from bs4 import BeautifulSoup

from src.bot_modules.base import BotModule


class HoliBotModule(BotModule):
    """
    A BotModule responsible for fetching and posting daily holidays.
    Content generation is separate from posting, which is distributed over a period.
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

        # State management for daily operations
        self._generated_content_queue: asyncio.Queue = asyncio.Queue()

        # Load last generation date from config, default to a very old date if not found
        last_gen_date_str = self.module_config.get("_last_generation_date", "1970-01-01")
        try:
            self._last_generation_date: datetime.date = datetime.fromisoformat(
                last_gen_date_str
            ).date()
        except ValueError:
            self.logger.warning(
                f"Invalid _last_generation_date '{last_gen_date_str}' in config. Resetting to 1970-01-01."
            )
            self._last_generation_date: datetime.date = datetime(1970, 1, 1).date()

        self._next_post_time: datetime | None = None
        self._post_interval_seconds: float = 0.0

    def register_handlers(self):
        pass

    def _get_todays_holidays(self) -> list[str]:
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
        async with semaphore:
            self.logger.debug(f"Generating content for '{holiday_name}' ({self.name})...")
            caption, image_url = await asyncio.gather(
                self._generate_caption(holiday_name), self._generate_image(holiday_name)
            )
            self.logger.debug(f"Finished content for '{holiday_name}' ({self.name}).")
            return holiday_name, caption, image_url

    async def _do_generate_and_queue_content(self):
        """
        Internal method to perform the content generation and queueing.
        Updates _last_generation_date and saves it to config.
        """
        now = datetime.now(timezone.utc)
        today = now.date()

        self.logger.info(f"'{self.name}': Starting content generation for {today}.")
        holidays = await asyncio.to_thread(self._get_todays_holidays)

        # Update and persist last generation date regardless of whether holidays are found
        # This prevents repeated generation attempts on bot restarts within the same day
        self._last_generation_date = today
        self._save_state_callback("_last_generation_date", today.isoformat())

        if not holidays:
            self.logger.warning(
                f"No holidays found for '{self.name}'. No content to queue."
            )
            self._next_post_time = None  # No posts
            return False

        # Clear previous queue
        while not self._generated_content_queue.empty():
            await self._generated_content_queue.get()

        llm_cfg = self.module_config.get("llm", {})
        concurrency = llm_cfg.get("concurrency_limit", 4)

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

        # Calculate posting intervals
        scheduler_cfg = self.module_config.get("scheduler", {})
        post_start_str = scheduler_cfg.get("post_start_time_utc")
        post_end_str = scheduler_cfg.get("post_end_time_utc")

        try:
            start_hour, start_minute = map(int, post_start_str.split(":"))
            end_hour, end_minute = map(int, post_end_str.split(":"))

            start_time_today = now.replace(
                hour=start_hour, minute=start_minute, second=0, microsecond=0
            )
            end_time_today = now.replace(
                hour=end_hour, minute=end_minute, second=0, microsecond=0
            )

            # Adjust end_time if it spans overnight
            if end_time_today < start_time_today:
                end_time_today += timedelta(days=1)

            total_posting_window_seconds = (
                end_time_today - start_time_today
            ).total_seconds()
            num_posts = self._generated_content_queue.qsize()

            if num_posts > 0 and total_posting_window_seconds > 0:
                self._post_interval_seconds = total_posting_window_seconds / max(
                    1, num_posts - 1
                )
                self.logger.info(
                    f"Calculated post interval for '{self.name}': {self._post_interval_seconds:.2f} seconds ({num_posts} posts over {total_posting_window_seconds / 3600:.2f} hours)."
                )
            else:
                self._post_interval_seconds = 0.0
                self.logger.warning(
                    f"No posts or invalid window for '{self.name}'. Interval set to 0."
                )

            # Set the first post time:
            # If current time is past today's posting end time, first post is tomorrow at start_time.
            # Otherwise, it's either `start_time_today` or `now` (if now is within the window).
            if now > end_time_today:
                self._next_post_time = start_time_today + timedelta(days=1)
                self.logger.info(
                    f"'{self.name}': Current time ({now.strftime('%H:%M:%S UTC')}) past today's posting window. First post scheduled for tomorrow at {self._next_post_time.strftime('%H:%M:%S UTC')}."
                )
            else:
                self._next_post_time = max(now, start_time_today)
                self.logger.info(
                    f"'{self.name}': First post scheduled for today at {self._next_post_time.strftime('%H:%M:%S UTC')}."
                )

        except (ValueError, KeyError) as e:
            self.logger.error(
                f"Invalid scheduler times (start/end) for '{self.name}': {e}. Posting might not be uniformly distributed."
            )
            self._post_interval_seconds = 0.0
            self._next_post_time = now  # Fallback: post immediately

        self.logger.info(
            f"'{self.name}': Content generation and queueing complete for {today}. Next post scheduled for: {self._next_post_time.strftime('%H:%M:%S UTC') if self._next_post_time else 'N/A'}"
        )
        return True  # Indicate content was generated

    async def _do_post_next_item(self, target_chat_ids: list[int] = None) -> bool:
        """
        Internal method to post the next item from the queue to Telegram.
        """
        if self._generated_content_queue.empty():
            self.logger.debug(
                f"'{self.name}': Post queue is empty. No more items to post."
            )
            self._next_post_time = None
            return False

        post_to_chats = (
            target_chat_ids
            if target_chat_ids is not None
            else self.global_config["telegram"]["chat_ids"]
        )
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
                except telebot.apihelper.ApiTelegramException as e:
                    self.logger.error(
                        f"Telegram API Error sending from '{self.name}' to {chat_id} for {holiday}: {e}"
                    )

            # Calculate next post time for the *next* item
            if not self._generated_content_queue.empty():
                self._next_post_time = datetime.now(timezone.utc) + timedelta(
                    seconds=self._post_interval_seconds
                )
            else:
                self.logger.info(
                    f"'{self.name}': Last item posted for today. Queue is now empty."
                )
                self._next_post_time = None

            # Check if the newly calculated _next_post_time falls outside the daily window
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
                    start_hour, start_minute = map(int, post_start_str.split(":"))
                    end_hour, end_minute = map(int, post_end_str.split(":"))

                    # Calculate today's start and end times for the posting window
                    start_time_today = datetime.now(timezone.utc).replace(
                        hour=start_hour, minute=start_minute, second=0, microsecond=0
                    )
                    end_time_today = datetime.now(timezone.utc).replace(
                        hour=end_hour, minute=end_minute, second=0, microsecond=0
                    )

                    # Adjust end_time if it spans overnight
                    if end_time_today < start_time_today:
                        end_time_today += timedelta(days=1)

                    if self._next_post_time > end_time_today:
                        self.logger.warning(
                            f"'{self.name}': Next post time ({self._next_post_time.strftime('%H:%M:%S UTC')}) exceeds daily end time ({end_time_today.strftime('%H:%M:%S UTC')}). Remaining {self._generated_content_queue.qsize()} posts will be dropped for today."
                        )
                        while not self._generated_content_queue.empty():
                            await self._generated_content_queue.get()
                        self._next_post_time = None

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

        next_event = None

        # 1. Determine the next relevant generation time
        if generation_time_str:
            try:
                gen_hour, gen_minute = map(int, generation_time_str.split(":"))
                today_gen_time = now.replace(
                    hour=gen_hour, minute=gen_minute, second=0, microsecond=0
                )

                # If generation not yet done for today AND today_gen_time is past or current
                if (
                    self._last_generation_date != now.date()
                    and now >= today_gen_time - timedelta(seconds=2)
                ):  # Small buffer for check
                    next_event = now  # Signal immediate execution for today's generation
                elif (
                    self._last_generation_date != now.date()
                ):  # Generation not done for today, but time is in future
                    next_event = today_gen_time
                else:  # Generation already done for today, schedule for tomorrow
                    next_event = today_gen_time + timedelta(days=1)
            except (ValueError, KeyError) as e:
                self.logger.error(
                    f"Invalid 'post_time_utc' for '{self.name}' generation: {e}"
                )

        # 2. Determine the next relevant post time (if content is queued)
        if self.has_pending_posts and self._next_post_time:
            # If the calculated next post time is in the past, or very soon, signal immediate execution
            if self._next_post_time <= now + timedelta(seconds=2):
                current_post_event = now
            else:
                current_post_event = self._next_post_time

            if next_event is None or current_post_event < next_event:
                next_event = current_post_event

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
                gen_hour, gen_minute = map(int, generation_time_str.split(":"))
                today_gen_time = now.replace(
                    hour=gen_hour, minute=gen_minute, second=0, microsecond=0
                )

                # Check if generation for today is due and not already done.
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
                f"'{self.name}': process_due_event called, but no action deemed necessary at {now.strftime('%H:%M:%S UTC')}. Last gen: {self._last_generation_date}. Next: {self.next_scheduled_event_time.strftime('%H:%M:%S UTC') if self.next_scheduled_event_time else 'N/A'}"
            )

    async def run_scheduled_job(self, target_chat_ids: list[int] = None):
        """
        This method is triggered by manual commands (/postnow, /posttome).
        It initiates content generation (even if already generated for the day)
        and then immediately posts all pending items to the specified chats.
        """
        self.logger.info(
            f"'{self.name}' received manual trigger for chat_ids: {target_chat_ids}."
        )

        # For manual triggers, always re-generate or ensure queue is filled
        # Setting _last_generation_date is handled by _do_generate_and_queue_content
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
