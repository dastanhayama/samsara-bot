"""
Saved-location (Samsara "addresses") support for the where-bot.

Samsara "addresses" are the saved places in the dashboard — receivers, shippers,
yards (e.g. "PA-IL JbHunt Receiver"). Each has a polygon geofence. This module:

- caches the address roster (name + geofence polygon),
- tests whether a GPS point falls inside a geofence (point-in-polygon),
- given an address, lists the units (trailers + trucks) currently on-site,
- given a point, names the saved place that contains it (powers /where).

The HTTP pager (`_get_all`) and the "time ago" helper (`_ago`) live in
where_bot.py; they're imported lazily inside functions to avoid an import cycle
(where_bot imports geofence at module load).
"""

import difflib
import math
import time

# Truck↔trailer pairing thresholds (tuned against live fleet data):
# true couplings sit within ~120 m even at highway speed, while the next-nearest
# trailer is typically tens of km away. We require the nearest to be close AND
# clearly separated from the runner-up, so clustered yards don't false-pair.
_PAIR_NEAR_M = 175  # nearest trailer must be within this to be a candidate
_PAIR_GAP_M = 800   # 2nd-nearest must be at least this much farther than nearest

_CACHE_TTL = 300  # seconds; refresh the address roster at most every 5 minutes

# Each entry: {"id", "name", "formatted", "verts": [(lat, lon), ...]}
_addresses: list[dict] = []
_addr_at: float = 0.0


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def point_in_polygon(lat: float, lon: float, verts: list[tuple[float, float]]) -> bool:
    """Ray-casting test. verts is a list of (lat, lon) tuples."""
    inside = False
    n = len(verts)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        yi, xi = verts[i]
        yj, xj = verts[j]
        if ((xi > lon) != (xj > lon)) and (lat < (yj - yi) * (lon - xi) / (xj - xi) + yi):
            inside = not inside
        j = i
    return inside


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# Address cache
# --------------------------------------------------------------------------- #

def _refresh_addresses() -> None:
    global _addresses, _addr_at
    from where_bot import _get_all  # lazy import to avoid a cycle

    out: list[dict] = []
    for a in _get_all("/addresses"):
        poly = (a.get("geofence") or {}).get("polygon")
        if not poly:
            continue  # only polygon geofences are usable for on-site checks
        verts = [(v["latitude"], v["longitude"]) for v in poly.get("vertices", [])]
        if len(verts) < 3:
            continue
        out.append(
            {
                "id": str(a["id"]),
                "name": str(a.get("name", "")).strip(),
                "formatted": str(a.get("formattedAddress", "")).strip(),
                "verts": verts,
            }
        )
    _addresses, _addr_at = out, time.monotonic()


def _ensure_addresses() -> None:
    if not _addresses or (time.monotonic() - _addr_at) > _CACHE_TTL:
        _refresh_addresses()


def all_addresses() -> list[dict]:
    """Full cached address list (used by /sites menu and inline mode)."""
    _ensure_addresses()
    return _addresses


def address_by_id(address_id: str) -> dict | None:
    _ensure_addresses()
    return next((a for a in _addresses if a["id"] == str(address_id)), None)


def search_addresses(text: str, limit: int = 8) -> list[dict]:
    """Match saved locations by substring first, then fuzzy on the remainder."""
    _ensure_addresses()
    q = text.strip().lower()
    if not q:
        return _addresses[:limit]

    subs = [a for a in _addresses if q in a["name"].lower()]
    if len(subs) >= limit:
        return subs[:limit]

    # Fill remaining slots with fuzzy matches not already in the substring hits.
    seen = {a["id"] for a in subs}
    names = [a["name"] for a in _addresses if a["id"] not in seen]
    close = difflib.get_close_matches(text, names, n=limit, cutoff=0.4)
    by_name = {a["name"]: a for a in _addresses if a["id"] not in seen}
    fuzzy = [by_name[n] for n in close if n in by_name]
    return (subs + fuzzy)[:limit]


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #

def place_for_point(lat: float | None, lon: float | None) -> str | None:
    """Name of the first saved location whose geofence contains the point."""
    if lat is None or lon is None:
        return None
    _ensure_addresses()
    for a in _addresses:
        if point_in_polygon(lat, lon, a["verts"]):
            return a["name"]
    return None


def _unit_gps(kinds: tuple[str, ...] = ("trailer", "vehicle")) -> list[tuple[str, str, dict]]:
    """Return (name, kind, gps) for the requested unit kinds (those with a fix)."""
    from where_bot import SAMSARA_BASE, HEADERS  # lazy import
    import requests

    paths = {"trailer": "/fleet/trailers/stats", "vehicle": "/fleet/vehicles/stats"}
    out: list[tuple[str, str, dict]] = []
    for kind in kinds:
        resp = requests.get(
            SAMSARA_BASE + paths[kind], headers=HEADERS, params={"types": "gps"}, timeout=30
        )
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            gps = item.get("gps")
            if gps and gps.get("latitude") is not None:
                out.append((str(item.get("name", "")), kind, gps))
    return out


def _all_unit_gps() -> list[tuple[str, str, dict]]:
    """Return (name, kind, gps) for every trailer and vehicle with a GPS fix."""
    return _unit_gps(("trailer", "vehicle"))


def trailer_for_truck(lat: float | None, lon: float | None) -> dict | None:
    """
    Infer which trailer a truck is pulling, from GPS proximity. Samsara has no
    trailer-assignment data in this org, so we pick the nearest trailer — but
    only when it's both close (<= _PAIR_NEAR_M) AND clearly separated from the
    next-nearest (so clustered yards don't false-pair).

    Returns {"name", "distance_m", "gps", "confident"} for the nearest trailer,
    or None if there are no trailers with a fix. `confident` is False when the
    match is ambiguous or far (caller should treat that as "couldn't confirm").
    """
    if lat is None or lon is None:
        return None
    trailers = _unit_gps(("trailer",))
    if not trailers:
        return None

    ranked = sorted(
        (
            (haversine_m(lat, lon, g["latitude"], g["longitude"]), name, g)
            for name, _kind, g in trailers
        ),
        key=lambda x: x[0],
    )
    d1, name1, gps1 = ranked[0]
    d2 = ranked[1][0] if len(ranked) > 1 else float("inf")
    confident = d1 <= _PAIR_NEAR_M and (d2 - d1) >= _PAIR_GAP_M
    return {"name": name1, "distance_m": d1, "gps": gps1, "confident": confident}


def units_on_site(address_id: str) -> tuple[dict | None, list[tuple[str, str, dict]]]:
    """
    Return (address, units_on_site) where units is a list of (name, kind, gps)
    whose last known position is inside that address's geofence. Includes stale
    fixes — the caller labels them with age.
    """
    addr = address_by_id(address_id)
    if addr is None:
        return None, []
    verts = addr["verts"]
    on_site = [
        (name, kind, gps)
        for name, kind, gps in _all_unit_gps()
        if point_in_polygon(gps["latitude"], gps["longitude"], verts)
    ]
    # Trailers first, then trucks; within each, by unit name.
    on_site.sort(key=lambda u: (u[1] != "trailer", u[0]))
    return addr, on_site


# --------------------------------------------------------------------------- #
# Geocoding + nearest-trucks (for /nearest)
# --------------------------------------------------------------------------- #

_M_PER_MILE = 1609.344


def geocode(text: str) -> tuple[float, float, str] | None:
    """Free-text address -> (lat, lon, display_name) via OpenStreetMap Nominatim.

    No API key; Nominatim's usage policy requires a descriptive User-Agent and
    light use (this is one call per /nearest), which we satisfy.
    """
    import requests

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": text, "format": "json", "limit": 1},
            headers={"User-Agent": "samsara-where-bot/1.0 (fleet dispatch tool)"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None
    if not data:
        return None
    top = data[0]
    return float(top["lat"]), float(top["lon"]), top.get("display_name", text)


def _polygon_center(verts: list[tuple[float, float]]) -> tuple[float, float]:
    """Average of polygon vertices — good enough as a geofence center point."""
    return (sum(v[0] for v in verts) / len(verts), sum(v[1] for v in verts) / len(verts))


def resolve_location(text: str) -> tuple[float, float, str] | None:
    """Turn user text into (lat, lon, label).

    Saved Samsara locations win (exact-ish fuzzy match); otherwise fall back to
    geocoding the free text. Returns None if neither resolves.
    """
    matches = search_addresses(text, 1)
    # Only treat a saved location as the answer if the query actually appears in
    # its name (avoids a loose fuzzy hit hijacking a real street address).
    if matches and text.strip().lower() in matches[0]["name"].lower():
        a = matches[0]
        lat, lon = _polygon_center(a["verts"])
        return lat, lon, a["name"]
    geo = geocode(text)
    if geo:
        return geo
    # Last resort: any fuzzy saved-location match.
    if matches:
        a = matches[0]
        lat, lon = _polygon_center(a["verts"])
        return lat, lon, a["name"]
    return None


# Trucks whose last GPS fix is older than this are excluded from /nearest —
# a truck that hasn't reported in half a day isn't a real dispatch candidate.
_NEAREST_MAX_FIX_AGE_S = 12 * 3600


def _fix_age_seconds(gps: dict) -> float:
    from datetime import datetime, timezone

    t = gps.get("time", "")
    try:
        then = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds()


def nearest_trucks(lat: float, lon: float, max_fix_age_s: float = _NEAREST_MAX_FIX_AGE_S) -> list[dict]:
    """Trucks ranked by distance from (lat, lon), nearest first.

    Excludes trucks whose last fix is older than max_fix_age_s so stale,
    long-parked positions don't masquerade as nearby. Returns dicts with
    {"name", "miles", "gps"}.
    """
    trucks = _unit_gps(("vehicle",))
    ranked = [
        {
            "name": name,
            "miles": haversine_m(lat, lon, g["latitude"], g["longitude"]) / _M_PER_MILE,
            "gps": g,
        }
        for name, _kind, g in trucks
        if _fix_age_seconds(g) <= max_fix_age_s
    ]
    ranked.sort(key=lambda r: r["miles"])
    return ranked
