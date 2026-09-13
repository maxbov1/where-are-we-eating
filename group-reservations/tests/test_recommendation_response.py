from groupreservations.recommendation_response import parse_recommendation_answer
import json


def test_parser_extracts_confirmation_handoff_and_followups():
    result = parse_recommendation_answer(
        "Primary. Booking link: https://example.com/book\n"
        "Confirm reservation? [Confirm reservation](https://example.com/confirm)"
    )

    assert [link["kind"] for link in result["links"]] == ["handoff", "confirmation"]
    assert result["actions"][0] == {
        "id": "confirm_reservation",
        "label": "Review and confirm reservation",
        "kind": "confirmation",
        "url": "https://example.com/confirm",
    }
    assert {action["id"] for action in result["actions"][1:]} == {
        "booking_handoff_1", "show_alternatives", "adjust_preferences"
    }


def test_parser_deduplicates_markdown_and_plain_urls_and_rejects_non_http():
    result = parse_recommendation_answer(
        "[Restaurant](https://example.com) https://example.com\n"
        "javascript:alert(1)"
    )
    assert result["links"] == [{
        "label": "Restaurant", "url": "https://example.com", "kind": "source"
    }]


def test_parser_returns_structured_recommendation_and_reservation_actions():
    result = parse_recommendation_answer(json.dumps({
        "group_fit": "The group leaned toward lively Italian food.",
        "primary": {
            "name": "Pronto",
            "description": "Fresh pasta in a lively dining room.",
            "tradeoff": "Best fit for the group energy.",
            "availability": "Observed at 7:00 PM.",
            "restaurant_url": "https://example.com/pronto",
            "booking_url": "https://example.com/pronto/book",
            "booking_label": "Get Pronto's reservation",
        },
        "alternatives": [{
            "name": "Pasta House",
            "description": "A quieter neighborhood Italian option.",
            "tradeoff": "Less lively, but easier for conversation.",
            "restaurant_url": "https://example.com/pasta-house",
            "booking_url": "https://example.com/pasta-house/book",
            "booking_label": "Get Pasta House's reservation",
        }],
    }))

    assert result["recommendation"]["primary"]["name"] == "Pronto"
    assert result["recommendation"]["alternatives"][0]["restaurant_url"] == "https://example.com/pasta-house"
    assert [action["id"] for action in result["actions"][:2]] == [
        "get_primary_reservation", "get_alternative_reservation_1"
    ]
