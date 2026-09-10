"""Google Places API (New). Geocodes the own project, finds residential projects nearby, names the nearest metro."""
from __future__ import annotations

from app.config import settings
from app.logic.geo import haversine_km
from app.models.schema import Candidate, Page, Project
from app.sources.base import NullSource, ScanContext

TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
RESIDENTIAL_TYPES = ["apartment_building", "apartment_complex", "housing_complex", "condominium_complex"]
FIELDS = "places.id,places.displayName,places.formattedAddress,places.location,places.types,places.primaryType"


def _headers() -> dict:
    return {"X-Goog-Api-Key": settings.google_maps_api_key or "", "X-Goog-FieldMask": FIELDS, "Content-Type": "application/json"}


async def _post(ctx: ScanContext, url: str, body: dict) -> dict:
    """Every Places call goes through here, because an error body parses fine.

    `{"error": {...}}` has no "places" key, so `.get("places") or []` turned a bad key
    into "no buildings near this project" and nobody could tell the two apart. Places
    is the only adapter that never checked; the rest have always read res.ok.
    """
    res = await ctx.fetcher.post(url, json_body=body, headers=_headers())
    if not res.ok:
        detail = ""
        try:
            detail = (res.json().get("error") or {}).get("message") or ""
        except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
            detail = res.text[:200]
        raise RuntimeError(f"places {url.rsplit('/', 1)[-1]} returned {res.status}: {detail}")
    return res.json()


class PlacesSource(NullSource):
    name = "places"

    async def geocode(self, ctx: ScanContext, query: str) -> tuple[float, float, str] | None:
        places = (await _post(ctx, TEXT_URL, {"textQuery": query, "pageSize": 1})).get("places") or []
        if not places:
            return None
        loc = places[0]["location"]
        return loc["latitude"], loc["longitude"], places[0].get("formattedAddress", "")

    async def nearest_metro(self, ctx: ScanContext, lat: float, lng: float) -> str | None:
        body = {"includedTypes": ["subway_station", "transit_station"], "maxResultCount": 3,
                "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 3000}},
                "rankPreference": "DISTANCE"}
        places = (await _post(ctx, NEARBY_URL, body)).get("places") or []
        if not places:
            return None
        p = places[0]
        d = haversine_km(lat, lng, p["location"]["latitude"], p["location"]["longitude"])
        return f"{p['displayName']['text']} · {d:.1f} km"

    async def discover(self, ctx: ScanContext) -> list[Candidate]:
        own = ctx.own
        if own.lat is None:
            return []
        out: list[Candidate] = []
        seen: set[str] = set()
        radius_m = int(ctx.radius_km * 1000)

        body = {"includedTypes": RESIDENTIAL_TYPES, "maxResultCount": 20,
                "locationRestriction": {"circle": {"center": {"latitude": own.lat, "longitude": own.lng}, "radius": radius_m}}}
        for p in (await _post(ctx, NEARBY_URL, body)).get("places") or []:
            self._add(p, out, seen)

        for q in [f"under construction residential project {own.locality or own.address}", f"new launch apartments {own.locality or own.address}"]:
            body = {"textQuery": q, "pageSize": 20,
                    "locationBias": {"circle": {"center": {"latitude": own.lat, "longitude": own.lng}, "radius": radius_m}}}
            for p in (await _post(ctx, TEXT_URL, body)).get("places") or []:
                self._add(p, out, seen)
        return out

    @staticmethod
    def _add(p: dict, out: list[Candidate], seen: set[str]) -> None:
        pid = p.get("id")
        if not pid or pid in seen:
            return
        seen.add(pid)
        types = [t for t in ([p.get("primaryType")] + (p.get("types") or [])) if t]
        out.append(Candidate(name=p["displayName"]["text"], lat=p["location"]["latitude"], lng=p["location"]["longitude"],
                             address=p.get("formattedAddress"), source="places", place_types=types,
                             source_url=f"https://www.google.com/maps/place/?q=place_id:{pid}"))

    async def pages_for(self, project: Project, ctx: ScanContext) -> list[Page]:
        return []  # Places has coordinates, not facts
