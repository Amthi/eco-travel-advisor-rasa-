"""
Custom actions for the Rasa Eco-Travel Advisor.

  - ActionAskDestination            : quick-reply buttons built from the data set
  - ValidateTripIntakeForm          : validates/normalises form slots
  - ActionCalculateCarbonFootprint  : Climatiq (if key set) + built-in estimator
  - ActionFetchTravelOptions        : curated eco hotels + Amadeus hotels/flights
                                      (fetched in parallel, token cached)
  - ActionShowExperiences           : curated community-based experiences
  - ActionShowOffsets               : carbon-offset programmes + indicative cost
  - ActionRankOptions               : weighted score (carbon, price, preference)
                                      + lowest-carbon transport + trip total
  - ActionHandoverToHuman           : packages full context for a human advisor
                                      (only runs AFTER the user consents)
  - ActionDefaultFallback           : two-stage clarification -> consent
  - ActionAskHandoverConsent / ActionHandleAffirm / ActionHandleDeny :
                                      consent-gated human handover

Every external call is wrapped in try/except and degrades to a local
fallback, so the bot never crashes and never returns an empty answer.
"""

import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Text, Tuple

import requests
from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet

try:  # optional: load keys from .env when running locally
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

CLIMATIQ_API_KEY = os.environ.get("CLIMATIQ_API_KEY", "")
CLIMATIQ_TRAVEL_URL = "https://api.climatiq.io/travel/v1/distance"

AMADEUS_API_KEY = os.environ.get("AMADEUS_API_KEY", "")
AMADEUS_API_SECRET = os.environ.get("AMADEUS_API_SECRET", "")
AMADEUS_TOKEN_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_HOTELS_BY_CITY_URL = (
    "https://test.api.amadeus.com/v1/reference-data/locations/hotels/by-city"
)

AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

OPENCAGE_API_KEY = os.environ.get("OPENCAGE_API_KEY", "")
OPENCAGE_URL = "https://api.opencagedata.com/geocode/v1/json"

HANDOVER_ENDPOINT = os.environ.get("HUMAN_ADVISOR_ENDPOINT", "")
HANDOVER_LOG_DIR = os.environ.get(
    "HANDOVER_LOG_DIR", os.path.join(os.path.dirname(__file__), "..", "handovers")
)
DEFAULT_ORIGIN = os.environ.get("DEFAULT_ORIGIN", "Berlin")

REQUEST_TIMEOUT_SECONDS = 2.0  # per call; calls run in parallel (3 s target)

CURATED_ECO_HOTELS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "curated_eco_hotels.json"
)
CURATED_EXPERIENCES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "curated_experiences.json"
)

# --------------------------------------------------------------------------
# Built-in reference data (used when no API key / API failure)
# --------------------------------------------------------------------------
# name -> (lat, lon, region, amadeus city code)
CITIES: Dict[str, Tuple[float, float, str, str]] = {
    "berlin": (52.52, 13.405, "europe", "BER"),
    "hamburg": (53.551, 9.994, "europe", "HAM"),
    "munich": (48.137, 11.575, "europe", "MUC"),
    "lisbon": (38.722, -9.139, "europe", "LIS"),
    "rome": (41.903, 12.496, "europe", "ROM"),
    "paris": (48.857, 2.352, "europe", "PAR"),
    "london": (51.507, -0.128, "europe", "LON"),
    "amsterdam": (52.368, 4.904, "europe", "AMS"),
    "vienna": (48.208, 16.373, "europe", "VIE"),
    "barcelona": (41.385, 2.173, "europe", "BCN"),
    "madrid": (40.417, -3.704, "europe", "MAD"),
    "prague": (50.075, 14.438, "europe", "PRG"),
    "scottish highlands": (57.48, -4.22, "europe", "INV"),
    "iceland": (64.147, -21.942, "island", "REK"),
    "kyoto": (35.012, 135.768, "asia", "UKY"),
    "bali": (-8.409, 115.189, "island", "DPS"),
    "costa rica": (9.928, -84.091, "central_america", "SJO"),
    "new york": (40.713, -74.006, "north_america", "NYC"),
}

# kg CO2e per passenger-km (approximate, DEFRA-style factors; air incl. RF)
RAIL_FACTOR = 0.035
COACH_FACTOR = 0.027
CAR_FACTOR = 0.170

MODE_LABELS = {
    "flight": "Flight",
    "train": "Train",
    "coach": "Coach",
    "car": "Car (1 person)",
    "car_shared": "Car (2 people sharing)",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _safe_get(url, headers=None, params=None):
    try:
        response = requests.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json(), None
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("API call to %s failed: %s", url, exc)
        return None, str(exc)


def _safe_post(url, headers=None, data=None, json_body=None):
    try:
        response = requests.post(
            url,
            headers=headers,
            data=data,
            json=json_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json(), None
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("API call to %s failed: %s", url, exc)
        return None, str(exc)


def _normalise_place(name: Optional[str]) -> str:
    """'the Scottish Highlands ' -> 'scottish highlands'"""
    if not name:
        return ""
    cleaned = re.sub(r"\s+", " ", str(name)).strip().lower()
    cleaned = re.sub(r"^(the|a|an)\s+", "", cleaned)
    return cleaned.strip(" .,!?")


def _display_place(name: Optional[str]) -> str:
    return _normalise_place(name).title() if name else ""


# --------------------------------------------------------------------------
# Travel-date parsing / validation
# --------------------------------------------------------------------------
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_RELATIVE_WORDS = (
    "weekend", "next week", "this week", "next month", "tomorrow", "christmas",
    "easter", "new year", "summer", "winter", "spring", "autumn", "fall",
    "holiday", "half term",
)


def _parse_travel_dates(text: Optional[str], today: Optional[date] = None) -> Tuple[str, Optional[date]]:
    """Classifies free-text dates.

    Returns (status, start_date):
      status 'ok'      - plausible dates (start_date set when exact day known)
      status 'past'    - the dates are before today
      status 'invalid' - not recognisable as dates (e.g. 'abc', '1500')
    """
    today = today or date.today()
    raw = str(text or "").strip().lower()
    if not raw:
        return "invalid", None

    # ISO (2026-10-12) or 12.10.2026 / 12/10/2026
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", raw)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", raw)
        if m:
            d, mo, y = map(int, m.groups())
        else:
            y = mo = d = None
    if y:
        try:
            start = date(y, mo, d)
        except ValueError:
            return "invalid", None
        return ("past", None) if start < today else ("ok", start)

    month = next((n for w, n in _MONTHS.items() if re.search(rf"\b{w}\b", raw)), None)
    year_match = re.search(r"\b(20\d{2})\b", raw)
    year = int(year_match.group(1)) if year_match else None

    if month:
        days = [int(x) for x in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", raw) if 1 <= int(x) <= 31]
        if year is None:
            year = today.year
            probe = date(year, month, days[0] if days else 28)
            if probe < today:
                year += 1  # "3 March" said in October means next March
        if days:
            try:
                start = date(year, month, days[0])
            except ValueError:
                return "invalid", None
            return ("past", None) if start < today else ("ok", start)
        # month only ("June 2027"): plausible, but no exact start day
        if (year, month) < (today.year, today.month):
            return "past", None
        return "ok", None

    if any(w in raw for w in _RELATIVE_WORDS):
        return "ok", None
    return "invalid", None


# --------------------------------------------------------------------------
# Place resolution: built-in table first, OpenCage geocoding as extension
# --------------------------------------------------------------------------
_CONTINENT_TO_REGION = {
    "europe": "europe", "asia": "asia", "north america": "north_america",
    "south america": "south_america", "africa": "africa", "oceania": "oceania",
}
_GEOCODE_CACHE: Dict[str, Optional[Tuple[float, float, str, Optional[str]]]] = {}


def _geocode(place: str) -> Optional[Tuple[float, float, str, Optional[str]]]:
    """OpenCage forward geocoding -> (lat, lon, region, None) or None.

    Only used for places missing from CITIES and only when OPENCAGE_API_KEY
    is set. Results are cached so a conversation geocodes each place once.
    """
    key = _normalise_place(place)
    if not key or not OPENCAGE_API_KEY:
        return None
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    data, error = _safe_get(
        OPENCAGE_URL, params={"q": key, "key": OPENCAGE_API_KEY, "limit": 1, "no_annotations": 1}
    )
    result = None
    if not error and isinstance(data, dict) and data.get("results"):
        top = data["results"][0]
        geo = top.get("geometry") or {}
        continent = str((top.get("components") or {}).get("continent", "")).lower()
        if "lat" in geo and "lng" in geo:
            result = (float(geo["lat"]), float(geo["lng"]), _CONTINENT_TO_REGION.get(continent, "unknown"), None)
    _GEOCODE_CACHE[key] = result
    return result


def _place_info(name: Optional[str]) -> Optional[Tuple[float, float, str, Optional[str]]]:
    """(lat, lon, region, amadeus_city_code) for a place, or None."""
    key = _normalise_place(name)
    return CITIES.get(key) or _geocode(key)


# --------------------------------------------------------------------------
# Amadeus access token (cached for its lifetime: saves one round trip per turn)
# --------------------------------------------------------------------------
_AMADEUS_TOKEN: Dict[str, Any] = {"value": None, "expires_at": 0.0}


def _get_amadeus_token() -> Optional[str]:
    if not AMADEUS_API_KEY or not AMADEUS_API_SECRET:
        return None
    if _AMADEUS_TOKEN["value"] and time.time() < _AMADEUS_TOKEN["expires_at"] - 30:
        return _AMADEUS_TOKEN["value"]
    data, error = _safe_post(
        AMADEUS_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": AMADEUS_API_KEY,
            "client_secret": AMADEUS_API_SECRET,
        },
    )
    if error or not isinstance(data, dict) or not data.get("access_token"):
        return None
    _AMADEUS_TOKEN["value"] = data["access_token"]
    _AMADEUS_TOKEN["expires_at"] = time.time() + float(data.get("expires_in", 1700))
    return _AMADEUS_TOKEN["value"]


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _emission_category(kg_co2e: Optional[float], distance_km: Optional[float] = None) -> str:
    """low / medium / high -> green / amber / red in the UI (per passenger,
    one way). With a distance the category uses emission INTENSITY (kg per
    km), so a long train trip is not amber just because it is long. Without
    a distance the absolute thresholds below apply."""
    if kg_co2e is None:
        return "unknown"
    if distance_km:
        intensity = kg_co2e / distance_km
        if intensity < 0.06:
            return "low"
        return "medium" if intensity < 0.12 else "high"
    if kg_co2e < 50:
        return "low"
    if kg_co2e < 200:
        return "medium"
    return "high"


def _hotel_category(kg_per_night: Optional[float]) -> str:
    if kg_per_night is None:
        return "unknown"
    if kg_per_night < 10:
        return "low"
    if kg_per_night < 15:
        return "medium"
    return "high"


_MONTHS = {
    name: i
    for i, names in enumerate(
        [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
         ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
         ("november", "nov"), ("december", "dec")], 1)
    for name in names
}
_WORD_NUM = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}


def _slot_before_last_user_message(tracker: Tracker, slot: str) -> Optional[Any]:
    """Value a slot had before the latest user message. Rasa applies freshly
    extracted values to the tracker before it calls a validation action, so
    tracker.get_slot() already shows the new value there."""
    events = list(tracker.events or [])
    last_user = max((i for i, e in enumerate(events) if e.get("event") == "user"), default=-1)
    for event in reversed(events[:last_user]):
        if event.get("event") == "slot" and event.get("name") == slot:
            return event.get("value")
    return None


def _parse_nights(text: Optional[str]) -> Optional[int]:
    """Number of nights implied by the user's dates, or None if unclear.

    Understands '12-18 October 2026', '3rd to 10th of March', '28 October - 3
    November 2026', '5 nights', '10 days', '2 weeks' and 'weekend'."""
    t = (text or "").lower().strip()
    if not t:
        return None
    nights: Optional[int] = None
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?([a-z]{3,9})\s*(?:-|–|—|to|until|till)\s*"
                  r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?([a-z]{3,9})(?:\s*,?\s*(\d{4}))?", t)
    if m and m.group(2) in _MONTHS and m.group(4) in _MONTHS:
        year = int(m.group(5)) if m.group(5) else date.today().year
        try:
            start = date(year, _MONTHS[m.group(2)], int(m.group(1)))
            end = date(year, _MONTHS[m.group(4)], int(m.group(3)))
            if end < start:
                end = end.replace(year=year + 1)
            nights = (end - start).days
        except ValueError:
            nights = None
    if nights is None:
        m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|—|to|until|till)\s*(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?([a-z]{3,9})", t)
        if m and m.group(3) in _MONTHS and int(m.group(2)) > int(m.group(1)):
            nights = int(m.group(2)) - int(m.group(1))
    if nights is None:
        m = re.search(r"(\d{1,2})\s*nights?", t)
        if m:
            nights = int(m.group(1))
    if nights is None:
        m = re.search(r"(\d{1,2})\s*days?", t)
        if m:
            nights = max(int(m.group(1)) - 1, 1)
    if nights is None:
        m = re.search(r"(\d{1,2}|a|one|two|three|four)\s*weeks?", t)
        if m:
            nights = 7 * (int(m.group(1)) if m.group(1).isdigit() else _WORD_NUM[m.group(1)])
    if nights is None and "weekend" in t:
        nights = 2
    return nights if nights and 1 <= nights <= 60 else None


def _trip_nights(tracker: Tracker, default: int = 4) -> Tuple[int, bool]:
    """(nights, assumed?). Uses the user's dates; falls back to a default."""
    parsed = _parse_nights(tracker.get_slot("travel_dates"))
    return (parsed, False) if parsed else (default, True)


CO2E_TOOLTIP = (
    "ℹ️ CO2e = carbon dioxide equivalent: all greenhouse gases expressed as one "
    "number, per passenger."
)


def _verification_status(option: Dict[str, Any]) -> str:
    """'verified' | 'unverified' | 'demo'.

    Anti-greenwashing rule: a certification is only shown as verified when the
    record is NOT demo data and is flagged as certified. The bundled JSON is
    demo data, so it is always labelled as such.
    """
    if option.get("source") == "amadeus_live":
        return "unverified"
    if option.get("data_status", "demo") == "demo":
        return "demo"
    return "verified" if option.get("eco_certified") else "unverified"


def _verification_badge(option: Dict[str, Any]) -> str:
    status = _verification_status(option)
    if status == "verified":
        body = option.get("certification_body") or "eco-certified"
        return f"✅ verified: {body}"
    if status == "demo":
        return "🧪 demo data (certification NOT verified)"
    return "⚠️ certification unverified"


def _pick_transport(
    carbon_results: Optional[List[Dict[str, Any]]], preferred: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The user's preferred mode if it exists on this route, else the
    lowest-emission mode."""
    if not carbon_results:
        return None
    if preferred:
        for opt in carbon_results:
            if opt.get("mode") == preferred:
                return opt
    valid = [o for o in carbon_results if o.get("co2e_kg") is not None]
    return min(valid, key=lambda o: o["co2e_kg"]) if valid else None


def _parse_amount(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"\d[\d,\.]*", str(text))
    if not match:
        return None
    raw = match.group(0).replace(",", "")
    try:
        return float(raw.rstrip("."))
    except ValueError:
        return None


def _load_curated() -> List[Dict[str, Any]]:
    try:
        with open(CURATED_ECO_HOTELS_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load curated data: %s", exc)
        return []


def _known_destinations() -> List[str]:
    seen: List[str] = []
    for entry in _load_curated():
        dest = entry.get("destination")
        if dest and dest not in seen:
            seen.append(dest)
    return seen


def _destination_buttons() -> List[Dict[str, str]]:
    return [
        {
            "title": d,
            "payload": '/inform_destination{"destination":"%s"}' % d,
        }
        for d in _known_destinations()[:6]
    ]


# --------------------------------------------------------------------------
# Form helpers
# --------------------------------------------------------------------------
class ActionAskDestination(Action):
    """Asks for the destination with quick-reply buttons generated from the
    data set (dynamic quick replies)."""

    def name(self) -> Text:
        return "action_ask_destination"

    def run(self, dispatcher, tracker, domain):
        dispatcher.utter_message(
            text="Where would you like to travel to? Pick one or type your own:",
            buttons=_destination_buttons(),
        )
        return []


class ActionAskOrigin(Action):
    """Asks where the journey starts, with quick replies. The frontend also
    offers a 📍 button (browser geolocation -> nearest supported city)."""

    def name(self) -> Text:
        return "action_ask_origin"

    def run(self, dispatcher, tracker, domain):
        starters = ["Berlin", "Hamburg", "Munich", "London", "Paris", "Amsterdam"]
        dispatcher.utter_message(
            text=(
                "Where will you start your journey? Pick a city, type your own, "
                "or tap 📍 in the chat box to use your location:"
            ),
            buttons=[
                {"title": c, "payload": '/inform_origin{"origin":"%s"}' % c} for c in starters
            ],
        )
        return []


class ValidateTripIntakeForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_trip_intake_form"

    def validate_destination(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict
    ) -> Dict[Text, Any]:
        # A city typed while ANOTHER slot (e.g. the origin) is being asked can be
        # tagged as a destination by the NLU. Keep the destination already chosen.
        requested = tracker.get_slot("requested_slot")
        previous = _slot_before_last_user_message(tracker, "destination")
        if previous and requested and requested != "destination":
            return {"destination": previous}
        key = _normalise_place(slot_value)
        if not key or len(key) < 2:
            dispatcher.utter_message(text="I didn't catch a destination. Please try again.")
            return {"destination": None}
        if key not in CITIES:
            dispatcher.utter_message(
                text=(
                    f"I don't have verified data for '{slot_value}' yet. "
                    "Please choose one of these, or ask for a human advisor:"
                ),
                buttons=_destination_buttons()
                + [{"title": "Human advisor", "payload": "/request_human_advisor"}],
            )
            return {"destination": None}
        return {"destination": _display_place(slot_value)}

    def validate_origin(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict
    ) -> Dict[Text, Any]:
        key = _normalise_place(slot_value)
        if not key or len(key) < 2:
            dispatcher.utter_message(text="I didn't catch the starting city. Please try again.")
            return {"origin": None}
        if _place_info(key) is None:
            known = ", ".join(c.title() for c in list(CITIES)[:8])
            dispatcher.utter_message(
                text=(
                    f"I can't locate '{slot_value}'. Try a larger nearby city (for example: {known})."
                ),
                buttons=[
                    {"title": c, "payload": '/inform_origin{"origin":"%s"}' % c}
                    for c in ("Berlin", "London", "Paris")
                ],
            )
            return {"origin": None}
        return {"origin": _display_place(slot_value)}

    def validate_travel_dates(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict
    ) -> Dict[Text, Any]:
        status, _ = _parse_travel_dates(slot_value)
        if status == "past":
            dispatcher.utter_message(text="Those dates are in the past. Please give future dates, e.g. 12-18 October 2026.")
            return {"travel_dates": None}
        if status == "invalid":
            dispatcher.utter_message(
                text="I couldn't read that as dates. Try something like '12-18 October 2026', 'June 2027' or 'next weekend'."
            )
            return {"travel_dates": None}
        return {"travel_dates": str(slot_value).strip()}

    def validate_budget(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict
    ) -> Dict[Text, Any]:
        amount = _parse_amount(slot_value)
        if amount is None or amount <= 0:
            dispatcher.utter_message(text="I need a positive number for the budget, e.g. 1500 euros.")
            return {"budget": None}
        return {"budget": str(slot_value).strip()}


# --------------------------------------------------------------------------
# Carbon footprint
# --------------------------------------------------------------------------
def _local_estimates(origin: str, destination: str) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    o = _place_info(origin)
    d = _place_info(destination)
    if not o or not d:
        return None, []
    straight = _haversine_km((o[0], o[1]), (d[0], d[1]))
    results: List[Dict[str, Any]] = []

    # Flight: add ~95 km for take-off/landing; factor falls with distance.
    flight_km = straight + 95
    if flight_km < 1500:
        flight_factor = 0.255
    elif flight_km < 4000:
        flight_factor = 0.156
    else:
        flight_factor = 0.150
    results.append(
        {"mode": "flight", "distance_km": round(flight_km), "co2e_kg": round(flight_km * flight_factor, 1)}
    )

    same_land = o[2] == d[2] and o[2] in (
        "europe", "asia", "north_america", "central_america", "south_america", "africa"
    )
    road_km = straight * 1.2
    if same_land and road_km < 3500:
        results.append({"mode": "train", "distance_km": round(road_km), "co2e_kg": round(road_km * RAIL_FACTOR, 1)})
        results.append({"mode": "coach", "distance_km": round(road_km), "co2e_kg": round(road_km * COACH_FACTOR, 1)})
        results.append({"mode": "car", "distance_km": round(road_km), "co2e_kg": round(road_km * CAR_FACTOR, 1)})
        results.append({"mode": "car_shared", "distance_km": round(road_km), "co2e_kg": round(road_km * CAR_FACTOR / 2, 1)})
    return straight, results


def _climatiq_estimate(origin: str, destination: str, mode: str) -> Optional[float]:
    """Returns kg CO2e from Climatiq's travel/distance endpoint or None."""
    api_mode = {"flight": "air", "train": "rail", "car": "car"}.get(mode)
    if not CLIMATIQ_API_KEY or not api_mode:
        return None
    body = {
        "origin": {"query": origin},
        "destination": {"query": destination},
        "travel_mode": api_mode,
    }
    data, error = _safe_post(
        CLIMATIQ_TRAVEL_URL,
        headers={"Authorization": f"Bearer {CLIMATIQ_API_KEY}"},
        json_body=body,
    )
    if error or not isinstance(data, dict) or data.get("co2e") is None:
        return None
    try:
        value = float(data["co2e"])
    except (TypeError, ValueError):
        return None
    unit = str(data.get("co2e_unit", "kg")).lower()
    return value * 1000 if unit == "t" else value


class ActionCalculateCarbonFootprint(Action):
    def name(self) -> Text:
        return "action_calculate_carbon_footprint"

    def run(self, dispatcher, tracker, domain):
        started = time.perf_counter()
        destination = tracker.get_slot("destination")
        origin = tracker.get_slot("origin") or DEFAULT_ORIGIN
        preferred_mode = _normalise_place(tracker.get_slot("transport_mode"))

        if not destination:
            dispatcher.utter_message(
                text="Which destination should I calculate emissions for?",
                buttons=_destination_buttons(),
            )
            return []

        try:
            distance, options = _local_estimates(origin, destination)
        except Exception as exc:  # defensive: never crash the action server
            logger.exception("Local estimate failed: %s", exc)
            distance, options = None, []

        if not options:
            dispatcher.utter_message(
                text=(
                    f"I can't estimate the journey from {origin} to {destination} "
                    "reliably right now. I can connect you with a human advisor "
                    "for a manual estimate."
                ),
                buttons=[{"title": "Human advisor", "payload": "/request_human_advisor"}],
            )
            return []

        # Try Climatiq per mode (in parallel); keep local estimate on failure.
        source = "built-in emission factors (approximate)"
        if CLIMATIQ_API_KEY:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {
                    opt["mode"]: pool.submit(_climatiq_estimate, origin, _display_place(destination), opt["mode"])
                    for opt in options
                    if opt["mode"] in ("flight", "train", "car")
                }
                live = 0
                for opt in options:
                    fut = futures.get(opt["mode"])
                    if fut is None:
                        continue
                    try:
                        value = fut.result(timeout=REQUEST_TIMEOUT_SECONDS + 1)
                    except Exception:
                        value = None
                    if value is not None:
                        opt["co2e_kg"] = round(value, 1)
                        opt["live"] = True
                        live += 1
            if live:
                source = "Climatiq API" + (" + built-in factors for coach" if live < len(options) else "")

        for opt in options:
            opt["category"] = _emission_category(opt["co2e_kg"], opt.get("distance_km"))
            opt["label"] = MODE_LABELS.get(opt["mode"], opt["mode"].title())
        options.sort(key=lambda x: x["co2e_kg"])

        best, worst = options[0], options[-1]
        lines = [f"Estimated one-way emissions per passenger, {origin} → {_display_place(destination)} (details in the card below)."]
        alert_text = None
        highs = [o for o in options if o["category"] == "high"]
        if worst["co2e_kg"] > 0 and len(options) > 1:
            saving = round((1 - best["co2e_kg"] / worst["co2e_kg"]) * 100)
            lines.append(f"Choosing {best['label'].lower()} instead of {worst['label'].lower()} cuts about {saving}%.")
        if highs and best["category"] != "high":
            top_high = max(highs, key=lambda o: o["co2e_kg"])
            cut = round((1 - best["co2e_kg"] / top_high["co2e_kg"]) * 100)
            alert_text = (
                f"High-emission option: {top_high['label'].lower()} produces about {top_high['co2e_kg']} kg CO2e. "
                f"{best['label']} would cut that by roughly {cut}%."
            )
        elif highs:
            alert_text = "Every practical option on this route is high-emission. A longer stay or offsetting can reduce the impact."
        dispatcher.utter_message(text="\n\n".join(["\n".join(lines), f"Source: {source}. Figures are estimates.", CO2E_TOOLTIP]))

        dispatcher.utter_message(
            json_message={
                "type": "carbon_card",
                "origin": origin,
                "destination": _display_place(destination),
                "source": source,
                "options": options,
                "high_emission_alert": any(o["category"] == "high" for o in options),
                "alert_text": alert_text,
            }
        )
        if preferred_mode and preferred_mode not in [o["mode"] for o in options]:
            dispatcher.utter_message(text=f"Note: '{preferred_mode}' isn't available for this route.")
        logger.info("action_calculate_carbon_footprint took %.2fs", time.perf_counter() - started)
        return [SlotSet("carbon_results", options)]


# --------------------------------------------------------------------------
# Travel options (hotels)
# --------------------------------------------------------------------------
def _parse_iso_duration(value: Optional[str]) -> str:
    """'PT2H35M' -> '2h35'"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(value or ""))
    if not m or not (m.group(1) or m.group(2)):
        return ""
    return f"{m.group(1) or 0}h{int(m.group(2) or 0):02d}"


def _amadeus_hotels(destination: str, token: Optional[str]) -> List[Dict[str, Any]]:
    city = CITIES.get(_normalise_place(destination))
    if not city or not token:
        return []
    data, error = _safe_get(
        AMADEUS_HOTELS_BY_CITY_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"cityCode": city[3]},
    )
    if error or not isinstance(data, dict):
        return []
    hotels = []
    for item in (data.get("data") or [])[:3]:
        name = item.get("name")
        if name:
            hotels.append(
                {
                    "destination": _display_place(destination),
                    "name": str(name).title(),
                    "price": None,  # hotel prices need the separate hotel-offers API
                    "eco_certified": False,  # Amadeus gives no eco flag
                    "carbon_kg_per_night": None,
                    "source": "amadeus_live",
                }
            )
    return hotels


def _amadeus_flights(
    origin: str, destination: str, depart: Optional[date], token: Optional[str]
) -> List[Dict[str, Any]]:
    """Cheapest live flight offers from the Amadeus *sandbox* (test data).

    Needs: token, an exact future departure date, and IATA city codes for both
    ends (origins found only through OpenCage have no code -> no flights).
    """
    o = CITIES.get(_normalise_place(origin))
    d = CITIES.get(_normalise_place(destination))
    if not token or not depart or not o or not d or not o[3] or not d[3] or o[3] == d[3]:
        return []
    data, error = _safe_get(
        AMADEUS_FLIGHT_OFFERS_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "originLocationCode": o[3],
            "destinationLocationCode": d[3],
            "departureDate": depart.isoformat(),
            "adults": 1,
            "currencyCode": "EUR",
            "max": 5,
        },
    )
    if error or not isinstance(data, dict):
        return []
    offers: List[Dict[str, Any]] = []
    for item in data.get("data") or []:
        try:
            price = float((item.get("price") or {}).get("total"))
            itinerary = (item.get("itineraries") or [{}])[0]
            offers.append(
                {
                    "price_eur": round(price, 2),
                    "stops": max(len(itinerary.get("segments") or []) - 1, 0),
                    "duration": _parse_iso_duration(itinerary.get("duration")),
                    "carrier": (item.get("validatingAirlineCodes") or [""])[0],
                    "departure_date": depart.isoformat(),
                }
            )
        except (TypeError, ValueError):
            continue
    offers.sort(key=lambda x: x["price_eur"])
    return offers[:3]


class ActionFetchTravelOptions(Action):
    """Curated eco hotels + Amadeus hotels and flights.

    The two Amadeus calls run in parallel after one (cached) token request, so
    the worst case is roughly 2 x the per-call timeout, not 3 x.
    """

    def name(self) -> Text:
        return "action_fetch_travel_options"

    def run(self, dispatcher, tracker, domain):
        started = time.perf_counter()
        destination = tracker.get_slot("destination")
        if not destination:
            dispatcher.utter_message(
                text="Which destination should I search options for?",
                buttons=_destination_buttons(),
            )
            return []

        origin = tracker.get_slot("origin") or DEFAULT_ORIGIN
        _, depart = _parse_travel_dates(tracker.get_slot("travel_dates"))

        key = _normalise_place(destination)
        options = [
            {**entry, "source": "curated_verified"}
            for entry in _load_curated()
            if _normalise_place(entry.get("destination")) == key
        ]

        flights: List[Dict[str, Any]] = []
        try:
            token = _get_amadeus_token()
            if token:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f_hotels = pool.submit(_amadeus_hotels, destination, token)
                    f_flights = pool.submit(_amadeus_flights, origin, destination, depart, token)
                    try:
                        options += f_hotels.result(timeout=REQUEST_TIMEOUT_SECONDS + 0.5)
                    except Exception as exc:
                        logger.warning("Amadeus hotels failed: %s", exc)
                    try:
                        flights = f_flights.result(timeout=REQUEST_TIMEOUT_SECONDS + 0.5)
                    except Exception as exc:
                        logger.warning("Amadeus flights failed: %s", exc)
        except Exception as exc:  # defensive: never crash the action server
            logger.warning("Amadeus lookup failed: %s", exc)
        logger.info("action_fetch_travel_options took %.2fs", time.perf_counter() - started)

        if not options:
            dispatcher.utter_message(
                text=(
                    f"I couldn't find verified accommodation data for {destination} right now. "
                    "I can escalate this to a human advisor if you'd like."
                ),
                buttons=[{"title": "Human advisor", "payload": "/request_human_advisor"}],
            )
        return [SlotSet("retrieved_options", options), SlotSet("flight_offers", flights)]


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
class ActionRankOptions(Action):
    PREFERENCE_WEIGHTS = {
        "budget_conscious": {"carbon": 0.2, "price": 0.7, "certification": 0.1},
        "balanced": {"carbon": 0.4, "price": 0.4, "certification": 0.2},
        "maximum_sustainability": {"carbon": 0.6, "price": 0.15, "certification": 0.25},
    }
    ASSUMED_NIGHTS = 4

    def name(self) -> Text:
        return "action_rank_options"

    def run(self, dispatcher, tracker, domain):
        options = tracker.get_slot("retrieved_options") or []
        preference = tracker.get_slot("sustainability_level") or "balanced"
        weights = self.PREFERENCE_WEIGHTS.get(preference, self.PREFERENCE_WEIGHTS["balanced"])
        budget = _parse_amount(tracker.get_slot("budget"))
        nights, nights_assumed = _trip_nights(tracker, self.ASSUMED_NIGHTS)

        if not options:
            dispatcher.utter_message(
                text="I don't have any options to rank yet. Tell me a destination and I'll look.",
                buttons=_destination_buttons(),
            )
            return []

        prices = [o["price"] for o in options if o.get("price")]
        max_price = max(prices) if prices else 1
        carbons = [o["carbon_kg_per_night"] for o in options if o.get("carbon_kg_per_night") is not None]
        max_carbon = max(carbons) if carbons else 1

        carbon_results = tracker.get_slot("carbon_results") or []
        transport = _pick_transport(
            carbon_results, _normalise_place(tracker.get_slot("transport_mode")) or None
        )
        # Transport emissions are identical for every hotel in one destination,
        # so they do not change the hotel ORDER; they are added to each hotel's
        # estimated TRIP total (round trip + nights) so the user sees the real
        # footprint of the whole choice.

        scored = []
        for option in options:
            price = option.get("price") or max_price
            price_score = 1 - (price / max_price) * 0.8
            if budget and option.get("price") and option["price"] * nights > budget:
                price_score *= 0.5  # over budget for the stay length
            carbon = option.get("carbon_kg_per_night")
            carbon_score = 0.4 if carbon is None else 1 - (carbon / max_carbon) * 0.8
            certification_score = 1.0 if option.get("eco_certified") else 0.2
            weighted = (
                weights["carbon"] * carbon_score
                + weights["price"] * price_score
                + weights["certification"] * certification_score
            )
            trip_total = None
            if carbon is not None and transport is not None:
                trip_total = round(2 * transport["co2e_kg"] + carbon * nights, 1)
            scored.append(
                {
                    **option,
                    "score": round(weighted, 3),
                    "category": _hotel_category(carbon),
                    "verification": _verification_status(option),
                    "trip_total_kg_co2e": trip_total,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:3]

        paragraphs = [f"Top picks for a '{preference.replace('_', ' ')}' trip (hotel details in the cards below):"]
        stay = f"{nights} night{'s' if nights != 1 else ''}"
        stay += " (assumed, as your dates didn't show the length)" if nights_assumed else " (from your dates)"
        notes = []
        if transport is not None:
            mode_label = transport.get("label", transport["mode"])
            msg = (
                f"Lowest-impact way to get there: {mode_label} "
                f"(~{transport['co2e_kg']} kg CO2e one way, per passenger)."
            )
            if top[0].get("trip_total_kg_co2e") is not None:
                msg += (
                    f" Estimated trip total with the top pick: "
                    f"{top[0]['trip_total_kg_co2e']} kg CO2e (round trip + {stay})."
                )
            notes.append(msg)
        flights = tracker.get_slot("flight_offers") or []
        if flights:
            f = flights[0]
            stops = "direct" if f["stops"] == 0 else f"{f['stops']} stop(s)"
            extra = f", {f['duration']}" if f.get("duration") else ""
            notes.append(
                f"Cheapest live flight offer on {f['departure_date']}: €{f['price_eur']:.0f} "
                f"({stops}{extra}). Amadeus sandbox TEST data, not bookable. A flight emits far "
                "more CO2e than the lower-carbon options above."
            )
        if budget:
            notes.append(f"Prices checked against your budget of {tracker.get_slot('budget')} for {stay}.")
        notes.append(
            "Certification data in this prototype is a demo data set; always confirm "
            "eco-labels with the provider before booking."
        )
        paragraphs.append("\n".join(notes))
        dispatcher.utter_message(text="\n\n".join(paragraphs))
        dispatcher.utter_message(
            json_message={
                "type": "hotel_carousel",
                "preference": preference,
                "hotels": top,
                "transport": transport,
                "flight_offers": flights,
                "nights": nights,
                "assumed_nights": nights_assumed,
            }
        )
        dispatcher.utter_message(
            text="Want a human advisor to refine this itinerary?",
            buttons=[
                {"title": "Yes, human advisor", "payload": "/request_human_advisor"},
                {"title": "No, thanks", "payload": "/goodbye"},
            ],
        )
        return [SlotSet("ranked_options", top)]


class ActionGreet(Action):
    """Full welcome the first time, a short one if the user greets again."""

    def name(self) -> Text:
        return "action_greet"

    def run(self, dispatcher, tracker, domain):
        greetings = 0
        for event in tracker.events or []:
            if event.get("event") == "user" and (event.get("parse_data") or {}).get("intent", {}).get("name") == "greet":
                greetings += 1
        dispatcher.utter_message(response="utter_greet_again" if greetings > 1 else "utter_greet")
        return []


# --------------------------------------------------------------------------
# Cultural experiences and carbon offsets (curated demo data set)
# --------------------------------------------------------------------------
def _load_experience_data() -> Dict[str, Any]:
    try:
        with open(CURATED_EXPERIENCES_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load experiences data: %s", exc)
        return {}


class ActionShowExperiences(Action):
    """Community-benefit cultural experiences for the destination."""

    def name(self) -> Text:
        return "action_show_experiences"

    def run(self, dispatcher, tracker, domain):
        destination = tracker.get_slot("destination")
        if not destination:
            dispatcher.utter_message(
                text="Which destination should I look up experiences for?",
                buttons=_destination_buttons(),
            )
            return []
        key = _normalise_place(destination)
        items = [
            e for e in _load_experience_data().get("experiences", [])
            if _normalise_place(e.get("destination")) == key
        ]
        if not items:
            dispatcher.utter_message(
                text=(
                    f"I have no curated experiences for {destination} yet. "
                    "A human advisor can suggest locally run options."
                ),
                buttons=[{"title": "Human advisor", "payload": "/request_human_advisor"}],
            )
            return []
        lines = [f"Community-based experiences in {_display_place(destination)} (🧪 demo data set):"]
        for e in items[:3]:
            lines.append(f"- {e['name']} ({e.get('type', 'experience')}): {e.get('community_benefit', '')}")
        lines.append("Check with the operator how the benefit reaches local people before booking.")
        dispatcher.utter_message(text="\n".join(lines))
        return []


class ActionShowOffsets(Action):
    """Offset programmes plus an indicative cost for the user's trip."""

    ASSUMED_NIGHTS = ActionRankOptions.ASSUMED_NIGHTS

    def name(self) -> Text:
        return "action_show_offsets"

    @staticmethod
    def _trip_kg(tracker: Tracker) -> Optional[float]:
        ranked = tracker.get_slot("ranked_options") or []
        if ranked and ranked[0].get("trip_total_kg_co2e") is not None:
            return float(ranked[0]["trip_total_kg_co2e"])
        transport = _pick_transport(tracker.get_slot("carbon_results"))
        if transport:
            return round(2 * transport["co2e_kg"], 1)  # round trip, no hotel
        return None

    def run(self, dispatcher, tracker, domain):
        programmes = _load_experience_data().get("offset_programmes", [])
        if not programmes:
            dispatcher.utter_message(
                text="I can't load offset programmes right now. A human advisor can help you choose one.",
                buttons=[{"title": "Human advisor", "payload": "/request_human_advisor"}],
            )
            return []
        kg = self._trip_kg(tracker)
        lines = []
        if kg is None:
            lines.append("Tell me your trip first and I will size an offset for it. Typical programmes:")
        else:
            lines.append(f"Your estimated trip footprint is about {kg} kg CO2e.")
        for pr in programmes[:3]:
            cost = ""
            if kg is not None:
                low = kg / 1000 * pr["eur_per_tonne_low"]
                high = kg / 1000 * pr["eur_per_tonne_high"]
                cost = f" - roughly €{low:.2f}-€{high:.2f} for your trip"
            lines.append(f"- {pr['name']} ({pr['standard']}, {pr['project_type']}){cost}")
        lines.append(
            "Offsets are indicative demo figures, and they do not cancel emissions: reduce "
            "first (train over flight), offset what remains."
        )
        dispatcher.utter_message(text="\n".join(lines))
        return []


# --------------------------------------------------------------------------
# Human handover
# --------------------------------------------------------------------------
def _do_handover(dispatcher, tracker, reason: str):
    """Packages the full context and delivers it (endpoint, else local file).

    reason: 'user_request' (user asked for a person) or 'fallback_failed'
    (the bot twice failed to understand the user).
    """
    transcript = []
    for e in tracker.events or []:
        if e.get("event") in ("user", "bot") and e.get("text"):
            transcript.append(
                {"speaker": e["event"], "text": e.get("text"), "timestamp": e.get("timestamp")}
            )

    payload = {
        "conversation_id": tracker.sender_id,
        "handover_time": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "slots": {
            name: tracker.get_slot(name)
            for name in (
                "destination",
                "travel_dates",
                "budget",
                "sustainability_level",
                "origin",
                "transport_mode",
            )
        },
        "carbon_options_shown": tracker.get_slot("carbon_results"),
        "recommendations_shown": [
            {"name": o.get("name"), "price": o.get("price"), "score": o.get("score")}
            for o in (tracker.get_slot("ranked_options") or [])
        ],
        "transcript": transcript,
    }

    delivered = False
    if HANDOVER_ENDPOINT:
        _, error = _safe_post(HANDOVER_ENDPOINT, json_body=payload)
        delivered = error is None
        if error:
            logger.warning("Handover endpoint unreachable: %s", error)
    if not delivered:
        try:  # local fallback so the context is never lost
            os.makedirs(HANDOVER_LOG_DIR, exist_ok=True)
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(tracker.sender_id))
            path = os.path.join(HANDOVER_LOG_DIR, f"{safe_id}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Could not store handover payload: %s", exc)

    dispatcher.utter_message(
        json_message={"type": "handover", "active": True, "reason": reason}
    )
    return [SlotSet("handover_active", True), SlotSet("handover_reason", None)]


class ActionHandoverToHuman(Action):
    """Runs only AFTER the user consented (see rules: utter_ask_handover_consent
    -> affirm). The reason is 'user_request' unless the fallback flow stored
    'fallback_failed' in the handover_reason slot."""

    def name(self) -> Text:
        return "action_handover_to_human"

    def run(self, dispatcher, tracker, domain):
        reason = tracker.get_slot("handover_reason") or "user_request"
        return _do_handover(dispatcher, tracker, reason)


# --------------------------------------------------------------------------
# Two-stage fallback
# --------------------------------------------------------------------------
def _previous_fallbacks(tracker: Tracker) -> int:
    """Number of action_default_fallback runs since the last turn that was
    understood (consecutive failed turns before the current one)."""
    count = 0
    for event in reversed(list(tracker.events or [])):
        kind = event.get("event")
        if kind == "action" and event.get("name") == "action_default_fallback":
            count += 1
        elif kind == "user":
            intent = (event.get("parse_data") or {}).get("intent", {}).get("name")
            if intent not in ("nlu_fallback", None):
                # the current (latest) user event is the fallback itself and
                # is skipped by the check above; an understood turn stops it
                break
    return count


class ActionDefaultFallback(Action):
    """1st failure -> clarification with quick-reply buttons.
    2nd failure -> ask for consent (the answer is handled by
    action_handle_affirm / action_handle_deny)."""

    def name(self) -> Text:
        return "action_default_fallback"

    def run(self, dispatcher, tracker, domain):
        if _previous_fallbacks(tracker) == 0:
            dispatcher.utter_message(response="utter_clarify_intent")
            return []
        dispatcher.utter_message(response="utter_default_fallback")
        # GDPR: the transcript is only shared after an explicit yes.
        dispatcher.utter_message(response="utter_ask_handover_consent")
        return [
            SlotSet("handover_reason", "fallback_failed"),
            SlotSet("awaiting_handover_consent", True),
        ]


# --------------------------------------------------------------------------
# Handover consent (GDPR): ask -> yes/no
# --------------------------------------------------------------------------
# These three actions make the consent flow deterministic for the dialogue
# policies: "request a human" always goes to action_ask_handover_consent and
# "yes"/"no" always go to the two handlers below. They read the slot
# awaiting_handover_consent to know whether a consent question is pending.
class ActionAskHandoverConsent(Action):
    def name(self) -> Text:
        return "action_ask_handover_consent"

    def run(self, dispatcher, tracker, domain):
        dispatcher.utter_message(response="utter_ask_handover_consent")
        return [SlotSet("awaiting_handover_consent", True)]


class ActionHandleAffirm(Action):
    """'yes': hands over if a consent question is pending, otherwise just
    asks what the user wants to do next."""

    def name(self) -> Text:
        return "action_handle_affirm"

    def run(self, dispatcher, tracker, domain):
        if not tracker.get_slot("awaiting_handover_consent"):
            dispatcher.utter_message(response="utter_what_next")
            return []
        reason = tracker.get_slot("handover_reason") or "user_request"
        events = _do_handover(dispatcher, tracker, reason)
        dispatcher.utter_message(response="utter_handover_notice")
        return events + [SlotSet("awaiting_handover_consent", False)]


class ActionHandleDeny(Action):
    """'no': nothing is shared; the bot carries on."""

    def name(self) -> Text:
        return "action_handle_deny"

    def run(self, dispatcher, tracker, domain):
        if not tracker.get_slot("awaiting_handover_consent"):
            dispatcher.utter_message(response="utter_what_next")
            return []
        dispatcher.utter_message(response="utter_handover_declined")
        return [
            SlotSet("awaiting_handover_consent", False),
            SlotSet("handover_reason", None),
        ]
