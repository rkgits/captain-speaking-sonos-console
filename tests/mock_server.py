"""
Run Captain Speaking against pretend speakers, so you can work on the UI without any Sonos hardware.

    python tests/mock_server.py            # then open http://localhost:8791
    python tests/mock_server.py --lag 2.0  # speakers take 2s to report a new track after Next

The real refresh loop, live-update stream and API all run; only the calls that would reach a
speaker are replaced. Four tracks with distinct artwork cycle on Next, and Next is slow to show
up (like a real speaker) so the "button pressed, track changes a moment later" path is exercised.
"""
import argparse
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A  # noqa: E402

COORD = "RINCON_MOCK0000000000001"
SPEAKERS = [
    (COORD, "Living Room", "192.0.2.1", "Sonos Beam", "S14"),
    ("RINCON_MOCK0000000000002", "Kitchen", "192.0.2.2", "Sonos Era 100", "E10"),
    ("RINCON_MOCK0000000000003", "Bedroom", "192.0.2.3", "Sonos Era 100", "E10"),
]
TRACKS = [("Song One", "Artist A"), ("Song Two", "Artist B"), ("Song Three", "Artist C"), ("Song Four", "Artist D")]
COLORS = [(200, 60, 60), (60, 160, 90), (70, 100, 200), (220, 170, 40)]

state = {"idx": 0, "next_at": 0.0, "lag": 0.8}


def _png(rgb, size=300):
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind, data):
        body = struct.pack(">I", len(data)) + kind + data
        return body + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _current() -> int:
    """The track the speaker reports. A Next press only shows up after `lag` seconds."""
    shown = state["idx"] + (1 if state["next_at"] and time.time() - state["next_at"] > state["lag"] else 0)
    return shown % len(TRACKS)


async def find_seed(client):
    return SPEAKERS[0][2]


async def fetch_topology(client, seed):
    devices = {u: {"uuid": u, "name": n, "ip": ip, "model": m, "model_number": mn} for u, n, ip, m, mn in SPEAKERS}
    return devices, [{"coordinator_uuid": COORD, "member_uuids": [s[0] for s in SPEAKERS]}]


async def get_volume(client, ip):
    return 30


async def get_mute(client, ip):
    return False


async def get_group_volume(client, ip):
    return 35


async def get_transport(client, ip):
    i = _current()
    title, artist = TRACKS[i]
    return {"state": "PLAYING", "uri": "x-sonos-vli:airplay:mock", "source": "AirPlay", "title": title,
            "artist": artist, "album": "Mock Album", "art": f"http://127.0.0.1:{PORT}/mock-art/{i}.png",
            "duration": "0:04:14", "elapsed": "0:01:10", "session": None}


async def do_next(client, ip):
    state["idx"] = _current()
    state["next_at"] = time.time()


async def noop(*args, **kwargs):
    return None


@A.app.get("/mock-art/{i}.png")
async def mock_art(i: int):
    from fastapi.responses import Response
    return Response(_png(COLORS[i % len(COLORS)]), media_type="image/png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--lag", type=float, default=0.8, help="seconds before a new track is reported after Next")
    args = parser.parse_args()
    PORT = args.port
    state["lag"] = args.lag
    for name, fn in dict(find_seed=find_seed, fetch_topology=fetch_topology, get_volume=get_volume,
                         get_mute=get_mute, get_group_volume=get_group_volume, get_transport=get_transport,
                         do_next=do_next, do_play=noop, do_pause=noop, do_prev=noop, set_volume=noop,
                         set_mute=noop, set_group_volume=noop, join_group=noop, leave_group=noop).items():
        setattr(A, name, fn)
    import uvicorn
    print(f"Mock speakers running: http://localhost:{PORT}")
    uvicorn.run(A.app, host="127.0.0.1", port=PORT, log_level="warning")
