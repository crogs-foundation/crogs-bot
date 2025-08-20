# src/bot_modules/holibot.py
import asyncio
import json
from asyncio import QueueEmpty
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional

import requests
from bs4 import BeautifulSoup
from telebot.apihelper import ApiTelegramException

from src.bot_modules.base import BotModule

STATE_FILE = "holibot_state.json"


def get_language_for_chat(chat_id: int, global_config: dict) -> str:
    """Gets the configured language for a chat, defaulting to 'en'."""
    chat_settings = global_config.get("chat_module_settings", {}).get(str(chat_id), {})
    return chat_settings.get("language", "en")


class HoliBotModule(BotModule):
    """
    BotModule responsible for fetching and posting daily holidays.
    """

    def __init__(
        self,
        name,
        bot,
        client,
        translator,  # Added translator
        module_config,
        global_config,
        logger,
        is_module_enabled_for_chat_callback: Callable[[int], bool],
    ):
        super().__init__(
            name,
            bot,
            client,
            translator,  # Pass to super
            module_config,
            global_config,
            logger,
            is_module_enabled_for_chat_callback,
        )
        self.logger.info(f"HoliBotModule '{self.name}' initialized.")
        self._generated_content_queue: asyncio.Queue = asyncio.Queue()
        self._last_generation_date: Optional[date] = None
        self._todays_posts: List[dict] = []
        self._load_state_from_disk()
        self.logger.info(
            f"HoliBot state loaded. Last generation date: {self._last_generation_date}. "
            f"Pending posts in queue: {self._generated_content_queue.qsize()}."
        )

    # ----- State Management on Disk (MODIFIED) -----

    def _load_state_from_disk(self):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)

            generation_date_str = state.get("generation_date")
            if not generation_date_str:
                return

            self._last_generation_date = datetime.fromisoformat(
                generation_date_str
            ).date()
            now = datetime.now(timezone.utc)

            if self._last_generation_date == now.date():
                self._todays_posts = state.get("posts", [])
                self._clear_queue()
                posts_loaded = 0

                # --- MODIFIED LOGIC HERE ---
                # A post is considered "pending" if its status is NOT 'posted' or 'skipped'.
                # This correctly includes items where the 'status' key is missing.
                for item in self._todays_posts:
                    if item.get("status") not in ["posted", "skipped"]:
                        post_time = datetime.fromisoformat(item["post_time"])

                        # --- NEW: Manually triggered posts might be in the past ---
                        # If a manually re-queued post's time is in the past,
                        # schedule it to post in a few seconds from now to avoid skipping it.
                        if post_time <= now:
                            self.logger.info(
                                f"Found a manually re-queued post '{item['holiday_name']}' with a past schedule. "
                                "Rescheduling for immediate posting."
                            )
                            post_time = now + timedelta(seconds=5)
                            # Update the record so it's saved correctly
                            item["post_time"] = post_time.isoformat()

                        post_tuple = (
                            item["holiday_name"],
                            item["caption"],
                            item["image_url"],
                            post_time,
                        )
                        self._generated_content_queue.put_nowait(post_tuple)
                        posts_loaded += 1

                self.logger.info(
                    f"Found {len(self._todays_posts)} total posts for today. "
                    f"Loaded {posts_loaded} pending/re-queued posts into queue."
                )
                if posts_loaded > 0:
                    # If we re-queued anything, save the state with the updated post times.
                    asyncio.create_task(self._save_state_to_disk())

        except FileNotFoundError:
            self.logger.info(f"{STATE_FILE} not found. Will be created on generation.")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            self.logger.error(f"Error loading state from {STATE_FILE}: {e}. Ignoring.")

    async def _save_state_to_disk(self):
        """Saves the current state (including all posts and their statuses) to the state file."""
        state = {
            "generation_date": self._last_generation_date.isoformat()
            if self._last_generation_date
            else None,
            "posts": self._todays_posts,
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            self.logger.debug(
                f"State saved to {STATE_FILE} with {len(self._todays_posts)} total posts."
            )
        except Exception as e:
            self.logger.error(f"Failed to save state to {STATE_FILE}: {e}")

    # ----- Required API (Largely unchanged, logic is now in helpers) -----

    # ... (register_handlers, has_pending_posts, next_scheduled_event_time, process_due_event, run_scheduled_job) ...
    # These methods are identical to the previous 'final' version.
    def register_handlers(self):
        pass

    @property
    def has_pending_posts(self) -> bool:
        return not self._generated_content_queue.empty()

    @property
    def next_scheduled_event_time(self) -> Optional[datetime]:
        now = datetime.now(timezone.utc)
        scheduler_cfg = self.module_config.get("scheduler", {})
        generation_time_str = scheduler_cfg.get("post_time_utc")
        next_gen_event: Optional[datetime] = None
        next_post_event: Optional[datetime] = None

        if generation_time_str:
            try:
                gen_hour, gen_minute = self._parse_hhmm(generation_time_str)
                today_gen_time = now.replace(
                    hour=gen_hour, minute=gen_minute, second=0, microsecond=0
                )
                if self._last_generation_date != now.date():
                    next_gen_event = max(now, today_gen_time)
                else:
                    next_gen_event = today_gen_time + timedelta(days=1)
            except (ValueError, KeyError) as e:
                self.logger.error(f"Invalid 'post_time_utc' for generation: {e}")

        if self.has_pending_posts:
            next_post_event = self._generated_content_queue._queue[0][3]

        if next_gen_event and next_post_event:
            return min(next_gen_event, next_post_event)
        return next_gen_event or next_post_event

    async def process_due_event(self):
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
            except (ValueError, KeyError):
                pass

        is_post_due = False
        if self.has_pending_posts:
            next_post_time = self._generated_content_queue._queue[0][3]
            if now >= next_post_time - timedelta(seconds=2):
                is_post_due = True

        if is_generation_due:
            await self._do_generate_and_queue_content()
        elif is_post_due:
            await self._do_post_next_item()
        else:
            self.logger.debug("process_due_event called, but no action is due.")

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        self.logger.info(
            f"'{self.name}' received manual trigger for chat_ids: {target_chat_ids}."
        )
        await self._do_generate_and_queue_content()
        posts_made = 0
        while self.has_pending_posts:
            posted = await self._do_post_next_item(
                target_chat_ids=target_chat_ids, force_post_now=True
            )
            if posted:
                posts_made += 1
            else:
                break
            await asyncio.sleep(0.5)
        self.logger.info(
            f"'{self.name}' manual posting finished. Posted {posts_made} items."
        )

    # ----- Internal helpers -----
    # ... (_parse_hhmm, _within_same_or_next_day_window, _clear_queue) ...
    # These methods are identical to the previous 'final' version.
    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        hour, minute = map(int, value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("HH:MM out of range")
        return hour, minute

    @staticmethod
    def _within_same_or_next_day_window(start, end):
        if end < start:
            end += timedelta(days=1)
        return start, end

    def _clear_queue(self):
        try:
            while True:
                self._generated_content_queue.get_nowait()
        except QueueEmpty:
            pass

    # ----- Scraping & generation (Unchanged) -----
    # ... (_get_todays_holidays, _generate_caption, _generate_image, _generate_holiday_content) ...
    # These methods are identical to the previous 'final' version.
    def _get_todays_holidays(self) -> list[str]:
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
            self.logger.error(f"Error fetching holidays: {e}")
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
            self.logger.error(f"Error generating caption for {holiday_name}: {e}")
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
                f"Image gen returned invalid URL for {holiday_name}: {image_url}"
            )
            return None
        except Exception as e:
            self.logger.error(f"Error generating image for {holiday_name}: {e}")
            return None

    async def _generate_holiday_content(self, holiday_name: str, semaphore):
        async with semaphore:
            self.logger.debug(f"Generating content for '{holiday_name}'...")
            caption, image_url = await asyncio.gather(
                self._generate_caption(holiday_name), self._generate_image(holiday_name)
            )
            self.logger.debug(f"Finished content for '{holiday_name}'.")
            return holiday_name, caption, image_url

    def _calculate_post_schedule(self, num_posts: int) -> List[datetime]:
        now = datetime.now(timezone.utc)
        scheduler_cfg = self.module_config.get("scheduler", {})
        start_str = scheduler_cfg.get("post_start_time_utc")
        end_str = scheduler_cfg.get("post_end_time_utc")
        schedule = []
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

            effective_start_time = max(now, start_time)
            if effective_start_time > end_time:
                self.logger.warning(
                    "Post window is entirely in the past. Will post immediately."
                )
                return [now + timedelta(seconds=i * 2) for i in range(num_posts)]

            total_window = (end_time - effective_start_time).total_seconds()
            interval = total_window / max(1, num_posts - 1) if num_posts > 1 else 0

            for i in range(num_posts):
                schedule.append(effective_start_time + timedelta(seconds=i * interval))

            self.logger.info(
                f"Calculated schedule for {num_posts} posts. "
                f"First: {schedule[0].isoformat()}, Last: {schedule[-1].isoformat()}"
            )
        except Exception as e:
            self.logger.error(
                f"Invalid scheduler times for '{self.name}': {e}. Posting immediately."
            )
            return [now + timedelta(seconds=i * 2) for i in range(num_posts)]
        return schedule

    async def _do_generate_and_queue_content(self):
        today = datetime.now(timezone.utc).date()
        self.logger.info(f"Starting content generation for {today}.")
        self._last_generation_date = today
        self._todays_posts = []  # Reset the in-memory log

        holidays = await asyncio.to_thread(self._get_todays_holidays)
        if not holidays:
            self.logger.warning("No holidays found. No content to queue.")
            await self._save_state_to_disk()
            return False

        self._clear_queue()
        schedule = self._calculate_post_schedule(len(holidays))

        concurrency = self.module_config.get("llm", {}).get("concurrency_limit", 4)
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [self._generate_holiday_content(h, semaphore) for h in holidays]
        generated_content = await asyncio.gather(*tasks)

        for i, content in enumerate(generated_content):
            holiday_name, caption, image_url = content
            post_time = schedule[i]
            # --- MODIFIED: Populate both the in-memory list and the queue ---
            post_record = {
                "holiday_name": holiday_name,
                "caption": caption,
                "image_url": image_url,
                "post_time": post_time.isoformat(),
                "status": "pending",
            }
            self._todays_posts.append(post_record)
            await self._generated_content_queue.put(
                (holiday_name, caption, image_url, post_time)
            )

        await self._save_state_to_disk()
        self.logger.info(f"Content generation and queueing complete for {today}.")
        return True

    async def _do_post_next_item(
        self, target_chat_ids: Optional[list[int]] = None, force_post_now=False
    ) -> bool:
        if not self.has_pending_posts:
            self.logger.debug("Post queue is empty. No items to post.")
            return False

        if force_post_now:
            holiday, caption, image_url, _ = await self._generated_content_queue.get()
        else:
            holiday, caption, image_url, post_time = (
                self._generated_content_queue.get_nowait()
            )

        english_caption = caption  # The generated caption is always English

        all_chats = target_chat_ids or self.global_config["telegram"]["chat_ids"]
        post_to_chats = [
            chat_id for chat_id in all_chats if self.is_enabled_for_chat(chat_id)
        ]

        post_status = "posted" if post_to_chats else "skipped"
        for post_record in self._todays_posts:
            if post_record["holiday_name"] == holiday:
                post_record["status"] = post_status
                break
        await self._save_state_to_disk()

        if not post_to_chats:
            self.logger.warning(
                f"No enabled chats found to post '{holiday}'. Marked as '{post_status}'."
            )
            return True

        # --- EFFICIENT BATCHING LOGIC ---

        # 1. Group chat IDs by their configured language
        lang_to_chats = defaultdict(list)
        for chat_id in post_to_chats:
            lang = get_language_for_chat(chat_id, self.global_config)
            lang_to_chats[lang].append(chat_id)

        self.logger.info(
            f"Posting '{holiday}'. Grouped {len(post_to_chats)} chats into {len(lang_to_chats)} language(s)."
        )

        # 2. Iterate over the language groups and post
        for lang, chat_ids in lang_to_chats.items():
            final_caption = english_caption
            # Only call the translation API if the language is not English
            if lang.lower() not in ["en", "en-us"]:
                self.logger.debug(f"Translating caption for '{holiday}' to '{lang}'.")
                final_caption = await self.translator.translate(english_caption, lang)

            telegram_cfg = self.module_config.get("telegram_settings", {})
            caption_limit = telegram_cfg.get("caption_character_limit", 1024)
            if len(final_caption) > caption_limit:
                final_caption = final_caption[: caption_limit - 3] + "..."

            self.logger.debug(f"Sending to {len(chat_ids)} chat(s) in '{lang}'.")
            for chat_id in chat_ids:
                try:
                    if image_url:
                        await self.bot.send_photo(
                            chat_id, image_url, caption=final_caption
                        )
                    else:
                        await self.bot.send_message(chat_id, final_caption)
                except ApiTelegramException as e:
                    self.logger.error(
                        f"Telegram API Error sending to {chat_id} for {holiday}: {e}"
                    )

        if not self.has_pending_posts:
            self.logger.info("Last item posted for today. Queue is now empty.")
        return True
