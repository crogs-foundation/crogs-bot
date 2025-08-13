import hashlib
import hmac
import json
import os
from urllib.parse import unquote

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


# --- Helper Class to Signal Main App ---
class MainAppSignal:
    def __init__(self):
        self.reload_config_signal = False


main_app_instance = MainAppSignal()


# --- NEW and ROBUST Authentication Validator ---
async def validate_telegram_auth(request: Request):
    """
    Validates the request by:
    1. Checking if the data is authentically from Telegram using the provided hash.
    2. Checking if the user who opened the Web App is in the admin_ids list.
    """
    init_data_str = request.headers.get("X-Telegram-Auth")
    if not init_data_str:
        raise HTTPException(status_code=401, detail="X-Telegram-Auth header missing.")

    # --- 1. Perform Secure Hash Validation ---
    try:
        # The initData is a URL query string. Let's parse it.
        parsed_data = dict(part.split("=", 1) for part in init_data_str.split("&"))
        received_hash = parsed_data.pop("hash", None)
        if not received_hash:
            raise ValueError("Hash not found in initData")

        # The data-check-string is the rest of the fields, sorted and joined by newlines.
        sorted_keys = sorted(parsed_data.keys())
        # IMPORTANT: The values must be unquoted before being part of the check string.
        data_check_string = "\n".join(
            f"{key}={unquote(parsed_data[key])}" for key in sorted_keys
        )

        # Now, calculate the expected hash
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            print("ERROR: TELEGRAM_BOT_TOKEN not configured for API validation.")
            raise HTTPException(
                status_code=500, detail="Internal server error: Auth token not set."
            )

        secret_key = hmac.new(
            "WebAppData".encode(), bot_token.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        # Compare the hash from Telegram with our calculated hash
        if calculated_hash != received_hash:
            raise HTTPException(
                status_code=401, detail="Authentication failed: Invalid hash."
            )

    except Exception as e:
        # This catches errors in parsing, missing hash, etc.
        raise HTTPException(status_code=400, detail=f"Invalid initData structure: {e}")

    # --- 2. Perform Authorization Check ---
    try:
        # Now that the data is verified, we can trust its contents.
        user_data_str = unquote(parsed_data.get("user", "{}"))
        user_data = json.loads(user_data_str)
        user_id = user_data.get("id")

        if not user_id:
            raise ValueError("User ID not found in verified data.")

        # Load admin IDs from the main config file
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        admin_ids = config.get("telegram", {}).get("admin_ids", [])

        if not admin_ids:
            raise HTTPException(
                status_code=403,
                detail="Forbidden: No admin users are configured on the server.",
            )

        # The final check: is the user an admin?
        if user_id not in admin_ids:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: User {user_id} is not an authorized admin.",
            )

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise HTTPException(
            status_code=400, detail=f"Could not validate user identity: {e}"
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500, detail="Internal server error: Config file not found."
        )

    # If both checks pass, the request is valid.
    return True


# --- FastAPI App Setup ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*"
    ],  # In production, you might restrict this to your ngrok/server URL
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- API Endpoints (no changes needed here) ---
@app.get("/api/config", dependencies=[Depends(validate_telegram_auth)])
def get_config():
    """Returns the current configuration as JSON."""
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


@app.post("/api/config", dependencies=[Depends(validate_telegram_auth)])
async def save_config(request: Request):
    """Saves the new configuration."""
    new_config = await request.json()

    if "telegram" not in new_config or "scheduler" not in new_config:
        raise HTTPException(status_code=400, detail="Invalid config structure.")

    with open("config.yaml", "w") as f:
        yaml.dump(new_config, f, sort_keys=False, indent=2)

    main_app_instance.reload_config_signal = True

    return {"status": "success", "message": "Configuration saved and will be reloaded."}


# --- Static File Serving (no changes needed here) ---
@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("webapp/index.html", "r") as f:
        return HTMLResponse(content=f.read())


app.mount("/static", StaticFiles(directory="webapp"), name="static")
