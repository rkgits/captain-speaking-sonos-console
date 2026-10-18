"""
Unit tests for how the server notices a track change (no speakers, no running server needed).

    pytest tests/test_track_change.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A  # noqa: E402

COORD = "RINCON_TEST"


def _transport(title, art):
    return {"state": "PLAYING", "title": title, "artist": "Artist", "art": art}


def test_signature_changes_with_artwork_even_if_title_is_the_same():
    a = _transport("Song", "http://x/1.jpg")
    b = _transport("Song", "http://x/2.jpg")
    assert A._transport_sig(a) != A._transport_sig(b)


def test_refresh_coordinator_pushes_to_open_pages_when_the_track_changes(monkeypatch):
    A.STATE.devices = {COORD: {"uuid": COORD, "name": "Test", "ip": "192.0.2.1"}}
    A.STATE.transport = {COORD: _transport("Song One", "http://x/1.jpg")}
    reported = {"value": _transport("Song One", "http://x/1.jpg")}

    async def fake_get_transport(client, ip):
        return dict(reported["value"])

    monkeypatch.setattr(A, "get_transport", fake_get_transport)
    queue = asyncio.Queue(maxsize=5)
    A._sse_clients.add(queue)
    try:
        # Unchanged track: nothing is pushed
        asyncio.run(A.refresh_coordinator(COORD))
        assert queue.empty()

        # The speaker now reports the next track: open pages are told straight away
        reported["value"] = _transport("Song Two", "http://x/2.jpg")
        asyncio.run(A.refresh_coordinator(COORD))
        assert not queue.empty()
        assert "Song Two" in queue.get_nowait()
    finally:
        A._sse_clients.discard(queue)
