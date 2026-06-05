"""
Telegram bot that reports the address of a truck or trailer.

Usage in Telegram:
    /where 797            one unit
    /where 797 9917       several units at once
    797                   bare number — no slash needed

The number is the Samsara unit name (the number painted on the truck/trailer).
The bot looks the name up in both vehicles and trailers, then returns the
reverse-geocoded street address, a native map pin, and live status.
"""

import asyncio
import difflib
import logging
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load .env.local for local dev; on a host like Render the vars come from the
# environment directly and this is a harmless no-op.
load_dotenv(".env.local")

SAMSARA_TOKEN = os.environ["SAMSARA_TOKEN"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

SAMSARA_BASE = "https://api.samsara.com"
HEADERS = {"Authorization": f"Bearer {SAMSARA_TOKEN}"}

# Org ID for building cloud dashboard deep links (from GET /me).
SAMSARA_ORG_ID = os.environ.get("SAMSARA_ORG_ID", "10001557")

# Render's free tier only keeps a *Web Service* awake, so we expose a tiny
# health-check HTTP server on $PORT alongside the bot. Unset locally = no server.
PORT = os.environ.get("PORT")


def samsara_link(kind: str, unit_id: str) -> str:
    """Cloud dashboard URL for a single unit. Vehicles and trailers both use
    the /devices/{id}/vehicle path."""
    return f"https://cloud.samsara.com/o/{SAMSARA_ORG_ID}/devices/{unit_id}/vehicle"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("where_bot")


# --------------------------------------------------------------------------- #
# Samsara API + an in-memory name->unit cache
# --------------------------------------------------------------------------- #

_CACHE_TTL = 300  # seconds; refresh the unit roster at most every 5 minutes
_cache: dict[str, tuple[str, str]] = {}  # lower-name -> (kind, id)
_cache_names: list[str] = []  # original-case names, for fuzzy suggestions
_cache_at: float = 0.0


def _get_all(path: str) -> list[dict]:
    """Fetch every page from a Samsara list endpoint and return the data items."""
    items: list[dict] = []
    cursor = None
    while True:
        params = {"limit": 512}
        if cursor:
            params["after"] = cursor
        resp = requests.get(SAMSARA_BASE + path, headers=HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        body = resp.json()
        items.extend(body.get("data", []))
        page = body.get("pagination", {})
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return items


def _refresh_cache() -> None:
    """Rebuild the name->unit map from Samsara. Trailers fill gaps, not overwrite."""
    global _cache, _cache_names, _cache_at
    mapping: dict[str, tuple[str, str]] = {}
    names: list[str] = []
    for vehicle in _get_all("/fleet/vehicles"):
        name = str(vehicle.get("name", "")).strip()
        if name:
            mapping[name.lower()] = ("vehicle", vehicle["id"])
            names.append(name)
    for trailer in _get_all("/fleet/trailers"):
        name = str(trailer.get("name", "")).strip()
        if name and name.lower() not in mapping:  # vehicles win on a name clash
            mapping[name.lower()] = ("trailer", trailer["id"])
            names.append(name)
    _cache, _cache_names, _cache_at = mapping, names, time.monotonic()
    log.info("Cached %d units from Samsara.", len(mapping))


def _ensure_cache() -> None:
    if not _cache or (time.monotonic() - _cache_at) > _CACHE_TTL:
        _refresh_cache()


def find_unit(name: str) -> tuple[str, str] | None:
    """Find a unit by name. Returns (kind, id) or None. Vehicles win ties."""
    _ensure_cache()
    return _cache.get(name.strip().lower())


def suggest_names(name: str, n: int = 3) -> list[str]:
    """Return up to n close-match unit names for a miss (typo help)."""
    _ensure_cache()
    return difflib.get_close_matches(name, _cache_names, n=n, cutoff=0.5)


def get_location(kind: str, unit_id: str) -> dict | None:
    """Fetch the latest GPS stat for a vehicle or trailer."""
    if kind == "vehicle":
        path, id_param = "/fleet/vehicles/stats", "vehicleIds"
    else:
        path, id_param = "/fleet/trailers/stats", "trailerIds"
    resp = requests.get(
        SAMSARA_BASE + path,
        headers=HEADERS,
        params={"types": "gps", id_param: unit_id},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return None
    return data[0].get("gps")


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _heading_to_compass(deg: float) -> str:
    return _COMPASS[round(deg / 45) % 8]


def _ago(iso_time: str) -> str:
    """Human 'time since' for an ISO-8601 UTC timestamp."""
    try:
        then = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    except ValueError:
        return iso_time
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{round(secs / 60)} min ago"
    if secs < 86400:
        return f"{round(secs / 3600)} h ago"
    return f"{round(secs / 86400)} d ago"


def format_unit(name: str, kind: str, gps: dict) -> tuple[str, float | None, float | None]:
    """Build the reply text. Returns (text, lat, lon)."""
    address = gps.get("reverseGeo", {}).get("formattedLocation")
    lat, lon = gps.get("latitude"), gps.get("longitude")
    speed = gps.get("speedMilesPerHour")
    heading = gps.get("headingDegrees")
    when = gps.get("time", "")

    label = "🚛 Truck" if kind == "vehicle" else "🛻 Trailer"
    lines = [f"*{label} {name}*"]
    lines.append(f"📍 {address}" if address else "📍 _no address resolved_")

    if speed is not None and speed >= 1:
        course = f" {_heading_to_compass(heading)}" if heading is not None else ""
        lines.append(f"🟢 Moving — {round(speed)} mph{course}")
    elif speed is not None:
        lines.append("🅿️ Parked")

    if when:
        lines.append(f"🕒 GPS fix {_ago(when)}")

    return "\n".join(lines), lat, lon


def _buttons(
    name: str, kind: str, unit_id: str, lat: float | None, lon: float | None
) -> InlineKeyboardMarkup:
    row = []
    if lat is not None and lon is not None:
        row.append(InlineKeyboardButton("🗺️ Maps", url=f"https://maps.google.com/?q={lat},{lon}"))
    row.append(InlineKeyboardButton("📡 Samsara", url=samsara_link(kind, unit_id)))
    row.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"where:{name}"))
    return InlineKeyboardMarkup([row])


# --------------------------------------------------------------------------- #
# Core lookup-and-reply, shared by the command, plain text, and refresh button
# --------------------------------------------------------------------------- #

async def respond_for(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    """Look up one unit and reply with text, buttons, and a map pin."""
    chat = update.effective_chat
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        match = await asyncio.to_thread(find_unit, name)
        if match is None:
            hint = ""
            sugg = await asyncio.to_thread(suggest_names, name)
            if sugg:
                hint = "\n\nDid you mean: " + ", ".join(f"`{s}`" for s in sugg) + "?"
            await chat.send_message(
                f"❓ No truck or trailer named *{name}*.{hint}",
                parse_mode="Markdown",
            )
            return

        kind, unit_id = match
        gps = await asyncio.to_thread(get_location, kind, unit_id)
        if not gps:
            await chat.send_message(f"*{name}* found, but no GPS data is available.", parse_mode="Markdown")
            return

        text, lat, lon = format_unit(name, kind, gps)
        await chat.send_message(
            text,
            parse_mode="Markdown",
            reply_markup=_buttons(name, kind, unit_id, lat, lon),
        )

    except requests.HTTPError as exc:
        log.exception("Samsara API error")
        await chat.send_message(f"⚠️ Samsara API error: {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error")
        await chat.send_message(f"⚠️ Something went wrong: {exc}")


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

WELCOME = (
    "👋 *Fleet Locator*\n\n"
    "Send me a unit number and I'll tell you exactly where it is — truck or trailer.\n\n"
    "*Examples*\n"
    "• `/where 797`\n"
    "• `/where 797 9917` — several at once\n"
    "• `797` — just the number, no slash\n\n"
    "Each result includes the street address, a map pin, and moving/parked status."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def where(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: `/where <unit number>` — e.g. `/where 797 9917`", parse_mode="Markdown"
        )
        return
    for name in context.args[:10]:  # cap to avoid flooding on a fat-fingered message
        await respond_for(update, context, name)


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Treat bare tokens (e.g. '797' or '797 9917') as unit lookups."""
    tokens = update.message.text.split()
    for name in tokens[:10]:
        await respond_for(update, context, name)


async def on_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline 🔄 Refresh button."""
    query = update.callback_query
    await query.answer("Refreshing…")
    name = query.data.split(":", 1)[1]
    await respond_for(update, context, name)


async def _post_init(app: Application) -> None:
    """Register the slash-command menu shown in Telegram's UI."""
    await app.bot.set_my_commands(
        [
            BotCommand("where", "Locate a truck or trailer, e.g. /where 797"),
            BotCommand("start", "How to use this bot"),
        ]
    )


def _start_health_server() -> None:
    """Serve 200 OK on $PORT in a daemon thread.

    Render requires a Web Service to bind a port, and an uptime pinger hitting
    this URL keeps the free instance from spinning down. No-op when PORT is unset
    (e.g. running locally).
    """
    if not PORT:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # silence per-request logging
            pass

    server = HTTPServer(("0.0.0.0", int(PORT)), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", PORT)


def main() -> None:
    _start_health_server()

    # Python 3.14 no longer creates an implicit event loop, which
    # python-telegram-bot 21.x's run_polling() expects. Create one explicitly.
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("where", where))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern=r"^where:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text))
    log.info("Bot started. Send /where <unit number> in Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()
