import pytest

from groupreservations.auth import mint_access_token, validate_user_id, verify_access_token


def test_access_token_round_trips_organizer_identity(monkeypatch):
    monkeypatch.setenv("GROUP_RESERVATIONS_JWT_SECRET", "test-secret-with-at-least-32-characters")
    token = mint_access_token("organizer-123")
    assert verify_access_token(token) == "organizer-123"


def test_access_token_rejects_tampering(monkeypatch):
    monkeypatch.setenv("GROUP_RESERVATIONS_JWT_SECRET", "test-secret-with-at-least-32-characters")
    token = mint_access_token("organizer-123")
    with pytest.raises(Exception):
        verify_access_token(token + "tampered")


def test_user_id_validation_rejects_unsafe_values():
    with pytest.raises(ValueError):
        validate_user_id("organizer/other-user")
