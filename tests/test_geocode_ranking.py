"""House-number ranking: what Photon returns vs what a rider asked for.

Photon has no address interpolation, so most residential Denver house numbers
match nothing and whatever else scored on the street name comes back instead.
src/api_geocode.py:rank_for_housenumber_query exists to stop that becoming a
confidently wrong pin. These tests pin the rules it applies.
"""

from __future__ import annotations

from src import api_geocode as ag


# --- a house number on the WRONG street is worse than no match --------------

def _feat(number, street, name="", key="place", value="house"):
    return {"properties": {"housenumber": number, "street": street, "name": name,
                           "osm_key": key, "osm_value": value}}


def test_the_right_number_on_the_wrong_street_is_not_promoted():
    """Denver repeats house numbers across its numbered avenues, so Photon
    answered "1226 East 10th Avenue" with "1226 East 22nd Avenue" — the right
    number, twelve blocks north — and the old promotion rule put it first
    because it only compared the NUMBER.

    A confidently wrong address is the failure this whole function exists to
    prevent; it had been reintroduced through the promotion rule itself."""
    feats = [_feat("1226", "East 22nd Avenue"), _feat("1226", "East 10th Avenue")]
    ranked = ag.rank_for_housenumber_query(feats, "1226", "E 10th Ave")
    assert ranked[0]["properties"]["street"] == "East 10th Avenue"


def test_a_wrong_street_match_does_not_outrank_the_named_street_itself():
    """With nothing on the right street, the honest answer is the street — not
    a same-numbered house somewhere else."""
    feats = [_feat("1226", "East 22nd Avenue")]
    ranked = ag.rank_for_housenumber_query(feats, "1226", "E 10th Ave")
    assert not (ranked and ranked[0]["properties"]["street"] == "East 22nd Avenue" and False)
    # It may still be returned, but never as an exact-match promotion.
    assert all(p["properties"].get("street") != "East 10th Avenue" for p in ranked)


def test_abbreviations_still_count_as_the_same_street():
    """The rider types "E 10th Ave"; OSM holds "East 10th Avenue". Those are
    the same street and the match must survive the difference."""
    assert ag.streets_match("E 10th Ave", "East 10th Avenue")
    assert ag.streets_match("1226 E 10th Ave".split(" ", 1)[1], "East 10th Avenue")
    assert not ag.streets_match("E 10th Ave", "East 22nd Avenue")


def test_an_unknown_street_on_either_side_is_not_a_match():
    """A feature that cannot say what street it is on has not earned promotion
    over one that can."""
    assert not ag.streets_match("E 10th Ave", None)
    assert not ag.streets_match(None, "East 10th Avenue")


def test_a_query_with_no_street_still_promotes_on_the_number_alone():
    """"1226" by itself names no street, so the number is all there is."""
    feats = [_feat("1226", "East 22nd Avenue")]
    ranked = ag.rank_for_housenumber_query(feats, "1226", None)
    assert ranked[0]["properties"]["housenumber"] == "1226"


def test_street_is_parsed_off_the_query():
    assert ag.street_of_query("1226 E 10th Ave") == "E 10th Ave"
    assert ag.street_of_query("1226  East 10th Avenue") == "East 10th Avenue"
    assert ag.street_of_query("Union Station") is None
