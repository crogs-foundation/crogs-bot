import argparse
import asyncio
import os
import signal
from datetime import datetime, timedelta, timezone

import telebot
import uvicorn
import yaml
from dotenv import load_dotenv
from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot

from src.bot_modules.base import BotModule
from src.bot_modules.holibot import HoliBotModule

# Import the base module class and specific module implementations
from src.bot_modules.jokebot import JokeGeneratorModule
from src.logger import Logger
from src.web_api import app, main_app_instance

# --- Load Environment Variables FIRST ---
load_dotenv()

# --- Argument Parsing, Logging, and Initialization ---
parser = argparse.ArgumentParser(description="Telegram Holiday Bot")
parser.add_argument(
    "--mode", type=str, choices=["dev", "prod"], default="prod", help="Set the run mode."
)
args = parser.parse_args()
DEV_MODE = args.mode == "dev"
logger = Logger(
    __name__,
    level="DEBUG" if args.mode == "dev" else "INFO",
    msg_format="{asctime} - {levelname} - {message}",
)
logger.info(f"Application starting in '{args.mode}' mode.")

# Load token from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in .env file!")

CONFIG_FILE = "config.yaml"
bot = AsyncTeleBot(TELEGRAM_BOT_TOKEN)
client = AsyncClient()  # g4f client, passed to modules
config = {}  # Global config dictionary

# Global list to hold instantiated bot modules
active_bot_modules: list[BotModule] = []


# --- Config Management ---
def load_and_merge_config():
    """
    Loads user config, merges with defaults, and ensures config.yaml exists and is up-to-date.
    """
    try:
        with open(CONFIG_FILE, "r") as f:
            return yaml.safe_load(f)
    except FileNotFoundError as e:
        logger.warning(f"'{CONFIG_FILE}' not found. Creating it with default values.")
        raise e


def save_config_chats(chat_ids):
    """Saves the updated list of chat IDs to the config file."""
    # This function is specifically for global chat_ids, which is fine
    current_config = load_and_merge_config()
    current_config["telegram"]["chat_ids"] = chat_ids
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(current_config, f, sort_keys=False, indent=2)


def save_module_state(module_name: str, key: str, value: str):
    """
    Saves a specific state key for a module to the config.yaml file.
    This is used to persist things like '_last_generation_date'.
    """
    global config  # Ensure we're working with the global in-memory config
    try:
        current_config_on_disk = load_and_merge_config()  # Load freshest state from disk

        # Update both the in-memory global config AND the disk version
        if module_name in config["parts"]:
            config["parts"][module_name][key] = value
        if module_name in current_config_on_disk["parts"]:
            current_config_on_disk["parts"][module_name][key] = value

        with open(CONFIG_FILE, "w") as f:
            yaml.dump(current_config_on_disk, f, sort_keys=False, indent=2)
        logger.debug(f"Saved state for module '{module_name}': {key} = {value}")
    except Exception as e:
        logger.error(f"Failed to save state for module '{module_name}': {e}")


def instantiate_bot_modules():
    """
    Instantiates enabled bot modules based on the current configuration.
    Clears existing module instances and registers their handlers.
    """
    global active_bot_modules
    for module in active_bot_modules:
        if hasattr(module, "close"):
            module.close()
    active_bot_modules.clear()

    module_classes = {
        "HoliBot": HoliBotModule,
        "JokeGenerator": JokeGeneratorModule,
    }

    if "parts" in config and isinstance(config["parts"], dict):
        for part_name, part_cfg in config["parts"].items():
            if part_cfg.get("enabled", False):
                module_type = part_cfg.get("type")
                if module_type in module_classes:
                    module_class = module_classes[module_type]
                    try:
                        # Pass save_module_state function to module for state persistence
                        module_instance = module_class(
                            bot=bot,
                            client=client,
                            module_config=part_cfg,
                            global_config=config,
                            logger=logger,
                            save_state_callback=lambda key,
                            value,
                            part_name=part_name: save_module_state(part_name, key, value),
                        )
                        active_bot_modules.append(module_instance)
                        module_instance.register_handlers()
                        logger.info(
                            f"Module '{part_name}' ({module_type}) loaded and handlers registered."
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to load module '{part_name}' ({module_type}): {e}"
                        )
                else:
                    logger.warning(
                        f"Unknown module type '{module_type}' for part '{part_name}'. Skipping."
                    )
            else:
                logger.info(f"Module '{part_name}' is disabled in config. Skipping.")
    else:
        logger.warning("No 'parts' section found or it's invalid in config.yaml.")


# --- Command Handlers (Global) ---
@bot.message_handler(commands=["start", "help"])
async def send_welcome(message):
    await bot.reply_to(
        message,
        "Hello! I am a modular bot. Check my /settings or ask admins for more info.",
    )


@bot.message_handler(commands=["settings"])
async def settings_handler(message):
    if message.from_user.id not in config["telegram"]["admin_ids"]:
        await bot.reply_to(message, "Sorry, you are not authorized to access settings.")
        return

    try:
        webapp_url = config["webapp"]["url"]
        if not webapp_url.startswith("https"):
            await bot.reply_to(
                message,
                "⚠️ **Configuration Warning:**\nThe Web App URL is not set to HTTPS. It will not work in Telegram.\nPlease set a valid https://... URL in config.yaml.",
                parse_mode="Markdown",
            )
            return
    except KeyError:
        logger.error("webapp.url not found in config.yaml!")
        await bot.reply_to(message, "Error: Web App URL is not configured on the server.")
        return

    await bot.send_message(
        message.chat.id,
        "Click the button below to open the settings panel.",
        reply_markup=telebot.types.InlineKeyboardMarkup(
            [
                [
                    telebot.types.InlineKeyboardButton(
                        "Open Settings", web_app=telebot.types.WebAppInfo(webapp_url)
                    )
                ]
            ]
        ),
    )


@bot.message_handler(commands=["postnow"])
async def post_now_handler(message):
    if message.from_user.id not in config["telegram"]["admin_ids"]:
        await bot.reply_to(message, "Sorry, you are not authorized to use this command.")
        return

    if not active_bot_modules:
        await bot.reply_to(message, "No bot modules are currently active to post.")
        return

    await bot.reply_to(
        message,
        "Authorized! Triggering all active modules to post to all configured chats...",
    )
    for module in active_bot_modules:
        asyncio.create_task(module.run_scheduled_job())
        await asyncio.sleep(0.1)


@bot.message_handler(commands=["posttome"])
async def post_to_me_handler(message):
    if message.from_user.id not in config["telegram"]["admin_ids"]:
        await bot.reply_to(message, "Sorry, you are not authorized to use this command.")
        return

    if not active_bot_modules:
        await bot.reply_to(message, "No bot modules are currently active to post.")
        return

    await bot.reply_to(
        message,
        f"Authorized! Triggering all active modules to post just for you in chat {message.chat.id}...",
    )
    for module in active_bot_modules:
        asyncio.create_task(module.run_scheduled_job(target_chat_ids=[message.chat.id]))
        await asyncio.sleep(0.1)


@bot.my_chat_member_handler()
async def my_chat_member_handler(message: telebot.types.ChatMemberUpdated):
    if message.new_chat_member.status in ["member", "administrator"]:
        chat_id = message.chat.id
        chat_ids = config["telegram"]["chat_ids"]
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
            save_config_chats(chat_ids)
            config["telegram"]["chat_ids"] = chat_ids
            logger.info(f"Bot added to new group: {chat_id}. Global config updated.")
            await bot.send_message(
                chat_id,
                "Hello! I have been added to this chat. My modules will now be able to post here.",
            )
    elif message.new_chat_member.status == "kicked":
        chat_id = message.chat.id
        chat_ids = config["telegram"]["chat_ids"]
        if chat_id in chat_ids:
            chat_ids.remove(chat_id)
            save_config_chats(chat_ids)
            config["telegram"]["chat_ids"] = chat_ids
            logger.info(f"Bot removed from group: {chat_id}. Global config updated.")


# --- Scheduler ---
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
            module_next_event_time = module.next_scheduled_event_time

            if module_next_event_time is None:
                module.logger.debug(
                    f"Scheduler: '{module.name}' has no future scheduled events."
                )
                continue

            # If the event is in the past or very nearly in the past (due now)
            if now >= module_next_event_time - timedelta(
                seconds=2
            ):  # Small buffer for due check
                logger.info(
                    f"Scheduler: '{module.name}' event is DUE NOW (scheduled for {module_next_event_time.strftime('%H:%M:%S UTC')}). Calling process_due_event."
                )
                tasks_to_run.append(module.process_due_event())
            else:
                sleep_for_this_module = (module_next_event_time - now).total_seconds()
                if sleep_for_this_module > 0:
                    next_sleep_duration_seconds = min(
                        next_sleep_duration_seconds, sleep_for_this_module
                    )

        if tasks_to_run:
            logger.info(f"Scheduler: Executing {len(tasks_to_run)} due tasks.")
            await asyncio.gather(*tasks_to_run, return_exceptions=True)
            logger.debug(
                "Scheduler: Tasks executed. Re-evaluating next sleep duration immediately."
            )
            await asyncio.sleep(1)
            continue

        next_sleep_duration_seconds = max(next_sleep_duration_seconds, 5)
        logger.info(
            f"Scheduler: Next global check in {next_sleep_duration_seconds:.2f} seconds ({next_sleep_duration_seconds / 3600:.2f} hours)."
        )

        try:
            await asyncio.sleep(next_sleep_duration_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled.")
            break
        except Exception as e:
            logger.error(f"An unexpected error occurred in background_scheduler: {e}")
            await asyncio.sleep(5)

    logger.info("Scheduler task has finished.")


async def polling_loop(shutdown_event: asyncio.Event):
    logger.info("Starting polling loop.")
    offset = 0
    timeout = 10

    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=timeout)
            if updates:
                offset = updates[-1].update_id + 1
                await bot.process_new_updates(updates)

        except asyncio.CancelledError:
            logger.info("Polling loop received cancel signal. Exiting.")
            break
        except Exception as e:
            logger.error(f"An error occurred in the polling loop: {e}")
            await asyncio.sleep(5)

    logger.info("Polling loop has finished.")


async def config_reloader_task(interval_seconds=5):
    global config
    logger.info("Starting config reloader task.")
    while True:
        try:
            if main_app_instance.reload_config_signal:
                logger.info(
                    "Reload signal received. Reloading config from file and re-instantiating modules."
                )
                config = load_and_merge_config()
                instantiate_bot_modules()
                main_app_instance.reload_config_signal = False
                logger.info("Config reloaded and modules re-instantiated successfully.")
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Config reloader task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in config reloader task: {e}")
            await asyncio.sleep(interval_seconds)


async def main():
    global config
    config = load_and_merge_config()
    instantiate_bot_modules()

    shutdown_event = asyncio.Event()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    uvicorn_config = uvicorn.Config(
        app, host="0.0.0.0", port=8000, log_level="info", access_log=False
    )
    server = uvicorn.Server(uvicorn_config)

    def initiate_shutdown(sig):
        logger.info(f"Signal {sig.name} received. Initiating graceful shutdown...")
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGINT, initiate_shutdown, signal.SIGINT)
    loop.add_signal_handler(signal.SIGTERM, initiate_shutdown, signal.SIGTERM)

    logger.info("Starting background tasks...")

    scheduler_task = asyncio.create_task(background_scheduler())
    polling_task = asyncio.create_task(polling_loop(shutdown_event))
    config_reloader_task_obj = asyncio.create_task(config_reloader_task())

    async def run_server():
        await server.serve()

    web_server_task = asyncio.create_task(run_server())

    await shutdown_event.wait()

    logger.info("Shutdown event received. Cleaning up all tasks...")

    logger.info("Closing Telegram bot session to unblock network calls...")
    await bot.close_session()

    server.should_exit = True
    scheduler_task.cancel()
    polling_task.cancel()
    config_reloader_task_obj.cancel()

    await asyncio.gather(
        scheduler_task,
        polling_task,
        config_reloader_task_obj,
        web_server_task,
        return_exceptions=True,
    )

    logger.info("Application has shut down gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Main function interrupted. Exiting.")
