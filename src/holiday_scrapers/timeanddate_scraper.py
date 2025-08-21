import asyncio
from datetime import datetime
from typing import List

import requests
from bs4 import BeautifulSoup

from src.holiday_scrapers.base import HolidayScraper


class TimeanddateScraper(HolidayScraper):
    """Scrapes holidays from Timeanddate.com."""

    _parts_selectors = [
        ("event", "", ".tad-otd__main > .tad-otd__section:first-child"),
        ("birth", "Birthday", ".tad-otd__main > .tad-otd__section ~ .tad-otd__section"),
        (
            "death",
            "Deathday",
            ".tad-otd__main > .tad-otd__section ~ .tad-otd__section ~ .tad-otd__section",
        ),
    ]

    def _scrap_and_merge(
        self, soup: BeautifulSoup, selector: str, merge: bool
    ) -> list[str]:
        headings = [
            h.text.strip() for h in soup.select(f"{selector} div.tad-details__heading")
        ]
        if not merge:
            return headings

        descriptions = [
            h.text.strip() for h in soup.select(f"{selector} div.tad-details__content")
        ]

        return [f"{h} - {d}" for h, d in zip(headings, descriptions)]

    async def scrape(self) -> List[str]:
        url = self.config.get("url")
        limit = self.config.get("limit", 0)
        parts = self.config.get("parts", [])

        if not url:
            self.logger.error("TimeanddateScraper is missing 'url' in its config.")
            return []

        try:
            url = f"{url}{datetime.strftime(datetime.now(), '%B/%d').lower()}"
            self.logger.info(f"Scraping {url}...")
            response = await asyncio.to_thread(requests.get, url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            holidays = []
            for part, prefix, global_selector in self._parts_selectors:
                if part not in parts:
                    continue
                holidays.extend(
                    [
                        f"{prefix} {x}"
                        for x in self._scrap_and_merge(
                            soup, global_selector, part != "event"
                        )
                    ]
                )

            self.logger.info(f"Found {len(holidays)} holidays from Timeanddate.")
            return holidays[:limit] if limit > 0 else holidays

        except requests.RequestException as e:
            self.logger.error(f"Error fetching holidays from Timeanddate: {e}")
            return []
