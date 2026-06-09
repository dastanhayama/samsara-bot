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
import re
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import json

import geofence
import monitor

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

# Fleet-event monitor: posts arrive/depart/stop/start/idle to this group chat.
# Get the ID by adding the bot to the group and sending /here there. Unset =
# monitor disabled (the bot still answers commands).
MONITOR_CHAT_ID = os.environ.get("MONITOR_CHAT_ID")
MONITOR_INTERVAL_S = int(os.environ.get("MONITOR_INTERVAL_S", "30"))


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


def format_unit(
    name: str, kind: str, gps: dict, pairing: dict | None = None
) -> tuple[str, float | None, float | None]:
    """Build the reply text. Returns (text, lat, lon).

    `pairing` (trucks only) is the result of geofence.trailer_for_truck — the
    trailer this truck appears to be pulling, with a confidence flag.
    """
    address = gps.get("reverseGeo", {}).get("formattedLocation")
    lat, lon = gps.get("latitude"), gps.get("longitude")
    speed = gps.get("speedMilesPerHour")
    heading = gps.get("headingDegrees")
    when = gps.get("time", "")

    label = "🚛 Truck" if kind == "vehicle" else "🛻 Trailer"
    lines = [f"*{label} {name}*"]
    if address:
        place = geofence.place_for_point(lat, lon)  # saved location, if inside one
        suffix = f" ({place})" if place else ""
        lines.append(f"📍 {address}{suffix}")
    else:
        lines.append("📍 _no address resolved_")

    if speed is not None and speed >= 1:
        course = f" {_heading_to_compass(heading)}" if heading is not None else ""
        lines.append(f"🟢 Moving — {round(speed)} mph{course}")
    elif speed is not None:
        lines.append("🅿️ Parked")

    if when:
        lines.append(f"🕒 GPS fix {_ago(when)}")

    # Truck → trailer pairing line.
    if pairing is not None:
        if pairing.get("confident"):
            d = round(pairing["distance_m"])
            t_age = _ago(pairing["gps"].get("time", ""))
            lines.append(f"🔗 Trailer: *{pairing['name']}* — {d} m away · fix {t_age}")
        else:
            lines.append("🔗 Trailer: _no confident match_")

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

        # Trailer pairing disabled for now (kept in geofence.trailer_for_truck).
        # To re-enable: compute pairing for vehicles and pass it to format_unit.
        pairing = None
        text, lat, lon = format_unit(name, kind, gps, pairing)
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
    "*Find a unit*\n"
    "• `/where 797` — locate a truck or trailer\n"
    "• `/where 797 9917` — several at once\n"
    "• `797` — just the number, no slash\n\n"
    "*Find what's at a location*\n"
    "• `/site pa-il` — units on-site at a saved place\n"
    "• `/sites` — browse all saved locations\n"
    "• `/nearest pa-il` — closest trucks to a place or address\n"
    "• type `@<this bot> pa-il` in any chat — inline search\n\n"
    "Unit results show the address (with the saved-location name if it's parked at one), "
    "a map pin, and moving/parked status."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def here(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/here — report this chat's ID, for wiring up the fleet-event monitor."""
    chat = update.effective_chat
    await update.message.reply_text(
        f"This chat's ID is `{chat.id}`.\n\n"
        "To turn on fleet-event alerts here, set `MONITOR_CHAT_ID` to this value "
        "(in `.env.local` locally, or the host's env vars) and restart the bot.",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — on-demand fleet overview (moving/parked, who's at each location)."""
    chat = update.effective_chat
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        trucks = await asyncio.to_thread(monitor.snapshot_trucks)
        if not trucks:
            await chat.send_message("No trucks with GPS data right now.")
            return
        await chat.send_message(monitor.fleet_summary(trucks), parse_mode="Markdown")
    except requests.HTTPError as exc:
        log.exception("Samsara API error")
        await chat.send_message(f"⚠️ Samsara API error: {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error")
        await chat.send_message(f"⚠️ Something went wrong: {exc}")


def _split_message(text: str, limit: int = 4000) -> list[str]:
    """Split a long report into <=limit chunks, breaking only between lines."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += (line + "\n")
    if cur.strip():
        chunks.append(cur)
    return chunks


async def statusall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/statusall — full fleet: moving, parked-at-locations, then other parked."""
    chat = update.effective_chat
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        fleet = await asyncio.to_thread(monitor.snapshot_fleet)
        if not fleet:
            await chat.send_message("No trucks or trailers with GPS data right now.")
            return
        report = monitor.fleet_status_report(fleet, _ago)
        for chunk in _split_message(report):
            await chat.send_message(chunk, parse_mode="Markdown")
            await asyncio.sleep(0.3)  # gentle pacing across multiple messages
    except requests.HTTPError as exc:
        log.exception("Samsara API error")
        await chat.send_message(f"⚠️ Samsara API error: {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error")
        await chat.send_message(f"⚠️ Something went wrong: {exc}")


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


# --------------------------------------------------------------------------- #
# Saved locations: /site (fuzzy), /sites (menu), inline search, on-site report
# --------------------------------------------------------------------------- #

_SITES_PER_PAGE = 8


def _on_site_text(addr: dict, units: list[tuple[str, str, dict]]) -> str:
    lines = [f"📍 *{addr['name']}*"]
    if addr.get("formatted"):
        lines.append(addr["formatted"])
    lines.append("")
    if not units:
        lines.append("No trailers or trucks on-site right now.")
        return "\n".join(lines)

    lines.append(f"🚛 *On-site ({len(units)}):*")
    for name, kind, gps in units:
        icon = "🛻" if kind == "trailer" else "🚛"
        age = _ago(gps.get("time", ""))
        stale = " ⚠️" if age.endswith(("h ago", "d ago")) else ""
        lines.append(f"• {icon} {name} — fix {age}{stale}")
    return "\n".join(lines)


async def report_on_site(update: Update, context: ContextTypes.DEFAULT_TYPE, address_id: str) -> None:
    """Shared renderer: list every unit currently inside a saved location."""
    chat = update.effective_chat
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        addr, units = await asyncio.to_thread(geofence.units_on_site, address_id)
        if addr is None:
            await chat.send_message("That location no longer exists.")
            return
        buttons = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Refresh", callback_data=f"site:{address_id}")]]
        )
        await chat.send_message(_on_site_text(addr, units), parse_mode="Markdown", reply_markup=buttons)
    except requests.HTTPError as exc:
        log.exception("Samsara API error")
        await chat.send_message(f"⚠️ Samsara API error: {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error")
        await chat.send_message(f"⚠️ Something went wrong: {exc}")


def _sites_keyboard(page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build a paged button menu of all saved locations."""
    addrs = geofence.all_addresses()
    pages = max(1, (len(addrs) + _SITES_PER_PAGE - 1) // _SITES_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _SITES_PER_PAGE
    rows = [
        [InlineKeyboardButton(a["name"], callback_data=f"site:{a['id']}")]
        for a in addrs[start : start + _SITES_PER_PAGE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"sites:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"sites:{page + 1}"))
    if nav:
        rows.append(nav)
    return f"📍 *Saved locations* (page {page + 1}/{pages})", InlineKeyboardMarkup(rows)


async def sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sites — tappable menu of every saved location."""
    await update.effective_chat.send_chat_action(ChatAction.TYPING)
    title, kb = await asyncio.to_thread(_sites_keyboard, 0)
    await update.message.reply_text(title, parse_mode="Markdown", reply_markup=kb)


_CITY_STATE_ZIP = re.compile(r"([A-Za-z .'-]+),?\s+([A-Z]{2})\s+\d{5}")


def _short_addr(formatted: str) -> str:
    """Trim a full address to 'City, ST' for compact button labels."""
    if not formatted:
        return ""
    # Prefer pulling 'City ST ZIP' directly — handles both comma-separated and
    # 'Street  City, ST ZIP' shapes.
    m = _CITY_STATE_ZIP.search(formatted)
    if m:
        return f"{m.group(1).strip()}, {m.group(2)}"
    parts = [p.strip() for p in formatted.split(",") if p.strip() and p.strip().upper() != "USA"]
    return ", ".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")


def _match_buttons(matches: list[dict]) -> InlineKeyboardMarkup:
    """Two-line buttons: location name on top, City, ST beneath."""
    rows = []
    for a in matches:
        city = _short_addr(a.get("formatted", ""))
        label = f"{a['name']}\n{city}" if city else a["name"]
        rows.append([InlineKeyboardButton(label, callback_data=f"site:{a['id']}")])
    return InlineKeyboardMarkup(rows)


async def site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/site [text] — fuzzy-match a location and show who's on-site.

    With no text, lists all saved locations (same as /sites). With text, jumps
    straight to the report on a single match, or shows a tappable list of the
    close matches (with their City, ST) to pick from.
    """
    if not context.args:
        await sites(update, context)
        return
    query = " ".join(context.args)
    matches = await asyncio.to_thread(geofence.search_addresses, query, 10)
    if not matches:
        hint = "\n\nTry `/sites` to browse them all."
        await update.message.reply_text(
            f"No saved location matches *{query}*.{hint}", parse_mode="Markdown"
        )
        return
    if len(matches) == 1:
        await report_on_site(update, context, matches[0]["id"])
        return
    await update.message.reply_text(
        f"📍 *{len(matches)} matches for “{query}”* — tap one:",
        parse_mode="Markdown",
        reply_markup=_match_buttons(matches),
    )


async def on_site_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a tapped location button (from /sites, /site, or disambiguation)."""
    query = update.callback_query
    await query.answer()
    address_id = query.data.split(":", 1)[1]
    await report_on_site(update, context, address_id)


async def on_sites_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Prev/Next paging in the /sites menu."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":", 1)[1])
    title, kb = await asyncio.to_thread(_sites_keyboard, page)
    await query.edit_message_text(title, parse_mode="Markdown", reply_markup=kb)


# --------------------------------------------------------------------------- #
# /nearest — closest trucks to a location (saved place or typed address)
# --------------------------------------------------------------------------- #

_NEAREST_PER_PAGE = 5


def _nearest_view(label: str, trucks: list[dict], page: int) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (len(trucks) + _NEAREST_PER_PAGE - 1) // _NEAREST_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _NEAREST_PER_PAGE
    chunk = trucks[start : start + _NEAREST_PER_PAGE]

    lines = [f"📍 *Nearest trucks to {label}*"]
    if pages > 1:
        lines[0] += f"  (page {page + 1}/{pages})"
    lines.append("")
    for i, t in enumerate(chunk, start=start + 1):
        g = t["gps"]
        spd = g.get("speedMilesPerHour") or 0
        state = f"🟢 {round(spd)} mph" if spd >= 1 else "🅿️ parked"
        age = _ago(g.get("time", ""))
        lines.append(f"{i}. *{t['name']}* — {t['miles']:.0f} mi · {state} · fix {age}")

    # Tap-to-/where row for this page's trucks.
    rows = [[InlineKeyboardButton(t["name"], callback_data=f"where:{t['name']}") for t in chunk]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"near:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"near:{page + 1}"))
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def nearest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/nearest <place|address> — list the closest trucks."""
    if not context.args:
        await update.message.reply_text(
            "Usage: `/nearest <place or address>` — e.g. `/nearest pa-il` or "
            "`/nearest 500 Main St, Chicago`",
            parse_mode="Markdown",
        )
        return
    query = " ".join(context.args)
    chat = update.effective_chat
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        # If several saved locations match the text, let the user pick one
        # (tapping routes to nearest:<address_id>) rather than guessing.
        saved = await asyncio.to_thread(geofence.search_addresses, query, 10)
        named = [a for a in saved if query.strip().lower() in a["name"].lower()]
        if len(named) > 1:
            rows = [
                [InlineKeyboardButton(a["name"], callback_data=f"nearloc:{a['id']}")]
                for a in named
            ]
            await chat.send_message(
                f"📍 *{len(named)} locations match “{query}”* — nearest trucks to which?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        loc = await asyncio.to_thread(geofence.resolve_location, query)
        if loc is None:
            await chat.send_message(
                f"Couldn't find a saved location or address for *{query}*.",
                parse_mode="Markdown",
            )
            return
        await _send_nearest(chat, context, loc[0], loc[1], loc[2])
    except requests.HTTPError as exc:
        log.exception("Samsara API error")
        await chat.send_message(f"⚠️ Samsara API error: {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error")
        await chat.send_message(f"⚠️ Something went wrong: {exc}")


async def _send_nearest(chat, context, lat: float, lon: float, label: str) -> None:
    """Compute nearest trucks for a point, stash for paging, and send page 1."""
    trucks = await asyncio.to_thread(geofence.nearest_trucks, lat, lon)
    if not trucks:
        await chat.send_message("No trucks with GPS data right now.")
        return
    short = label.split(",")[0]
    context.chat_data["nearest"] = {"label": short, "trucks": trucks}
    text, kb = _nearest_view(short, trucks, 0)
    await chat.send_message(text, parse_mode="Markdown", reply_markup=kb)


async def on_nearest_loc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a tapped location when /nearest text was ambiguous."""
    query = update.callback_query
    await query.answer()
    address_id = query.data.split(":", 1)[1]
    addr = await asyncio.to_thread(geofence.address_by_id, address_id)
    if addr is None:
        await query.edit_message_text("That location no longer exists.")
        return
    lat, lon = geofence._polygon_center(addr["verts"])
    await _send_nearest(update.effective_chat, context, lat, lon, addr["name"])


async def on_nearest_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Prev/Next paging in a /nearest result."""
    query = update.callback_query
    await query.answer()
    state = context.chat_data.get("nearest")
    if not state:
        await query.edit_message_text("This result expired — run /nearest again.")
        return
    page = int(query.data.split(":", 1)[1])
    text, kb = _nearest_view(state["label"], state["trucks"], page)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def inline_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline mode: typing '@bot pa-il' shows a live filtered location list."""
    text = update.inline_query.query
    matches = await asyncio.to_thread(geofence.search_addresses, text, 20)
    results = [
        InlineQueryResultArticle(
            id=a["id"],
            title=a["name"],
            description=a.get("formatted") or "Tap to see units on-site",
            input_message_content=InputTextMessageContent(f"/site {a['name']}"),
        )
        for a in matches
    ]
    await update.inline_query.answer(results, cache_time=10, is_personal=True)


# --------------------------------------------------------------------------- #
# Fleet-event monitor (background loop)
# --------------------------------------------------------------------------- #

_STATE_FILE = "monitor_state.json"


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        log.exception("Could not save monitor state")


async def _monitor_loop(app: Application) -> None:
    """Poll the fleet on an interval and post change-events to the group.

    Always tracks state so toggling alerts off never causes a backlog burst when
    they're turned back on; it just suppresses *posting* while paused.
    """
    state = _load_state()
    log.info(
        "Monitor running: chat=%s interval=%ss (%d trucks in saved state)",
        MONITOR_CHAT_ID, MONITOR_INTERVAL_S, len(state),
    )
    first = True
    while True:
        try:
            trucks = await asyncio.to_thread(monitor.snapshot_trucks)
            messages, state = monitor.diff_events(state, trucks)
            _save_state(state)
            if first:
                # Heartbeat so the group knows the monitor is live, even during
                # quiet periods. The first cycle only seeds state (no events).
                first = False
                moving = sum(1 for t in trucks if t["speed"] >= monitor.MOVING_MPH)
                await app.bot.send_message(
                    MONITOR_CHAT_ID,
                    f"📡 *Fleet monitor online* — watching {len(trucks)} trucks "
                    f"({moving} moving, {len(trucks) - moving} parked).\n"
                    f"I'll post arrivals, departures, stops, starts, and long idles.",
                    parse_mode="Markdown",
                )
            if app.bot_data.get("monitor_on", True):
                for msg in messages:
                    await app.bot.send_message(MONITOR_CHAT_ID, msg, parse_mode="Markdown")
                    await asyncio.sleep(0.3)  # gentle pacing if many events at once
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("Monitor cycle failed; will retry")
        await asyncio.sleep(MONITOR_INTERVAL_S)


async def monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/monitor [on|off|status] — pause or resume fleet-event alerts."""
    if not MONITOR_CHAT_ID:
        await update.message.reply_text(
            "The monitor isn't configured. Run /here and set `MONITOR_CHAT_ID` first.",
            parse_mode="Markdown",
        )
        return
    arg = (context.args[0].lower() if context.args else "status")
    bot_data = context.application.bot_data
    if arg == "on":
        bot_data["monitor_on"] = True
        await update.message.reply_text("✅ Fleet alerts *on*.", parse_mode="Markdown")
    elif arg == "off":
        bot_data["monitor_on"] = False
        await update.message.reply_text(
            "🔇 Fleet alerts *off*. Still tracking — /monitor on to resume.",
            parse_mode="Markdown",
        )
    else:
        on = bot_data.get("monitor_on", True)
        await update.message.reply_text(
            f"Fleet alerts are *{'on' if on else 'off'}*.\nUse `/monitor on` or `/monitor off`.",
            parse_mode="Markdown",
        )


async def _post_init(app: Application) -> None:
    """Register the command menu. (Monitor loop disabled for now.)"""
    await app.bot.set_my_commands(
        [
            BotCommand("where", "Locate a truck or trailer, e.g. /where 797"),
            BotCommand("site", "Find a location by name, e.g. /site pa-il"),
            BotCommand("sites", "Browse all saved locations"),
            BotCommand("nearest", "Closest trucks to a place, e.g. /nearest pa-il"),
            BotCommand("statusall", "Full fleet status — all trucks & trailers"),
            BotCommand("start", "How to use this bot"),
        ]
    )
    # Fleet-event monitor disabled for now. To re-enable: restore the /status,
    # /monitor, /here menu entries and the handler registrations in main(), then:
    #   if MONITOR_CHAT_ID:
    #       app.create_task(_monitor_loop(app))
    log.info("Monitor disabled.")


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
        def _ok(self, body: bool = True):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            if body:
                self.wfile.write(b"ok")

        def do_GET(self):  # noqa: N802
            self._ok()

        def do_HEAD(self):  # noqa: N802 — UptimeRobot default; avoid 501s
            self._ok(body=False)

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
    # Monitor commands disabled for now (handlers kept above; re-add to enable):
    # app.add_handler(CommandHandler("here", here))
    # app.add_handler(CommandHandler("monitor", monitor_cmd))
    # app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("statusall", statusall))
    app.add_handler(CommandHandler("where", where))
    app.add_handler(CommandHandler("site", site))
    app.add_handler(CommandHandler("sites", sites))
    app.add_handler(CommandHandler("nearest", nearest))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern=r"^where:"))
    app.add_handler(CallbackQueryHandler(on_sites_page, pattern=r"^sites:"))
    app.add_handler(CallbackQueryHandler(on_site_button, pattern=r"^site:"))
    app.add_handler(CallbackQueryHandler(on_nearest_page, pattern=r"^near:"))
    app.add_handler(CallbackQueryHandler(on_nearest_loc, pattern=r"^nearloc:"))
    app.add_handler(InlineQueryHandler(inline_sites))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text))
    log.info("Bot started. Send /where <unit number> in Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()
