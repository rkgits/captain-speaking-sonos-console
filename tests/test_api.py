"""
API contract tests for the Sonos app.
These run against a live server (staging or production) and do NOT require
Sonos hardware to be reachable — they test the HTTP API contract only.

Usage:
    TEST_URL=http://localhost:8788 pytest tests/
"""
import os
import httpx
import pytest

BASE_URL = os.environ.get("TEST_URL", "http://localhost:8788")


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        yield c


class TestHealth:
    def test_root_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "<html" in r.text.lower()

    def test_state_returns_200(self, client):
        r = client.get("/state")
        assert r.status_code == 200

    def test_state_has_expected_keys(self, client):
        r = client.get("/state")
        data = r.json()
        expected_keys = {"devices", "groups", "transport", "volumes", "group_volumes", "last_error"}
        assert expected_keys.issubset(data.keys())

    def test_state_devices_is_dict(self, client):
        r = client.get("/state")
        assert isinstance(r.json()["devices"], dict)

    def test_state_groups_is_list(self, client):
        r = client.get("/state")
        assert isinstance(r.json()["groups"], list)


class TestValidation:
    def test_volume_missing_fields_returns_422(self, client):
        r = client.post("/volume", json={})
        assert r.status_code == 422

    def test_volume_bad_type_returns_422(self, client):
        r = client.post("/volume", json={"uuid": "abc", "volume": "loud"})
        assert r.status_code == 422

    def test_mute_missing_fields_returns_422(self, client):
        r = client.post("/mute", json={})
        assert r.status_code == 422

    def test_play_missing_fields_returns_422(self, client):
        r = client.post("/play", json={})
        assert r.status_code == 422

    def test_join_missing_fields_returns_422(self, client):
        r = client.post("/join", json={})
        assert r.status_code == 422


class TestUnknownDevice:
    def test_volume_unknown_uuid_returns_404(self, client):
        r = client.post("/volume", json={"uuid": "nonexistent-device-uuid", "volume": 50})
        assert r.status_code == 404

    def test_play_unknown_uuid_returns_404(self, client):
        r = client.post("/play", json={"coordinator_uuid": "nonexistent-uuid"})
        assert r.status_code == 404

    def test_pause_unknown_uuid_returns_404(self, client):
        r = client.post("/pause", json={"coordinator_uuid": "nonexistent-uuid"})
        assert r.status_code == 404

    def test_mute_unknown_uuid_returns_404(self, client):
        r = client.post("/mute", json={"uuid": "nonexistent-uuid", "muted": True})
        assert r.status_code == 404
