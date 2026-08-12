"""Denver address lookup: "1226 e 10th" -> a door in Capitol Hill.

WHY NOT JUST PHOTON. Photon indexes OSM, and OSM does not have Denver's
addresses. Measured against the routing extract: East 10th Avenue carries
address nodes for 607, 609, 611, 613, 1412, 1424, 3009 — and not 1226.
Citywide there are 250 `addr:interpolation` ways and not one of them is tagged
with a street. Denver publishes 413,405 authoritative address points instead;
this module is the index over them.

Photon keeps everything it is good at — businesses, parks, landmarks, "Union
Station". It is simply no longer asked questions about house numbers.

THE SHAPE. A query splits into a house number and a street, and those want
opposite handling:

    1226            never approximate. Nobody types 1226 meaning 1228.
    e 10th ave      abbreviated, half-typed, misspelled, cased at random.

There are 909 distinct street names in the city and 413,405 points. So the
fuzzy work happens over the *small* set, and the number is then an exact,
indexed lookup. Trigram-searching whole address strings would be slower AND
worse: it returns the right number on the wrong street, which is precisely how
this service broke production earlier today.

WHY A TRIE AND NOT pg_trgm. Autocomplete's hardest input is its shortest one.
A rider three characters in has typed "10t", and trigram similarity between
"10t" and "10TH" is poor — short strings share too few trigrams, so the very
case that must feel instant is the case trigrams handle worst. A trie is
O(len(prefix)) regardless, and prefix containment is exactly the relationship
"still typing" means. Edit distance then covers the other failure — a rider who
finished the word and got it wrong ("Colfx") — over a candidate set small
enough that the cost never shows.

Both structures are built from the database at first use and held in memory:
909 names is well under a megabyte, and rebuilding is a single query.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .pg import connection

log = logging.getLogger(__name__)

# Source values are inconsistently cased ("10th", "109TH", "101ST") and the
# street name is sometimes NULL, so everything is normalised on the way in and
# nothing is trusted raw.
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
# As _PUNCT_RE, but a slash survives so a fractional house number can be
# recognised before the street name is normalised.
_KEEP_SLASH_RE = re.compile(r"[^\w\s/]")

# A leading house number, optionally with a letter or fraction: 1226, 1226A,
# 1226 1/2. The suffix distinguishes real neighbours, so it is captured.
#
# THE LETTER MUST BE ADJACENT. Allowing a space made "1226 e 10th ave" parse as
# house number 1226 suffix "E" on a street called "10th", silently discarding
# the directional and making it identical to "1226 w 10th ave" — opposite ends
# of the city, same query. Denver writes suffixed numbers closed up ("1226A")
# and fractions spaced ("1226 1/2"), so the rule follows the writing.
_NUMBER_RE = re.compile(r"^\s*(\d+)([A-Za-z])?(?:\s+(\d/\d))?(?=\s|$)")

_DIRECTIONALS = {
    "N": "N", "NORTH": "N", "S": "S", "SOUTH": "S",
    "E": "E", "EAST": "E", "W": "W", "WEST": "W",
    "NE": "NE", "NORTHEAST": "NE", "NW": "NW", "NORTHWEST": "NW",
    "SE": "SE", "SOUTHEAST": "SE", "SW": "SW", "SOUTHWEST": "SW",
}

# Denver's actual vocabulary, plus the abbreviations riders type. Mapped to the
# form the city dataset uses so a parsed query and an indexed row agree.
_POSTTYPES = {
    "AVE": "AVE", "AV": "AVE", "AVENUE": "AVE",
    "ST": "ST", "STR": "ST", "STREET": "ST",
    "BLVD": "BLVD", "BOULEVARD": "BLVD",
    "DR": "DR", "DRIVE": "DR",
    "RD": "RD", "ROAD": "RD",
    "PL": "PL", "PLACE": "PL",
    "CT": "CT", "COURT": "CT",
    "CIR": "CIR", "CIRCLE": "CIR",
    "LN": "LN", "LANE": "LN",
    "WAY": "WAY",
    "PKWY": "PKWY", "PARKWAY": "PKWY", "PKY": "PKWY",
    "TER": "TER", "TERR": "TER", "TERRACE": "TER",
    "TRL": "TRL", "TRAIL": "TRL",
    "LOOP": "LOOP", "RUN": "RUN", "ROW": "ROW", "MALL": "MALL",
    "PT": "PT", "POINT": "PT",
    "SQ": "SQ", "SQUARE": "SQ",
    "HWY": "HWY", "HIGHWAY": "HWY",
    "BYP": "BYP", "BYPASS": "BYP",
    "EXPY": "EXPY", "EXPRESSWAY": "EXPY",
    "FWY": "FWY", "FREEWAY": "FWY",
}

# "10" should find "10TH", because a rider typing a numbered street rarely
# types the ordinal and the city is full of them.
_ORDINAL_RE = re.compile(r"^(\d+)(ST|ND|RD|TH)$")

# A house-number suffix keeps its slash, for the same reason a query does.
_SUFFIX_CLEAN_RE = re.compile(r"[^\w\s/]")


def house_number_text(number: Any, suffix: Any) -> str:
    """How a house number is written back to the rider. "4039" + "1/2".

    The city's ADDRESS_NUMBER_SUFFIX is not just apartment letters. Counted
    against the live layer: 974 rows carry "1/2", eight carry a bare letter,
    and a handful carry BSMT / REAR / UPPER / CA. Concatenating blindly — and
    normalising the slash away first, as normalize_token does — rendered
    4039 1/2 N Wyandot St as "40391 2 N Wyandot St". That is precisely the
    label a rider cannot trust, which is the whole reason this index exists.

    A single letter closes up ("1226B"), because that is how Denver writes a
    real neighbouring door. Everything else is spaced, which is how the city's
    own FULL_ADDRESS renders it.
    """
    text = _WS_RE.sub(" ", _SUFFIX_CLEAN_RE.sub(" ", str(suffix or "")).upper()).strip()
    if not text:
        return str(number)
    if len(text) == 1 and text.isalpha():
        return f"{number}{text}"
    return f"{number} {text}"


def normalize_token(value: Any) -> str:
    """Uppercase, punctuation-stripped, whitespace-collapsed. "" for None."""
    if value is None:
        return ""
    text = _PUNCT_RE.sub(" ", str(value)).upper()
    return _WS_RE.sub(" ", text).strip()


def canonical_directional(value: Any) -> str | None:
    token = normalize_token(value).replace(" ", "")
    return _DIRECTIONALS.get(token)


def canonical_posttype(value: Any) -> str | None:
    return _POSTTYPES.get(normalize_token(value).replace(" ", ""))


def ordinal_stem(name: str) -> str | None:
    """"10TH" -> "10". None when the name is not an ordinal.

    Lets "1226 e 10" reach East 10th Avenue: numbered streets are most of
    Denver's grid and nobody types the suffix.
    """
    match = _ORDINAL_RE.match(normalize_token(name).replace(" ", ""))
    return match.group(1) if match else None


@dataclass(frozen=True)
class StreetRow:
    """One street identity — direction, name, type, direction — as indexed."""
    id: int
    predirectional: str | None
    street_name: str
    posttype: str | None
    postdirectional: str | None
    search_key: str
    name_key: str
    display_name: str
    point_count: int = 0


def street_keys(predirectional: Any, street_name: Any,
                posttype: Any, postdirectional: Any) -> tuple[str, str, str]:
    """(search_key, name_key, display_name) for one street identity.

    `search_key` is everything joined — what a fully typed query matches.
    `name_key` is the bare name — what a half-typed query matches, since a
    rider reaches "10th" long before "Ave".
    """
    pre = canonical_directional(predirectional) or ""
    post_t = canonical_posttype(posttype) or ""
    post_d = canonical_directional(postdirectional) or ""
    name = normalize_token(street_name)
    search_key = " ".join(p for p in (pre, name, post_t, post_d) if p)
    display = " ".join(p for p in (
        pre, _title_street(name), _title_type(post_t), post_d) if p)
    return search_key, name, display


def _title_street(name: str) -> str:
    """"10TH" -> "10th", "COLFAX" -> "Colfax". Ordinals stay lowercase."""
    out = []
    for token in name.split():
        m = _ORDINAL_RE.match(token)
        out.append(f"{m.group(1)}{m.group(2).lower()}" if m else token.title())
    return " ".join(out)


def _title_type(posttype: str) -> str:
    return posttype.title() if posttype else ""


# --- query parsing -----------------------------------------------------------

@dataclass(frozen=True)
class ParsedQuery:
    """What a rider's text decomposes into. Any part may be missing."""
    number: int | None
    number_text: str
    predirectional: str | None
    street_tokens: tuple[str, ...]
    posttype: str | None
    postdirectional: str | None

    @property
    def street_name(self) -> str:
        return " ".join(self.street_tokens)

    @property
    def has_number(self) -> bool:
        return self.number is not None


def parse_query(q: str) -> ParsedQuery:
    """"1226 e 10th ave" -> its parts. Tolerant: every part is optional.

    Deliberately positional, matching how addresses are actually written: a
    directional binds at the front, a street type at the back. Matching them
    anywhere would turn "St Paul St" into nonsense — the same trap
    expand_street_abbreviations documents in api_geocode.
    """
    # Uppercase and collapse, but KEEP THE SLASH. Stripping all punctuation
    # first turned "1226 1/2 Champa" into "1226 1 2 CHAMPA", which no longer
    # matches the fraction and leaves "1 2" glued to the street name.
    text = _WS_RE.sub(" ", _KEEP_SLASH_RE.sub(" ", str(q or "").upper())).strip()
    number: int | None = None
    number_text = ""

    match = _NUMBER_RE.match(text)
    if match:
        letter = match.group(2)
        # Belt and braces on top of adjacency: a bare directional letter is a
        # direction in this city's grid, not an apartment, whenever a street
        # still follows it.
        rest_after = text[match.end():].strip()
        if letter and canonical_directional(letter) and rest_after:
            letter = None
            text = text[match.start(2):].strip()
        else:
            text = rest_after
        number = int(match.group(1))
        number_text = match.group(1) + (letter or "") + (
            " " + match.group(3) if match.group(3) else "")

    tokens = [normalize_token(t) for t in text.split()]
    tokens = [t for t in tokens if t]
    pre = post_d = post_t = None

    if tokens and canonical_directional(tokens[0]):
        # Only when something follows: "1226 E" is a street called E, not a
        # directional with no street.
        if len(tokens) > 1:
            pre = canonical_directional(tokens[0])
            tokens = tokens[1:]

    if len(tokens) > 1 and canonical_directional(tokens[-1]):
        post_d = canonical_directional(tokens[-1])
        tokens = tokens[:-1]

    if len(tokens) > 1 and canonical_posttype(tokens[-1]):
        post_t = canonical_posttype(tokens[-1])
        tokens = tokens[:-1]

    return ParsedQuery(number, number_text, pre, tuple(tokens), post_t, post_d)


# --- the trie ----------------------------------------------------------------

@dataclass
class _Node:
    children: dict[str, "_Node"] = field(default_factory=dict)
    ids: list[int] = field(default_factory=list)


class StreetIndex:
    """Prefix and fuzzy lookup over the city's ~909 street names.

    Two structures over the same small set:

    * a TRIE, for the input autocomplete actually receives — a prefix. Three
      characters in ("10t") a trie answers in three steps, while trigram
      similarity is at its worst, because short strings share too few
      trigrams. This is the common case and it has to feel instant.
    * EDIT DISTANCE, for a rider who finished the word and got it wrong
      ("Colfx"). Only consulted when the trie finds nothing, and only over
      names of a plausible length, so the quadratic part never sees more than
      a few hundred candidates.

    Held in memory. Rebuilding is one query over a table of a few thousand
    rows, so `refresh()` after an ingest is cheap and there is no invalidation
    protocol to get wrong.
    """

    def __init__(self, rows: Iterable[StreetRow] | None = None) -> None:
        self._by_id: dict[int, StreetRow] = {}
        self._root = _Node()
        self._exact: dict[str, list[int]] = {}
        if rows is not None:
            self.load(rows)

    # -- build --
    def load(self, rows: Iterable[StreetRow]) -> "StreetIndex":
        self._by_id = {}
        self._root = _Node()
        self._exact = {}
        for row in rows:
            self._by_id[row.id] = row
            self._exact.setdefault(row.search_key, []).append(row.id)
            self._exact.setdefault(row.name_key, []).append(row.id)
            self._insert(row.name_key, row.id)
            if row.search_key != row.name_key:
                self._insert(row.search_key, row.id)
            # "10" reaches "10TH": numbered streets are most of the grid and
            # riders do not type the ordinal.
            stem = ordinal_stem(row.street_name)
            if stem:
                self._insert(stem, row.id)
                self._exact.setdefault(stem, []).append(row.id)
        return self

    def _insert(self, key: str, street_id: int) -> None:
        node = self._root
        for ch in key:
            node = node.children.setdefault(ch, _Node())
            node.ids.append(street_id)

    def __len__(self) -> int:
        return len(self._by_id)

    # -- query --
    def prefix(self, text: str, limit: int = 25) -> list[StreetRow]:
        """Streets whose name or full key begins with `text`."""
        key = normalize_token(text)
        if not key:
            return []
        node = self._root
        for ch in key:
            node = node.children.get(ch)
            if node is None:
                return []
        seen: set[int] = set()
        out: list[StreetRow] = []
        # Busiest streets first: with 909 names a prefix can match dozens, and
        # the one with 4,000 addresses on it is the likelier intent.
        for sid in sorted(set(node.ids),
                          key=lambda i: -self._by_id[i].point_count):
            if sid in seen:
                continue
            seen.add(sid)
            out.append(self._by_id[sid])
            if len(out) >= limit:
                break
        return out

    def fuzzy(self, text: str, limit: int = 10,
              max_distance: int = 2) -> list[StreetRow]:
        """Streets within a small edit distance. For finished-but-wrong input.

        Length-gated before the distance is computed: a name that differs in
        length by more than the budget cannot possibly be within it, and
        skipping those keeps this comfortably sub-millisecond over the whole
        city.
        """
        key = normalize_token(text)
        if not key:
            return []
        scored: list[tuple[int, int, StreetRow]] = []
        for row in self._by_id.values():
            for candidate in {row.name_key, row.search_key}:
                if abs(len(candidate) - len(key)) > max_distance:
                    continue
                dist = _bounded_levenshtein(key, candidate, max_distance)
                if dist is not None:
                    scored.append((dist, -row.point_count, row))
                    break
        scored.sort(key=lambda t: (t[0], t[1]))
        return [row for _, _, row in scored[:limit]]

    def lookup(self, text: str, limit: int = 25) -> list[StreetRow]:
        """Exact, then prefix, then fuzzy — cheapest and most certain first."""
        key = normalize_token(text)
        if not key:
            return []
        if key in self._exact:
            ids = list(dict.fromkeys(self._exact[key]))
            return [self._by_id[i] for i in ids][:limit]
        hits = self.prefix(key, limit)
        return hits if hits else self.fuzzy(key, limit)


def _bounded_levenshtein(a: str, b: str, max_distance: int) -> int | None:
    """Edit distance, or None once it provably exceeds the budget.

    Bailing out early is what makes calling this across every street cheap:
    most comparisons fail within the first few characters.
    """
    if abs(len(a) - len(b)) > max_distance:
        return None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        best = current[0]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
            best = min(best, current[-1])
        if best > max_distance:
            return None
        previous = current
    return previous[-1] if previous[-1] <= max_distance else None


# --- the index, loaded from the database -------------------------------------

_INDEX: StreetIndex | None = None
_INDEX_BUILT_AT: float = 0.0

# THE INDEX MUST EXPIRE, and this is not an optimisation.
#
# refresh_address_points runs from cron, which lives in the SCHEDULER
# container. This index lives in the API's. Nothing in-process is notified when
# the table is rebuilt, so a build cached for the life of the process is a
# build that never sees an update.
#
# The failure that matters is not the weekly refresh, though — it is the first
# deploy. The migration creates the tables EMPTY, the API starts and answers
# its first address query by building an index over zero rows, and the load job
# runs afterwards. Cached forever, that empty index makes every address lookup
# return nothing and fall through to Photon: the feature would be dead on
# arrival, and dead SILENTLY, because falling through to Photon is also what
# success looks like from outside. A timer is what makes that self-heal.
_INDEX_TTL_SECONDS = 900.0
# An empty index is retried far sooner. It means "not loaded yet", which is a
# state that should last minutes, not until the next restart.
_EMPTY_INDEX_TTL_SECONDS = 60.0


def _load_street_rows() -> list[StreetRow]:
    """Every indexed street, or [] if the table cannot be read.

    Separate from street_index so the caching rules above can be tested
    against a clock rather than against a database.
    """
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, predirectional, street_name, posttype, "
                    "       postdirectional, search_key, name_key, "
                    "       display_name, point_count "
                    "FROM address_streets")
                return [StreetRow(*r) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.warning("address street index unavailable (%s) — "
                    "address lookup will defer to photon", exc)
        return []


def street_index(refresh: bool = False) -> StreetIndex:
    """The process-wide street index, rebuilt on first use and on a timer.

    A failure to build is never fatal: address lookup quietly returns nothing
    and the caller falls back to Photon, which is the behaviour that existed
    before this module.
    """
    global _INDEX, _INDEX_BUILT_AT
    now = time.monotonic()
    if _INDEX is not None and not refresh:
        ttl = _INDEX_TTL_SECONDS if len(_INDEX) else _EMPTY_INDEX_TTL_SECONDS
        if now - _INDEX_BUILT_AT < ttl:
            return _INDEX
    built = StreetIndex(_load_street_rows())
    _INDEX_BUILT_AT = now
    # Only publish a build that found something. A database blip on a scheduled
    # re-read must not swap a working index out for an empty one mid-day; the
    # old one is stale at worst, and street names barely move.
    if len(built) or _INDEX is None:
        _INDEX = built
        log.info("address street index: %d streets", len(_INDEX))
    else:
        log.warning("address street index re-read found 0 streets — "
                    "keeping the %d already loaded", len(_INDEX))
    return _INDEX


# --- ingest ------------------------------------------------------------------
#
# Denver's open-data feature service, paginated. 413k points at the service's
# 2,000-record ceiling is ~207 requests, which is why this is a scheduled job
# and not something a request path ever touches.

# LICENCE. Denver's Open Data Catalog is CC BY 3.0 and attribution is a
# condition of use: "you are free to copy, distribute, transmit and adapt the
# data, as long as you credit the 'City of Denver Open Data Catalog' and
# clearly indicate the license terms of this work (CC BY 3.0)." The credit
# lives in the app's About drawer, alongside the same catalog's high-injury
# network and tree canopy — both of which had been shipping uncredited.
ARCGIS_LAYER = (
    "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/ArcGIS/rest/services"
    "/ODC_CITY_LOC_ADDRESSPUBLIC_P/FeatureServer/31"
)
ARCGIS_PAGE = 2000
_FIELDS = ("ADDRESS_NUMBER,ADDRESS_NUMBER_SUFFIX,PREDIRECTIONAL,STREET_NAME,"
           "POSTTYPE,POSTDIRECTIONAL,UNIT_IDENTIFIER,ADDRESS_TYPE,"
           "FULL_ADDRESS,LATITUDE,LONGITUDE")


def _fetch_page(client, offset: int) -> tuple[list[dict], bool]:
    """One page, and whether the service says there is more.

    The "more" flag comes from ArcGIS's own `exceededTransferLimit` rather than
    from `len(rows) == ARCGIS_PAGE`. The layer's maxRecordCount is 2000 today
    and ARCGIS_PAGE matches it — but the server silently clamps to its own
    ceiling, so if Denver ever lowers it, a length test would read one short
    page, conclude it had reached the end, and index 1,000 of 413,405 addresses
    while reporting success. Silent truncation is the failure mode that would
    be hardest to notice from the outside: every query still answers, just for
    a city that is mostly missing.
    """
    r = client.get(f"{ARCGIS_LAYER}/query", params={
        "where": "1=1", "outFields": _FIELDS, "returnGeometry": "false",
        "resultOffset": offset, "resultRecordCount": ARCGIS_PAGE,
        "orderByFields": "OBJECTID", "f": "json",
    }, timeout=90)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"arcgis: {body['error']}")
    rows = [f.get("attributes") or {} for f in (body.get("features") or [])]
    return rows, bool(body.get("exceededTransferLimit"))


def _fetch_count(client) -> int | None:
    """How many rows the service says it holds, for the completeness check.

    None when the service will not say, which downgrades the check rather than
    failing the run.
    """
    try:
        r = client.get(f"{ARCGIS_LAYER}/query", params={
            "where": "1=1", "returnCountOnly": "true", "f": "json"}, timeout=30)
        r.raise_for_status()
        count = r.json().get("count")
        return int(count) if count is not None else None
    except Exception as exc:  # noqa: BLE001
        log.warning("address count probe failed (%s) — "
                    "completeness check disabled for this run", exc)
        return None


def refresh_address_points(page_limit: int | None = None) -> dict[str, Any]:
    """Rebuild address_streets + address_points from Denver open data.

    Loaded into a scratch table and swapped in one transaction: a half-written
    address index is worse than a stale one, since the geocoder would answer
    confidently from whatever happened to have landed.

    POINTS ARE SPOOLED TO DISK, not accumulated in memory. Measured against the
    live layer: the Python-side list cost 816 bytes a row, so 413k rows is
    ~337 MB — inside a scheduler container capped at 1 GiB, before psycopg
    builds its own copy of every parameter for the insert. This service has
    OOM-killed itself on exactly that pattern before. The spool is a few tens
    of megabytes and streams straight into COPY, which is also several times
    faster than executemany at this row count.
    """
    import csv
    import tempfile

    import httpx

    streets: dict[tuple, dict] = {}
    fetched = pages = skipped = written = 0

    with tempfile.TemporaryFile("w+", newline="", encoding="utf-8") as spool:
        writer = csv.writer(spool)
        with httpx.Client() as client:
            expected = _fetch_count(client)
            offset = 0
            while True:
                rows, more = _fetch_page(client, offset)
                if not rows:
                    break
                pages += 1
                fetched += len(rows)
                for a in rows:
                    lat, lon = a.get("LATITUDE"), a.get("LONGITUDE")
                    name = normalize_token(a.get("STREET_NAME"))
                    number = a.get("ADDRESS_NUMBER")
                    # A point with no street, no number or no position cannot
                    # be looked up or routed to; it is not worth indexing.
                    if not name or lat is None or lon is None or number is None:
                        skipped += 1
                        continue
                    pre = canonical_directional(a.get("PREDIRECTIONAL"))
                    post_t = canonical_posttype(a.get("POSTTYPE"))
                    post_d = canonical_directional(a.get("POSTDIRECTIONAL"))
                    key = (pre, name, post_t, post_d)
                    street = streets.get(key)
                    if street is None:
                        sk, nk, disp = street_keys(pre, name, post_t, post_d)
                        # The id is assigned here so a point can be spooled
                        # immediately; the temp table carries no foreign key,
                        # so the streets themselves can be inserted last.
                        street = streets[key] = {
                            "id": len(streets) + 1, "search_key": sk,
                            "name_key": nk, "display_name": disp, "n": 0}
                    street["n"] += 1
                    writer.writerow([
                        street["id"], int(number),
                        house_number_text(int(number),
                                          a.get("ADDRESS_NUMBER_SUFFIX")),
                        normalize_token(a.get("UNIT_IDENTIFIER")),
                        float(lat), float(lon),
                        normalize_token(a.get("ADDRESS_TYPE")),
                        a.get("FULL_ADDRESS") or "",
                    ])
                    written += 1
                offset += len(rows)
                if not more:
                    break
                if page_limit and pages >= page_limit:
                    break

        log.info("address refresh: fetched %d rows over %d pages (%d unusable),"
                 " %d streets", fetched, pages, skipped, len(streets))
        if not written:
            return {"error": "no address points fetched", "fetched": fetched}

        # A short read must not overwrite a good index. Losing half the city
        # silently is worse than serving last week's copy, and every symptom of
        # it — queries that answer, just not for your street — looks exactly
        # like the Photon fallback working as designed.
        if expected and not page_limit and fetched < expected * 0.98:
            log.error("address refresh: short read, %d of %d rows — "
                      "refusing to swap", fetched, expected)
            return {"error": "short read; index left unchanged",
                    "fetched": fetched, "expected": expected}

        spool.flush()
        spool.seek(0)

        with connection() as conn:
            with conn.cursor() as cur:
                # One transaction, entered only after the fetch has finished
                # and been checked: the old index stays queryable throughout,
                # and a failure anywhere leaves it untouched.
                cur.execute("CREATE TEMP TABLE new_streets (LIKE "
                            "address_streets INCLUDING DEFAULTS) ON COMMIT DROP")
                cur.execute("CREATE TEMP TABLE new_points (LIKE address_points "
                            "INCLUDING DEFAULTS) ON COMMIT DROP")
                cur.executemany(
                    "INSERT INTO new_streets (id, predirectional, street_name, "
                    "posttype, postdirectional, search_key, name_key, "
                    "display_name, point_count) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(v["id"], k[0], k[1], k[2], k[3], v["search_key"],
                      v["name_key"], v["display_name"], v["n"])
                     for k, v in streets.items()])
                # An unquoted empty field is NULL under `NULL ''`; a genuinely
                # empty string would arrive quoted. csv.writer emits None and
                # "" identically, and for these columns absent and empty mean
                # the same thing anyway.
                with cur.copy(
                    "COPY new_points (street_id, number, number_text, unit, "
                    "lat, lon, address_type, full_address) FROM STDIN "
                    "WITH (FORMAT csv, NULL '')"
                ) as copy:
                    while chunk := spool.read(1 << 20):
                        copy.write(chunk)
                cur.execute("TRUNCATE address_points, address_streets "
                            "RESTART IDENTITY")
                cur.execute("INSERT INTO address_streets "
                            "SELECT * FROM new_streets")
                cur.execute("INSERT INTO address_points (street_id, number, "
                            "number_text, unit, lat, lon, address_type, "
                            "full_address) SELECT street_id, number, "
                            "number_text, unit, lat, lon, address_type, "
                            "full_address FROM new_points")
                cur.execute("SELECT setval(pg_get_serial_sequence("
                            "'address_streets','id'), (SELECT max(id) "
                            "FROM address_streets))")
                cur.execute(
                    "INSERT INTO system_state (key, value, updated_at) "
                    "VALUES ('address_points_refreshed_at', NOW()::text, NOW())"
                    " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_at = NOW()")
            conn.commit()

    street_index(refresh=True)
    return {"fetched": fetched, "pages": pages, "unusable": skipped,
            "streets": len(streets), "points": written}


# --- lookup ------------------------------------------------------------------

def lookup(q: str, limit: int = 8) -> list[dict[str, Any]]:
    """Address matches for a rider's text, best first. [] when there are none.

    Never raises: the caller falls through to Photon, which is what happened
    before this index existed.
    """
    parsed = parse_query(q)
    if not parsed.street_tokens:
        return []
    idx = street_index()
    if not len(idx):
        return []

    candidates = idx.lookup(
        " ".join(p for p in (parsed.predirectional, parsed.street_name,
                             parsed.posttype) if p), limit=12)
    if not candidates:
        candidates = idx.lookup(parsed.street_name, limit=12)
    # A directional the rider gave is a HARD filter, never a preference:
    # E 10th Ave and W 10th Ave are different streets, and offering one for
    # the other is the wrong-side-of-town failure this service exists to end.
    if parsed.predirectional:
        exact_dir = [c for c in candidates
                     if c.predirectional == parsed.predirectional]
        if exact_dir:
            candidates = exact_dir
    if parsed.posttype:
        exact_type = [c for c in candidates if c.posttype == parsed.posttype]
        if exact_type:
            candidates = exact_type
    if not candidates:
        return []

    if not parsed.has_number:
        return [{"label": c.display_name, "kind": "street", "street_id": c.id,
                 "lat": None, "lon": None, "point_count": c.point_count}
                for c in candidates[:limit]]

    ids = [c.id for c in candidates[:8]]
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT p.street_id, p.number_text, p.unit, p.lat, p.lon, "
                    "       s.display_name "
                    "FROM address_points p "
                    "JOIN address_streets s ON s.id = p.street_id "
                    "WHERE p.street_id = ANY(%s) AND p.number = %s "
                    "ORDER BY p.unit NULLS FIRST LIMIT %s",
                    (ids, parsed.number, limit * 4))
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("address point lookup failed (%s)", exc)
        return []

    order = {sid: i for i, sid in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r[0], 99))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for street_id, number_text, unit, lat, lon, display in rows:
        # DEDUPE ON THE LABEL, not on position. Denver files one point per
        # UNIT, so 1226 E 10th Ave is six rows and 1500 Champa St is six more,
        # each a separate apartment behind one street door. This first shipped
        # keyed on the rounded coordinate, which did nothing: the units are
        # scattered 5-25 m apart across the building footprint, well outside
        # the ~1 m that five decimal places collapses. Riders saw the same
        # address six times and no way to tell the rows apart.
        #
        # Two rows with the same label ARE the same destination — that is what
        # the label being identical means. Ordering put `unit IS NULL` first,
        # so the survivor is the street door itself where the city files one.
        key = f"{number_text} {display}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": f"{number_text} {display}, Denver",
                    "kind": "house", "lat": round(lat, 6), "lon": round(lon, 6),
                    "street_id": street_id, "matched_housenumber": True})
        if len(out) >= limit:
            break
    return out
