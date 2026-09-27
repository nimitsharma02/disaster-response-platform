"""Offline gazetteer and rule-based place extraction.

This module is the reason the platform needs no API keys and no network to work.
It does two jobs:

1. **Extract** a probable place name from free text
   ("Heavy flooding has affected Manhattan, NYC." -> "Manhattan, NYC").
2. **Resolve** a place name to coordinates from a bundled lookup table, when the
   online geocoder is unavailable, disabled, or rate-limited.

The extractor is deliberately rule-based rather than a full NER model. For this
domain the text is short and formulaic - incident reports name places after a
small set of prepositions - so a handful of patterns plus a known-place
dictionary gets most of the value at none of the cost, and unlike a model it is
deterministic, instant, and explains itself. The LLM path in ``llm.py`` handles
the messier inputs when a key is configured; this is the floor, not the ceiling.

Coordinates are approximate centroids, adequate for radius search at city or
district scale. They are not survey data and the confidence score says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GazetteerEntry:
    name: str        # canonical display name
    latitude: float
    longitude: float
    country: str


# --------------------------------------------------------------------------- #
# Bundled places. Keys are lowercase lookup aliases; several may map to one entry.
# --------------------------------------------------------------------------- #

_RAW_PLACES: list[tuple[list[str], str, float, float, str]] = [
    # --- United States -----------------------------------------------------
    (["manhattan", "manhattan nyc", "manhattan, nyc", "manhattan new york"],
     "Manhattan, New York, USA", 40.7831, -73.9712, "US"),
    (["new york", "nyc", "new york city"], "New York City, USA", 40.7128, -74.0060, "US"),
    (["brooklyn"], "Brooklyn, New York, USA", 40.6782, -73.9442, "US"),
    (["queens"], "Queens, New York, USA", 40.7282, -73.7949, "US"),
    (["the bronx", "bronx"], "The Bronx, New York, USA", 40.8448, -73.8648, "US"),
    (["staten island"], "Staten Island, New York, USA", 40.5795, -74.1502, "US"),
    (["los angeles", "la"], "Los Angeles, USA", 34.0522, -118.2437, "US"),
    (["san francisco", "sf"], "San Francisco, USA", 37.7749, -122.4194, "US"),
    (["chicago"], "Chicago, USA", 41.8781, -87.6298, "US"),
    (["houston"], "Houston, USA", 29.7604, -95.3698, "US"),
    (["new orleans"], "New Orleans, USA", 29.9511, -90.0715, "US"),
    (["miami"], "Miami, USA", 25.7617, -80.1918, "US"),
    (["seattle"], "Seattle, USA", 47.6062, -122.3321, "US"),
    (["boston"], "Boston, USA", 42.3601, -71.0589, "US"),
    (["washington dc", "washington, d.c.", "washington d.c."],
     "Washington, D.C., USA", 38.9072, -77.0369, "US"),
    (["puerto rico", "san juan"], "San Juan, Puerto Rico", 18.4655, -66.1057, "PR"),
    (["california"], "California, USA", 36.7783, -119.4179, "US"),
    (["florida"], "Florida, USA", 27.6648, -81.5158, "US"),
    (["texas"], "Texas, USA", 31.9686, -99.9018, "US"),

    # --- India -------------------------------------------------------------
    (["mumbai", "bombay"], "Mumbai, India", 19.0760, 72.8777, "IN"),
    (["delhi", "new delhi"], "New Delhi, India", 28.6139, 77.2090, "IN"),
    (["bengaluru", "bangalore"], "Bengaluru, India", 12.9716, 77.5946, "IN"),
    (["chennai", "madras"], "Chennai, India", 13.0827, 80.2707, "IN"),
    (["kolkata", "calcutta"], "Kolkata, India", 22.5726, 88.3639, "IN"),
    (["hyderabad"], "Hyderabad, India", 17.3850, 78.4867, "IN"),
    (["pune"], "Pune, India", 18.5204, 73.8567, "IN"),
    (["ahmedabad"], "Ahmedabad, India", 23.0225, 72.5714, "IN"),
    (["jaipur"], "Jaipur, India", 26.9124, 75.7873, "IN"),
    (["lucknow"], "Lucknow, India", 26.8467, 80.9462, "IN"),
    (["kochi", "cochin"], "Kochi, India", 9.9312, 76.2673, "IN"),
    (["thiruvananthapuram", "trivandrum"],
     "Thiruvananthapuram, India", 8.5241, 76.9366, "IN"),
    (["kerala"], "Kerala, India", 10.8505, 76.2711, "IN"),
    (["assam", "guwahati"], "Guwahati, Assam, India", 26.1445, 91.7362, "IN"),
    (["bihar", "patna"], "Patna, Bihar, India", 25.5941, 85.1376, "IN"),
    (["odisha", "bhubaneswar"], "Bhubaneswar, Odisha, India", 20.2961, 85.8245, "IN"),
    (["uttarakhand", "dehradun"], "Dehradun, Uttarakhand, India", 30.3165, 78.0322, "IN"),
    (["himachal pradesh", "shimla"], "Shimla, Himachal Pradesh, India", 31.1048, 77.1734, "IN"),
    (["gujarat", "surat"], "Surat, Gujarat, India", 21.1702, 72.8311, "IN"),
    (["maharashtra", "nagpur"], "Nagpur, Maharashtra, India", 21.1458, 79.0882, "IN"),
    (["tamil nadu", "coimbatore"], "Coimbatore, Tamil Nadu, India", 11.0168, 76.9558, "IN"),
    (["west bengal", "siliguri"], "Siliguri, West Bengal, India", 26.7271, 88.3953, "IN"),
    (["andhra pradesh", "visakhapatnam", "vizag"],
     "Visakhapatnam, Andhra Pradesh, India", 17.6868, 83.2185, "IN"),
    (["chandigarh"], "Chandigarh, India", 30.7333, 76.7794, "IN"),
    (["srinagar", "kashmir"], "Srinagar, India", 34.0837, 74.7973, "IN"),
    (["leh", "ladakh"], "Leh, Ladakh, India", 34.1526, 77.5771, "IN"),

    # --- Asia-Pacific ------------------------------------------------------
    (["tokyo"], "Tokyo, Japan", 35.6762, 139.6503, "JP"),
    (["osaka"], "Osaka, Japan", 34.6937, 135.5023, "JP"),
    (["sendai"], "Sendai, Japan", 38.2682, 140.8694, "JP"),
    (["manila"], "Manila, Philippines", 14.5995, 120.9842, "PH"),
    (["jakarta"], "Jakarta, Indonesia", -6.2088, 106.8456, "ID"),
    (["banda aceh", "aceh"], "Banda Aceh, Indonesia", 5.5483, 95.3238, "ID"),
    (["bangkok"], "Bangkok, Thailand", 13.7563, 100.5018, "TH"),
    (["kathmandu"], "Kathmandu, Nepal", 27.7172, 85.3240, "NP"),
    (["dhaka"], "Dhaka, Bangladesh", 23.8103, 90.4125, "BD"),
    (["chittagong", "chattogram"], "Chattogram, Bangladesh", 22.3569, 91.7832, "BD"),
    (["karachi"], "Karachi, Pakistan", 24.8607, 67.0011, "PK"),
    (["lahore"], "Lahore, Pakistan", 31.5204, 74.3587, "PK"),
    (["islamabad"], "Islamabad, Pakistan", 33.6844, 73.0479, "PK"),
    (["colombo"], "Colombo, Sri Lanka", 6.9271, 79.8612, "LK"),
    (["beijing"], "Beijing, China", 39.9042, 116.4074, "CN"),
    (["shanghai"], "Shanghai, China", 31.2304, 121.4737, "CN"),
    (["hong kong"], "Hong Kong", 22.3193, 114.1694, "HK"),
    (["singapore"], "Singapore", 1.3521, 103.8198, "SG"),
    (["seoul"], "Seoul, South Korea", 37.5665, 126.9780, "KR"),
    (["sydney"], "Sydney, Australia", -33.8688, 151.2093, "AU"),
    (["melbourne"], "Melbourne, Australia", -37.8136, 144.9631, "AU"),
    (["brisbane"], "Brisbane, Australia", -27.4698, 153.0251, "AU"),
    (["auckland"], "Auckland, New Zealand", -36.8485, 174.7633, "NZ"),
    (["christchurch"], "Christchurch, New Zealand", -43.5321, 172.6362, "NZ"),

    # --- Europe ------------------------------------------------------------
    (["london"], "London, United Kingdom", 51.5074, -0.1278, "GB"),
    (["manchester"], "Manchester, United Kingdom", 53.4808, -2.2426, "GB"),
    (["paris"], "Paris, France", 48.8566, 2.3522, "FR"),
    (["berlin"], "Berlin, Germany", 52.5200, 13.4050, "DE"),
    (["hamburg"], "Hamburg, Germany", 53.5511, 9.9937, "DE"),
    (["madrid"], "Madrid, Spain", 40.4168, -3.7038, "ES"),
    (["valencia"], "Valencia, Spain", 39.4699, -0.3763, "ES"),
    (["rome"], "Rome, Italy", 41.9028, 12.4964, "IT"),
    (["naples"], "Naples, Italy", 40.8518, 14.2681, "IT"),
    (["athens"], "Athens, Greece", 37.9838, 23.7275, "GR"),
    (["istanbul"], "Istanbul, Turkey", 41.0082, 28.9784, "TR"),
    (["ankara"], "Ankara, Turkey", 39.9334, 32.8597, "TR"),
    (["amsterdam"], "Amsterdam, Netherlands", 52.3676, 4.9041, "NL"),
    (["lisbon"], "Lisbon, Portugal", 38.7223, -9.1393, "PT"),
    (["kyiv", "kiev"], "Kyiv, Ukraine", 50.4501, 30.5234, "UA"),
    (["warsaw"], "Warsaw, Poland", 52.2297, 21.0122, "PL"),

    # --- Americas / Africa / Middle East ----------------------------------
    (["mexico city"], "Mexico City, Mexico", 19.4326, -99.1332, "MX"),
    (["sao paulo", "são paulo"], "Sao Paulo, Brazil", -23.5505, -46.6333, "BR"),
    (["rio de janeiro", "rio"], "Rio de Janeiro, Brazil", -22.9068, -43.1729, "BR"),
    (["buenos aires"], "Buenos Aires, Argentina", -34.6037, -58.3816, "AR"),
    (["santiago"], "Santiago, Chile", -33.4489, -70.6693, "CL"),
    (["lima"], "Lima, Peru", -12.0464, -77.0428, "PE"),
    (["bogota", "bogotá"], "Bogota, Colombia", 4.7110, -74.0721, "CO"),
    (["port-au-prince", "haiti"], "Port-au-Prince, Haiti", 18.5944, -72.3074, "HT"),
    (["toronto"], "Toronto, Canada", 43.6532, -79.3832, "CA"),
    (["vancouver"], "Vancouver, Canada", 49.2827, -123.1207, "CA"),
    (["montreal"], "Montreal, Canada", 45.5017, -73.5673, "CA"),
    (["cairo"], "Cairo, Egypt", 30.0444, 31.2357, "EG"),
    (["lagos"], "Lagos, Nigeria", 6.5244, 3.3792, "NG"),
    (["nairobi"], "Nairobi, Kenya", -1.2921, 36.8219, "KE"),
    (["addis ababa"], "Addis Ababa, Ethiopia", 9.0320, 38.7469, "ET"),
    (["cape town"], "Cape Town, South Africa", -33.9249, 18.4241, "ZA"),
    (["johannesburg"], "Johannesburg, South Africa", -26.2041, 28.0473, "ZA"),
    (["accra"], "Accra, Ghana", 5.6037, -0.1870, "GH"),
    (["dubai"], "Dubai, United Arab Emirates", 25.2048, 55.2708, "AE"),
    (["doha"], "Doha, Qatar", 25.2854, 51.5310, "QA"),
    (["riyadh"], "Riyadh, Saudi Arabia", 24.7136, 46.6753, "SA"),
    (["beirut"], "Beirut, Lebanon", 33.8938, 35.5018, "LB"),
    (["damascus"], "Damascus, Syria", 33.5138, 36.2765, "SY"),
    (["baghdad"], "Baghdad, Iraq", 33.3152, 44.3661, "IQ"),
    (["tehran"], "Tehran, Iran", 35.6892, 51.3890, "IR"),
    (["kabul"], "Kabul, Afghanistan", 34.5553, 69.2075, "AF"),
]

GAZETTEER: dict[str, GazetteerEntry] = {}
for _aliases, _name, _lat, _lng, _country in _RAW_PLACES:
    _entry = GazetteerEntry(name=_name, latitude=_lat, longitude=_lng, country=_country)
    for _alias in _aliases:
        GAZETTEER[_alias] = _entry

# Longest first, so "manhattan, nyc" wins over the substring "manhattan".
_SORTED_ALIASES: list[str] = sorted(GAZETTEER, key=len, reverse=True)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

# Words that begin a sentence or are otherwise capitalised without being places.
# Without this list, "Heavy flooding..." yields the candidate "Heavy".
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "for", "from", "has",
    "have", "heavy", "in", "into", "is", "it", "its", "major", "severe", "several", "of", "on",
    "or", "over", "that", "the", "their", "there", "they", "this", "to", "under", "urgent",
    "was", "were", "with", "after", "before", "during", "following", "amid", "due", "massive",
    "large", "small", "breaking", "update", "alert", "warning", "emergency", "reported",
    "reports", "report", "residents", "people", "families", "thousands", "hundreds", "dozens",
    "many", "some", "all", "we", "our", "us", "need", "needs", "needed", "help", "please",
    "immediately", "now", "today", "tonight", "yesterday", "morning", "evening", "night",
    "flood", "floods", "flooding", "fire", "fires", "wildfire", "earthquake", "quake",
    "cyclone", "hurricane", "typhoon", "storm", "landslide", "tsunami", "drought", "outbreak",
    "explosion", "collapse",
})

# Prepositions that reliably precede a place name in incident reports.
_CUES = r"(?:in|at|near|across|around|throughout|affecting|affected|hit|struck|around)"

# "in Manhattan, NYC" / "near Banda Aceh" - a cue followed by a capitalised run,
# optionally continued after a comma ("Manhattan, NYC").
_CUE_PATTERN = re.compile(
    rf"\b{_CUES}\s+((?:[A-Z][\w.'-]*)(?:\s+[A-Z][\w.'-]*)*(?:\s*,\s*[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*)?)",
    re.UNICODE,
)

# Any capitalised run, used as the weakest fallback signal.
_PROPER_RUN = re.compile(
    r"\b((?:[A-Z][\w.'-]*)(?:\s+[A-Z][\w.'-]*)*(?:\s*,\s*[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*)?)",
    re.UNICODE,
)


# Abbreviations whose trailing period does NOT end a sentence. Without this the
# candidate "St. Louis" would be truncated to "St".
_ABBREVIATIONS = frozenset(
    {"st", "mt", "ft", "pt", "rd", "dr", "ave", "blvd", "no", "dept", "co", "jr", "sr"}
)

_SENTENCE_BREAK = re.compile(r"\.\s")


def _cut_at_sentence_end(candidate: str) -> str:
    """Truncate a candidate at the first real sentence boundary.

    The token pattern deliberately allows an internal '.' so that "St. Louis"
    stays one candidate. The side effect was that a match could run straight
    past the end of a sentence: "...a warehouse in Brooklyn. Smoke is drifting"
    produced the candidate "Brooklyn. Smoke", which then failed to geocode.

    A period ends the candidate unless the word before it is a known
    abbreviation, which distinguishes "Brooklyn." (sentence end) from "St."
    (part of the name).
    """
    for match in _SENTENCE_BREAK.finditer(candidate):
        preceding_word = re.search(r"([\w'-]+)\.$", candidate[: match.start() + 1])
        if preceding_word and preceding_word.group(1).lower() in _ABBREVIATIONS:
            continue
        return candidate[: match.start()]
    return candidate


def _clean(candidate: str) -> str:
    candidate = _cut_at_sentence_end(candidate)
    text = re.sub(r"\s+", " ", candidate).strip(" .,;:!?-")
    # Drop a leading stopword that the regex swept in ("The Bronx" -> "Bronx"
    # is wrong, but "Several Manhattan" -> "Manhattan" is right, so only strip
    # when what remains is still a plausible name).
    parts = text.split()
    while parts and parts[0].lower() in _STOPWORDS and len(parts) > 1:
        parts.pop(0)
    return " ".join(parts)


def _is_plausible(candidate: str) -> bool:
    if len(candidate) < 3 or len(candidate) > 120:
        return False
    words = [w.lower().strip(",") for w in candidate.split()]
    # All-stopword runs ("Heavy Flooding") are not places.
    return not all(w in _STOPWORDS for w in words)


def lookup(place: str) -> GazetteerEntry | None:
    """Resolve a place name against the bundled table.

    Tries an exact alias match first, then the longest alias contained in the
    string, so "downtown Manhattan, NYC" still resolves.
    """
    key = re.sub(r"\s+", " ", place.strip().lower()).strip(" .,;:")
    if not key:
        return None
    if key in GAZETTEER:
        return GAZETTEER[key]
    for alias in _SORTED_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", key):
            return GAZETTEER[alias]
    return None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    place: str | None
    confidence: float
    method: str


def extract_place(description: str) -> ExtractionResult:
    """Pull the most likely place name out of an incident description.

    Candidates are scored by how much evidence supports them:

    * 0.90 ``rules:cue_known``    - a known place preceded by a locative cue
    * 0.75 ``rules:known``        - a known place anywhere in the text
    * 0.55 ``rules:cue``          - an unknown proper noun after a locative cue
    * 0.35 ``rules:proper_noun``  - an unknown proper noun, no cue
    * 0.00 ``none``               - nothing plausible found

    The method name describes only how the *name* was found. The geocoder that
    turned it into coordinates is appended separately by the geocoding service,
    producing e.g. ``rules:cue_known+nominatim``.

    The confidence is propagated onto the disaster record, so a caller can tell
    a solid match from a guess.
    """
    if not description or not description.strip():
        return ExtractionResult(None, 0.0, "none")

    text = description.strip()

    cue_candidates = [_clean(m.group(1)) for m in _CUE_PATTERN.finditer(text)]
    cue_candidates = [c for c in cue_candidates if _is_plausible(c)]

    # Strongest signal: cue + known place.
    for candidate in cue_candidates:
        if lookup(candidate):
            return ExtractionResult(candidate, 0.90, "rules:cue_known")

    # Known place anywhere in the text, even without a cue.
    lowered = text.lower()
    for alias in _SORTED_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return ExtractionResult(GAZETTEER[alias].name, 0.75, "rules:known")

    # Unknown proper noun, but a cue says it is a location. Nominatim may know it.
    if cue_candidates:
        return ExtractionResult(cue_candidates[0], 0.55, "rules:cue")

    # Weakest: any plausible proper noun. Skip the first word of the string,
    # which is capitalised merely because sentences are.
    for match in _PROPER_RUN.finditer(text):
        if match.start() == 0:
            continue
        candidate = _clean(match.group(1))
        if _is_plausible(candidate):
            return ExtractionResult(candidate, 0.35, "rules:proper_noun")

    log.debug("place_extraction_found_nothing", preview=text[:80])
    return ExtractionResult(None, 0.0, "none")
