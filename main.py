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
    current_config = load_and_merge_config()  # Reload to get the latest base config
    current_config["telegram"]["chat_ids"] = chat_ids
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(current_config, f, sort_keys=False, indent=2)


def instantiate_bot_modules():
    """
    Instantiates enabled bot modules based on the current configuration.
    Clears existing module instances and registers their handlers.
    """
    global active_bot_modules
    active_bot_modules.clear()  # Remove old instances (important for config reloads)

    # Map 'type' strings from config to actual BotModule classes
    module_classes = {
        "HoliBot": HoliBotModule,
        "JokeGenerator": JokeGeneratorModule,  # <--- Add this line
        # Add other module types here as they are developed:
        # "JokeGenerator": JokeGeneratorModule,
        # "NewsScraper": NewsScraperModule,
    }

    if "parts" in config and isinstance(config["parts"], dict):
        for part_name, part_cfg in config["parts"].items():
            if part_cfg.get("enabled", False):
                module_type = part_cfg.get("type")
                if module_type in module_classes:
                    module_class = module_classes[module_type]
                    try:
                        module_instance = module_class(
                            bot=bot,
                            client=client,
                            module_config=part_cfg,
                            global_config=config,  # Pass global config for shared data like chat_ids
                            logger=logger,
                        )
                        active_bot_modules.append(module_instance)
                        module_instance.register_handlers()  # Call module to register its specific handlers
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


# --- Command Handlers (Global - affecting all enabled modules) ---
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
    """Triggers run_scheduled_job for all active modules to all configured chats."""
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
        await asyncio.sleep(0.1)  # Small delay to avoid overwhelming the event loop


@bot.message_handler(commands=["posttome"])
async def post_to_me_handler(message):
    """Triggers run_scheduled_job for all active modules, but only to the sender's chat."""
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
        await asyncio.sleep(0.1)  # Small delay


@bot.my_chat_member_handler()
async def my_chat_member_handler(message: telebot.types.ChatMemberUpdated):
    """Handles bot being added to/removed from groups, updating global chat_ids."""
    if message.new_chat_member.status in ["member", "administrator"]:
        chat_id = message.chat.id
        chat_ids = config["telegram"]["chat_ids"]
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
            save_config_chats(chat_ids)
            config["telegram"]["chat_ids"] = (
                chat_ids  # Update in-memory config immediately
            )
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
            config["telegram"]["chat_ids"] = (
                chat_ids  # Update in-memory config immediately
            )
            logger.info(f"Bot removed from group: {chat_id}. Global config updated.")


# --- Scheduler (Updated to handle multiple modules) ---
async def background_scheduler():
    """
    Manages the scheduling of jobs for all active bot modules based on their configurations.
    """
    logger.info("Scheduler: Starting background scheduler for modules.")
    while True:
        now = datetime.now(timezone.utc)

        # Track the minimum sleep duration needed until the *next* scheduled event
        min_sleep_duration_seconds = timedelta(
            days=1
        ).total_seconds()  # Default to max sleep

        for module in active_bot_modules:
            scheduler_cfg = module.module_config.get("scheduler", {})
            time_str = scheduler_cfg.get("post_time_utc")

            if not time_str:
                module.logger.debug(
                    f"Module '{module.name}' has no 'post_time_utc' defined. Skipping scheduled run check."
                )
                continue

            try:
                post_hour, post_minute = map(int, time_str.split(":"))
                target_time_today = now.replace(
                    hour=post_hour, minute=post_minute, second=0, microsecond=0
                )

                # If the target time for today has passed, schedule for the next day
                # Else, it's a future time today.
                effective_target_time = target_time_today
                if now >= target_time_today:
                    effective_target_time += timedelta(days=1)

                # Check if this module's job should be run *now*
                # This check ensures we run tasks that are due, and only once per day.
                # We can add a simple state management here or rely on the small window approach.
                # For simplicity and to avoid complex state, let's rely on the execution and next day calculation.

                # If current time is past the target time for today (but not too far past)
                # and it hasn't been run for today yet.
                # A more robust solution would involve storing last_run_date for each module.
                # For this example, let's use a simple heuristic: if `now` is past `target_time_today`
                # and `effective_target_time` is still `target_time_today + 1 day`, it means
                # the job for *today* is due or has just happened.

                # Let's refine the logic to check if it's the right "day" for this time.
                # If `now` is between `target_time_today` and `target_time_today + grace_period`
                grace_period = timedelta(
                    minutes=10
                )  # Run within 10 minutes of scheduled time
                if now >= target_time_today and now < (target_time_today + grace_period):
                    module.logger.info(
                        f"Scheduler: Executing '{module.name}' job (scheduled at {time_str} UTC)."
                    )
                    asyncio.create_task(module.run_scheduled_job())
                else:
                    module.logger.debug(
                        f"Scheduler: '{module.name}' job not due yet or already processed for today. Next expected run at {effective_target_time} UTC."
                    )

                # Calculate sleep duration needed to reach the *next* occurrence of this module's schedule
                sleep_for_this_module = (effective_target_time - now).total_seconds()
                min_sleep_duration_seconds = min(
                    min_sleep_duration_seconds, sleep_for_this_module
                )

            except (ValueError, KeyError) as e:
                module.logger.error(
                    f"Invalid 'post_time_utc' for module '{module.name}': {e}. Skipping schedule for this module."
                )

        # Ensure minimum sleep is at least a few seconds to prevent tight loops
        min_sleep_duration_seconds = max(
            min_sleep_duration_seconds, 10
        )  # Minimum 10 seconds sleep
        logger.info(
            f"Scheduler: Next global check in {min_sleep_duration_seconds / 3600:.2f} hours ({min_sleep_duration_seconds} seconds)."
        )
        try:
            await asyncio.sleep(min_sleep_duration_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled.")
            break

    logger.info("Scheduler task has finished.")


async def polling_loop(shutdown_event: asyncio.Event):
    """
    A robust polling loop that processes Telegram updates.
    """
    logger.info("Starting polling loop.")
    offset = 0
    timeout = 10  # Seconds to wait for an update

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
            await asyncio.sleep(5)  # Wait before retrying on error

    logger.info("Polling loop has finished.")


async def config_reloader_task(interval_seconds=5):
    """
    Periodically checks if the config needs to be reloaded from disk
    and re-instantiates bot modules if needed.
    """
    global config
    logger.info("Starting config reloader task.")
    while True:
        try:
            if main_app_instance.reload_config_signal:
                logger.info(
                    "Reload signal received. Reloading config from file and re-instantiating modules."
                )
                config = load_and_merge_config()
                instantiate_bot_modules()  # Re-instantiate modules after config reload
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
    """Main entry point to load config, set up signal handlers, and start all services."""
    global config
    config = load_and_merge_config()
    instantiate_bot_modules()  # Initial instantiation of modules at startup

    shutdown_event = asyncio.Event()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 'RuntimeError: There is no current event loop...'
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
    config_reloader_task_obj = asyncio.create_task(
        config_reloader_task()
    )  # Store task object to cancel it

    async def run_server():
        await server.serve()

    web_server_task = asyncio.create_task(run_server())

    # The application now waits here until initiate_shutdown() sets the event.
    await shutdown_event.wait()

    logger.info("Shutdown event received. Cleaning up all tasks...")

    logger.info("Closing Telegram bot session to unblock network calls...")
    await bot.close_session()

    server.should_exit = True
    scheduler_task.cancel()
    polling_task.cancel()
    config_reloader_task_obj.cancel()  # Cancel the reloader task

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
