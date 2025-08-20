# src/bot_modules/base.py
import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from src.logger import Logger
from src.translators.base import Translator


class BotModule(ABC):
    """
    Abstract base class for all bot modules.
    Each module represents a distinct functionality of the bot.
    """

    _state_folder_prod: Path = Path("./state")
    _state_folder_dev: Path = Path("./state_dev")

    def __init__(
        self,
        name: str,
        bot: AsyncTeleBot,
        client: AsyncClient,
        translator: Translator,
        module_config: dict,
        global_config: dict,
        logger: Logger,
        is_module_enabled_for_chat_callback: Callable[[int], bool],
        dev: bool,
    ):
        """
        Args:
            bot: Telegram bot instance.
            client: g4f client instance for LLM interactions.
            translator: Service for handling text translations.
            module_config: Configuration specific to this module.
            global_config: The entire loaded application configuration.
            logger: Application logger instance.
            save_state_callback: Callback to persist module-specific state.
            is_module_enabled_for_chat_callback: Callback to check if the module is enabled for a given chat.
        """
        self.name = name
        self.bot = bot
        self.client = client
        self.translator = translator
        self.module_config = module_config
        self.global_config = global_config
        self.logger = logger.get_child(self.__class__.__name__)
        self.is_enabled_for_chat = is_module_enabled_for_chat_callback
        self.state_folder = self._state_folder_dev if dev else self._state_folder_prod
        os.makedirs(self.state_folder, exist_ok=True)

        self._base_text_model = self.global_config.get("llm_settings", {}).get(
            "base_text_model", "qwen-3-32b"
        )
        self._base_image_model = self.global_config.get("llm_settings", {}).get(
            "base_image_model", "flux"
        )

    def _sign_response(self, response: str) -> str:
        return f"{response}\n\n#{self.name}"

    async def _translate_response(
        self,
        response: str,
        utility: bool = False,
        target_lang: Optional[str] = None,
    ) -> str:
        if target_lang is not None and (not utility or self.translator.translate_utility):
            return await self.translator.translate(response, target_lang)
        return response

    async def sign_reply(
        self,
        message: Message,
        response: str,
        utility: bool = False,
        target_lang: Optional[str] = None,
        **kwargs,
    ):
        response = await self._translate_response(response, utility, target_lang)

        if "parse_mode" not in kwargs:
            kwargs["parse_mode"] = "Markdown"

        await self.bot.reply_to(message, self._sign_response(response), **kwargs)

    async def sign_send_message(
        self,
        chat_id: int,
        response: str,
        utility: bool = False,
        target_lang: Optional[str] = None,
        **kwargs,
    ):
        response = await self._translate_response(response, utility, target_lang)

        if "parse_mode" not in kwargs:
            kwargs["parse_mode"] = "Markdown"
        await self.bot.send_message(chat_id, self._sign_response(response), **kwargs)

    async def sign_send_photo(
        self,
        chat_id: int,
        image_url: str,
        caption: Optional[str] = None,
        **kwargs,
    ):
        if "parse_mode" not in kwargs:
            kwargs["parse_mode"] = "Markdown"
        await self.bot.send_photo(
            chat_id,
            image_url,
            caption=self._sign_response(caption) if caption else None,
            **kwargs,
        )

    def get_commands(self) -> list[dict[str, Any]]:
        """
        Returns a list of commands provided by this module.
        Each command is a dict: {'command': 'name', 'description': 'desc', 'admin_only': False}
        """
        return []

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
