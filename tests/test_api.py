"""
API contract tests for Captain Speaking.
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


class TestAttention:
    """Side-effect-free checks only — never uploads/broadcasts (would play audio on real speakers)."""

    def test_state_exposes_https_port(self, client):
        assert "https_port" in client.get("/state").json()

    def test_list_returns_recordings_array(self, client):
        r = client.get("/attention/list")
        assert r.status_code == 200
        assert isinstance(r.json()["recordings"], list)

    def test_broadcast_missing_file_returns_404(self, client):
        r = client.post("/attention/broadcast", json={"file": "does-not-exist.wav", "volume": 40})
        assert r.status_code == 404

    def test_delete_missing_file_returns_404(self, client):
        r = client.request("DELETE", "/attention/does-not-exist.wav")
        assert r.status_code == 404

    def test_delete_rejects_path_traversal(self, client):
        r = client.request("DELETE", "/attention/..%2Fapp.py")
        assert r.status_code == 404


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


class TestAlbumArt:
    def test_art_unknown_uuid_returns_404(self, client):
        r = client.get("/art/nonexistent-uuid")
        assert r.status_code == 404
