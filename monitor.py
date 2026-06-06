"""
Fleet-event monitor: turns successive GPS snapshots into human events.

Every cycle the bot fetches all trucks' GPS once (the bulk feed — one request
for the whole fleet) and calls `diff_events()` with the prior state. The diff
engine decides what changed and returns a list of messages to post to the group,
plus the new state to persist.

Design notes
------------
- Stateless functions + an explicit state dict so it's unit-testable and the
  state can be saved to JSON between restarts.
- Stop/start uses *dwell filtering*: a truck must be stopped for STOP_DWELL_S
  before we announce "stopped", and moving for MOVE_DWELL_S before "moving".
  This kills the red-light / brief-slowdown noise.
- Geofence arrive/depart fires immediately (it's inherently low-noise and the
  most useful signal).
- Long-idle fires once when a stopped truck crosses IDLE_ALERT_S.
"""

import time

import geofence

MOVING_MPH = 5          # at/above this = "moving"
STOP_DWELL_S = 300      # must be stopped 5 min before we announce a stop
MOVE_DWELL_S = 120      # must be moving 2 min before we announce a start
IDLE_ALERT_S = 1800     # announce a long-idle once at 30 min stopped


def _now() -> float:
    return time.time()


def snapshot_trucks() -> list[dict]:
    """One bulk fetch of every truck's GPS + its current geofence (if any).

    Returns [{name, lat, lon, speed, address, place}] — `place` is the saved
    location name the truck is inside, or None.
    """
    from where_bot import SAMSARA_BASE, HEADERS  # lazy import to avoid a cycle
    import requests

    resp = requests.get(
        SAMSARA_BASE + "/fleet/vehicles/stats",
        headers=HEADERS,
        params={"types": "gps"},
        timeout=30,
    )
    resp.raise_for_status()
    out = []
    for item in resp.json().get("data", []):
        g = item.get("gps")
        if not g or g.get("latitude") is None:
            continue
        lat, lon = g["latitude"], g["longitude"]
        out.append(
            {
                "name": str(item.get("name", "")),
                "lat": lat,
                "lon": lon,
                "speed": g.get("speedMilesPerHour") or 0,
                "address": (g.get("reverseGeo") or {}).get("formattedLocation", ""),
                "place": geofence.place_for_point(lat, lon),  # uses cached geofences
            }
        )
    return out


def fleet_summary(trucks: list[dict]) -> str:
    """A point-in-time overview: counts moving/parked and trucks per location."""
    total = len(trucks)
    moving = [t for t in trucks if t["speed"] >= MOVING_MPH]
    parked = [t for t in trucks if t["speed"] < MOVING_MPH]

    # Group by saved location (trucks not at any saved place are bucketed apart).
    by_place: dict[str, list[str]] = {}
    for t in trucks:
        if t["place"]:
            by_place.setdefault(t["place"], []).append(t["name"])

    lines = [
        f"🚚 *Fleet status* — {total} trucks",
        f"🟢 Moving: {len(moving)}   🅿️ Parked: {len(parked)}",
    ]
    if by_place:
        lines.append("")
        lines.append("*At saved locations:*")
        for place in sorted(by_place):
            names = sorted(by_place[place])
            lines.append(f"📍 {place} — {', '.join(names)}")
    on_road = total - sum(len(v) for v in by_place.values())
    lines.append("")
    lines.append(f"🛣️ {on_road} truck(s) not at a saved location.")
    return "\n".join(lines)


def diff_events(prev: dict, trucks: list[dict], now: float | None = None) -> tuple[list[str], dict]:
    """
    Compare this snapshot against prior per-truck state.

    `prev` maps truck name -> {
        moving, place, since (ts of current motion-state), pending (None|'stop'|'move'),
        pending_since, idle_alerted
    }
    Returns (messages, new_state).
    """
    now = now or _now()
    new_state: dict = {}
    messages: list[str] = []

    for t in trucks:
        name = t["name"]
        moving = t["speed"] >= MOVING_MPH
        place = t["place"]
        addr = t["address"] or "unknown location"
        p = prev.get(name)

        # First time we've seen this truck — seed state silently, no event.
        if p is None:
            new_state[name] = {
                "moving": moving,
                "place": place,
                "since": now,
                "pending": None,
                "pending_since": now,
                "idle_alerted": False,
            }
            continue

        s = dict(p)  # carry prior state forward, then mutate

        # --- Geofence arrive / depart (immediate) ---
        if place != p.get("place"):
            if place:
                messages.append(f"🟢 *{name}* arrived at *{place}*")
            elif p.get("place"):
                messages.append(f"🔵 *{name}* departed *{p['place']}*")
            s["place"] = place

        # --- Stop / start with dwell filtering ---
        if moving != p["moving"]:
            # Motion state flipped vs the last *confirmed* state — start (or
            # restart) a pending timer for the opposite event.
            want = "move" if moving else "stop"
            if s.get("pending") != want:
                s["pending"] = want
                s["pending_since"] = now
        else:
            # Back to the confirmed state before the pending one matured — cancel.
            if (s.get("pending") == "move") != moving:
                s["pending"] = None

        # Mature a pending change once it has dwelled long enough.
        if s.get("pending") == "stop" and (now - s["pending_since"]) >= STOP_DWELL_S:
            where = f"*{place}*" if place else addr
            messages.append(f"🛑 *{name}* stopped at {where}")
            s["moving"] = False
            s["since"] = now
            s["pending"] = None
            s["idle_alerted"] = False
        elif s.get("pending") == "move" and (now - s["pending_since"]) >= MOVE_DWELL_S:
            messages.append(f"🚚 *{name}* started moving")
            s["moving"] = True
            s["since"] = now
            s["pending"] = None
            s["idle_alerted"] = False

        # --- Long-idle alert (fires once) ---
        if not s["moving"] and not s.get("idle_alerted"):
            if (now - s["since"]) >= IDLE_ALERT_S:
                mins = round((now - s["since"]) / 60)
                where = f"*{place}*" if place else addr
                messages.append(f"⏳ *{name}* idle {mins} min at {where}")
                s["idle_alerted"] = True

        new_state[name] = s

    return messages, new_state
