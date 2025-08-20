# src/settings_manager.py

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

# --- Configuration for what top-level keys are editable in "Global Settings" ---
EDITABLE_GLOBAL_SECTIONS = ["translation", "llm_settings"]


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
        if message.from_user is None or not message.text:
            return
        user_key = (message.chat.id, message.from_user.id)

        # --- NEW: Handle /cancel command during input ---
        if message.text.lower() == "/cancel" and user_key in self._pending_param_inputs:
            context = self._pending_param_inputs.pop(user_key)
            await self.bot.send_message(message.chat.id, "Operation cancelled.")

            # Restore the previous menu
            if context.get("section") == "parts":
                new_text, new_markup = await self._generate_module_params_menu(
                    context["key_path"].split(".")[0]
                )
            else:
                new_text, new_markup = await self._generate_global_params_menu(
                    context["section"]
                )

            await self.bot.send_message(
                message.chat.id, new_text, reply_markup=new_markup, parse_mode="Markdown"
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

    async def _handle_callback_query(self, call: CallbackQuery):
        if call.data and call.data.startswith("lang_set"):
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

    async def _process_public_language_set(self, call: CallbackQuery):
        if not call.data:
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
        await self.bot.answer_callback_query(call.id, f"Language set to {lang_code}.")
        await self.bot.delete_message(call.message.chat.id, call.message.message_id)

    async def _process_admin_callback(self, call: CallbackQuery):
        if not call.data:
            return
        parts = call.data.split(":")
        action = parts[0]
        is_private = call.message.chat.type == "private"
        new_text, new_markup = "Bot Settings (Admin Panel):", None

        # Determine new menu state
        if action == "settings_main":
            new_markup = await self._generate_main_menu(call.message.chat.id, is_private)
        elif action == "settings_select_chat":
            new_text, new_markup = await self._generate_chat_selection_menu(int(parts[1]))
        elif action == "settings_show_chat":
            new_text, new_markup = await self._generate_chat_config_menu(
                int(parts[1]), from_pm=is_private
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
            new_text, new_markup = await self._generate_language_menu(
                int(parts[1]), from_admin_menu=True
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
        elif action == "settings_module_menu":
            new_text, new_markup = await self._generate_module_selection_menu()
        elif action == "settings_module_params":
            new_text, new_markup = await self._generate_module_params_menu(
                parts[1], int(parts[2])
            )
        elif action == "settings_module_edit_idx":
            _, module_name, index_str = parts
            params = self._flatten_dict(self.config.get("parts", {}).get(module_name, {}))
            key_path, _ = params[int(index_str)]
            full_key_path = f"parts.{module_name}.{key_path}"
            await self._prompt_for_new_param_value(call, "parts", full_key_path)
            return
        elif action == "settings_reload":
            await self.bot.answer_callback_query(
                call.id, "Configuration reloaded.", show_alert=True
            )
            self.reload_config_and_modules()
            new_markup = await self._generate_main_menu(call.message.chat.id, is_private)
        # --- NEW: Dynamic Global Settings Handlers ---
        elif action == "settings_global_section":
            new_text, new_markup = await self._generate_global_params_menu(parts[1])
        elif action == "settings_global_params":
            new_text, new_markup = await self._generate_global_params_menu(
                parts[1], int(parts[2])
            )
        elif action == "settings_global_edit_idx":
            _, section, index_str = parts
            params = self._flatten_dict(self.config.get(section, {}))
            key_path, _ = params[int(index_str)]
            full_key_path = f"{section}.{key_path}"
            await self._prompt_for_new_param_value(call, section, full_key_path)
            return

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
        self, call: CallbackQuery, section: str, full_key_path: str
    ):
        prompt_text = (
            f"Editing parameter:\n`{full_key_path}`\n\n"
            f"Current value:\n`{self._get_nested_key(self.config, full_key_path)}`\n\n"
            "Please send the new value, or send `/cancel` to abort."
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
            "section": section,
            "key_path": full_key_path,
        }
        await self.bot.answer_callback_query(call.id)

    async def _process_new_param_value(
        self, message: Message, section: str, key_path: str
    ):
        if not message.text:
            return
        new_value_str = message.text

        # Improved type conversion
        new_value: object
        if new_value_str.lower() == "true":
            new_value = True
        elif new_value_str.lower() == "false":
            new_value = False
        elif new_value_str.isdigit():
            new_value = int(new_value_str)
        else:
            try:
                new_value = float(new_value_str)
            except ValueError:
                new_value = new_value_str

        self._set_nested_key(self.config, key_path, new_value)
        self.save_config(self.config)
        await self.bot.send_message(
            message.chat.id, f"✅ Set `{key_path}` to `{new_value}`."
        )
        self.reload_config_and_modules()

        # Restore the correct menu
        if section == "parts":
            module_name = key_path.split(".")[1]
            new_text, new_markup = await self._generate_module_params_menu(module_name)
        else:
            new_text, new_markup = await self._generate_global_params_menu(section)
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
        start, end = page * 5, (page + 1) * 5
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

    # --- MODIFIED: Global settings menu is now dynamic ---
    async def _generate_global_settings_menu(self):
        markup = InlineKeyboardMarkup(row_width=1)
        buttons = [
            InlineKeyboardButton(
                f"🔧 {section.replace('_', ' ').capitalize()}",
                callback_data=f"settings_global_section:{section}",
            )
            for section in EDITABLE_GLOBAL_SECTIONS
        ]
        markup.add(*buttons)
        markup.add(
            InlineKeyboardButton(
                "⚙️ Configure Modules", callback_data="settings_module_menu"
            )
        )
        markup.add(InlineKeyboardButton("🔙 Main Menu", callback_data="settings_main"))
        return "Global Bot Settings:", markup

    # --- NEW: Menu for browsing a specific global section's parameters ---
    async def _generate_global_params_menu(self, section_name, page=0):
        markup = InlineKeyboardMarkup(row_width=1)
        params_dict = self.config.get(section_name, {})
        flat_params = self._flatten_dict(params_dict)
        start, end = page * 5, (page + 1) * 5
        buttons = []
        for i, (key, value) in enumerate(flat_params):
            if start <= i < end:
                display_value = str(value).replace("\n", " ")
                if len(display_value) > 30:
                    display_value = display_value[:27] + "..."
                buttons.append(
                    InlineKeyboardButton(
                        f"{key}: {display_value}",
                        callback_data=f"settings_global_edit_idx:{section_name}:{i}",
                    )
                )
        markup.add(*buttons)
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "<<",
                    callback_data=f"settings_global_params:{section_name}:{page - 1}",
                )
            )
        if end < len(flat_params):
            nav.append(
                InlineKeyboardButton(
                    ">>",
                    callback_data=f"settings_global_params:{section_name}:{page + 1}",
                )
            )
        if nav:
            markup.row(*nav)
        markup.add(
            InlineKeyboardButton("🔙 Global Settings", callback_data="settings_global")
        )
        return f"Parameters for *{section_name.replace('_', ' ').capitalize()}*:", markup

    async def _generate_module_selection_menu(self):
        markup = InlineKeyboardMarkup(row_width=1)
        buttons = [
            InlineKeyboardButton(
                name.capitalize(), callback_data=f"settings_module_params:{name}:0"
            )
            for name in self.config.get("parts", {})
        ]
        markup.add(*buttons)
        markup.add(
            InlineKeyboardButton("🔙 Global Settings", callback_data="settings_global")
        )
        return "Select a module to configure:", markup

    async def _generate_module_params_menu(self, module_name, page=0):
        markup = InlineKeyboardMarkup(row_width=1)
        params_dict = self.config.get("parts", {}).get(module_name, {})
        flat_params = self._flatten_dict(params_dict)
        start, end = page * 5, (page + 1) * 5
        buttons = []
        for i, (key, value) in enumerate(flat_params):
            if start <= i < end:
                display_value = str(value).replace("\n", " ")
                if len(display_value) > 30:
                    display_value = display_value[:27] + "..."
                buttons.append(
                    InlineKeyboardButton(
                        f"{key}: {display_value}",
                        callback_data=f"settings_module_edit_idx:{module_name}:{i}",
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
        # --- IMPROVED: Handles None user and compares strings to strings ---
        return user is not None and str(user.id) in [
            str(admin_id)
            for admin_id in self.config.get("telegram", {}).get("admin_ids", [])
        ]

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
