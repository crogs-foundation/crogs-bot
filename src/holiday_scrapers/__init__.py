from src.holiday_scrapers.checkiday_scraper import CheckidayScraper
from src.holiday_scrapers.officeholidays_scraper import OfficeHolidaysScraper
from src.holiday_scrapers.timeanddate_scraper import TimeanddateScraper

# A mapping of adapter names (from config) to their classes
SCRAPER_ADAPTERS = {
    "checkiday": CheckidayScraper,
    "officeholidays": OfficeHolidaysScraper,
    "timeanddate": TimeanddateScraper,
}


def get_scraper_adapters(logger, scraper_config):
    """Factory function to create scraper instances based on config."""
    adapters = []
    adapter_configs = scraper_config.get("adapters", [])

    for config in adapter_configs:
        name = config.get("name")
        adapter_class = SCRAPER_ADAPTERS.get(name)

        if adapter_class:
            adapters.append(adapter_class(logger, config.get("config", {})))
        else:
            logger.warning(
                f"Unknown holiday scraper adapter '{name}' configured. Skipping."
            )

    return adapters
