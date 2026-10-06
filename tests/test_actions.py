"""
Unit tests for the custom actions. External calls are mocked, so the tests run
offline.   Run:  pytest tests/test_actions.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date  # noqa: E402

from actions.actions import (  # noqa: E402
    ActionAskDestination,
    ActionAskOrigin,
    ActionShowExperiences,
    ActionShowOffsets,
    _amadeus_flights,
    _parse_iso_duration,
    _parse_travel_dates,
    _place_info,
    ActionCalculateCarbonFootprint,
    ActionAskHandoverConsent,
    ActionDefaultFallback,
    ActionHandleAffirm,
    ActionHandleDeny,
    ActionFetchTravelOptions,
    ActionHandoverToHuman,
    ActionRankOptions,
    ValidateTripIntakeForm,
    _emission_category,
    _pick_transport,
    _verification_badge,
    _verification_status,
    _local_estimates,
    _normalise_place,
    _parse_amount,
)


class DummyDispatcher:
    def __init__(self):
        self.messages = []
        self.custom = []
        self.responses = []
        self.buttons = []

    def utter_message(self, text=None, json_message=None, response=None, buttons=None, **kwargs):
        if text:
            self.messages.append(text)
        if json_message:
            self.custom.append(json_message)
        if response:
            self.responses.append(response)
        if buttons:
            self.buttons.append(buttons)


class DummyTracker:
    def __init__(self, slots=None, events=None, sender_id="test-user"):
        self.slots = slots or {}
        self.events = events or []
        self.sender_id = sender_id

    def get_slot(self, name):
        return self.slots.get(name)


def _ev_user(intent):
    return {"event": "user", "text": "x", "parse_data": {"intent": {"name": intent}}}


def test_emission_category_boundaries():
    assert _emission_category(10) == "low"
    assert _emission_category(100) == "medium"
    assert _emission_category(300) == "high"
    assert _emission_category(None) == "unknown"


def test_helpers():
    assert _normalise_place("the Scottish Highlands ") == "scottish highlands"
    assert _parse_amount("1,500 euros") == 1500.0
    assert _parse_amount("no idea") is None


def test_local_estimates_train_cleaner_than_flight():
    distance, options = _local_estimates("Berlin", "Paris")
    modes = {o["mode"]: o["co2e_kg"] for o in options}
    assert distance > 500
    assert modes["train"] < modes["flight"]


def test_local_estimates_island_has_flight_only():
    _, options = _local_estimates("Berlin", "Iceland")
    assert [o["mode"] for o in options] == ["flight"]


@patch("actions.actions.CLIMATIQ_API_KEY", "")
def test_carbon_footprint_works_without_api_key():
    d, t = DummyDispatcher(), DummyTracker(slots={"destination": "Rome"})
    events = ActionCalculateCarbonFootprint().run(d, t, {})
    assert d.custom and d.custom[0]["type"] == "carbon_card"
    assert events[0]["name"] == "carbon_results"


@patch("actions.actions.CLIMATIQ_API_KEY", "fake")
@patch("actions.actions._safe_post", return_value=(None, "timeout"))
def test_carbon_footprint_api_failure_falls_back_to_local(_mock):
    d, t = DummyDispatcher(), DummyTracker(slots={"destination": "Lisbon"})
    ActionCalculateCarbonFootprint().run(d, t, {})
    # per-mode figures live in the card now (the text no longer repeats them)
    assert d.custom[0]["type"] == "carbon_card" and d.custom[0]["options"]
    assert any("estimates" in m.lower() for m in d.messages)
    assert "built-in" in d.custom[0]["source"]


@patch("actions.actions.CLIMATIQ_API_KEY", "fake")
@patch("actions.actions._safe_post", return_value=({"co2e": 42.0, "co2e_unit": "kg"}, None))
def test_carbon_footprint_uses_live_value(_mock):
    d, t = DummyDispatcher(), DummyTracker(slots={"destination": "Lisbon"})
    ActionCalculateCarbonFootprint().run(d, t, {})
    assert "Climatiq" in d.custom[0]["source"]


def test_carbon_footprint_without_destination_asks():
    d = DummyDispatcher()
    assert ActionCalculateCarbonFootprint().run(d, DummyTracker(), {}) == []
    assert d.buttons


def test_fetch_options_returns_curated_for_known_destination():
    d, t = DummyDispatcher(), DummyTracker(slots={"destination": "Lisbon"})
    events = ActionFetchTravelOptions().run(d, t, {})
    assert len(events[0]["value"]) >= 3


def test_fetch_options_unknown_destination_offers_handover():
    d, t = DummyDispatcher(), DummyTracker(slots={"destination": "Atlantis"})
    ActionFetchTravelOptions().run(d, t, {})
    assert any("human advisor" in m for m in d.messages)


def test_rank_prefers_certified_low_carbon_for_max_sustainability():
    options = [
        {"name": "Cheap Unverified", "price": 40, "eco_certified": False, "carbon_kg_per_night": 20},
        {"name": "Certified Lodge", "price": 120, "eco_certified": True, "carbon_kg_per_night": 6},
    ]
    t = DummyTracker(slots={"retrieved_options": options, "sustainability_level": "maximum_sustainability"})
    d = DummyDispatcher()
    events = ActionRankOptions().run(d, t, {})
    assert events[0]["value"][0]["name"] == "Certified Lodge"
    assert d.custom[0]["type"] == "hotel_carousel"


def test_rank_prefers_cheap_for_budget_conscious():
    options = [
        {"name": "Cheap", "price": 40, "eco_certified": False, "carbon_kg_per_night": 20},
        {"name": "Certified Lodge", "price": 120, "eco_certified": True, "carbon_kg_per_night": 6},
    ]
    t = DummyTracker(slots={"retrieved_options": options, "sustainability_level": "budget_conscious"})
    events = ActionRankOptions().run(DummyDispatcher(), t, {})
    assert events[0]["value"][0]["name"] == "Cheap"


def test_handover_sets_slot_even_if_endpoint_unreachable(tmp_path):
    t = DummyTracker(
        slots={"destination": "Kyoto", "budget": "1500"},
        events=[{"event": "user", "text": "hi", "timestamp": 1}],
    )
    with patch("actions.actions.HANDOVER_ENDPOINT", "http://x"), patch(
        "actions.actions._safe_post", return_value=(None, "connection_error")
    ), patch("actions.actions.HANDOVER_LOG_DIR", str(tmp_path)):
        d = DummyDispatcher()
        events = ActionHandoverToHuman().run(d, t, {})
    assert events[0]["value"] is True
    assert d.custom[0]["type"] == "handover"
    assert list(tmp_path.iterdir())  # context saved locally


def test_fallback_first_clarifies_then_second_asks_consent(tmp_path):
    first = DummyTracker(events=[_ev_user("nlu_fallback")])
    d1 = DummyDispatcher()
    ActionDefaultFallback().run(d1, first, {})
    assert d1.responses == ["utter_clarify_intent"]

    second = DummyTracker(
        events=[
            _ev_user("nlu_fallback"),
            {"event": "action", "name": "action_default_fallback"},
            {"event": "action", "name": "action_listen"},
            _ev_user("nlu_fallback"),
        ]
    )
    d2 = DummyDispatcher()
    with patch("actions.actions.HANDOVER_LOG_DIR", str(tmp_path)):
        events = ActionDefaultFallback().run(d2, second, {})
    # GDPR: nothing is shared yet - the consent question comes first
    assert not list(tmp_path.iterdir())
    assert "utter_handover_notice" not in d2.responses
    assert {"event": "slot", "name": "handover_reason", "value": "fallback_failed"}.items() <= events[0].items()
    assert "utter_ask_handover_consent" in d2.responses
    assert {"event": "slot", "name": "awaiting_handover_consent", "value": True}.items() <= events[1].items()


def test_fallback_counter_resets_after_understood_turn():
    t = DummyTracker(
        events=[
            _ev_user("nlu_fallback"),
            {"event": "action", "name": "action_default_fallback"},
            _ev_user("greet"),
            _ev_user("nlu_fallback"),
        ]
    )
    d = DummyDispatcher()
    ActionDefaultFallback().run(d, t, {})
    assert d.responses == ["utter_clarify_intent"]


def test_ask_destination_has_buttons():
    d = DummyDispatcher()
    ActionAskDestination().run(d, DummyTracker(), {})
    assert d.buttons and d.buttons[0][0]["payload"].startswith("/inform_destination")


def test_validate_destination_and_budget():
    v = ValidateTripIntakeForm()
    d = DummyDispatcher()
    assert v.validate_destination("the Scottish Highlands", d, DummyTracker(), {}) == {
        "destination": "Scottish Highlands"
    }
    assert v.validate_destination("Atlantis", d, DummyTracker(), {}) == {"destination": None}
    assert v.validate_budget("abc", d, DummyTracker(), {}) == {"budget": None}
    assert v.validate_budget("1500 euros", d, DummyTracker(), {}) == {"budget": "1500 euros"}


# ---- additions: anti-greenwashing labels, transport in ranking, handover reason


def test_demo_hotels_are_never_shown_as_verified():
    demo = {"name": "X", "eco_certified": True, "certification_body": "Green Key", "data_status": "demo"}
    assert _verification_status(demo) == "demo"
    assert "NOT verified" in _verification_badge(demo)
    assert "✅" not in _verification_badge(demo)


def test_amadeus_listing_is_unverified():
    assert _verification_status({"source": "amadeus_live", "eco_certified": False}) == "unverified"


def test_real_verified_record_gets_check_mark():
    rec = {"eco_certified": True, "certification_body": "Green Key", "data_status": "verified"}
    assert _verification_status(rec) == "verified"
    assert "Green Key" in _verification_badge(rec)


def test_pick_transport_lowest_and_preferred():
    res = [
        {"mode": "flight", "co2e_kg": 200.0, "label": "Flight"},
        {"mode": "train", "co2e_kg": 30.0, "label": "Train"},
    ]
    assert _pick_transport(res)["mode"] == "train"
    assert _pick_transport(res, "flight")["mode"] == "flight"
    assert _pick_transport(res, "boat")["mode"] == "train"  # unavailable -> lowest
    assert _pick_transport(None) is None


def test_rank_adds_transport_and_trip_total():
    options = [{"name": "Lodge", "price": 100, "eco_certified": True,
                "carbon_kg_per_night": 10, "data_status": "demo"}]
    carbon = [{"mode": "train", "co2e_kg": 30.0, "label": "Train"}]
    t = DummyTracker(slots={"retrieved_options": options, "carbon_results": carbon})
    d = DummyDispatcher()
    ActionRankOptions().run(d, t, {})
    hotel = d.custom[0]["hotels"][0]
    assert hotel["trip_total_kg_co2e"] == 2 * 30.0 + 10 * 4
    assert hotel["verification"] == "demo"
    assert any("Lowest-impact way" in m for m in d.messages)
    assert any("demo data" in m for m in d.messages)


def test_rank_works_without_carbon_results():
    options = [{"name": "Lodge", "price": 100, "carbon_kg_per_night": 10, "data_status": "demo"}]
    d = DummyDispatcher()
    ActionRankOptions().run(d, DummyTracker(slots={"retrieved_options": options}), {})
    assert d.custom[0]["hotels"][0]["trip_total_kg_co2e"] is None


@patch("actions.actions.CLIMATIQ_API_KEY", "")
def test_carbon_message_contains_co2e_tooltip():
    d = DummyDispatcher()
    ActionCalculateCarbonFootprint().run(d, DummyTracker(slots={"destination": "Rome"}), {})
    assert any("CO2e = carbon dioxide equivalent" in m for m in d.messages)


def test_handover_payload_contains_reason_and_context(tmp_path):
    import json
    t = DummyTracker(
        slots={"destination": "Kyoto", "ranked_options": [{"name": "Lodge", "price": 90, "score": 0.7}]},
        events=[{"event": "user", "text": "hi", "timestamp": 1}],
        sender_id="abc",
    )
    with patch("actions.actions.HANDOVER_ENDPOINT", ""), patch(
        "actions.actions.HANDOVER_LOG_DIR", str(tmp_path)
    ):
        d = DummyDispatcher()
        ActionHandoverToHuman().run(d, t, {})
    data = json.loads((tmp_path / "abc.json").read_text(encoding="utf-8"))
    assert data["reason"] == "user_request"
    assert data["recommendations_shown"][0]["name"] == "Lodge"
    assert d.custom[0]["reason"] == "user_request"


def test_fallback_handover_reason_is_fallback_failed(tmp_path):
    """After the user consents, the handover action reads the stored reason."""
    import json
    t = DummyTracker(
        slots={"handover_reason": "fallback_failed"},
        events=[{"event": "user", "text": "gibberish", "timestamp": 1}],
        sender_id="fb",
    )
    with patch("actions.actions.HANDOVER_ENDPOINT", ""), patch(
        "actions.actions.HANDOVER_LOG_DIR", str(tmp_path)
    ):
        ActionHandoverToHuman().run(DummyDispatcher(), t, {})
    assert json.loads((tmp_path / "fb.json").read_text(encoding="utf-8"))["reason"] == "fallback_failed"


# ---- date validation (assignment: reject nonsense such as "abc") ----------

TODAY = date(2026, 10, 3)


def test_dates_accept_typical_inputs():
    for text in ["12-18 October 2026", "3rd to 10th of March", "June 2027", "next weekend",
                 "around Christmas", "2026-12-20", "15.11.2026", "early spring", "in September"]:
        assert _parse_travel_dates(text, TODAY)[0] == "ok", text


def test_dates_reject_nonsense_and_numbers():
    for text in ["abc", "1500", "xyz 123", "", "banana", "99"]:
        assert _parse_travel_dates(text, TODAY)[0] == "invalid", text


def test_dates_reject_past_and_invalid_calendar_dates():
    assert _parse_travel_dates("12 March 2025", TODAY)[0] == "past"
    assert _parse_travel_dates("2026-02-30", TODAY)[0] == "invalid"
    assert _parse_travel_dates("June 2025", TODAY)[0] == "past"


def test_dates_year_rolls_over_when_month_already_passed():
    status, start = _parse_travel_dates("3 March", TODAY)
    assert status == "ok" and start == date(2027, 3, 3)


def test_dates_exact_start_extracted():
    assert _parse_travel_dates("12-18 October 2026", TODAY) == ("ok", date(2026, 10, 12))
    assert _parse_travel_dates("June 2027", TODAY) == ("ok", None)


def test_validate_dates_in_form():
    v = ValidateTripIntakeForm()
    d = DummyDispatcher()
    assert v.validate_travel_dates("abc", d, DummyTracker(), {}) == {"travel_dates": None}
    assert v.validate_travel_dates("12 March 2020", d, DummyTracker(), {}) == {"travel_dates": None}
    assert v.validate_travel_dates("December 2030", d, DummyTracker(), {}) == {"travel_dates": "December 2030"}
    assert len(d.messages) == 2  # one helpful re-prompt per rejection


def test_validate_budget_rejects_zero_and_negative_text():
    v = ValidateTripIntakeForm()
    d = DummyDispatcher()
    assert v.validate_budget("0", d, DummyTracker(), {}) == {"budget": None}
    assert v.validate_budget("lots", d, DummyTracker(), {}) == {"budget": None}


# ---- origin slot / location ------------------------------------------------

@patch("actions.actions.OPENCAGE_API_KEY", "")
def test_validate_origin_known_unknown_and_empty():
    v = ValidateTripIntakeForm()
    d = DummyDispatcher()
    assert v.validate_origin("hamburg", d, DummyTracker(), {}) == {"origin": "Hamburg"}
    assert v.validate_origin("Narnia", d, DummyTracker(), {}) == {"origin": None}
    assert v.validate_origin("", d, DummyTracker(), {}) == {"origin": None}
    assert d.buttons  # the unknown city got constrained quick replies


def test_ask_origin_has_inform_origin_buttons():
    d = DummyDispatcher()
    ActionAskOrigin().run(d, DummyTracker(), {})
    assert all(b["payload"].startswith("/inform_origin") for b in d.buttons[0])


@patch("actions.actions.OPENCAGE_API_KEY", "key")
@patch("actions.actions._GEOCODE_CACHE", {})
@patch("actions.actions._safe_get")
def test_opencage_geocoding_extends_known_places(mock_get):
    mock_get.return_value = ({"results": [{"geometry": {"lat": 50.94, "lng": 6.96},
                                           "components": {"continent": "Europe"}}]}, None)
    info = _place_info("Cologne")
    assert info[2] == "europe" and info[3] is None
    _place_info("Cologne")
    assert mock_get.call_count == 1  # cached


@patch("actions.actions.OPENCAGE_API_KEY", "key")
@patch("actions.actions._GEOCODE_CACHE", {})
@patch("actions.actions._safe_get", return_value=(None, "timeout"))
def test_opencage_failure_returns_none(_mock):
    assert _place_info("Nowhereville") is None


@patch("actions.actions.OPENCAGE_API_KEY", "")
def test_estimates_use_origin_from_slot_not_always_berlin():
    from_berlin = _local_estimates("Berlin", "Rome")[1]
    from_london = _local_estimates("London", "Rome")[1]
    assert from_berlin[0]["distance_km"] != from_london[0]["distance_km"]


# ---- Amadeus flights ---------------------------------------------------------

_FLIGHT_JSON = {"data": [
    {"price": {"total": "212.40"}, "validatingAirlineCodes": ["LH"],
     "itineraries": [{"duration": "PT2H35M", "segments": [{}, {}]}]},
    {"price": {"total": "99.90"}, "validatingAirlineCodes": ["FR"],
     "itineraries": [{"duration": "PT2H10M", "segments": [{}]}]},
    {"price": {"total": "bad"}, "itineraries": [{}]},
]}


@patch("actions.actions._safe_get", return_value=(_FLIGHT_JSON, None))
def test_flights_parsed_sorted_and_bad_rows_skipped(_mock):
    offers = _amadeus_flights("Berlin", "Rome", date(2026, 12, 1), "tok")
    assert [o["price_eur"] for o in offers] == [99.9, 212.4]
    assert offers[0]["stops"] == 0 and offers[0]["duration"] == "2h10"


@patch("actions.actions._safe_get", return_value=(None, "500"))
def test_flights_api_failure_returns_empty(_mock):
    assert _amadeus_flights("Berlin", "Rome", date(2026, 12, 1), "tok") == []


def test_flights_skipped_without_token_date_or_codes():
    assert _amadeus_flights("Berlin", "Rome", date(2026, 12, 1), None) == []
    assert _amadeus_flights("Berlin", "Rome", None, "tok") == []
    assert _amadeus_flights("Berlin", "Berlin", date(2026, 12, 1), "tok") == []
    assert _amadeus_flights("Cologne", "Rome", date(2026, 12, 1), "tok") == []  # no IATA code


def test_iso_duration():
    assert _parse_iso_duration("PT2H35M") == "2h35"
    assert _parse_iso_duration("PT45M") == "0h45"
    assert _parse_iso_duration(None) == ""


@patch("actions.actions._amadeus_flights", return_value=[{"price_eur": 120.0, "stops": 0,
       "duration": "2h00", "carrier": "LH", "departure_date": "2026-12-01"}])
@patch("actions.actions._amadeus_hotels", return_value=[])
@patch("actions.actions._get_amadeus_token", return_value="tok")
def test_fetch_stores_flight_offers_in_slot(_t, _h, _f):
    t = DummyTracker(slots={"destination": "Rome", "origin": "Berlin", "travel_dates": "1 December 2030"})
    events = ActionFetchTravelOptions().run(DummyDispatcher(), t, {})
    slots = {e["name"]: e["value"] for e in events}
    assert slots["flight_offers"][0]["price_eur"] == 120.0
    assert len(slots["retrieved_options"]) >= 1  # curated hotels still returned


@patch("actions.actions._get_amadeus_token", side_effect=RuntimeError("boom"))
def test_fetch_survives_amadeus_crash(_t):
    t = DummyTracker(slots={"destination": "Rome"})
    events = ActionFetchTravelOptions().run(DummyDispatcher(), t, {})
    assert {e["name"] for e in events} == {"retrieved_options", "flight_offers"}


def test_rank_mentions_flight_offer_as_sandbox_data():
    options = [{"name": "Lodge", "price": 100, "carbon_kg_per_night": 10, "data_status": "demo"}]
    flights = [{"price_eur": 120.0, "stops": 1, "duration": "3h10", "carrier": "LH", "departure_date": "2026-12-01"}]
    d = DummyDispatcher()
    ActionRankOptions().run(d, DummyTracker(slots={"retrieved_options": options, "flight_offers": flights}), {})
    assert any("sandbox TEST data" in m for m in d.messages)


# ---- experiences and offsets ------------------------------------------------

def test_experiences_for_known_destination_are_labelled_demo():
    d = DummyDispatcher()
    ActionShowExperiences().run(d, DummyTracker(slots={"destination": "Kyoto"}), {})
    assert "demo data" in d.messages[0] and "Kyoto" in d.messages[0]


def test_experiences_without_destination_asks_with_buttons():
    d = DummyDispatcher()
    ActionShowExperiences().run(d, DummyTracker(), {})
    assert d.buttons


def test_offsets_scale_with_trip_footprint_and_warn():
    ranked = [{"name": "Lodge", "trip_total_kg_co2e": 500.0}]
    d = DummyDispatcher()
    ActionShowOffsets().run(d, DummyTracker(slots={"ranked_options": ranked}), {})
    text = d.messages[0]
    assert "500.0 kg CO2e" in text and "€5.00-€12.50" in text  # 0.5 t * 10..25 EUR/t
    assert "do not cancel emissions" in text


def test_offsets_without_trip_still_answers():
    d = DummyDispatcher()
    ActionShowOffsets().run(d, DummyTracker(), {})
    assert "Typical programmes" in d.messages[0]


@patch("actions.actions._load_experience_data", return_value={})
def test_offsets_missing_data_offers_human(_m):
    d = DummyDispatcher()
    ActionShowOffsets().run(d, DummyTracker(), {})
    assert d.buttons and d.buttons[0][0]["payload"] == "/request_human_advisor"


# ---- Amadeus token cache -----------------------------------------------------

@patch("actions.actions.AMADEUS_API_KEY", "id")
@patch("actions.actions.AMADEUS_API_SECRET", "secret")
@patch("actions.actions._AMADEUS_TOKEN", {"value": None, "expires_at": 0.0})
@patch("actions.actions._safe_post", return_value=({"access_token": "abc", "expires_in": 1799}, None))
def test_amadeus_token_is_cached(mock_post):
    from actions.actions import _get_amadeus_token
    assert _get_amadeus_token() == "abc"
    assert _get_amadeus_token() == "abc"
    assert mock_post.call_count == 1


# ---- handover consent / privacy ----------------------------------------------

def test_handover_action_defaults_to_user_request_reason(tmp_path):
    with patch("actions.actions.HANDOVER_ENDPOINT", ""), patch(
        "actions.actions.HANDOVER_LOG_DIR", str(tmp_path)
    ):
        d = DummyDispatcher()
        ActionHandoverToHuman().run(d, DummyTracker(events=[]), {})
    assert d.custom[0]["reason"] == "user_request"


def test_handover_resets_reason_slot_after_use(tmp_path):
    with patch("actions.actions.HANDOVER_ENDPOINT", ""), patch(
        "actions.actions.HANDOVER_LOG_DIR", str(tmp_path)
    ):
        events = ActionHandoverToHuman().run(DummyDispatcher(), DummyTracker(slots={"handover_reason": "fallback_failed"}), {})
    assert {"event": "slot", "name": "handover_reason", "value": None}.items() <= events[1].items()


# ---- consent handlers --------------------------------------------------------
def test_ask_consent_sets_pending_flag():
    d = DummyDispatcher()
    events = ActionAskHandoverConsent().run(d, DummyTracker(), {})
    assert d.responses == ["utter_ask_handover_consent"]
    assert events[0]["name"] == "awaiting_handover_consent" and events[0]["value"] is True


def test_affirm_with_pending_consent_hands_over(tmp_path):
    t = DummyTracker(
        slots={"awaiting_handover_consent": True, "destination": "Rome"},
        events=[{"event": "user", "text": "hi", "timestamp": 1}],
    )
    d = DummyDispatcher()
    with patch("actions.actions.HANDOVER_ENDPOINT", ""), patch("actions.actions.HANDOVER_LOG_DIR", str(tmp_path)):
        events = ActionHandleAffirm().run(d, t, {})
    assert "utter_handover_notice" in d.responses
    assert list(tmp_path.iterdir())
    assert any(e["name"] == "awaiting_handover_consent" and e["value"] is False for e in events)


def test_affirm_without_pending_consent_shares_nothing(tmp_path):
    d = DummyDispatcher()
    with patch("actions.actions.HANDOVER_LOG_DIR", str(tmp_path)):
        events = ActionHandleAffirm().run(d, DummyTracker(), {})
    assert d.responses == ["utter_what_next"]
    assert events == [] and not list(tmp_path.iterdir())


def test_deny_with_pending_consent_declines_and_shares_nothing(tmp_path):
    d = DummyDispatcher()
    t = DummyTracker(slots={"awaiting_handover_consent": True})
    with patch("actions.actions.HANDOVER_LOG_DIR", str(tmp_path)):
        events = ActionHandleDeny().run(d, t, {})
    assert d.responses == ["utter_handover_declined"]
    assert not list(tmp_path.iterdir())
    assert any(e["name"] == "awaiting_handover_consent" and e["value"] is False for e in events)


# ------------------------------------------------------------ fixes (round 3)
from actions.actions import _parse_nights, _trip_nights, ActionGreet, ActionRankOptions  # noqa: E402


def test_nights_are_read_from_the_dates():
    assert _parse_nights("12-18 October 2026") == 6
    assert _parse_nights("3rd to 10th of March") == 7
    assert _parse_nights("28 October - 3 November 2026") == 6
    assert _parse_nights("next weekend") == 2
    assert _parse_nights("2 weeks") == 14
    assert _parse_nights("June 2027") is None


def test_nights_default_when_dates_have_no_length():
    assert _trip_nights(DummyTracker(slots={"travel_dates": "June 2027"})) == (4, True)
    assert _trip_nights(DummyTracker(slots={"travel_dates": "12-18 October 2026"})) == (6, False)


def test_rank_uses_nights_from_dates():
    opts = [{"name": "A", "price": 100, "eco_certified": True, "carbon_kg_per_night": 10}]
    d = DummyDispatcher()
    tracker = DummyTracker(slots={"retrieved_options": opts, "travel_dates": "12-18 October 2026",
                                  "budget": "500 euros", "sustainability_level": "balanced"})
    ActionRankOptions().run(d, tracker, {})
    assert d.custom[0]["nights"] == 6 and d.custom[0]["assumed_nights"] is False
    assert any("6 nights" in m for m in d.messages)
    assert any("over budget" in m.lower() or "budget" in m.lower() for m in d.messages)


def test_colours_follow_intensity_not_total():
    assert _emission_category(97.1, 2775) == "low"      # train: long but clean
    assert _emission_category(235.8, 2775) == "medium"  # shared car
    assert _emission_category(375.5, 2407) == "high"    # flight


def test_carbon_card_has_green_amber_red_and_specific_alert():
    d, t = DummyDispatcher(), DummyTracker(slots={"origin": "Berlin", "destination": "Lisbon"})
    with patch("actions.actions.CLIMATIQ_API_KEY", ""):
        ActionCalculateCarbonFootprint().run(d, t, {})
    card = d.custom[0]
    cats = {o["mode"]: o["category"] for o in card["options"]}
    assert cats["train"] == "low" and cats["coach"] == "low"
    assert cats["car_shared"] == "medium" and cats["flight"] == "high" and cats["car"] == "high"
    assert "would cut that by roughly" in card["alert_text"]
    # the text no longer repeats the per-mode list
    assert not any("Coach:" in m for m in d.messages)


def test_greeting_is_short_the_second_time():
    ev = lambda: {"event": "user", "parse_data": {"intent": {"name": "greet"}}}
    d1 = DummyDispatcher()
    ActionGreet().run(d1, DummyTracker(events=[ev()]), {})
    d2 = DummyDispatcher()
    ActionGreet().run(d2, DummyTracker(events=[ev(), ev()]), {})
    assert d1.responses == ["utter_greet"] and d2.responses == ["utter_greet_again"]


def test_destination_not_overwritten_while_origin_is_requested():
    form = ValidateTripIntakeForm()
    events = [
        {"event": "slot", "name": "destination", "value": "Barcelona"},
        {"event": "user", "text": "Madrid"},
        {"event": "slot", "name": "destination", "value": "Madrid"},  # applied by Rasa before validation
    ]
    tracker = DummyTracker(slots={"destination": "Madrid", "requested_slot": "origin"}, events=events)
    d = DummyDispatcher()
    assert form.validate_destination("Madrid", d, tracker, {}) == {"destination": "Barcelona"}
    assert not d.messages
    tracker2 = DummyTracker(slots={"destination": "rome", "requested_slot": "destination"})
    assert form.validate_destination("rome", DummyDispatcher(), tracker2, {}) == {"destination": "Rome"}
