"""Priority classifier and location-extraction tests."""

from __future__ import annotations

import pytest

from app.db.models.enums import ReportPriority
from app.integrations import gazetteer
from app.services.classification import classify_text

# Priority classification


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Life-threatening -> critical
        ("URGENT: family of four trapped on the second floor, water still rising",
         ReportPriority.CRITICAL),
        ("Medical help needed - elderly neighbour is diabetic and out of insulin",
         ReportPriority.CRITICAL),
        ("My grandmother is missing since yesterday evening, please share",
         ReportPriority.CRITICAL),
        ("Gas leak reported near the market, area being evacuated", ReportPriority.CRITICAL),
        # Urgent need -> high
        ("Need drinking water near Manhattan, we have been without supply since morning",
         ReportPriority.HIGH),
        ("Roads into the area are completely blocked, do not attempt to drive in",
         ReportPriority.HIGH),
        ("Shelter at the school has reached capacity, they are turning people away",
         ReportPriority.HIGH),
        # Situational -> medium
        ("Power has been out across the district for over 14 hours now", ReportPriority.MEDIUM),
        ("Anyone know if the relief camp is still accepting people?", ReportPriority.MEDIUM),
        # Reassuring / informational -> low
        ("Volunteers distributing food packets at the community hall", ReportPriority.LOW),
        ("Mobile network is patchy but we are all safe here", ReportPriority.LOW),
        ("Clean-up crews have started work in the eastern part of town", ReportPriority.LOW),
        ("Water level dropping slowly, some streets now passable", ReportPriority.LOW),
    ],
)
def test_reports_land_in_the_expected_priority_bucket(text: str, expected: ReportPriority):
    assert classify_text(text).priority == expected


def test_classification_is_deterministic():
    """The same text must always score identically, or the feature is untestable."""
    text = "URGENT: two people trapped in a collapsed building, send help immediately"
    results = [classify_text(text) for _ in range(5)]

    assert len({r.score for r in results}) == 1
    assert len({r.priority for r in results}) == 1


def test_every_score_is_explainable():
    """matched_signals must name the rules that fired."""
    result = classify_text("URGENT: child trapped, we need water and medical help")

    assert result.signals, "a scored report must say why it scored"
    assert any("trapped" in s for s in result.signals)
    assert any(s.startswith("life_threatening:") for s in result.signals)
    assert 0.0 <= result.score <= 1.0


def test_reassurance_pulls_the_score_down():
    """Negative weights must actually de-escalate, not merely fail to add."""
    worried = classify_text("power is out and roads are blocked")
    reassured = classify_text("power is out and roads are blocked but we are all safe")

    assert reassured.score < worried.score


def test_vulnerable_group_raises_an_otherwise_identical_report():
    plain = classify_text("neighbour needs food")
    vulnerable = classify_text("elderly neighbour needs food")

    assert vulnerable.score > plain.score
    assert "vulnerable_group" in vulnerable.signals


def test_overlapping_signals_are_not_double_counted():
    """Regression test for a real defect found while tuning."""
    result = classify_text("Baby formula and diapers urgently needed at the relief centre")

    assert result.priority == ReportPriority.HIGH, (
        f"a supply request should be high, not {result.priority} (score {result.score})"
    )
    assert "vulnerable_group" not in result.signals, (
        "'baby' is already inside the matched phrase 'baby formula'"
    )


def test_empty_and_whitespace_input_is_safe():
    for text in ("", "   ", "\n\t"):
        result = classify_text(text)
        assert result.priority == ReportPriority.LOW
        assert result.score == 0.0
        assert result.signals == []


def test_score_is_clamped_to_the_unit_interval():
    """Stacking every signal must not produce a score above 1.0."""
    extreme = (
        "URGENT EMERGENCY SOS: 500 people trapped, children drowning, not breathing, "
        "gas leak, building collapsed, bleeding, cannot breathe since 3 days!!!"
    )
    result = classify_text(extreme)

    assert result.score == 1.0
    assert result.priority == ReportPriority.CRITICAL


# Location extraction


def test_manhattan_example_resolves():
    """"Heavy flooding has affected Manhattan, NYC." -> Manhattan, NYC -> coordinates."""
    result = gazetteer.extract_place("Heavy flooding has affected Manhattan, NYC.")

    assert result.place == "Manhattan, NYC"
    assert result.confidence >= 0.9

    entry = gazetteer.lookup(result.place)
    assert entry is not None
    assert entry.latitude == pytest.approx(40.7831, abs=0.01)
    assert entry.longitude == pytest.approx(-73.9712, abs=0.01)


@pytest.mark.parametrize(
    ("description", "expected_place"),
    [
        ("Severe landslides reported in Wayanad, Kerala after three days of rain.", "Kerala"),
        ("A magnitude 6.2 earthquake struck near Kathmandu overnight.", "Kathmandu"),
        ("Wildfire spreading across Los Angeles county, evacuations ordered.", "Los Angeles"),
        ("Cyclone Remal made landfall in Chattogram, Bangladesh this morning.", "Chattogram"),
        ("Water logging in Andheri, Mumbai has stranded commuters.", "Mumbai"),
    ],
)
def test_places_are_extracted_and_resolvable(description: str, expected_place: str):
    result = gazetteer.extract_place(description)

    assert result.place is not None
    entry = gazetteer.lookup(result.place)
    assert entry is not None, f"could not resolve {result.place!r}"
    assert expected_place.lower() in entry.name.lower()


def test_sentence_capitalisation_is_not_mistaken_for_a_place():
    """"Heavy" starts the sentence; it is not a location."""
    result = gazetteer.extract_place("Heavy flooding has affected Manhattan, NYC.")
    assert result.place != "Heavy"


def test_unknown_place_with_a_locative_cue_scores_lower_but_is_still_returned():
    """An unrecognised name still goes to the geocoder, flagged as less certain."""
    result = gazetteer.extract_place("Building collapse reported at Zindabazar Road, Sylhet.")

    assert result.place is not None
    assert "Sylhet" in result.place
    assert 0.4 <= result.confidence < 0.8
    assert gazetteer.lookup(result.place) is None


def test_extraction_stops_at_a_sentence_boundary():
    """Regression test for a defect caught while seeding."""
    result = gazetteer.extract_place(
        "A large fire has broken out at a warehouse in Brooklyn. "
        "Smoke is drifting across nearby residential blocks."
    )

    assert result.place == "Brooklyn"
    assert gazetteer.lookup(result.place) is not None


def test_abbreviations_are_not_mistaken_for_sentence_ends():
    """The other half of the same fix: "St." must not truncate the name."""
    result = gazetteer.extract_place("Damage reported near St. Louis. Crews are responding.")
    assert result.place == "St. Louis"


def test_text_with_no_place_returns_nothing():
    result = gazetteer.extract_place("something bad is happening and nobody knows where")

    assert result.place is None
    assert result.confidence == 0.0


def test_lookup_handles_aliases_and_embedded_names():
    assert gazetteer.lookup("NYC").name.startswith("New York")
    assert gazetteer.lookup("bombay").name.startswith("Mumbai")
    assert gazetteer.lookup("downtown Manhattan, NYC") is not None
    assert gazetteer.lookup("Atlantis") is None
