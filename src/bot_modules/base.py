# src/bot_modules/base.py
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Optional

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
        Args:
            bot: Telegram bot instance.
            client: g4f client instance for LLM interactions.
            module_config: Configuration specific to this module.
            global_config: The entire loaded application configuration.
            logger: Application logger instance.
            save_state_callback: Callback to persist module-specific state (key, value) to config.
        """
        self.bot = bot
        self.client = client
        self.module_config = module_config
        self.global_config = global_config
        self.name = module_config.get("name", self.__class__.__name__)
        self.logger = logger.get_child(self.name)
        self._save_state_callback = save_state_callback

    # ----- Abstract API -----
    @abstractmethod
    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        """Run scheduled jobs for the module."""

    @abstractmethod
    def register_handlers(self):
        """Register Telegram command/message handlers."""

    @property
    @abstractmethod
    def has_pending_posts(self) -> bool:
        """Whether the module has posts waiting to be sent."""

    @property
    @abstractmethod
    def next_scheduled_event_time(self) -> Optional[datetime]:
        """Datetime of the next scheduled event, if any."""

    @abstractmethod
    async def process_due_event(self):
        """Process events that are due for execution."""
