import argparse
import asyncio
import os
import signal
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import requests
import telebot
import uvicorn
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot

from src.logger import Logger
from src.web_api import app

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
client = AsyncClient()
config = {}


# --- Config Management ---
def load_and_merge_config():
    DEFAULTS = {
        "telegram": {
            "admin_ids": [],
            "chat_ids": [],
            "caption_character_limit": 1024,
            "post_delay_seconds": 1,
        },
        "scheduler": {"post_time_utc": "08:00"},
        "scraper": {"holiday_url": "https://www.checkiday.com/", "holiday_limit": 0},
        "llm": {
            "concurrency_limit": 4,
            "text_model": "gpt-3.5-turbo",
            "image_model": "dall-e-3",
            "text_prompt": "Generate a short, funny caption for '{holiday_name}'.",
            "image_prompt": "A humorous image for '{holiday_name}'.",
        },
        # Add the new webapp section to defaults
        "webapp": {
            "url": "https://127.0.0.1:8000"  # A default for local testing
        },
    }
    try:
        with open(CONFIG_FILE, "r") as f:
            user_config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"'{CONFIG_FILE}' not found. Creating it with default values.")
        with open(CONFIG_FILE, "w") as f:
            yaml.dump(DEFAULTS, f, sort_keys=False)
        return DEFAULTS

    merged_config = deepcopy(DEFAULTS)
    for key, value in user_config.items():
        if isinstance(value, dict):
            # Use deepcopy for nested dictionaries
            merged_config[key] = deepcopy(merged_config.get(key, {}))
            merged_config[key].update(value)
        else:
            merged_config[key] = value

    # Save the potentially updated config (with new default sections)
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(merged_config, f, sort_keys=False, indent=2)

    return merged_config


def save_config_chats(chat_ids):
    current_config = load_and_merge_config()
    current_config["telegram"]["chat_ids"] = chat_ids
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(current_config, f, sort_keys=False, indent=2)


def get_todays_holidays(cfg):
    try:
        url, limit = cfg["scraper"]["holiday_url"], cfg["scraper"]["holiday_limit"]
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        elements = [
            h.text.strip() for h in soup.find_all("h2", class_="mdl-card__title-text")
        ]
        return elements[:limit] if limit > 0 else elements
    except requests.exceptions.RequestException:
        logger.error("Error fetching holidays")
        return []


async def generate_caption(holiday_name, cfg):
    try:
        prompt, model = (
            cfg["llm"]["text_prompt"].format(holiday_name=holiday_name),
            cfg["llm"]["text_model"],
        )
        response = await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception:
        logger.error(f"Error generating caption for {holiday_name}")
        return f"Happy {holiday_name}!"


async def generate_image(holiday_name, cfg):
    try:
        prompt, model = (
            cfg["llm"]["image_prompt"].format(holiday_name=holiday_name),
            cfg["llm"]["image_model"],
        )
        response = await client.images.generate(
            model=model, prompt=prompt, response_format="url"
        )
        image_url = response.data[0].url
        if image_url and image_url.startswith("http"):
            return image_url
        logger.warning(
            f"Image generation returned invalid URL for {holiday_name}: {image_url}"
        )
        return None
    except Exception:
        logger.error(f"Error generating image for {holiday_name}")
        return None


async def generate_holiday_content(holiday_name, semaphore, cfg):
    async with semaphore:
        logger.debug(f"Generating content for '{holiday_name}'...")
        caption, image_url = await asyncio.gather(
            generate_caption(holiday_name, cfg), generate_image(holiday_name, cfg)
        )
        logger.debug(f"Finished content for '{holiday_name}'.")
        return holiday_name, caption, image_url


async def post_holidays(target_chat_ids=None):
    logger.info("Holiday job started.")
    holidays = await asyncio.to_thread(get_todays_holidays, config)
    post_to_chats = (
        target_chat_ids if target_chat_ids is not None else config["telegram"]["chat_ids"]
    )
    if not holidays or not post_to_chats:
        logger.warning("No holidays or chats found. Job aborted.")
        return
    concurrency, caption_limit, post_delay = (
        config["llm"]["concurrency_limit"],
        config["telegram"]["caption_character_limit"],
        config["telegram"]["post_delay_seconds"],
    )
    logger.info(
        f"Found {len(holidays)} holidays. Generating content with concurrency {concurrency}."
    )
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [generate_holiday_content(h, semaphore, config) for h in holidays]
    generated_content = await asyncio.gather(*tasks)
    logger.info(f"Content generation complete. Posting to {len(post_to_chats)} chat(s).")
    for holiday, caption, image_url in generated_content:
        if len(caption) > caption_limit:
            caption = caption[: caption_limit - 3] + "..."
        for chat_id in post_to_chats:
            try:
                if image_url:
                    await bot.send_photo(chat_id, image_url, caption=caption)
                else:
                    await bot.send_message(chat_id, caption)
                await asyncio.sleep(post_delay)
            except telebot.apihelper.ApiTelegramException as e:
                logger.error(
                    f"Telegram API Error sending to {chat_id} for {holiday}: {e}"
                )
    logger.info("Holiday job finished.")


# --- Command Handlers (No changes) ---
@bot.message_handler(commands=["start", "help"])
async def send_welcome(message):
    await bot.reply_to(message, "...")


@bot.message_handler(commands=["settings"])
async def settings_handler(message):
    if message.from_user.id not in config["telegram"]["admin_ids"]:
        await bot.reply_to(message, "Sorry, you are not authorized.")
        return

    # Load URL from the global config object
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
        await bot.reply_to(message, "Sorry, you are not authorized.")
        return
    await bot.reply_to(message, "Authorized! Posting to all chats...")
    asyncio.create_task(post_holidays())


@bot.message_handler(commands=["posttome"])
async def post_to_me_handler(message):
    if message.from_user.id not in config["telegram"]["admin_ids"]:
        await bot.reply_to(message, "Sorry, you are not authorized.")
        return
    await bot.reply_to(message, "Authorized! Posting just for you...")
    asyncio.create_task(post_holidays(target_chat_ids=[message.chat.id]))


@bot.my_chat_member_handler()
async def my_chat_member_handler(message: telebot.types.ChatMemberUpdated):
    if message.new_chat_member.status in ["member", "administrator"]:
        chat_id, chat_ids = message.chat.id, config["telegram"]["chat_ids"]
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
            save_config_chats(chat_ids)
            config["telegram"]["chat_ids"] = chat_ids
            logger.info(f"Bot added to new group: {chat_id}. Config updated.")
            await bot.send_message(chat_id, "Hello! I will now post daily holidays here.")


# --- Scheduler (No changes) ---
async def background_scheduler():
    while True:
        try:
            time_str = config["scheduler"]["post_time_utc"]
            post_hour, post_minute = map(int, time_str.split(":"))
        except (ValueError, KeyError):
            logger.error("Invalid 'post_time_utc'. Defaulting to 08:00.")
            post_hour, post_minute = 8, 0
        now = datetime.now(timezone.utc)
        target_time = now.replace(
            hour=post_hour, minute=post_minute, second=0, microsecond=0
        )
        if now > target_time:
            target_time += timedelta(days=1)
        sleep_duration = (target_time - now).total_seconds()
        logger.info(
            f"Scheduler: Next run at {target_time} UTC. Sleeping for {sleep_duration / 3600:.2f} hours."
        )
        try:
            await asyncio.sleep(sleep_duration)
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled.")
            break
        logger.info("Scheduler: Waking up for daily job.")
        await post_holidays()


async def polling_loop(shutdown_event: asyncio.Event):
    """
    A robust polling loop that is guaranteed to stop on cancellation.
    """
    logger.info("Starting polling loop.")
    offset = 0
    timeout = 10  # Seconds to wait for an update

    while True:
        try:
            # The asyncio.shield() call is not strictly necessary with the right shutdown logic,
            # but it makes our intent clear: the get_updates call should not be cancelled directly.
            # Instead, the outer task is cancelled, which this loop will catch.
            updates = await bot.get_updates(offset=offset, timeout=timeout)
            if updates:
                offset = updates[-1].update_id + 1
                await bot.process_new_updates(updates)

        except asyncio.CancelledError:
            # This is the signal for a graceful shutdown.
            logger.info("Polling loop received cancel signal. Exiting.")
            break
        except Exception as e:
            logger.error(f"An error occurred in the polling loop: {e}")
            # Avoid spamming logs on persistent errors.
            await asyncio.sleep(5)

    logger.info("Polling loop has finished.")


async def main():
    """Main entry point to load config, set up signal handlers, and start all services."""
    global config
    config = load_and_merge_config()

    shutdown_event = asyncio.Event()

    # --- THIS IS THE KEY CHANGE: Get the running event loop ---
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 'RuntimeError: There is no current event loop...'
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    uvicorn_config = uvicorn.Config(
        app, host="0.0.0.0", port=8000, log_level="info", access_log=False
    )
    server = uvicorn.Server(uvicorn_config)

    # This is the function that will be called by the signal.
    # It needs to be a regular function, not an async one.
    def initiate_shutdown(sig):
        logger.info(f"Signal {sig.name} received. Initiating graceful shutdown...")
        # Setting the event is a thread-safe way to signal the main task.
        shutdown_event.set()

    # --- Register the handler with the asyncio event loop ---
    # This is the correct way to handle signals in an asyncio application.
    loop.add_signal_handler(signal.SIGINT, initiate_shutdown, signal.SIGINT)
    loop.add_signal_handler(signal.SIGTERM, initiate_shutdown, signal.SIGTERM)

    logger.info("Starting background tasks...")

    scheduler_task = asyncio.create_task(background_scheduler())
    polling_task = asyncio.create_task(polling_loop(shutdown_event))

    async def run_server():
        await server.serve()

    web_server_task = asyncio.create_task(run_server())

    # The application now waits here until initiate_shutdown() sets the event.
    await shutdown_event.wait()

    # The rest of your proven cleanup logic from before.
    logger.info("Shutdown event received. Cleaning up all tasks...")

    logger.info("Closing Telegram bot session to unblock network calls...")
    await bot.close_session()

    server.should_exit = True
    scheduler_task.cancel()
    polling_task.cancel()

    await asyncio.gather(
        scheduler_task, polling_task, web_server_task, return_exceptions=True
    )

    logger.info("Application has shut down gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Main function interrupted. Exiting.")
