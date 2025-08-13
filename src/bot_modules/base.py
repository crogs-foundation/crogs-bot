# src/bot_modules/base.py
from abc import ABC, abstractmethod, abstractproperty
from datetime import datetime
from typing import Callable  # Import Callable for type hinting

from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot

from src.logger import Logger


class BotModule(ABC):
    """
    Abstract base class for all bot modules.
    Each module represents a distinct functionality of the bot.
    """

    def __init__(
        self,
        bot: AsyncTeleBot,
        client: AsyncClient,
        module_config: dict,
        global_config: dict,
        logger: Logger,
        save_state_callback: Callable[[str, str], None],
    ):
        """
        Initializes a BotModule.

        Args:
            bot (AsyncTeleBot): The Telegram bot instance.
            client (AsyncClient): The g4f client instance for LLM interactions.
            module_config (dict): Configuration specific to this module.
            global_config (dict): The entire loaded application configuration.
            logger (Logger): The application logger instance.
            save_state_callback (Callable[[str, str], None]): A callback function
                                  to persist module-specific state (key, value) to config.
        """
        self.bot = bot
        self.client = client
        self.module_config = module_config
        self.global_config = global_config
        self.name = module_config.get("name", self.__class__.__name__)
        # self.logger = logger.getChild(self.name)
        self.logger = logger
        self._save_state_callback = save_state_callback  # Store the callback

    @abstractmethod
    async def run_scheduled_job(self, target_chat_ids: list[int] = None):
        pass

    @abstractmethod
    def register_handlers(self):
        pass

    @abstractproperty
    def has_pending_posts(self) -> bool:
        pass

    @abstractproperty
    def next_scheduled_event_time(self) -> datetime | None:
        pass

    @abstractmethod
    async def process_due_event(self):
        pass
