"""Foreign cities whose country code collides with a US state / CA province."""
from utils.geo import parse_location


def test_tel_aviv_il_is_israel_not_illinois():
    got = parse_location("Tel Aviv, IL")
    assert got.country_code == "IL"
    assert got.admin1_code is None
    assert got.locality == "Tel Aviv"


def test_bangalore_in_is_india_not_indiana():
    got = parse_location("bangalore, IN")
    assert got.country_code == "IN"
    assert got.admin1_code is None


def test_indian_state_abbrev_does_not_win():
    """TN is Tamil Nadu here, not Tennessee."""
    assert parse_location("Chennai, TN").country_code == "IN"
    assert parse_location("Pune, MH").country_code == "IN"


def test_indian_state_spelled_out():
    got = parse_location("Bangalore, Karnataka")
    assert got.country_code == "IN"
    assert got.locality == "Bangalore"


def test_bare_foreign_city_resolves():
    assert parse_location("Tel Aviv").country_code == "IL"
    assert parse_location("Herzliya").country_code == "IL"
    assert parse_location("Casablanca").country_code == "MA"


def test_us_cities_sharing_those_abbrevs_stay_us():
    assert parse_location("Springfield, IL").country_code == "US"
    assert parse_location("Springfield, IL").admin1_code == "IL"
    assert parse_location("Indianapolis, IN").admin1_code == "IN"
    assert parse_location("Boise, ID").admin1_code == "ID"
    assert parse_location("Boston, MA").admin1_code == "MA"
    assert parse_location("Saskatoon, SK").country_code == "CA"


def test_ambiguous_city_needs_iso_code():
    """Berlin is Germany only when the posting says DE."""
    assert parse_location("Berlin, DE").country_code == "DE"
    assert parse_location("Berlin, CT").country_code == "US"
    assert parse_location("Berlin, CT").admin1_code == "CT"
    assert parse_location("Lima, PE").country_code == "PE"
    assert parse_location("Lima, OH").admin1_code == "OH"
    assert parse_location("Delhi, IN").country_code == "IN"
    assert parse_location("Delhi, CA").country_code == "US"


def test_spelled_out_country_still_works():
    assert parse_location("Tel Aviv, Israel").country_code == "IL"
    assert parse_location("Remote - India").country_code == "IN"


def test_us_ca_preferred_locations_unaffected():
    sac = parse_location("Sacramento, CA")
    assert (sac.country_code, sac.admin1_code) == ("US", "CA")
    assert parse_location("California").admin1_code == "CA"
    assert parse_location("Remote - US").country_code == "US"
    assert parse_location("Toronto, ON").country_code == "CA"
