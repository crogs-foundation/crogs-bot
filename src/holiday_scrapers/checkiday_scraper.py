import asyncio
from typing import List

import requests
from bs4 import BeautifulSoup

from src.holiday_scrapers.base import HolidayScraper


class CheckidayScraper(HolidayScraper):
    """Scrapes holidays from checkiday.com."""

    async def scrape(self) -> List[str]:
        url = self.config.get("url")
        limit = self.config.get("limit", 0)
        selector = self.config.get("selector", "h2.mdl-card__title-text")

        if not url:
            self.logger.error("CheckidayScraper is missing 'url' in its config.")
            return []

        try:
            self.logger.info(f"Scraping {url}...")
            response = await asyncio.to_thread(requests.get, url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            holidays = list(
                filter(
                    lambda x: x not in ["Daily Updates", "On This Day in History"],
                    [h.text.strip() for h in soup.select(selector)],
                )
            )
            self.logger.info(f"Found {len(holidays)} holidays from Checkiday.")
            return holidays[:limit] if limit > 0 else holidays

        except requests.RequestException as e:
            self.logger.error(f"Error fetching holidays from Checkiday: {e}")
            return []
