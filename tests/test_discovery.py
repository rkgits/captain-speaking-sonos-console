"""
Unit tests for finding speakers after a network change, the rescan endpoint and the dashboard
network panel. No speakers and no running server needed.

    pytest tests/test_discovery.py
"""
import asyncio
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A  # noqa: E402

COORD = "RINCON_TEST"
SPEAKER = {"uuid": COORD, "name": "Kitchen", "ip": "192.168.1.20"}


@pytest.fixture(autouse=True)
def clean_state():
    A._reset_discovery()
    A.STATE.network_prefix = None
    A.STATE.last_error = None
    A._refresh_lock = None
    yield
    A._reset_discovery()
    A._refresh_lock = None


def _fake_network(monkeypatch, prefix, speakers_on_this_network):
    """Pretend the computer is on `prefix` and speakers are reachable only if listed."""
    monkeypatch.setattr(A, "get_local_prefix", lambda: prefix)
    monkeypatch.setattr(A, "prime_arp", lambda p: None)

    async def find_seed(client):
        return speakers_on_this_network[0]["ip"] if speakers_on_this_network else None

    async def fetch_topology(client, seed):
        devs = {s["uuid"]: dict(s) for s in speakers_on_this_network}
        return devs, [{"coordinator_uuid": speakers_on_this_network[0]["uuid"],
                       "member_uuids": list(devs)}]

    async def attach(devices):
        for d in devices.values():
            d["model"] = "Era 100"

    async def zero(*a, **k):
        return 20

    async def false_(*a, **k):
        return False

    async def transport(client, ip):
        return {"state": "STOPPED", "title": None, "artist": None, "art": None}

    monkeypatch.setattr(A, "find_seed", find_seed)
    monkeypatch.setattr(A, "fetch_topology", fetch_topology)
    monkeypatch.setattr(A, "attach_device_info", attach)
    monkeypatch.setattr(A, "get_volume", zero)
    monkeypatch.setattr(A, "get_mute", false_)
    monkeypatch.setattr(A, "get_group_volume", zero)
    monkeypatch.setattr(A, "get_transport", transport)


def test_joining_a_different_network_forgets_the_old_speakers(monkeypatch):
    # Speakers were found at home...
    _fake_network(monkeypatch, "192.168.1.", [SPEAKER])
    asyncio.run(A.refresh_state())
    assert list(A.STATE.devices) == [COORD]

    # ...then the laptop joins a network with no speakers: the old ones must disappear
    _fake_network(monkeypatch, "10.0.0.", [])
    asyncio.run(A.refresh_state())
    assert A.STATE.devices == {}
    assert A.STATE.ip_pool == set()
    assert A.STATE.network_prefix == "10.0.0."
    assert A.STATE.last_error == "No Sonos device reachable"

    # ...and a network that has speakers finds them again without any click
    other = {"uuid": "RINCON_OFFICE", "name": "Office", "ip": "172.16.5.9"}
    _fake_network(monkeypatch, "172.16.5.", [other])
    asyncio.run(A.refresh_state())
    assert [d["name"] for d in A.STATE.devices.values()] == ["Office"]
    assert A.STATE.devices["RINCON_OFFICE"]["model"] == "Era 100"


def test_rescan_endpoint_returns_what_it_found(monkeypatch):
    _fake_network(monkeypatch, "192.168.1.", [SPEAKER])
    monkeypatch.setattr(A, "get_network_info", lambda: {"ssid": "Home", "ip": "192.168.1.5"})
    A.STATE.devices = {"RINCON_OLD": {"uuid": "RINCON_OLD", "name": "Stale", "ip": "10.9.9.9"}}
    r = TestClient(A.app).post("/rescan")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["found"] == 1 and body["speakers"] == ["Kitchen"]
    assert "RINCON_OLD" not in A.STATE.devices          # the stale speaker was dropped
    assert body["network"]["ssid"] == "Home"


def test_rescan_reports_when_nothing_is_found(monkeypatch):
    _fake_network(monkeypatch, "10.0.0.", [])
    monkeypatch.setattr(A, "get_network_info", lambda: {"ssid": None})
    body = TestClient(A.app).post("/rescan").json()
    assert body["ok"] is False and body["found"] == 0
    assert body["error"] == "No Sonos device reachable"


def test_dashboard_shows_network_and_model(monkeypatch):
    A.STATE.devices = {COORD: {**SPEAKER, "model": "Beam", "model_number": "S31",
                               "software": "97.2-81220", "announcements": True}}
    monkeypatch.setattr(A, "get_network_info", lambda: {
        "interface": "en0", "connection": "wifi", "ssid": "Home Wi-Fi", "ssid_hidden": False,
        "ip": "192.168.1.5", "subnet": "192.168.1.0/24", "gateway": "192.168.1.1"})
    page = TestClient(A.app).get("/dashboard").text
    assert "Rescan for speakers" in page
    assert "Home Wi-Fi" in page and "192.168.1.0/24" in page
    assert "Beam" in page and "S31" in page and "97.2-81220" in page


def test_dashboard_explains_when_macos_hides_the_wifi_name(monkeypatch):
    monkeypatch.setattr(A, "get_network_info", lambda: {
        "interface": "en0", "connection": "wifi", "ssid": None, "ssid_hidden": True,
        "ip": "192.168.1.5", "subnet": "192.168.1.0/24", "gateway": "192.168.1.1"})
    page = TestClient(A.app).get("/dashboard").text
    assert "Hidden by macOS" in page and "Location Services" in page
