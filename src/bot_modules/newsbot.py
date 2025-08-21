# src/bot_modules/newsbot.py
import asyncio
import html
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from telebot.apihelper import ApiTelegramException

from src.bot_modules.base import BotModule
from src.llm import generate_image, generate_text

STATE_FILE = "newsbot_state.json"


class NewsBotModule(BotModule):
    """
    Scrapes top news from multiple sources in a round-robin fashion, reads the
    article content, generates summaries and images, and posts them on a schedule.
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
        self.posted_article_urls = set()
        self._state_data: dict = {"posted_articles": {}}
        self._next_post_time = None
        self.last_source_index = -1
        self._state_file = self.state_folder / STATE_FILE
        self._load_state_from_disk()
        self._calculate_next_post_time()
        self._image_placeholder = module_config.get("llm", {}).get(
            "image_placeholder", ""
        )
        self.logger.info(
            f"NewsBotModule '{self.name}' initialized. Next post scheduled for {self._next_post_time}."
        )

    def _load_state_from_disk(self):
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                self._state_data = json.load(f)
            history_days = self.module_config.get("state_management", {}).get(
                "history_days", 7
            )
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=history_days)
            fresh_articles = {
                url: ts
                for url, ts in self._state_data.get("posted_articles", {}).items()
                if datetime.fromisoformat(ts) > cutoff_date
            }
            if len(fresh_articles) != len(self._state_data.get("posted_articles", {})):
                self.logger.info("Pruned old articles.")
            self._state_data["posted_articles"] = fresh_articles
            self.posted_article_urls = set(fresh_articles.keys())
            self.last_source_index: int = self._state_data.get("last_source_index", -1)
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            self.logger.warning(
                f"Could not load state from {self._state_file}: {e}. Starting fresh."
            )
            self._state_data = {"posted_articles": {}}
            self.last_source_index = -1

    async def _save_state_to_disk(self):
        try:
            self._state_data["last_source_index"] = self.last_source_index
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(self._state_data, f, indent=2)
            self.logger.debug(
                f"NewsBot state saved with {len(self.posted_article_urls)} articles."
            )
        except Exception as e:
            self.logger.error(f"Failed to save NewsBot state: {e}")

    def _add_article_to_history(self, url: str):
        if url not in self.posted_article_urls:
            self.posted_article_urls.add(url)
            self._state_data["posted_articles"][url] = datetime.now(
                timezone.utc
            ).isoformat()
            asyncio.create_task(self._save_state_to_disk())

    @property
    def next_scheduled_event_time(self) -> Optional[datetime]:
        return self._next_post_time

    async def process_due_event(self):
        self.logger.info("Scheduled time reached. Starting news processing job.")
        await self._run_news_job()
        self._calculate_next_post_time()
        self.logger.info(
            f"News job finished. Next post is now scheduled for {self._next_post_time}."
        )

    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        self.logger.info(f"Manual trigger for NewsBot. Target chats: {target_chat_ids}.")
        await self._run_news_job(force_post=True, target_chat_ids=target_chat_ids)

    def register_handlers(self):
        pass

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        hour, minute = map(int, value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("HH:MM out of range")
        return hour, minute

    def _calculate_next_post_time(self):
        cfg = self.module_config.get("scheduler", {})
        now = datetime.now(timezone.utc)
        try:
            start_h, start_m = self._parse_hhmm(cfg["post_start_time_utc"])
            end_h, end_m = self._parse_hhmm(cfg["post_end_time_utc"])
            interval = timedelta(minutes=int(cfg["post_interval_minutes"]))
            start_today = now.replace(
                hour=start_h, minute=start_m, second=0, microsecond=0
            )
            end_today = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            search_start_time = max(now, start_today)
            if search_start_time > end_today:
                self._next_post_time = start_today + timedelta(days=1)
                return
            next_slot = start_today
            while next_slot < search_start_time:
                next_slot += interval
            if next_slot <= end_today:
                self._next_post_time = next_slot
            else:
                self._next_post_time = start_today + timedelta(days=1)
        except (KeyError, ValueError) as e:
            self.logger.error(f"Invalid scheduler config: {e}. Disabling schedule.")
            self._next_post_time = None

    # --- Round-robin logic---
    async def _run_news_job(self, force_post=False, target_chat_ids=None):
        sources = self.module_config.get("scraper", {}).get("sources", [])
        if not sources:
            self.logger.warning("No news sources configured to run job.")
            return

        num_sources = len(sources)

        # Determine the starting index for the loop.
        # For a scheduled run, continue from the last source.
        # For a manual/forced run, always start from the beginning.
        start_index = 0 if force_post else (self.last_source_index + 1) % num_sources

        if force_post:
            self.logger.info("Manual trigger: searching all sources from the beginning.")

        # Iterate through all sources, starting from the calculated index.
        for i in range(num_sources):
            current_index = (start_index + i) % num_sources
            source_cfg = sources[current_index]
            source_name = source_cfg.get("name", f"Source #{current_index}")

            if not force_post:
                self.logger.info(f"Round-robin: Checking source '{source_name}'...")

            try:
                articles = await self._scrape_source_for_articles(source_cfg)
                new_article = next(
                    (a for a in articles if a["url"] not in self.posted_article_urls),
                    None,
                )

                if not new_article:
                    if force_post:  # Be more verbose on manual runs
                        self.logger.info(f"No new articles found from '{source_name}'.")
                    continue  # Try the next source

                self.logger.info(
                    f"Found new article from '{source_name}': {new_article['headline']}"
                )

                content = await self._scrape_article_content(
                    new_article["url"], source_cfg
                )
                if not content:
                    self.logger.warning(
                        "Could not retrieve content for article. Skipping and adding to history."
                    )
                    self._add_article_to_history(new_article["url"])
                    continue

                new_article["content"] = content
                await self._generate_and_post_news(new_article, target_chat_ids)

                self.logger.info(f"Successfully posted article from '{source_name}'.")

                # Only update the round-robin index on a successful SCHEDULED post.
                # A manual post should not affect the schedule's order.
                if not force_post:
                    self.last_source_index = current_index

                self._add_article_to_history(new_article["url"])

                # We found and posted one article, so the job is done for this run.
                return

            except Exception as e:
                self.logger.error(
                    f"An error occurred while processing source '{source_name}': {e}",
                )
                continue

        self.logger.info("Completed a full cycle. No new articles found from any source.")

    # --- Scraping methods (Unchanged) ---
    async def _scrape_source_for_articles(self, source_cfg: dict) -> List[dict]:
        name, url = source_cfg.get("name", "Unknown"), source_cfg.get("news_url")
        if not url:
            return []
        resp = await asyncio.to_thread(
            requests.get, url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        limit = source_cfg.get("news_limit", 5)
        found_articles = []
        for item in soup.select(source_cfg["article_selector"], limit=limit * 3):
            h_tag = item.select_one(source_cfg["headline_selector"])
            a_tag = item.select_one(source_cfg["link_selector"]) or item.find("a")
            if h_tag and a_tag and a_tag.has_attr("href"):  # type: ignore
                href = str(a_tag["href"])  # type: ignore
                if name == "CNN" and not re.search(r"/\d{4}/\d{2}/\d{2}/", href):
                    continue
                headline = h_tag.get_text(strip=True)
                article_url = urljoin(url, href)
                if headline and article_url:
                    found_articles.append({"headline": headline, "url": article_url})
                    if len(found_articles) >= limit:
                        break
        self.logger.info(f"Found {len(found_articles)} articles from {name}.")
        return found_articles

    async def _scrape_article_content(self, url: str, source_cfg: dict) -> Optional[str]:
        self.logger.info(f"Fetching content from article: {url}")
        try:
            response = await asyncio.to_thread(
                requests.get, url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            content_selector = source_cfg.get("content_selector")
            if not content_selector:
                return None
            paragraphs = soup.select(content_selector)
            if not paragraphs:
                return None
            full_text = " ".join(p.get_text(strip=True) for p in paragraphs)
            max_len = self.module_config.get("llm", {}).get("max_content_length", 4000)
            return full_text[:max_len]
        except Exception as e:
            self.logger.error(f"Failed to scrape content from {url}: {e}")
            return None

    # --- Applies escaping to headline and summary ---
    async def _generate_and_post_news(self, article: dict, target_chat_ids=None):
        llm_cfg = self.module_config.get("llm", {})
        summary_prompt = llm_cfg["summary_prompt"].format(
            headline=article["headline"], content=article["content"]
        )
        image_prompt = llm_cfg["image_prompt"].format(headline=article["headline"])

        summary_res, image_data = await asyncio.gather(
            generate_text(summary_prompt, llm_cfg["text_model"], self.client),
            generate_image(image_prompt, llm_cfg["image_model"], self.client),
            return_exceptions=True,
        )
        summary: str = (
            summary_res
            if isinstance(summary_res, str)
            else "Summary could not be generated."
        )
        try:
            image_url: str = image_data[0]  # type: ignore
        except Exception:
            image_url = self._image_placeholder

        all_chats = target_chat_ids or self.global_config["telegram"]["chat_ids"]
        post_to_chats = [cid for cid in all_chats if self.is_enabled_for_chat(cid)]
        if not post_to_chats:
            return

        lang_to_chats = defaultdict(list)
        for chat_id in post_to_chats:
            lang = (
                self.global_config.get("chat_module_settings", {})
                .get(str(chat_id), {})
                .get("language", "en")
            )
            lang_to_chats[lang].append(chat_id)

        for lang, chat_ids in lang_to_chats.items():
            final_headline, final_summary = await self.translator.translate_batch(
                [article["headline"], summary], lang
            )

            # Escape only HTML-sensitive characters
            escaped_headline = html.escape(final_headline)
            escaped_summary = html.escape(final_summary)

            caption1 = f"<b>{escaped_headline}</b>\n\n"
            caption3 = f"<a href='{article['url']}'>Read More</a>"

            max_caption_length = 1000
            caption2 = escaped_summary
            caption_sum_length = len(caption2) + len(caption1) + len(caption3) + 2
            if caption_sum_length > max_caption_length:
                caption2 = caption2[: max_caption_length - caption_sum_length - 3] + "..."

            caption = f"{caption1}{caption2}\n\n{caption3}"

            for chat_id in chat_ids:
                try:
                    await self.sign_send_photo(
                        chat_id, image_url, caption=caption, parse_mode="HTML"
                    )
                except ApiTelegramException as e:
                    self.logger.error(
                        f"Telegram API Error sending news to {chat_id}: {e}"
                    )
                except Exception as e:
                    self.logger.error(f"Failed to send news to {chat_id}: {e}")

    @property
    def has_pending_posts(self) -> bool:
        return False
