import argparse
import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone
from functools import partial

import uvicorn
import yaml
from dotenv import load_dotenv
from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from src.bot_modules.base import BotModule
from src.bot_modules.holibot import HoliBotModule
from src.bot_modules.jokebot import JokeGeneratorModule
from src.logger import Logger
from src.web_api import app, main_app_instance

# --- Load environment variables ---
load_dotenv()

# --- Argument parsing ---
parser = argparse.ArgumentParser(description="Telegram Holiday Bot")
parser.add_argument("--mode", type=str, choices=["dev", "prod"], default="prod")
args = parser.parse_args()
DEV_MODE = args.mode == "dev"

# --- Logger ---
logger = Logger(
    __name__,
    level="DEBUG" if DEV_MODE else "INFO",
    msg_format="{asctime} - {levelname} - {message}",
)
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

# --- Config management ---


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            loaded_config = yaml.safe_load(f)
            # Ensure the new settings key exists to prevent KeyErrors
            if "chat_module_settings" not in loaded_config:
                loaded_config["chat_module_settings"] = {}
            return loaded_config
    except FileNotFoundError:
        logger.warning(f"{CONFIG_FILE} not found. Please create it with defaults.")
        raise


def save_config(updated_config: dict):
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(updated_config, f, sort_keys=False, indent=2)


def save_chat_ids(chat_ids: list[int]):
    config["telegram"]["chat_ids"] = chat_ids
    save_config(config)


def save_module_state(module_name: str, key: str, value: str):
    try:
        if module_name in config.get("parts", {}):
            config["parts"][module_name][key] = value
        disk_config = load_config()
        if module_name in disk_config.get("parts", {}):
            disk_config["parts"][module_name][key] = value
        save_config(disk_config)
        logger.debug(f"Saved state for {module_name}: {key} = {value}")
    except Exception as e:
        logger.error(f"Failed to save state for {module_name}: {e}")


# --- Bot module instantiation ---


def instantiate_bot_modules():
    global active_bot_modules
    for module in active_bot_modules:
        getattr(module, "close", lambda: None)()
    active_bot_modules.clear()

    module_classes = {"holibot": HoliBotModule, "jokebot": JokeGeneratorModule}

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
                module_config=part_cfg,
                global_config=config,
                logger=logger,
                is_module_enabled_for_chat_callback=partial(
                    is_module_enabled_for_chat, module_name=name
                ),
                save_state_callback=lambda k, v, part_name=name: save_module_state(
                    part_name, k, v
                ),
            )
            instance.register_handlers()
            active_bot_modules.append(instance)
            logger.info(f"Module '{name}'  loaded.")
        except Exception as e:
            logger.error(f"Failed to load module '{name}': {e}")


# --- Helpers ---


def is_admin(user_id: int) -> bool:
    return user_id in config.get("telegram", {}).get("admin_ids", [])


def is_module_enabled_for_chat(
    chat_id: int,
    module_name: str,
) -> bool:
    """Checks if a module is enabled for a specific chat, respecting global and local settings."""
    # 1. Check global setting first. If globally disabled, it's always false.
    module_global_config = config.get("parts", {}).get(module_name, {})
    if not module_global_config.get("enabled", False):
        return False

    # 2. Check for a specific override for this chat.
    # The chat_id must be converted to a string to be used as a YAML/JSON key.
    chat_settings = config.get("chat_module_settings", {}).get(str(chat_id), {})
    if module_name in chat_settings:
        return chat_settings[module_name]

    # 3. If no specific override, the module is enabled for this chat because
    #    we already confirmed it's globally enabled in step 1.
    return True


async def trigger_modules(target_chat_ids=None):
    if not active_bot_modules:
        return False
    for module in active_bot_modules:
        asyncio.create_task(module.run_scheduled_job(target_chat_ids=target_chat_ids))
        await asyncio.sleep(0.1)
    return True


# --- Bot handlers ---


@bot.message_handler(commands=["start", "help"])
async def handle_start(message):
    await bot.reply_to(
        message, "Hello! I am a modular bot. Check /settings for more info."
    )


@bot.message_handler(commands=["settings"])
async def handle_settings(message):
    if not is_admin(message.from_user.id):
        await bot.reply_to(message, "You are not authorized.")
        return
    webapp_url = config.get("webapp", {}).get("url")
    if not webapp_url or not webapp_url.startswith("https"):
        await bot.reply_to(
            message,
            "⚠️ Web App URL not HTTPS or not configured in config.yaml.",
            parse_mode="Markdown",
        )
        return
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Settings", web_app=WebAppInfo(webapp_url))]]
    )
    await bot.send_message(
        message.chat.id, "Open the settings panel:", reply_markup=markup
    )


@bot.message_handler(commands=["postnow"])
async def handle_postnow(message):
    if not is_admin(message.from_user.id):
        await bot.reply_to(message, "You are not authorized.")
        return
    if not await trigger_modules():
        await bot.reply_to(message, "No active modules to post.")
        return
    await bot.reply_to(message, "Triggered all active modules to post.")


@bot.message_handler(commands=["posttome"])
async def handle_posttome(message):
    if not is_admin(message.from_user.id):
        await bot.reply_to(message, "You are not authorized.")
        return
    if not await trigger_modules(target_chat_ids=[message.chat.id]):
        await bot.reply_to(message, "No active modules to post.")
        return
    await bot.reply_to(
        message, f"Triggered modules to post just for you in chat {message.chat.id}."
    )


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
    elif message.new_chat_member.status == "kicked" and chat_id in chat_ids:
        chat_ids.remove(chat_id)
        save_chat_ids(chat_ids)
        # Clean up per-chat settings when the bot is removed.
        config.get("chat_module_settings", {}).pop(str(chat_id), None)
        save_config(config)
        logger.info(f"Bot removed from group: {chat_id}. Cleared its specific settings.")


# --- Background tasks ---


async def background_scheduler():
    logger.info("Scheduler: Starting background scheduler for modules.")
    while True:
        now = datetime.now(timezone.utc)
        next_sleep_duration_seconds = timedelta(days=2).total_seconds()
        tasks_to_run = []

        if not active_bot_modules:
            logger.warning("Scheduler: No active bot modules. Sleeping for 1 minute.")
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
            logger.info(f"Scheduler: Executing {len(tasks_to_run)} due tasks.")
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


async def config_reloader(interval_seconds=5):
    while True:
        try:
            if main_app_instance.reload_config_signal:
                logger.info("Reloading config...")
                global config
                config = load_config()
                instantiate_bot_modules()
                main_app_instance.reload_config_signal = False
                logger.info("Config reloaded.")
            await asyncio.sleep(interval_seconds)
        except Exception as e:
            logger.error(f"Config reloader error: {e}")
            await asyncio.sleep(interval_seconds)


# --- Main ---


async def main():
    global config
    config = load_config()
    instantiate_bot_modules()

    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    scheduler_task = asyncio.create_task(background_scheduler())
    polling_task = asyncio.create_task(polling_loop(shutdown_event))
    config_task = asyncio.create_task(config_reloader())
    web_server_task = asyncio.create_task(
        uvicorn.Server(
            uvicorn.Config(
                app, host="0.0.0.0", port=8000, log_level="info", access_log=False
            )
        ).serve()
    )

    await shutdown_event.wait()

    logger.info("Shutting down...")
    await bot.close_session()
    for task in [scheduler_task, polling_task, config_task, web_server_task]:
        task.cancel()
    await asyncio.gather(
        scheduler_task, polling_task, config_task, web_server_task, return_exceptions=True
    )
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Exited by user.")
