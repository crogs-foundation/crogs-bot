import os
from typing import Any

import yaml

from src.logger import Logger


class ConfigManager:
    """Config manager"""

    _config_file_prod = "config.yaml"
    _config_file_dev = "config.dev.yaml"

    @staticmethod
    def get_language_for_chat(chat_id: int, config: dict) -> str:
        chat_settings = config.get("chat_module_settings", {}).get(str(chat_id), {})
        return chat_settings.get("language", "en")

    def __init__(self, logger: Logger, dev: bool = False) -> None:
        self._config_file = self._config_file_dev if dev else self._config_file_prod

        self.logger = logger.get_child("ConfigManager")
        self._config = self._load_config()
        _tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if _tg_token is None:
            raise ValueError("TELEGRAM_BOT_TOKEN not found in .env file!")
        self._tg_token: str = _tg_token

    @property
    def config(self) -> dict:
        """Config file"""
        return self._config

    @property
    def tg_token(self) -> str:
        """Telegram Bot token file"""
        return self._tg_token

    def _load_config(self) -> dict:
        """Loads config file"""
        try:
            with open(self._config_file, "r", encoding="utf-8") as f:
                loaded_config = yaml.safe_load(f)
                if "chat_module_settings" not in loaded_config:
                    loaded_config["chat_module_settings"] = {}
                return loaded_config
        except FileNotFoundError:
            self.logger.warning(
                f"{self._config_file} not found. Please create it with defaults."
            )
            raise

    def save_config_file(self):
        """Saves the global config dictionary to the YAML file."""
        with open(self._config_file, "w", encoding="utf-8") as f:
            yaml.dump(self._config, f, sort_keys=False, indent=2)
        self.logger.debug("Configuration saved to disk.")

    def reload(self):
        self._config = self._load_config()

    def update_config(self, new_config: dict):
        self._config = new_config
        self.save_config_file()

    def save_chat_ids(self, chat_ids: list[str]):
        """Save new chat ids"""
        self._config["telegram"]["chat_ids"] = chat_ids
        self.save_config_file()

    def extract(self, selector: str, default_value=None) -> Any:
        try:
            parts = list(selector.split("."))
            v = self._config
            for p in parts:
                v = v[p]
            return v
        except Exception:
            return default_value
