import asyncio
from datetime import datetime, timezone
from typing import List

import requests
from bs4 import BeautifulSoup

from src.holiday_scrapers.base import HolidayScraper


class OfficeHolidaysScraper(HolidayScraper):
    async def scrape(self) -> List[str]:
        url = self.config.get("url")
        limit = self.config.get("limit", 0)
        selector = self.config.get("selector", "figure h3")

        if not url:
            self.logger.error("OfficeHolidaysScraper is missing 'url' in its config.")
            return []

        try:
            url = f"{url}{datetime.strftime(datetime.now(tz=timezone.utc), '%Y/%m/%d')}"
            self.logger.info(f"Scraping {url}...")
            response = await asyncio.to_thread(requests.get, url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            holidays = [h.text.strip() for h in soup.select(selector)]
            self.logger.info(f"Found {len(holidays)} holidays from OfficeHolidays.")
            return holidays[:limit] if limit > 0 else holidays

        except requests.RequestException as e:
            self.logger.error(f"Error fetching holidays from OfficeHolidays: {e}")
            return []
