import argparse
import asyncio
import signal
from datetime import datetime, timedelta, timezone
from functools import partial

from dotenv import load_dotenv
from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot
from telebot.types import BotCommand, ChatMemberUpdated, Message

from src.bot_modules.base import BotModule
from src.bot_modules.holibot import HoliBotModule
from src.bot_modules.imagebot import ImageGeneratorModule
from src.bot_modules.jokebot import JokeGeneratorModule
from src.bot_modules.newsbot import NewsBotModule
from src.config_management import ConfigManager
from src.logger import Logger
from src.settings_manager import SettingsManager  # <-- IMPORT
from src.translators.base import Translator
from src.translators.google_translator import GoogleTranslator
from src.translators.llm_translator import LLMTranslator

# --- Prse arguments and setup Logger---
load_dotenv()
parser = argparse.ArgumentParser(description="Telegram Holiday Bot")
parser.add_argument("--mode", type=str, choices=["dev", "prod"], default="prod")
args = parser.parse_args()
DEV_MODE = args.mode == "dev"
logger = Logger("main", level="DEBUG" if DEV_MODE else "ERROR")
logger.info(f"Application starting in '{args.mode}' mode.")

# --- Bot and client initialization ---
CONFIG_MANAGER = ConfigManager(logger, dev=DEV_MODE)
bot = AsyncTeleBot(CONFIG_MANAGER.tg_token)
client = AsyncClient()
ACTIVE_BOT_MODULES: list[BotModule] = []


def translator_factory(
    logger_param: Logger, config: dict, client_param: AsyncClient
) -> Translator:
    """Creates a translator instance based on the application config."""
    provider = config.get("translation", {}).get("provider", "google").lower()
    logger.info(f"Initializing translator with provider: '{provider}'")

    if provider == "llm":
        return LLMTranslator(config, logger_param, client_param)
    elif provider == "google":
        return GoogleTranslator(config, logger_param)
    else:
        logger.error(
            f"Unknown translator provider '{provider}'. Defaulting to GoogleTranslator."
        )
        return GoogleTranslator(config, logger_param)


TRANSLATOR: Translator = translator_factory(logger, CONFIG_MANAGER.config, client)


# --- Bot module instantiation ---
def instantiate_bot_modules():
    for module in ACTIVE_BOT_MODULES:
        getattr(module, "close", lambda: None)()
    ACTIVE_BOT_MODULES.clear()
    module_classes = {
        "holibot": HoliBotModule,
        "jokebot": JokeGeneratorModule,
        "imagebot": ImageGeneratorModule,
        "newsbot": NewsBotModule,
    }
    for name, part_cfg in CONFIG_MANAGER.extract("parts", {}).items():
        if not part_cfg.get("enabled"):
            logger.info(f"Module '{name}' disabled globally. Skipping.")
            continue
        module_cls = module_classes.get(name)
        if not module_cls:
            logger.warning(f"Unknown module name '{name}'. Skipping.")
            continue
        try:
            instance = module_cls(
                name=name,
                bot=bot,
                client=client,
                translator=TRANSLATOR,
                module_config=part_cfg,
                global_config=CONFIG_MANAGER.config,
                logger=logger,
                is_module_enabled_for_chat_callback=partial(
                    is_module_enabled_for_chat_helper, module_name=name
                ),
                dev=DEV_MODE,
            )
            instance.register_handlers()
            ACTIVE_BOT_MODULES.append(instance)
            logger.info(f"Module '{name}' loaded.")
        except Exception as e:
            logger.error(f"Failed to load module '{name}': {e}")


def is_module_enabled_for_chat_helper(chat_id: int, module_name: str) -> bool:
    module_global_config = CONFIG_MANAGER.extract(f"parts.{module_name}", {})
    if not module_global_config.get("enabled", False):
        return False
    chat_settings = CONFIG_MANAGER.extract(f"chat_module_settings.{chat_id}", {})
    if module_name in chat_settings:
        return chat_settings[module_name]
    return module_global_config.get("default_enabled_on_join", True)


async def trigger_modules(target_chat_ids=None) -> bool:
    if not ACTIVE_BOT_MODULES:
        return False
    for module in ACTIVE_BOT_MODULES:
        asyncio.create_task(module.run_scheduled_job(target_chat_ids=target_chat_ids))
        await asyncio.sleep(0.1)
    return True


# --- Bot Handlers---
@bot.message_handler(commands=["start", "help"])
async def handle_start(message: Message):
    if message.from_user is None:
        return
    user_is_admin = str(message.from_user.id) in [
        str(aid) for aid in CONFIG_MANAGER.extract("telegram.admin_ids", [])
    ]

    # --- Build the Help String ---
    help_text = "Hello! I am a modular bot. Here are the commands you can use:\n\n"

    # 1. Global Commands
    help_text += "*Everyone*\n"
    help_text += "/help - Shows this help message.\n"
    if message.chat.type != "private":
        help_text += "/language - Change the language for this chat.\n"

    # 2. Module-Specific Commands
    for module in ACTIVE_BOT_MODULES:
        if is_module_enabled_for_chat_helper(message.chat.id, module.name):
            commands = module.get_commands()
            for cmd_info in commands:
                if not cmd_info.get("admin_only"):
                    help_text += f"/{cmd_info['command']} - {cmd_info['description']}\n"

    # 3. Admin-Only Commands
    if user_is_admin:
        help_text += "\n*Admins Only*\n"
        help_text += "/settings - Open the settings panel.\n"
        # help_text += (
        #     "/postnow - Manually trigger all active modules to post in their channels.\n"
        # )
        # help_text += "/posttome - Trigger modules to post only in this chat.\n"
        # Add admin commands from modules
        for module in ACTIVE_BOT_MODULES:
            if is_module_enabled_for_chat_helper(message.chat.id, module.name):
                commands = module.get_commands()
                for cmd_info in commands:
                    if cmd_info.get("admin_only"):
                        help_text += (
                            f"/{cmd_info['command']} - {cmd_info['description']}\n"
                        )

    await bot.send_message(message.chat.id, help_text, parse_mode="Markdown")


@bot.message_handler(commands=["postnow"])
async def handle_postnow(message):
    user_id = str(message.from_user.id)
    if user_id not in CONFIG_MANAGER.extract("telegram.admin_ids", []):
        await bot.reply_to(message, "You are not authorized.")
        return

    if not await trigger_modules():
        await bot.reply_to(message, "No active modules to post.")
        return
    await bot.reply_to(message, "Triggered all modules to post.")


@bot.message_handler(commands=["posttome"])
async def handle_posttome(message):
    user_id = str(message.from_user.id)
    if user_id not in CONFIG_MANAGER.extract("telegram.admin_ids", []):
        await bot.reply_to(message, "You are not authorized.")
        return

    if not await trigger_modules(target_chat_ids=[message.chat.id]):
        await bot.reply_to(message, "No active modules to post.")
        return
    await bot.reply_to(message, "Triggered modules to post to this chat.")


@bot.my_chat_member_handler()
async def handle_chat_update(message: ChatMemberUpdated):
    chat_id = str(message.chat.id)
    chat_ids: list[str] = CONFIG_MANAGER.extract("telegram.chat_ids", [])
    if (
        message.new_chat_member.status in ["member", "administrator"]
        and chat_id not in chat_ids
    ):
        chat_ids.append(chat_id)
        CONFIG_MANAGER.save_chat_ids(chat_ids)
        await bot.send_message(chat_id, "Hello! I can now post in this chat.")
        logger.info(f"Bot added to new group: {chat_id}")
    elif message.new_chat_member.status in ["kicked", "left"] and chat_id in chat_ids:
        chat_ids.remove(chat_id)
        CONFIG_MANAGER.save_chat_ids(chat_ids)
        CONFIG_MANAGER.config.get("chat_module_settings", {}).pop(str(chat_id), None)
        CONFIG_MANAGER.save_config_file()
        logger.info(f"Bot removed from group: {chat_id}. Cleared its specific settings.")


async def set_bot_commands(bot_param: AsyncTeleBot):
    """
    Collects all possible commands from modules and global scope
    and registers them with BotFather.
    """
    commands = [
        BotCommand("help", "Show this help message"),
        BotCommand("language", "Change chat language (groups only)"),
        # BotCommand("settings", "Access admin settings (admins only)"),
        # BotCommand("postnow", "Force all modules to post now (admins only)"),
        # BotCommand("posttome", "Force modules to post to you (admins only)"),
    ]

    # Collect commands from all defined module classes
    module_classes = [
        HoliBotModule,
        JokeGeneratorModule,
        ImageGeneratorModule,
        NewsBotModule,
    ]
    for module_cls in module_classes:
        # We don't need to fully initialize the class, just call the static method
        module_commands = module_cls.get_commands(module_cls)
        for cmd_info in module_commands:
            commands.append(BotCommand(cmd_info["command"], cmd_info["description"]))

    # Remove duplicate commands (if any)
    unique_commands = list({cmd.command: cmd for cmd in commands}.values())

    await bot_param.set_my_commands(unique_commands)
    logger.info(f"Successfully registered {len(unique_commands)} commands with Telegram.")


# --- Background tasks & Main execution ---
async def background_scheduler():
    logger.info("Scheduler: Starting background scheduler for modules.")
    while True:
        now = datetime.now(timezone.utc)
        next_sleep_duration_seconds = timedelta(days=2).total_seconds()
        tasks_to_run = []
        if not ACTIVE_BOT_MODULES:
            await asyncio.sleep(60)
            continue
        for module in ACTIVE_BOT_MODULES:
            next_event_time = module.next_scheduled_event_time
            if not next_event_time:
                continue
            if now >= next_event_time - timedelta(seconds=2):
                tasks_to_run.append(module.process_due_event())
            else:
                sleep_for_module = (next_event_time - now).total_seconds()
                if sleep_for_module > 0:
                    next_sleep_duration_seconds = min(
                        next_sleep_duration_seconds, sleep_for_module
                    )
        if tasks_to_run:
            await asyncio.gather(*tasks_to_run, return_exceptions=True)
            await asyncio.sleep(1)
            continue
        next_sleep_duration_seconds = max(next_sleep_duration_seconds, 5)
        logger.info(
            f"Scheduler: Next global check in {next_sleep_duration_seconds:.2f} seconds."
        )
        try:
            await asyncio.sleep(next_sleep_duration_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled.")
            break
        except Exception as e:
            logger.error(f"An unexpected error in background_scheduler: {e}")
            await asyncio.sleep(5)
    logger.info("Scheduler task has finished.")


async def polling_loop(shutdown_event: asyncio.Event):
    offset, timeout = 0, 10
    while not shutdown_event.is_set():
        try:
            updates = await bot.get_updates(offset=offset, timeout=timeout)
            if updates:
                offset = updates[-1].update_id + 1
                await bot.process_new_updates(updates)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)


def reload_config_and_modules():
    """Function to reload config from disk and re-instantiate modules."""
    global TRANSLATOR
    logger.info("Reloading config and modules...")
    CONFIG_MANAGER.reload()
    TRANSLATOR = translator_factory(logger, CONFIG_MANAGER.config, client)

    instantiate_bot_modules()
    logger.info("Config and modules reloaded.")


async def main():
    await TRANSLATOR.check_api()
    instantiate_bot_modules()

    settings_manager = SettingsManager(
        bot=bot,
        config_ref=CONFIG_MANAGER.config,
        logger=logger,
        save_callback=CONFIG_MANAGER.update_config,
        reload_callback=reload_config_and_modules,
    )
    settings_manager.register_handlers()
    await set_bot_commands(bot)

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    tasks = [
        background_scheduler(),
        polling_loop(shutdown_event),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Main tasks cancelled.")
    finally:
        logger.info("Shutting down...")

        await bot.close_session()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Exited by user.")
