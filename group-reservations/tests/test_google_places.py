from groupreservations.adapters import google_places


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"suggestions": []}


def test_autocomplete_restricts_guest_origin_to_selected_city(monkeypatch):
    captured = {}

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(google_places.httpx, "post", fake_post)

    google_places.autocomplete_locations(
        "test-key",
        "Mission",
        near_lat=37.7749,
        near_lng=-122.4194,
        radius_miles=75,
    )

    restriction = captured["json"]["locationRestriction"]["rectangle"]
    assert restriction["low"]["latitude"] < 37.7749
    assert restriction["high"]["latitude"] > 37.7749
    assert restriction["low"]["longitude"] < -122.4194
    assert restriction["high"]["longitude"] > -122.4194


def test_autocomplete_does_not_restrict_city_picker_without_context(monkeypatch):
    captured = {}

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(google_places.httpx, "post", fake_post)
    google_places.autocomplete_locations("test-key", "San", cities_only=True)

    assert "locationRestriction" not in captured["json"]
