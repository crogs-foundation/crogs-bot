from typing import Callable, Optional

from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from src.logger import Logger


class SettingsManager:
    """
    Manages all bot settings through a Telegram inline keyboard interface.
    """

    def __init__(
        self,
        bot: AsyncTeleBot,
        config_ref: dict,
        logger: Logger,
        save_callback: Callable[[dict], None],
        reload_callback,
    ):
        self.bot = bot
        self.config = config_ref
        self.logger = logger.get_child("SettingsManager")
        self.save_config = save_callback
        self.reload_config_and_modules = reload_callback
        self._pending_param_inputs = {}

    def register_handlers(self):
        """Registers all command and callback handlers for settings."""
        self.bot.register_message_handler(
            self._handle_settings_command, commands=["settings"]
        )
        self.bot.register_message_handler(
            self._handle_language_command, commands=["language"]
        )

        self.bot.register_callback_query_handler(
            self._handle_callback_query,
            func=lambda call: call.data.startswith(("settings_", "lang_")),
        )

        self.bot.register_message_handler(
            self._handle_pending_input, content_types=["text"]
        )

        self.logger.info("Settings handlers registered.")

    async def _handle_pending_input(self, message: Message):
        """A generic message handler that checks if we are waiting for input from a user."""
        if message.from_user is None:
            return
        user_key = (message.chat.id, message.from_user.id)

        if message.text == "/cancel" and user_key in self._pending_param_inputs:
            self._pending_param_inputs.pop(user_key)
            await self.bot.send_message(message.chat.id, "Operation cancelled.")
            context = self._pending_param_inputs.pop(user_key, None)
            if context:
                new_text, new_markup = await self._generate_module_params_menu(
                    context["module_name"]
                )
                await self.bot.send_message(
                    message.chat.id,
                    new_text,
                    reply_markup=new_markup,
                    parse_mode="Markdown",
                )
            return

        if user_key in self._pending_param_inputs:
            context = self._pending_param_inputs.pop(user_key)
            await self._process_new_param_value(message, **context)

    async def _handle_settings_command(self, message: Message):
        if not self._is_admin(message.from_user):
            await self.bot.reply_to(message, "You are not authorized.")
            return

        is_private = message.chat.type == "private"
        markup = await self._generate_main_menu(message.chat.id, is_private)
        text = "Bot Settings (Admin Panel):"
        if not is_private:
            text += "\n\nFor global & module settings, please message me directly."
        await self.bot.send_message(
            message.chat.id, text, reply_markup=markup, parse_mode="Markdown"
        )

    async def _handle_language_command(self, message: Message):
        if message.chat.type == "private":
            await self.bot.reply_to(
                message, "This command can only be used in a group chat."
            )
            return
        text, markup = await self._generate_language_menu(
            message.chat.id, from_admin_menu=False
        )
        await self.bot.send_message(message.chat.id, text, reply_markup=markup)

    # --- Main Callback Router ---
    async def _handle_callback_query(self, call: CallbackQuery):
        if call.data is None or call.data.startswith("lang_set"):
            await self._process_public_language_set(call)
            return

        if not self._is_admin(call.from_user):
            await self.bot.answer_callback_query(
                call.id, "You are not authorized.", show_alert=True
            )
            return

        try:
            await self._process_admin_callback(call)
        except Exception as e:
            if "message is not modified" not in str(e):
                self.logger.error(f"Error in settings callback: {e}")
            await self.bot.answer_callback_query(
                call.id, "An error occurred or the menu is unchanged."
            )

    # --- Callback Processors ---
    async def _process_public_language_set(self, call: CallbackQuery):
        if call.data is None:
            return
        parts = call.data.split(":")
        target_chat_id, lang_code = int(parts[1]), parts[2]
        if call.message.chat.id != target_chat_id:
            await self.bot.answer_callback_query(
                call.id, "Invalid request.", show_alert=True
            )
            return
        self._get_or_create_chat_settings(target_chat_id)["language"] = lang_code
        self.save_config(self.config)
        await self.bot.answer_callback_query(
            call.id, f"Language for this chat set to {lang_code}."
        )
        await self.bot.delete_message(call.message.chat.id, call.message.message_id)

    async def _process_admin_callback(self, call: CallbackQuery):
        if call.data is None:
            return
        parts = call.data.split(":")
        action = parts[0]
        is_private = call.message.chat.type == "private"
        new_text, new_markup = "Bot Settings (Admin Panel):", None

        # Determine new menu state
        if action == "settings_main":
            new_markup = await self._generate_main_menu(call.message.chat.id, is_private)
        elif action == "settings_select_chat":
            page = int(parts[1])
            new_text, new_markup = await self._generate_chat_selection_menu(page)
        elif action == "settings_show_chat":
            target_chat_id = int(parts[1])
            new_text, new_markup = await self._generate_chat_config_menu(
                target_chat_id, from_pm=is_private
            )
        elif action == "settings_toggle_module":
            target_chat_id, module_name = int(parts[1]), parts[2]
            current_status = self._is_module_enabled_for_chat(target_chat_id, module_name)
            self._get_or_create_chat_settings(target_chat_id)[
                module_name
            ] = not current_status
            self.save_config(self.config)
            await self.bot.answer_callback_query(
                call.id,
                f"'{module_name}' is now {'enabled' if not current_status else 'disabled'}.",
            )
            new_text, new_markup = await self._generate_chat_config_menu(
                target_chat_id, from_pm=is_private
            )
        elif action == "settings_select_lang":
            target_chat_id = int(parts[1])
            new_text, new_markup = await self._generate_language_menu(
                target_chat_id, from_admin_menu=True
            )
        elif action == "settings_set_lang":
            target_chat_id, lang_code = int(parts[1]), parts[2]
            self._get_or_create_chat_settings(target_chat_id)["language"] = lang_code
            self.save_config(self.config)
            await self.bot.answer_callback_query(call.id, f"Language set to {lang_code}.")
            new_text, new_markup = await self._generate_chat_config_menu(
                target_chat_id, from_pm=is_private
            )
        elif action == "settings_global":
            new_text, new_markup = await self._generate_global_settings_menu()
        elif action == "settings_select_translation_strategy":
            new_text, new_markup = await self._generate_translation_strategy_menu()
        elif action == "settings_set_translation_strategy":
            strategy = parts[1]
            self.config["translation"]["strategy"] = strategy
            self.save_config(self.config)
            await self.bot.answer_callback_query(
                call.id, f"Translation strategy set to '{strategy}'."
            )
            new_text, new_markup = await self._generate_global_settings_menu()
        elif action == "settings_module_menu":
            new_text, new_markup = await self._generate_module_selection_menu()
        elif action == "settings_module_params":
            module_name, page = parts[1], int(parts[2])
            new_text, new_markup = await self._generate_module_params_menu(
                module_name, page
            )
        elif action == "settings_module_edit_idx":
            _, module_name, index_str = parts
            index = int(index_str)
            params_dict = self.config.get("parts", {}).get(module_name, {})
            flat_params = self._flatten_dict(params_dict)
            if index < len(flat_params):
                key_path, _ = flat_params[index]
                await self._prompt_for_new_param_value(call, module_name, key_path)
            else:
                await self.bot.answer_callback_query(
                    call.id, "Error: Stale config, please go back.", show_alert=True
                )
            return
        elif action == "settings_reload":
            await self.bot.answer_callback_query(
                call.id, "Configuration reloaded from disk.", show_alert=True
            )
            self.reload_config_and_modules()
            new_markup = await self._generate_main_menu(call.message.chat.id, is_private)

        # Update the message if content has changed
        old_markup_json = (
            call.message.reply_markup.to_json() if call.message.reply_markup else None
        )
        if new_markup and (
            new_text != call.message.text or new_markup.to_json() != old_markup_json
        ):
            await self.bot.edit_message_text(
                new_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=new_markup,
                parse_mode="Markdown",
            )
        else:
            await self.bot.answer_callback_query(call.id)

    async def _prompt_for_new_param_value(
        self, call: CallbackQuery, module_name: str, key_path: str
    ):
        prompt_text = (
            f"Current value for `{key_path}` is:\n"
            f"`{self._get_nested_key(self.config, f'parts.{module_name}.{key_path}')}`\n\n"
            "Please send the new value.\n"
            "Send `/cancel` to abort."
        )
        await self.bot.edit_message_text(
            prompt_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None,
            parse_mode="Markdown",
        )
        user_key = (call.message.chat.id, call.from_user.id)
        self._pending_param_inputs[user_key] = {
            "module_name": module_name,
            "key_path": key_path,
        }
        await self.bot.answer_callback_query(call.id)

    async def _process_new_param_value(
        self, message: Message, module_name: str, key_path: str
    ):
        new_value_str = message.text
        if new_value_str is None:
            return
        new_value = None
        if new_value_str.lower() in ["true", "false"]:
            new_value = new_value_str.lower() == "true"
        elif len(new_value_str) > 8:  # Heuristic to skip big numerics
            new_value = new_value_str

        elif new_value_str.isdigit():
            new_value = int(new_value_str)
        else:
            try:
                new_value = float(new_value_str)
            except ValueError:
                new_value = new_value_str

        self._set_nested_key(self.config, f"parts.{module_name}.{key_path}", new_value)
        self.save_config(self.config)
        await self.bot.send_message(
            message.chat.id, f"✅ Successfully set `{key_path}` to `{new_value}`."
        )
        self.reload_config_and_modules()
        new_text, new_markup = await self._generate_module_params_menu(module_name)
        await self.bot.send_message(
            message.chat.id, new_text, reply_markup=new_markup, parse_mode="Markdown"
        )

    # --- Menu Generators ---
    async def _generate_main_menu(self, chat_id, is_private):
        markup = InlineKeyboardMarkup(row_width=1)
        if is_private:
            markup.add(
                InlineKeyboardButton(
                    "🌐 Configure a Chat", callback_data="settings_select_chat:0"
                ),
                InlineKeyboardButton(
                    "🛠️ Global Settings", callback_data="settings_global"
                ),
                InlineKeyboardButton(
                    "🔄 Reload Config from Disk", callback_data="settings_reload"
                ),
            )
        else:
            markup.add(
                InlineKeyboardButton(
                    "⚙️ Configure This Chat", callback_data=f"settings_show_chat:{chat_id}"
                )
            )
        return markup

    async def _generate_chat_selection_menu(self, page=0):
        markup = InlineKeyboardMarkup(row_width=1)
        chat_ids = self.config["telegram"].get("chat_ids", [])
        _items_per_page, start, end = 5, page * 5, (page + 1) * 5
        buttons = []
        for chat_id in chat_ids[start:end]:
            try:
                chat = await self.bot.get_chat(chat_id)
                title = chat.title or chat.username or f"ID: {chat_id}"
                buttons.append(
                    InlineKeyboardButton(
                        title, callback_data=f"settings_show_chat:{chat_id}"
                    )
                )
            except Exception:
                buttons.append(
                    InlineKeyboardButton(
                        f"Unknown: {chat_id}",
                        callback_data=f"settings_show_chat:{chat_id}",
                    )
                )
        markup.add(*buttons)
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "<<", callback_data=f"settings_select_chat:{page - 1}"
                )
            )
        if end < len(chat_ids):
            nav.append(
                InlineKeyboardButton(
                    ">>", callback_data=f"settings_select_chat:{page + 1}"
                )
            )
        if nav:
            markup.row(*nav)
        markup.add(InlineKeyboardButton("🔙 Main Menu", callback_data="settings_main"))
        return "Select a chat to configure:", markup

    async def _generate_chat_config_menu(self, chat_id, from_pm):
        markup = InlineKeyboardMarkup(row_width=1)
        for name in self.config.get("parts", {}):
            enabled = self._is_module_enabled_for_chat(chat_id, name)
            icon = "✅" if enabled else "❌"
            markup.add(
                InlineKeyboardButton(
                    f"{icon} {name.capitalize()}",
                    callback_data=f"settings_toggle_module:{chat_id}:{name}",
                )
            )
        lang = self._get_language_for_chat(chat_id)
        markup.add(
            InlineKeyboardButton(
                f"🌎 Language: {lang}", callback_data=f"settings_select_lang:{chat_id}"
            )
        )
        back_cb = "settings_select_chat:0" if from_pm else "settings_main"
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=back_cb))
        try:
            chat = await self.bot.get_chat(chat_id)
            title = chat.title or chat.username
            text = f"Settings for: *{title}*"
        except Exception:
            text = f"Settings for ID: `{chat_id}`"
        return text, markup

    async def _generate_language_menu(self, chat_id, from_admin_menu):
        markup = InlineKeyboardMarkup(row_width=3)
        languages = {"en": "English", "ru": "Русский", "zh-cn": "中文 (简体)"}
        cb = "settings_set_lang" if from_admin_menu else "lang_set"
        buttons = [
            InlineKeyboardButton(name, callback_data=f"{cb}:{chat_id}:{code}")
            for code, name in languages.items()
        ]
        markup.add(*buttons)
        if from_admin_menu:
            markup.add(
                InlineKeyboardButton(
                    "🔙 Back", callback_data=f"settings_show_chat:{chat_id}"
                )
            )
        return "Select a language:", markup

    async def _generate_global_settings_menu(self):
        markup = InlineKeyboardMarkup(row_width=1)
        strategy = self.config.get("translation", {}).get("strategy", "N/A")
        markup.add(
            InlineKeyboardButton(
                f"Translation Strategy: {strategy.capitalize()}",
                callback_data="settings_select_translation_strategy",
            ),
            InlineKeyboardButton(
                "🔧 Configure Modules", callback_data="settings_module_menu"
            ),
        )
        markup.add(InlineKeyboardButton("🔙 Main Menu", callback_data="settings_main"))
        return "Global Bot Settings:", markup

    async def _generate_translation_strategy_menu(self):
        markup = InlineKeyboardMarkup(row_width=1)
        strategies = ["prompt", "response"]
        current = self.config.get("translation", {}).get("strategy")
        buttons = [
            InlineKeyboardButton(
                f"{'>> ' if s == current else ''}{s.capitalize()}",
                callback_data=f"settings_set_translation_strategy:{s}",
            )
            for s in strategies
        ]
        markup.add(*buttons)
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="settings_global"))
        return "Select translation strategy:", markup

    async def _generate_module_selection_menu(self):
        markup = InlineKeyboardMarkup(row_width=1)
        buttons = [
            InlineKeyboardButton(
                name.capitalize(), callback_data=f"settings_module_params:{name}:0"
            )
            for name in self.config.get("parts", {})
        ]
        markup.add(*buttons)
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="settings_global"))
        return "Select a module to configure:", markup

    async def _generate_module_params_menu(self, module_name, page=0):
        markup = InlineKeyboardMarkup(row_width=1)
        params_dict = self.config.get("parts", {}).get(module_name, {})
        flat_params = self._flatten_dict(params_dict)

        _items_per_page, start, end = 5, page * 5, (page + 1) * 5
        buttons = []
        # Enumerate the full list to get an absolute index for each param
        for i, (key, value) in enumerate(flat_params):
            if start <= i < end:  # Only display items for the current page
                # Use the absolute index 'i' in the callback data
                callback_data = f"settings_module_edit_idx:{module_name}:{i}"

                # Truncate long values for display purposes
                display_value = str(value).replace("\n", " ")
                if len(display_value) > 30:
                    display_value = display_value[:27] + "..."

                buttons.append(
                    InlineKeyboardButton(
                        f"{key}: {display_value}", callback_data=callback_data
                    )
                )
        markup.add(*buttons)

        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "<<", callback_data=f"settings_module_params:{module_name}:{page - 1}"
                )
            )
        if end < len(flat_params):
            nav.append(
                InlineKeyboardButton(
                    ">>", callback_data=f"settings_module_params:{module_name}:{page + 1}"
                )
            )
        if nav:
            markup.row(*nav)

        markup.add(
            InlineKeyboardButton("🔙 Module List", callback_data="settings_module_menu")
        )
        return f"Parameters for *{module_name.capitalize()}*:", markup

    # --- Helper Methods ---
    def _is_admin(self, user: Optional[User]):
        return user is not None and str(user.id) in self.config.get("telegram", {}).get(
            "admin_ids", []
        )

    def _get_or_create_chat_settings(self, chat_id):
        chat_id_str = str(chat_id)
        if chat_id_str not in self.config["chat_module_settings"]:
            self.config["chat_module_settings"][chat_id_str] = {}
        return self.config["chat_module_settings"][chat_id_str]

    def _get_language_for_chat(self, chat_id):
        return self._get_or_create_chat_settings(chat_id).get("language", "en")

    def _is_module_enabled_for_chat(self, chat_id, module_name):
        module_global_config = self.config.get("parts", {}).get(module_name, {})
        if not module_global_config.get("enabled", False):
            return False
        chat_settings = self.config.get("chat_module_settings", {}).get(str(chat_id), {})
        if module_name in chat_settings:
            return chat_settings[module_name]
        return module_global_config.get("default_enabled_on_join", True)

    def _flatten_dict(self, d, parent_key="", sep="."):
        items = []
        for k, v in d.items():
            new_key = parent_key + sep + k if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep=sep))
            else:
                items.append((new_key, v))
        return items

    def _get_nested_key(self, d, key_path):
        keys = key_path.split(".")
        for key in keys:
            d = d[key]
        return d

    def _set_nested_key(self, d, key_path, value):
        keys = key_path.split(".")
        for key in keys[:-1]:
            d = d.setdefault(key, {})
        d[keys[-1]] = value
