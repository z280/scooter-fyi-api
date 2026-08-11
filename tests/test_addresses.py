"""Denver address lookup — parsing, the street index, and the ranking rules.

The failure this whole module exists to prevent is a confidently wrong pin:
before it, "1226 East 10th Avenue" returned 1226 East 22nd Avenue, twelve
blocks north, flagged in_coverage. Most of what is asserted here is about
refusing to guess rather than about finding things.
"""

from __future__ import annotations

import pytest

from src import addresses as A


def _street(i, pre, name, posttype, n=100, postdir=None):
    sk, nk, disp = A.street_keys(pre, name, posttype, postdir)
    return A.StreetRow(i, A.canonical_directional(pre), A.normalize_token(name),
                       A.canonical_posttype(posttype),
                       A.canonical_directional(postdir), sk, nk, disp, n)


@pytest.fixture
def index():
    return A.StreetIndex([
        _street(1, "E", "10th", "Ave", 900),
        _street(2, "E", "10th", "Pl", 20),
        _street(3, "W", "10th", "Ave", 300),
        _street(4, "E", "Colfax", "Ave", 4000),
        _street(5, None, "Champa", "St", 800),
        _street(6, "E", "106th", "Ave", 40),
        _street(7, None, "17th", "St", 600),
        _street(8, None, "St Paul", "St", 150),
    ])


# --- parsing ----------------------------------------------------------------

def test_a_directional_is_not_eaten_as_a_house_number_suffix():
    """The bug this parser shipped with: "1226 e 10th ave" parsed as house
    number 1226 suffix "E" on a street called 10th, silently discarding the
    direction — making it identical to "1226 w 10th ave", which is the
    opposite side of the city. Exactly the wrong-side-of-town failure the
    address index exists to end."""
    east = A.parse_query("1226 e 10th ave")
    west = A.parse_query("1226 w 10th ave")
    assert east.predirectional == "E"
    assert west.predirectional == "W"
    assert east.number_text == "1226" and west.number_text == "1226"
    assert east != west


def test_a_genuine_letter_suffix_survives():
    """Denver writes those closed up, which is how they stay distinguishable
    from a directional."""
    p = A.parse_query("1226A E 10th Ave")
    assert p.number_text == "1226A"
    assert p.predirectional == "E"
    assert p.street_name == "10TH"


def test_a_fractional_number_does_not_leak_into_the_street():
    """Stripping punctuation before matching turned "1226 1/2 Champa" into
    "1226 1 2 CHAMPA" and glued the fraction onto the street name."""
    p = A.parse_query("1226 1/2 Champa")
    assert p.number == 1226
    assert "1/2" in p.number_text
    assert p.street_name == "CHAMPA"


def test_abbreviations_and_spelling_out_parse_the_same():
    a = A.parse_query("1226 E 10th Ave")
    b = A.parse_query("1226 East 10th Avenue")
    assert (a.number, a.predirectional, a.street_name, a.posttype) == \
           (b.number, b.predirectional, b.street_name, b.posttype)


def test_a_street_without_a_number_still_parses():
    p = A.parse_query("E Colfax Ave")
    assert p.number is None and not p.has_number
    assert (p.predirectional, p.street_name, p.posttype) == ("E", "COLFAX", "AVE")


def test_a_lone_directional_is_a_street_not_a_direction():
    """"1226 E" names a street called E, since nothing follows to be the
    street. Consuming it would leave the query with no street at all."""
    p = A.parse_query("1226 E")
    assert p.street_name == "E"
    assert p.predirectional is None


def test_a_street_type_inside_the_name_is_left_alone():
    """"St Paul St" — leading "St" is part of the name, trailing "St" is the
    type. Matching either anywhere mangles the street."""
    p = A.parse_query("1600 St Paul St")
    assert p.street_name == "ST PAUL"
    assert p.posttype == "ST"


# --- the index --------------------------------------------------------------

def test_a_three_character_prefix_finds_the_street(index):
    """Autocomplete's hardest input is its shortest. This is the case a trie
    is for and trigram similarity is worst at."""
    hits = index.prefix("10t")
    assert hits and all("10th" in h.display_name for h in hits)


def test_prefix_hits_are_ordered_by_how_busy_the_street_is(index):
    """A prefix can match a dozen streets; the one with thousands of addresses
    on it is the likelier intent."""
    hits = index.prefix("10")
    assert hits[0].display_name == "E 10th Ave"   # 900 points, vs 300 and 20


def test_a_numbered_street_is_reachable_without_its_ordinal(index):
    """Most of Denver's grid is numbered and nobody types "th"."""
    assert any(h.display_name == "E 10th Ave" for h in index.lookup("10"))


def test_a_typo_still_finds_the_street(index):
    """A rider who finished the word and got it wrong. Prefix cannot help;
    edit distance can, over a set small enough that the cost never shows."""
    assert index.lookup("colfx")[0].display_name == "E Colfax Ave"
    assert index.lookup("champia")[0].display_name == "Champa St"


def test_a_nonsense_query_finds_nothing(index):
    """Fuzzy must not mean "always answers". A bad match is worse than none —
    the caller falls through to Photon, which may genuinely know the place."""
    assert index.lookup("qqqqzzzzxx") == []


def test_edit_distance_gives_up_once_the_budget_is_blown():
    assert A._bounded_levenshtein("colfax", "colfax", 2) == 0
    assert A._bounded_levenshtein("colfx", "colfax", 2) == 1
    assert A._bounded_levenshtein("colfax", "champa", 2) is None


# --- how a number is written back -------------------------------------------

def test_a_fractional_address_is_not_mangled_into_the_label():
    """974 rows in the city file carry a "1/2" suffix. Concatenating it — and
    normalising the slash away first — rendered 4039 1/2 N Wyandot St as
    "40391 2 N Wyandot St", which is exactly the label a rider cannot trust
    and the reason the index exists at all."""
    assert A.house_number_text(4039, "1/2") == "4039 1/2"


def test_a_bare_letter_still_closes_up():
    """1226B is a real neighbouring door and Denver writes it closed up."""
    assert A.house_number_text(1226, "B") == "1226B"


def test_a_word_suffix_is_spaced_the_way_the_city_writes_it():
    """The suffix column is not only letters: BSMT, REAR, UPPER and CA all
    appear, and one row carries a second number. The city's own FULL_ADDRESS
    spaces them ("11400 11420 E 51st Ave"), so this matches."""
    assert A.house_number_text(1226, "BSMT") == "1226 BSMT"
    assert A.house_number_text(11400, "11420") == "11400 11420"


def test_no_suffix_is_just_the_number():
    assert A.house_number_text(1226, None) == "1226"
    assert A.house_number_text(1226, "") == "1226"


# --- the index has to notice a rebuild --------------------------------------

def test_an_empty_index_is_retried_rather_than_cached_forever(monkeypatch):
    """THE DEPLOY-ORDER TRAP. The migration creates the tables empty, the API
    starts, its first address query builds an index over zero rows, and the
    load job runs afterwards — in a different container, which cannot reach
    into this process. Cached for the life of the process, that empty index
    makes the whole feature a silent no-op: every lookup returns nothing and
    falls through to Photon, which is also what success looks like."""
    calls = {"n": 0}

    def rows(_refresh=False):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [_street(1, "E", "10th", "Ave")]

    monkeypatch.setattr(A, "_INDEX", None)
    monkeypatch.setattr(A, "_INDEX_BUILT_AT", 0.0)
    monkeypatch.setattr(A, "_load_street_rows", rows)
    # A clock the test controls, so this asserts on the TTL rather than on
    # having waited a real minute.
    clock = {"t": 1000.0}
    monkeypatch.setattr(A.time, "monotonic", lambda: clock["t"])

    assert len(A.street_index()) == 0
    clock["t"] += A._EMPTY_INDEX_TTL_SECONDS + 1
    assert len(A.street_index()) == 1, "an empty index must be retried"


def test_a_loaded_index_is_not_re_read_on_every_query(monkeypatch):
    """The other half: 909 streets do not change between two keystrokes."""
    calls = {"n": 0}

    def rows(_refresh=False):
        calls["n"] += 1
        return [_street(1, "E", "10th", "Ave")]

    monkeypatch.setattr(A, "_INDEX", None)
    monkeypatch.setattr(A, "_INDEX_BUILT_AT", 0.0)
    monkeypatch.setattr(A, "_load_street_rows", rows)
    monkeypatch.setattr(A.time, "monotonic", lambda: 1000.0)

    for _ in range(5):
        A.street_index()
    assert calls["n"] == 1


def test_a_failed_re_read_keeps_the_index_it_already_had(monkeypatch):
    """A database blip on the scheduled re-read must not swap a working index
    out for an empty one. Stale street names are harmless; no street names
    silently disables address search."""
    state = {"fail": False}

    def rows(_refresh=False):
        return [] if state["fail"] else [_street(1, "E", "10th", "Ave")]

    monkeypatch.setattr(A, "_INDEX", None)
    monkeypatch.setattr(A, "_INDEX_BUILT_AT", 0.0)
    monkeypatch.setattr(A, "_load_street_rows", rows)
    clock = {"t": 1000.0}
    monkeypatch.setattr(A.time, "monotonic", lambda: clock["t"])

    assert len(A.street_index()) == 1
    state["fail"] = True
    clock["t"] += A._INDEX_TTL_SECONDS + 1
    assert len(A.street_index()) == 1, "a blip must not empty a working index"


# --- ingest refuses to publish a partial city -------------------------------

def test_pagination_follows_the_service_not_the_page_size(monkeypatch):
    """ARCGIS_PAGE matches the layer's maxRecordCount today. If Denver lowers
    it, the server clamps silently — and a `len(rows) < ARCGIS_PAGE` test would
    read one short page, conclude it had finished, and index a fraction of the
    city while reporting success. Every query would still answer, just not for
    most streets."""
    import inspect
    src = inspect.getsource(A.refresh_address_points)
    assert "if not more:" in src
    assert "len(rows) < ARCGIS_PAGE" not in src


def test_a_short_read_leaves_the_previous_index_serving(monkeypatch):
    """Losing half the city silently is worse than serving last week's copy."""
    pages = [([{"LATITUDE": 39.7, "LONGITUDE": -104.9, "STREET_NAME": "10TH",
                "ADDRESS_NUMBER": 1226, "PREDIRECTIONAL": "E",
                "POSTTYPE": "AVE"}], False)]
    monkeypatch.setattr(A, "_fetch_page", lambda c, o: pages[0])
    monkeypatch.setattr(A, "_fetch_count", lambda c: 413405)

    def no_database(*a, **k):
        raise AssertionError("a short read must never reach the swap")

    monkeypatch.setattr(A, "connection", no_database)
    out = A.refresh_address_points()
    assert "short read" in out["error"]
    assert out["fetched"] == 1 and out["expected"] == 413405


# --- normalisation ----------------------------------------------------------

def test_the_source_is_not_trusted_raw():
    """The city ships "10th", "109TH" and "101ST" in one column, plus NULL
    street names. Everything is normalised on the way in."""
    a, _, _ = A.street_keys("East", "10TH", "AVENUE", None)
    b, _, _ = A.street_keys("e", "10th", "ave", None)
    assert a == b == "E 10TH AVE"


def test_display_names_read_like_an_address():
    _, _, disp = A.street_keys("E", "10TH", "AVE", None)
    assert disp == "E 10th Ave"
    _, _, disp2 = A.street_keys(None, "COLFAX", "AVENUE", None)
    assert disp2 == "Colfax Ave"


def test_ordinal_stem_only_applies_to_ordinals():
    assert A.ordinal_stem("10TH") == "10"
    assert A.ordinal_stem("101ST") == "101"
    assert A.ordinal_stem("COLFAX") is None


# --- the geocoder defers to the index, and the index never blocks -----------

def test_the_address_index_is_consulted_before_photon():
    """OSM does not carry Denver's house numbers, so a numbered query has to
    reach the city file first — asking Photon and ranking harder cannot find
    an address that was never mapped."""
    import inspect
    from src import api_geocode
    src = inspect.getsource(api_geocode.query_photon)
    assert "addresses.lookup" in src
    assert src.index("addresses.lookup") < src.index("_fetch(upstream")


def test_a_broken_address_index_never_fails_a_search(monkeypatch):
    """The index is an improvement layered over Photon, not a dependency of
    it. A database blip, a missing table or an unparseable query must degrade
    to exactly the behaviour that existed before it."""
    from src import api_geocode

    def boom(*a, **k):
        raise RuntimeError("index on fire")

    monkeypatch.setattr(api_geocode.addresses, "lookup", boom)
    called = {}

    def fake_fetch(upstream, q, params):
        called["hit"] = True
        return {"features": []}

    monkeypatch.setattr(api_geocode, "_fetch", fake_fetch)
    api_geocode.query_photon("http://photon", "1226 E 10th Ave", None, None, 5)
    assert called.get("hit"), "photon must still be consulted"


def test_a_street_only_hit_is_not_returned_as_a_point():
    """lookup() answers a query with no house number with STREET rows carrying
    no coordinates. Those must not be dressed up as resolved addresses — the
    caller filters on lat/lon being present."""
    import inspect
    from src import api_geocode
    src = inspect.getsource(api_geocode.query_photon)
    assert 'h.get("lat") is not None' in src
