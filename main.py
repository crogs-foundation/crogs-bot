import argparse
import asyncio
import os
import signal
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import requests
import telebot
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from g4f.client import AsyncClient
from telebot.async_telebot import AsyncTeleBot

from src.logger import Logger

# --- Argument Parsing, Logging, and Initialization (No changes) ---
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
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found!")
CONFIG_FILE = "config.yaml"
bot = AsyncTeleBot(TELEGRAM_BOT_TOKEN)
client = AsyncClient()
config = {}


# --- Config Management and Content Generation (No changes) ---
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
            merged_config[key].update(value)
        else:
            merged_config[key] = value
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
    logger.info("Starting custom polling loop.")
    offset = 0
    timeout = 1 if DEV_MODE else 10  # shorter in dev to make reload safe
    try:
        while not shutdown_event.is_set():
            try:
                updates = await asyncio.wait_for(
                    bot.get_updates(offset=offset, timeout=timeout), timeout=timeout + 1
                )
                if updates:
                    offset = updates[-1].update_id + 1
                    await bot.process_new_updates(updates)
                await asyncio.sleep(0.05)
            except asyncio.TimeoutError:
                continue
            except telebot.asyncio_helper.ApiTelegramException as e:
                if e.error_code == 409 and DEV_MODE:
                    logger.warning("409 Conflict in dev mode — exiting polling early.")
                    break
                raise
            except asyncio.CancelledError:
                logger.info("Polling loop cancelled.")
                break
            except Exception:
                logger.error("Error in polling loop")
                await asyncio.sleep(1)
    finally:
        logger.info("Closing bot session...")
        await bot.close_session()
        logger.info("Polling loop has finished.")


async def main():
    """Main entry point to load config, set up signal handlers, and start the bot."""
    global config
    config = load_and_merge_config()
    shutdown_event = asyncio.Event()

    def signal_handler(sig, _frame):
        logger.info(f"Signal {sig} received. Initiating shutdown.")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger.info("Starting background tasks...")
    scheduler_task = asyncio.create_task(background_scheduler())
    # Start our custom, well-behaved polling loop instead of the library's
    polling_task = asyncio.create_task(polling_loop(shutdown_event))

    await shutdown_event.wait()
    logger.info("Shutdown event received. Cleaning up...")

    scheduler_task.cancel()
    polling_task.cancel()

    # Ensure cancellation finishes
    await asyncio.gather(scheduler_task, polling_task, return_exceptions=True)

    # Double-close in case polling loop never ran finally block
    await bot.close_session()

    logger.info("Application has shut down gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Main function interrupted. Exiting.")
