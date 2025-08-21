from abc import ABC, abstractmethod
from typing import List

from src.logger import Logger


class HolidayScraper(ABC):
    """Abstract base class for all holiday scraper implementations."""

    def __init__(self, logger: Logger, config: dict):
        self.logger = logger
        self.config = config

    @abstractmethod
    async def scrape(self) -> List[str]:
        """Scrapes a website and returns a list of holiday names."""
