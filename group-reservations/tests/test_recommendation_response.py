from groupreservations.recommendation_response import parse_recommendation_answer


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
