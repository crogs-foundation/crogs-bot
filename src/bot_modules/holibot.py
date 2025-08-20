# src/bot_modules/holibot.py
import asyncio
import html
import json
from asyncio import QueueEmpty
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional

import requests
from bs4 import BeautifulSoup
from telebot.apihelper import ApiTelegramException

from src.bot_modules.base import BotModule
from src.llm import generate_image, generate_text

STATE_FILE = "holibot_state.json"


class HoliBotModule(BotModule):
    """
    BotModule responsible for fetching and posting daily holidays.
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
        self.logger.info(f"HoliBotModule '{self.name}' initialized.")
        self._generated_content_queue: asyncio.Queue = asyncio.Queue()
        self._last_generation_date: Optional[date] = None
        self._todays_posts: List[dict] = []
        self._image_placeholder = module_config.get("llm", {}).get(
            "image_placeholder", ""
        )

        self._state_file = self.state_folder / STATE_FILE
        self._load_state_from_disk()
        self.logger.info(
            f"HoliBot state loaded. Last generation date: {self._last_generation_date}. "
            f"Pending posts in queue: {self._generated_content_queue.qsize()}."
        )

    # --- State Management on Disk  ---
    def _load_state_from_disk(self):
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
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
                for item in self._todays_posts:
                    if item.get("status") not in ["posted", "skipped"]:
                        post_time = datetime.fromisoformat(item["post_time"])
                        if post_time <= now:
                            self.logger.info(
                                f"Rescheduling past post '{item['holiday_name']}'."
                            )
                            post_time = now + timedelta(seconds=5)
                            item["post_time"] = post_time.isoformat()
                        post_tuple = (
                            item["holiday_name"],
                            item["caption"],
                            item["image_url"],
                            post_time,
                        )
                        self._generated_content_queue.put_nowait(post_tuple)
                        posts_loaded += 1
                self.logger.info(f"Loaded {posts_loaded} pending posts into queue.")
                if posts_loaded > 0:
                    asyncio.create_task(self._save_state_to_disk())
        except FileNotFoundError:
            self.logger.info(f"{self._state_file} not found.")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            self.logger.error(f"Error loading state from {self._state_file}: {e}.")

    async def _save_state_to_disk(self):
        state = {
            "generation_date": self._last_generation_date.isoformat()
            if self._last_generation_date
            else None,
            "posts": self._todays_posts,
        }
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            self.logger.debug(f"State saved to {self._state_file}.")
        except Exception as e:
            self.logger.error(f"Failed to save state to {self._state_file}: {e}")

    # --- Required API ---
    def register_handlers(self):
        pass

    @property
    def has_pending_posts(self) -> bool:
        return not self._generated_content_queue.empty()

    # --- MODIFIED: Fixed logic to prevent skipping the daily generation ---
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

                # Correct logic: if we haven't run today, the event is today's time.
                # If we have run today, the event is tomorrow's time.
                if self._last_generation_date != now.date():
                    next_gen_event = today_gen_time
                else:
                    next_gen_event = today_gen_time + timedelta(days=1)

            except (ValueError, KeyError) as e:
                self.logger.error(f"Invalid 'post_time_utc' for generation: {e}")

        if self.has_pending_posts:
            # Safely peek at the next item in the queue
            next_post_event = self._generated_content_queue._queue[0][3]  # type: ignore

        # Return the soonest of the two possible events
        if next_gen_event and next_post_event:
            return min(next_gen_event, next_post_event)
        return next_gen_event or next_post_event

    # --- MODIFIED: Simplified logic to trust the main scheduler ---
    async def process_due_event(self):
        now = datetime.now(timezone.utc)
        today = now.date()

        # Check if a post is due first
        if self.has_pending_posts:
            next_post_time = self._generated_content_queue._queue[0][3]  # type: ignore
            if now >= next_post_time:
                self.logger.info("A scheduled post is due. Posting now.")
                await self._do_post_next_item()
                return

        # If no post was due, the event must be for content generation
        if self._last_generation_date != today:
            self.logger.info("Scheduled generation time reached.")
            await self._do_generate_and_queue_content()
        else:
            self.logger.debug(
                "process_due_event called, but no action taken. "
                "Posts are not due and generation is complete for today."
            )

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        self.logger.info(f"Manual trigger for chat_ids: {target_chat_ids}.")
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
        self.logger.info(f"Manual posting finished. Posted {posts_made} items.")

    # --- Internal helpers ---
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

    # --- Scraping & generation ---
    def _get_todays_holidays(self) -> list[str]:
        try:
            cfg = self.module_config.get("scraper", {})
            url = cfg.get("holiday_url", "https://www.checkiday.com/")
            limit = cfg.get("holiday_limit", 0)
            response = requests.get(url, timeout=10)
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
        model = llm_cfg.get("text_model", self._base_text_model)
        try:
            prompt = prompt_template.format(holiday_name=holiday_name)
            response = await generate_text(prompt, model, self.client, max_size=1000)
            return response
        except Exception as e:
            self.logger.error(f"Error generating caption for {holiday_name}: {e}")
            return f"Today is a great day to celebrate {holiday_name}!"

    async def _generate_image(self, holiday_name: str) -> str | None:
        llm_cfg = self.module_config.get("llm", {})
        prompt_template = llm_cfg.get(
            "image_prompt", "A humorous image for '{holiday_name}'."
        )
        model = llm_cfg.get("image_model", self._base_image_model)
        try:
            prompt = prompt_template.format(holiday_name=holiday_name)
            image_url, _ = await generate_image(prompt, model, self.client)
            if image_url and image_url.startswith("http"):
                return image_url
            return self._image_placeholder
        except Exception as e:
            self.logger.error(f"Error generating image for {holiday_name}: {e}")
            return None

    async def _generate_holiday_content(self, holiday_name: str, semaphore):
        async with semaphore:
            self.logger.debug(f"Generating content for '{holiday_name}'...")
            caption, image_url = await asyncio.gather(
                self._generate_caption(holiday_name), self._generate_image(holiday_name)
            )
            return holiday_name, caption, image_url

    def _calculate_post_schedule(self, num_posts: int) -> List[datetime]:
        now = datetime.now(timezone.utc)
        scheduler_cfg = self.module_config.get("scheduler", {})
        start_str, end_str = (
            scheduler_cfg.get("post_start_time_utc"),
            scheduler_cfg.get("post_end_time_utc"),
        )
        try:
            start_h, start_m = self._parse_hhmm(start_str)
            end_h, end_m = self._parse_hhmm(end_str)
            start_time = now.replace(
                hour=start_h, minute=start_m, second=0, microsecond=0
            )
            end_time = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            start_time, end_time = self._within_same_or_next_day_window(
                start_time, end_time
            )
            effective_start = max(now, start_time)
            if effective_start > end_time:
                return [now + timedelta(seconds=i * 2) for i in range(num_posts)]
            total_window = (end_time - effective_start).total_seconds()
            interval = total_window / max(1, num_posts - 1) if num_posts > 1 else 0
            schedule = [
                effective_start + timedelta(seconds=i * interval)
                for i in range(num_posts)
            ]
            return schedule
        except Exception as e:
            self.logger.error(
                f"Invalid scheduler times for '{self.name}': {e}. Posting immediately."
            )
            return [now + timedelta(seconds=i * 2) for i in range(num_posts)]

    async def _do_generate_and_queue_content(self):
        today = datetime.now(timezone.utc).date()
        self.logger.info(f"Starting content generation for {today}.")
        self._last_generation_date = today
        self._todays_posts = []
        holidays = await asyncio.to_thread(self._get_todays_holidays)
        if not holidays:
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
        return True

    async def _do_post_next_item(
        self, target_chat_ids: Optional[list[int]] = None, force_post_now=False
    ):
        if not self.has_pending_posts:
            return False

        try:
            holiday_name, llm_caption, image_url, _ = (
                await self._generated_content_queue.get()
                if force_post_now
                else await self._generated_content_queue.get()  # Always wait if we decided to post
            )
        except asyncio.QueueEmpty:
            return False

        all_chats = target_chat_ids or self.global_config["telegram"]["chat_ids"]
        post_to_chats = [cid for cid in all_chats if self.is_enabled_for_chat(cid)]

        post_status = "posted" if post_to_chats else "skipped"
        for post in self._todays_posts:
            if post["holiday_name"] == holiday_name:
                post["status"] = post_status
                break
        await self._save_state_to_disk()

        if not post_to_chats:
            return True

        lang_to_chats = defaultdict(list)
        for chat_id in post_to_chats:
            lang = (
                self.global_config.get("chat_module_settings", {})
                .get(str(chat_id), {})
                .get("language", "en")
            )
            lang_to_chats[lang].append(chat_id)

        header_text_en = f"Happy {holiday_name}!"

        for lang, chat_ids in lang_to_chats.items():
            translated_header, translated_caption = await self.translator.translate_batch(
                [header_text_en, llm_caption], lang
            )

            escaped_header = html.escape(translated_header)
            escaped_caption = html.escape(translated_caption)

            final_caption = f"<b>{escaped_header}</b>\n\n{escaped_caption}"

            telegram_cfg = self.module_config.get("telegram_settings", {})
            caption_limit = telegram_cfg.get("caption_character_limit", 1024)
            if len(final_caption) > caption_limit:
                final_caption = final_caption[:caption_limit]

            for chat_id in chat_ids:
                try:
                    if image_url:
                        await self.sign_send_photo(
                            chat_id,
                            image_url,
                            caption=final_caption,
                            parse_mode="HTML",
                        )
                    else:
                        await self.sign_send_photo(
                            chat_id, final_caption, parse_mode="HTML"
                        )
                except ApiTelegramException as e:
                    if "can't parse entities" in e.description:
                        self.logger.warning(
                            f"HTML parsing failed for chat {chat_id}. Sending without formatting."
                        )
                        fallback_caption = f"{translated_header}\n\n{translated_caption}"
                        if image_url:
                            await self.bot.send_photo(
                                chat_id, image_url, caption=fallback_caption
                            )
                        else:
                            await self.bot.send_message(chat_id, fallback_caption)
                    else:
                        self.logger.error(
                            f"Telegram API Error sending to {chat_id} for {holiday_name}: {e}"
                        )
                except Exception as e:
                    self.logger.error(
                        f"Failed to send post to {chat_id} for {holiday_name}: {e}"
                    )

        if not self.has_pending_posts:
            self.logger.info("Last item posted for today. Queue is now empty.")
        return True
