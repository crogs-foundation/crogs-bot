# main.py

import argparse
import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone
from functools import partial

import yaml
from dotenv import load_dotenv
from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot
from telebot.types import ChatMemberUpdated

from src.bot_modules.base import BotModule
from src.bot_modules.holibot import HoliBotModule
from src.bot_modules.imagebot import ImageGeneratorModule
from src.bot_modules.jokebot import JokeGeneratorModule
from src.bot_modules.newsbot import NewsBotModule
from src.logger import Logger
from src.settings_manager import SettingsManager  # <-- IMPORT
from src.translator import Translator

# --- Load environment variables, Argument parsing, Logger ---
load_dotenv()
parser = argparse.ArgumentParser(description="Telegram Holiday Bot")
parser.add_argument("--mode", type=str, choices=["dev", "prod"], default="prod")
args = parser.parse_args()
DEV_MODE = args.mode == "dev"
logger = Logger(__name__, level="DEBUG" if DEV_MODE else "INFO")
logger.info(f"Application starting in '{args.mode}' mode.")

# --- Bot and client initialization ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in .env file!")
bot = AsyncTeleBot(TELEGRAM_BOT_TOKEN)
client = AsyncClient()
CONFIG_FILE = "config.yaml"
config: dict = {}
active_bot_modules: list[BotModule] = []
translator: Translator = None


# --- Config management ---
def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            loaded_config = yaml.safe_load(f)
            if "chat_module_settings" not in loaded_config:
                loaded_config["chat_module_settings"] = {}
            return loaded_config
    except FileNotFoundError:
        logger.warning(f"{CONFIG_FILE} not found. Please create it with defaults.")
        raise


def save_config_file():
    """Saves the global config dictionary to the YAML file."""
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, sort_keys=False, indent=2)
    logger.debug("Configuration saved to disk.")


def save_chat_ids(chat_ids: list[int]):
    config["telegram"]["chat_ids"] = chat_ids
    save_config_file()


# --- Bot module instantiation ---
def instantiate_bot_modules():
    global active_bot_modules
    for module in active_bot_modules:
        getattr(module, "close", lambda: None)()
    active_bot_modules.clear()
    module_classes = {
        "holibot": HoliBotModule,
        "jokebot": JokeGeneratorModule,
        "imagebot": ImageGeneratorModule,
        "newsbot": NewsBotModule,
    }
    for name, part_cfg in config.get("parts", {}).items():
        if not part_cfg.get("enabled"):
            logger.info(f"Module '{name}' disabled globally. Skipping.")
            continue
        module_cls = module_classes.get(name)
        if not module_cls:
            logger.warning(f"Unknown module name '{name}'. Skipping.")
            continue
        try:
            instance = module_cls(
                bot=bot,
                client=client,
                translator=translator,
                module_config=part_cfg,
                global_config=config,
                logger=logger,
                # Pass a simple helper for the callback, not the whole manager
                is_module_enabled_for_chat_callback=partial(
                    is_module_enabled_for_chat_helper, module_name=name
                ),
                save_state_callback=None,  # State is now saved directly in config
            )
            instance.register_handlers()
            active_bot_modules.append(instance)
            logger.info(f"Module '{name}' loaded.")
        except Exception as e:
            logger.error(f"Failed to load module '{name}': {e}")


# --- Helper, now outside the manager for module use ---
def is_module_enabled_for_chat_helper(chat_id: int, module_name: str) -> bool:
    module_global_config = config.get("parts", {}).get(module_name, {})
    if not module_global_config.get("enabled", False):
        return False
    chat_settings = config.get("chat_module_settings", {}).get(str(chat_id), {})
    if module_name in chat_settings:
        return chat_settings[module_name]
    return module_global_config.get("default_enabled_on_join", True)


async def trigger_modules(target_chat_ids=None):
    if not active_bot_modules:
        return False
    for module in active_bot_modules:
        asyncio.create_task(module.run_scheduled_job(target_chat_ids=target_chat_ids))
        await asyncio.sleep(0.1)
    return True


# --- Basic Bot handlers ---
@bot.message_handler(commands=["start", "help"])
async def handle_start(message):
    await bot.reply_to(
        message, "Hello! I am a modular bot. Admins can use /settings to configure me."
    )


# --- THIS IS THE FIX ---
# Restore decorators for simple command handlers in main.py
@bot.message_handler(commands=["postnow"])
async def handle_postnow(message):
    user_id = message.from_user.id
    if user_id not in config.get("telegram", {}).get("admin_ids", []):
        await bot.reply_to(message, "You are not authorized.")
        return

    if not await trigger_modules():
        await bot.reply_to(message, "No active modules to post.")
        return
    await bot.reply_to(message, "Triggered all modules to post.")


@bot.message_handler(commands=["posttome"])
async def handle_posttome(message):
    user_id = message.from_user.id
    if user_id not in config.get("telegram", {}).get("admin_ids", []):
        await bot.reply_to(message, "You are not authorized.")
        return

    if not await trigger_modules(target_chat_ids=[message.chat.id]):
        await bot.reply_to(message, "No active modules to post.")
        return
    await bot.reply_to(message, "Triggered modules to post to this chat.")


# --------------------


@bot.my_chat_member_handler()
async def handle_chat_update(message: ChatMemberUpdated):
    chat_id = message.chat.id
    chat_ids = config["telegram"].get("chat_ids", [])
    if (
        message.new_chat_member.status in ["member", "administrator"]
        and chat_id not in chat_ids
    ):
        chat_ids.append(chat_id)
        save_chat_ids(chat_ids)
        await bot.send_message(chat_id, "Hello! I can now post in this chat.")
        logger.info(f"Bot added to new group: {chat_id}")
    elif message.new_chat_member.status in ["kicked", "left"] and chat_id in chat_ids:
        chat_ids.remove(chat_id)
        save_chat_ids(chat_ids)
        config.get("chat_module_settings", {}).pop(str(chat_id), None)
        save_config_file()
        logger.info(f"Bot removed from group: {chat_id}. Cleared its specific settings.")


# --- Background tasks & Main execution ---
# (This part remains the same, no changes needed)
async def background_scheduler():
    logger.info("Scheduler: Starting background scheduler for modules.")
    while True:
        now = datetime.now(timezone.utc)
        next_sleep_duration_seconds = timedelta(days=2).total_seconds()
        tasks_to_run = []
        if not active_bot_modules:
            await asyncio.sleep(60)
            continue
        for module in active_bot_modules:
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
    logger.info("Reloading config and modules...")
    global config
    config = load_config()
    instantiate_bot_modules()
    logger.info("Config and modules reloaded.")


async def main():
    global config, translator
    config = load_config()
    translator = Translator(logger=logger)
    await translator.check_api()
    instantiate_bot_modules()

    # --- SETUP SETTINGS MANAGER ---
    settings_manager = SettingsManager(
        bot=bot,
        config_ref=config,
        logger=logger,
        save_callback=save_config_file,
        reload_callback=reload_config_and_modules,
    )
    settings_manager.register_handlers()
    # ----------------------------

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    # Use a list of tasks to run with gather

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
