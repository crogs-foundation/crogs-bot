# src/bot_modules/base.py
from abc import ABC, abstractmethod
from typing import Optional

from g4f.client import AsyncClient  # Assuming g4f client is shared across modules
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
    ):
        """
        Initializes a BotModule.

        Args:
            bot (AsyncTeleBot): The Telegram bot instance.
            client (AsyncClient): The g4f client instance for LLM interactions.
            module_config (dict): Configuration specific to this module.
            global_config (dict): The entire loaded application configuration.
            logger (Logger): The application logger instance.
        """
        self.bot = bot
        self.client = client
        self.module_config = module_config
        self.global_config = global_config
        self.logger = logger
        self.name = module_config.get(
            "name", self.__class__.__name__
        )  # Use name from config or class name

    @abstractmethod
    async def run_scheduled_job(self, target_chat_ids: Optional[list[int]] = None):
        """
        Abstract method for the main job this module performs (e.g., daily post, news fetch).
        This method will be called by the scheduler or manual commands.
        target_chat_ids allows overriding global chat_ids for specific posts (e.g., /posttome).
        """

    @abstractmethod
    def register_handlers(self):
        """
        Abstract method to register all Telegram message handlers specific to this module.
        Implementations should use self.bot.message_handler(...) decorators internally.
        """
