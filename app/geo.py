"""
Rebuy — geospatial primitives.

Pure standard library, no PostGIS. Shop footprints are small (a branch is tens
to hundreds of metres), so an equirectangular projection around the shop is
accurate to well under a metre over that radius and far cheaper than haversine
in the hot path. Haversine is used for anything spanning longer distances.

Everything here is deliberately side-effect free so it can be unit tested and,
more importantly, mirrored in the native apps.
"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres. Used for reporting and long distances."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def local_xy_m(lat: float, lng: float, ref_lat: float, ref_lng: float) -> tuple[float, float]:
    """Metres east/north of a reference point (equirectangular, local only)."""
    x = math.radians(lng - ref_lng) * math.cos(math.radians((lat + ref_lat) / 2)) * EARTH_RADIUS_M
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def distance_m(lat: float, lng: float, shop_lat: float, shop_lng: float) -> float:
    """Distance between a device fix and a shop, in metres."""
    x, y = local_xy_m(lat, lng, shop_lat, shop_lng)
    return math.hypot(x, y)


def inside(lat: float, lng: float, shop_lat: float, shop_lng: float, radius_m: float) -> bool:
    return distance_m(lat, lng, shop_lat, shop_lng) <= float(radius_m)


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Compass bearing from point 1 to point 2, 0 = north, 90 = east."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def approach_direction(prev_lat: float, prev_lng: float, lat: float, lng: float,
                       shop_lat: float, shop_lng: float) -> str:
    """Is the customer getting closer, moving away, or roughly stationary?

    Decided by comparing the previous and current distance to the shop, with a
    small dead-band so GPS jitter does not read as movement.
    """
    before = distance_m(prev_lat, prev_lng, shop_lat, shop_lng)
    now = distance_m(lat, lng, shop_lat, shop_lng)
    if now < before - 15:
        return "approaching"
    if now > before + 15:
        return "leaving"
    return "nearby"


def eta_minutes(distance: float, speed_mps: float | None = None, walk_mps: float = 1.35) -> int:
    """Rough walking ETA. Used for the '2 minutes away' line in a nudge."""
    speed = speed_mps if (speed_mps and speed_mps > 0.4) else walk_mps
    return max(1, int(round(distance / speed / 60)))


def walking_minutes(distance: float) -> int:
    return eta_minutes(distance, walk_mps=1.35)


def describe_distance(m: float) -> str:
    if m < 60:
        return "right outside"
    if m < 250:
        return "a minute away"
    if m < 700:
        return f"{int(round(m / 75))} minutes away"   # ~75 m per walking minute
    return f"{m / 1000:.1f} km away"


def bbox(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    """Cheap bounding box for a pre-filter before exact distance tests."""
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlng = math.degrees(radius_m / (EARTH_RADIUS_M * max(0.01, math.cos(math.radians(lat)))))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def parse_fix(body: dict) -> dict:
    """Normalise a location fix coming from a native client.

    Rejects anything outside valid ranges rather than clamping it: a client that
    sends lat=999 is broken and should hear about it, not get a silent nudge
    to the Gulf of Guinea.
    """
    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
    except (TypeError, ValueError):
        raise ValueError("lat and lng are required numbers")
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        raise ValueError("lat/lng out of range")
    if lat == 0 and lng == 0:
        raise ValueError("lat/lng is the null island — no fix yet")
    # Clients send "accuracy" (the browser/OS field name); some send "accuracy_m".
    # Accept either. Getting this wrong is not cosmetic: accuracy defaults to 0,
    # which reads as a perfect fix and disables the coarse-fix guard entirely.
    try:
        acc = float(body.get("accuracy_m") if body.get("accuracy_m") not in (None, "")
                    else (body.get("accuracy") or 0))
    except (TypeError, ValueError):
        acc = 0.0
    return {
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "accuracy_m": max(0.0, min(acc, 5000.0)),
        "ts": body.get("ts") or "",
        "speed_mps": _num(body.get("speed_mps")),
        "heading": _num(body.get("heading")),
    }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trust_fix(fix: dict, last_seen_at: str = "") -> tuple[bool, str]:
    """Decide whether a fix is good enough to act on.

    Acting on a 2 km-accuracy cell-tower fix would fire a nudge for someone in a
    different neighbourhood, so coarse fixes are recorded but not used.
    """
    if fix["accuracy_m"] and fix["accuracy_m"] > 250:
        return False, "fix too imprecise (%.0f m)" % fix["accuracy_m"]
    if fix["lat"] == 0 and fix["lng"] == 0:
        return False, "no fix"
    return True, ""
