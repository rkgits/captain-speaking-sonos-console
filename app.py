"""
Captain Speaking — a vintage-style local web console for Sonos speakers.
Run: uvicorn app:app --host 0.0.0.0 --port 8787
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import html
import ipaddress
import json
import logging
import os
import re
import subprocess
import time
import wave
from typing import Any

import urllib.parse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

VERSION = "1.0.0"

SOAP_TIMEOUT = 4.0
REFRESH_INTERVAL = 2.0

# Sonos local control (audioClip announcements) — well-known public local key, not a secret.
SONOS_API_KEY = "123e4567-e89b-12d3-a456-426655440000"
# Port Sonos speakers use to fetch recordings (plain HTTP; they can't use our self-signed HTTPS).
PUBLIC_HTTP_PORT = int(os.environ.get("PUBLIC_HTTP_PORT", "8787"))
# HTTPS port (for mic access on phones); surfaced to the UI so it can link there. 0 = none.
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "0"))
MAX_RECORDING_BYTES = 30 * 1024 * 1024

RC_SVC = "urn:schemas-upnp-org:service:RenderingControl:1"
AV_SVC = "urn:schemas-upnp-org:service:AVTransport:1"
GRC_SVC = "urn:schemas-upnp-org:service:GroupRenderingControl:1"
ZGT_SVC = "urn:schemas-upnp-org:service:ZoneGroupTopology:1"
RC_PATH = "/MediaRenderer/RenderingControl/Control"
AV_PATH = "/MediaRenderer/AVTransport/Control"
GRC_PATH = "/MediaRenderer/GroupRenderingControl/Control"
ZGT_PATH = "/ZoneGroupTopology/Control"


class State:
    def __init__(self):
        self.devices: dict[str, dict] = {}
        self.groups: list[dict] = []
        self.transport: dict[str, dict] = {}
        self.volumes: dict[str, dict] = {}
        self.group_volumes: dict[str, int] = {}
        self.ip_pool: set[str] = set()
        self.last_error: str | None = None
        self.network_prefix: str | None = None   # e.g. "192.168.1."; changes when the computer joins another network
        self.last_scan: float | None = None


STATE = State()
DEVICE_INFO: dict[str, dict] = {}   # uuid -> model details, read once per speaker
_refresh_lock: asyncio.Lock | None = None
_sse_clients: set[asyncio.Queue] = set()

# ---------- In-memory log ring buffer ----------
_log_ring: collections.deque = collections.deque(maxlen=200)

class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_ring.append({
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "msg": self.format(record),
        })

_log = logging.getLogger("captain-speaking")
_log.setLevel(logging.DEBUG)
_rh = _RingHandler()
_rh.setFormatter(logging.Formatter("%(message)s"))
_log.addHandler(_rh)
_log.propagate = False

app = FastAPI(title="Captain Speaking", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- SOAP ----------
def envelope(service: str, action: str, inner: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{inner}</u:{action}></s:Body>'
        "</s:Envelope>"
    )


async def soap(client: httpx.AsyncClient, ip: str, path: str, service: str, action: str, inner: str = "") -> str:
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service}#{action}"',
    }
    r = await client.post(
        f"http://{ip}:1400{path}",
        content=envelope(service, action, inner),
        headers=headers,
        timeout=SOAP_TIMEOUT,
    )
    r.raise_for_status()
    return r.text


def tag(txt: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<]*)</{name}>", txt)
    return html.unescape(m.group(1)) if m else None


# ---------- Topology ----------
async def fetch_topology(client: httpx.AsyncClient, ip: str) -> tuple[dict[str, dict], list[dict]]:
    txt = await soap(client, ip, ZGT_PATH, ZGT_SVC, "GetZoneGroupState")
    raw = html.unescape(html.unescape(txt))
    devices: dict[str, dict] = {}
    groups: list[dict] = []
    for grp in re.finditer(r"<ZoneGroup\s+([^>]*)>(.*?)</ZoneGroup>", raw, re.S):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', grp.group(1)))
        member_uuids: list[str] = []
        for m in re.finditer(r"<ZoneGroupMember\s+(.*?)/>", grp.group(2)):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            ipm = re.search(r"https?://([0-9.]+)", a.get("Location", ""))
            if not ipm:
                continue
            uuid = a.get("UUID")
            if not uuid:
                continue
            devices[uuid] = {
                "uuid": uuid,
                "name": a.get("ZoneName", "?"),
                "ip": ipm.group(1),
            }
            member_uuids.append(uuid)
        if member_uuids:
            groups.append({"coordinator_uuid": attrs.get("Coordinator"), "member_uuids": member_uuids})
    return devices, groups


# ---------- Rendering control ----------
async def get_volume(client: httpx.AsyncClient, ip: str) -> int | None:
    txt = await soap(
        client, ip, RC_PATH, RC_SVC, "GetVolume",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>",
    )
    v = tag(txt, "CurrentVolume")
    return int(v) if v is not None else None


async def get_mute(client: httpx.AsyncClient, ip: str) -> bool | None:
    txt = await soap(
        client, ip, RC_PATH, RC_SVC, "GetMute",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>",
    )
    v = tag(txt, "CurrentMute")
    return None if v is None else v == "1"


async def set_volume(client: httpx.AsyncClient, ip: str, v: int) -> None:
    v = max(0, min(100, v))
    await soap(
        client, ip, RC_PATH, RC_SVC, "SetVolume",
        f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{v}</DesiredVolume>",
    )


async def set_mute(client: httpx.AsyncClient, ip: str, muted: bool) -> None:
    await soap(
        client, ip, RC_PATH, RC_SVC, "SetMute",
        f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredMute>{1 if muted else 0}</DesiredMute>",
    )


# ---------- Transport ----------
SOURCE_MAP = [
    ("airplay:", "AirPlay"),
    ("sonos-spotify:", "Spotify Connect"),
    ("spotify:", "Spotify Connect"),
    ("x-rincon-stream:", "Line-in"),
    ("x-sonos-htastream:", "TV"),
    ("x-sonosapi-radio", "Radio"),
    ("x-sonosapi-hls", "Stream"),
    ("x-sonosapi-stream", "Stream"),
    ("x-rincon:", "Group member"),
    ("x-rincon-queue:", "Queue"),
    ("x-file-cifs:", "Library"),
]


def classify(uri: str) -> str:
    if not uri:
        return "Idle"
    for token, label in SOURCE_MAP:
        if token in uri:
            return label
    return "Unknown"


async def get_transport(client: httpx.AsyncClient, ip: str) -> dict[str, Any]:
    ti, mi, pi = await asyncio.gather(
        soap(client, ip, AV_PATH, AV_SVC, "GetTransportInfo", "<InstanceID>0</InstanceID>"),
        soap(client, ip, AV_PATH, AV_SVC, "GetMediaInfo", "<InstanceID>0</InstanceID>"),
        soap(client, ip, AV_PATH, AV_SVC, "GetPositionInfo", "<InstanceID>0</InstanceID>"),
    )
    raw_pi = html.unescape(html.unescape(pi))
    state = tag(ti, "CurrentTransportState")
    uri = tag(mi, "CurrentURI") or ""
    title = artist = album = None
    m = re.search(r"<dc:title>([^<]*)</dc:title>", raw_pi)
    if m:
        title = m.group(1)
    m = re.search(r"<dc:creator>([^<]*)</dc:creator>", raw_pi)
    if m:
        artist = m.group(1)
    m = re.search(r"<upnp:album>([^<]*)</upnp:album>", raw_pi)
    if m:
        album = m.group(1)
    art = None
    m = re.search(r"<upnp:albumArtURI>([^<]*)</upnp:albumArtURI>", raw_pi)
    if m:
        art = html.unescape(m.group(1)).strip() or None
        if art and art.startswith("/"):
            art = f"http://{ip}:1400{art}"
        elif art and not art.startswith(("http://", "https://")):
            art = None
    duration = tag(pi, "TrackDuration")
    elapsed = tag(pi, "RelTime")
    # Extract AirPlay/Spotify session tag if present
    session = None
    m = re.search(r"(?:airplay|spotify|sonos-spotify):([A-Za-z0-9]+)", uri)
    if m:
        session = m.group(1)
    return {
        "state": state,
        "uri": uri,
        "source": classify(uri),
        "title": title,
        "artist": artist,
        "album": album,
        "art": art,
        "duration": duration,
        "elapsed": elapsed,
        "session": session,
    }


# ---------- Grouping ----------
async def join_group(client: httpx.AsyncClient, ip: str, coordinator_uuid: str) -> None:
    await soap(
        client, ip, AV_PATH, AV_SVC, "SetAVTransportURI",
        f"<InstanceID>0</InstanceID>"
        f"<CurrentURI>x-rincon:{coordinator_uuid}</CurrentURI>"
        f"<CurrentURIMetaData></CurrentURIMetaData>",
    )


async def leave_group(client: httpx.AsyncClient, ip: str) -> None:
    await soap(
        client, ip, AV_PATH, AV_SVC, "BecomeCoordinatorOfStandaloneGroup",
        "<InstanceID>0</InstanceID>",
    )


# ---------- Group volume ----------
async def get_group_volume(client: httpx.AsyncClient, ip: str) -> int | None:
    txt = await soap(client, ip, GRC_PATH, GRC_SVC, "GetGroupVolume", "<InstanceID>0</InstanceID>")
    v = tag(txt, "CurrentVolume")
    return int(v) if v is not None else None


async def set_group_volume(client: httpx.AsyncClient, ip: str, v: int) -> None:
    v = max(0, min(100, v))
    await soap(
        client, ip, GRC_PATH, GRC_SVC, "SetGroupVolume",
        f"<InstanceID>0</InstanceID><DesiredVolume>{v}</DesiredVolume>",
    )


# ---------- Transport actions ----------
async def do_play(client: httpx.AsyncClient, ip: str) -> None:
    await soap(client, ip, AV_PATH, AV_SVC, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")


async def do_pause(client: httpx.AsyncClient, ip: str) -> None:
    await soap(client, ip, AV_PATH, AV_SVC, "Pause", "<InstanceID>0</InstanceID>")


async def do_next(client: httpx.AsyncClient, ip: str) -> None:
    await soap(client, ip, AV_PATH, AV_SVC, "Next", "<InstanceID>0</InstanceID>")


async def do_prev(client: httpx.AsyncClient, ip: str) -> None:
    await soap(client, ip, AV_PATH, AV_SVC, "Previous", "<InstanceID>0</InstanceID>")


# ---------- Discovery ----------
def _run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=3).decode(errors="replace").strip()
    except Exception:
        return ""


def default_interface() -> str | None:
    """The network interface the computer reaches the internet through (en0 for Wi-Fi on most Macs)."""
    m = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", "default"]))
    return m.group(1) if m else None


def _candidate_interfaces() -> list[str]:
    first = default_interface()
    return ([first] if first else []) + [i for i in ("en0", "en1") if i != first]


def get_local_ip() -> str | None:
    for iface in _candidate_interfaces():
        ip = _run(["ipconfig", "getifaddr", iface])
        if ip:
            return ip
    return None


def get_local_prefix() -> str | None:
    ip = get_local_ip()
    return ".".join(ip.split(".")[:3]) + "." if ip else None


def get_network_info() -> dict[str, Any]:
    """Where this computer is on the network: Wi-Fi name (when macOS allows it), IP, subnet, router."""
    iface = next((i for i in _candidate_interfaces() if _run(["ipconfig", "getifaddr", i])), None)
    info: dict[str, Any] = {"interface": iface, "connection": None, "ssid": None, "ssid_hidden": False,
                            "ip": None, "subnet": None, "gateway": None}
    if not iface:
        return info
    info["ip"] = _run(["ipconfig", "getifaddr", iface]) or None
    m = re.search(r"gateway:\s*(\S+)", _run(["route", "-n", "get", "default"]))
    info["gateway"] = m.group(1) if m else None
    m = re.search(r"netmask (0x[0-9a-fA-F]+)", _run(["ifconfig", iface]))
    if info["ip"] and m:
        try:
            mask = str(ipaddress.IPv4Address(int(m.group(1), 16)))
            info["subnet"] = str(ipaddress.ip_network(f"{info['ip']}/{mask}", strict=False))
        except ValueError:
            pass
    ports = _run(["networksetup", "-listallhardwareports"])
    port = re.search(r"Hardware Port:\s*(.+)\s*\nDevice:\s*" + re.escape(iface) + r"\b", ports)
    kind = port.group(1).strip() if port else ""
    info["connection"] = "wifi" if kind == "Wi-Fi" else ("ethernet" if "Ethernet" in kind or "LAN" in kind else "other")
    if info["connection"] == "wifi":
        out = _run(["networksetup", "-getairportnetwork", iface])
        m = re.search(r"Current Wi-Fi Network:\s*(.+)", out)
        if m:
            info["ssid"] = m.group(1).strip()
        else:
            # Recent macOS hides the network name from apps without Location Services permission
            summary = _run(["ipconfig", "getsummary", iface])
            m = re.search(r"\bSSID\s*:\s*(.+)", summary)
            name = m.group(1).strip() if m else ""
            if name and "redacted" not in name.lower():
                info["ssid"] = name
            else:
                info["ssid_hidden"] = True
    return info


def arp_ips() -> list[str]:
    try:
        out = subprocess.check_output(["arp", "-a"]).decode()
        return re.findall(r"\((\d+\.\d+\.\d+\.\d+)\)", out)
    except Exception:
        return []


def prime_arp(prefix: str) -> None:
    procs = []
    for i in range(1, 255):
        procs.append(subprocess.Popen(
            ["ping", "-c", "1", "-W", "150", f"{prefix}{i}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    for p in procs:
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()


async def find_seed(client: httpx.AsyncClient) -> str | None:
    """Find any reachable speaker. Candidates are probed together, so a stale address left over
    from another network does not hold things up."""
    prefix = get_local_prefix()
    cands = list(STATE.ip_pool)
    for ip in arp_ips():
        if ip not in cands:
            cands.append(ip)
    if prefix:
        cands = [ip for ip in cands if ip.startswith(prefix)]   # drop addresses from a previous network

    async def probe(ip: str) -> str:
        await asyncio.wait_for(soap(client, ip, ZGT_PATH, ZGT_SVC, "GetZoneGroupState"), timeout=2.5)
        return ip

    tasks = [asyncio.ensure_future(probe(ip)) for ip in cands[:64]]
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                return await fut
            except Exception:
                continue
        return None
    finally:
        for t in tasks:
            t.cancel()


async def fetch_device_info(ip: str) -> dict[str, Any]:
    """Model, firmware and announcement support, read from the speaker itself."""
    info: dict[str, Any] = {"model": None, "model_number": None, "software": None, "announcements": None}
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://{ip}:1400/xml/device_description.xml", timeout=3.0)
        r.raise_for_status()
        name = tag(r.text, "modelName") or tag(r.text, "displayName")
        info["model"] = re.sub(r"^Sonos\s+", "", name) if name else None
        info["model_number"] = tag(r.text, "modelNumber")
        info["software"] = tag(r.text, "softwareVersion")
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(verify=False) as c:
            r = await c.get(f"https://{ip}:1443/api/v1/players/local/info",
                            headers={"X-Sonos-Api-Key": SONOS_API_KEY}, timeout=3.0)
        r.raise_for_status()
        info["announcements"] = "AUDIO_CLIP" in (r.json().get("device", {}).get("capabilities") or [])
    except Exception:
        pass
    return info


async def attach_device_info(devices: dict[str, dict]) -> None:
    """Add model details to each speaker (fetched once, then remembered)."""
    todo = [(u, d) for u, d in devices.items()
            if u not in DEVICE_INFO or DEVICE_INFO[u].get("_ip") != d["ip"]]
    results = await asyncio.gather(*(fetch_device_info(d["ip"]) for _, d in todo))
    for (u, d), info in zip(todo, results):
        DEVICE_INFO[u] = {**info, "_ip": d["ip"]}
    for u, d in devices.items():
        d.update({k: v for k, v in DEVICE_INFO.get(u, {}).items() if not k.startswith("_")})


def _reset_discovery() -> None:
    """Forget every speaker (they may belong to a network we have left)."""
    STATE.ip_pool.clear()
    STATE.devices, STATE.groups = {}, []
    STATE.transport, STATE.volumes, STATE.group_volumes = {}, {}, {}
    DEVICE_INFO.clear()


async def play_audio_clip(
    client: httpx.AsyncClient, ip: str, player_id: str, stream_url: str, volume: int | None = None
) -> None:
    """Play an announcement clip *over* whatever a player is doing (ducks, then restores).

    Uses the Sonos local audioClip API on port 1443 (self-signed cert → caller must use
    an httpx client with verify=False). Does not disturb the underlying AirPlay/queue source.
    """
    body: dict[str, Any] = {"name": "Attention", "appId": "com.sonos.console", "streamUrl": stream_url}
    if volume is not None:
        body["volume"] = max(0, min(100, int(volume)))
    r = await client.post(
        f"https://{ip}:1443/api/v1/players/{player_id}/audioClip",
        headers={"X-Sonos-Api-Key": SONOS_API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=6.0,
    )
    r.raise_for_status()


# ---------- Refresh loop ----------
def _transport_sig(t: dict) -> tuple:
    """What a client needs to redraw the now-playing panel: track, state and artwork."""
    return (t.get("title"), t.get("artist"), t.get("state"), t.get("art"))


async def refresh_state(force_scan: bool = False) -> None:
    transport_before = {k: _transport_sig(v) for k, v in STATE.transport.items()}
    prefix = await asyncio.to_thread(get_local_prefix)
    if prefix != STATE.network_prefix:
        if STATE.network_prefix is not None:
            _log.info("Network changed (%s -> %s): looking for speakers again", STATE.network_prefix, prefix)
            _reset_discovery()
            _broadcast_state()
            force_scan = True
        STATE.network_prefix = prefix
    async with httpx.AsyncClient() as client:
        if force_scan and prefix:
            await asyncio.to_thread(prime_arp, prefix)   # wake every address so the ARP table is complete
        seed = await find_seed(client)
        if not seed and not force_scan:
            if prefix:
                await asyncio.to_thread(prime_arp, prefix)
                seed = await find_seed(client)
        if not seed:
            STATE.last_error = "No Sonos device reachable"
            _log.error("No Sonos device reachable on the network")
            return

        try:
            devices, groups = await fetch_topology(client, seed)
        except Exception as e:
            STATE.last_error = f"topology error: {e!r}"
            _log.error("Topology fetch failed: %s", e)
            return

        await attach_device_info(devices)
        STATE.devices = devices
        STATE.groups = groups
        STATE.ip_pool.update(d["ip"] for d in devices.values())
        STATE.last_error = None

        # Volumes/mute in parallel
        async def vol_for(uuid: str, ip: str) -> None:
            try:
                v, m = await asyncio.gather(get_volume(client, ip), get_mute(client, ip))
                if STATE.volumes.get(uuid, {}).get("reachable") is False:
                    _log.info("Device back online: %s (%s)", STATE.devices.get(uuid, {}).get("name", uuid), ip)
                STATE.volumes[uuid] = {"volume": v, "muted": m, "reachable": True}
            except Exception as e:
                name = STATE.devices.get(uuid, {}).get("name", uuid)
                if STATE.volumes.get(uuid, {}).get("reachable") is not False:
                    _log.warning("Device unreachable: %s (%s) — %s", name, ip, e)
                STATE.volumes[uuid] = {
                    **STATE.volumes.get(uuid, {"volume": None, "muted": None}),
                    "reachable": False,
                }

        await asyncio.gather(*(vol_for(d["uuid"], d["ip"]) for d in devices.values()))

        # Transport + group volume per coordinator
        async def coord_for(coord_uuid: str) -> None:
            dev = STATE.devices.get(coord_uuid)
            if not dev:
                return
            try:
                STATE.transport[coord_uuid] = await get_transport(client, dev["ip"])
            except Exception:
                pass
            try:
                gv = await get_group_volume(client, dev["ip"])
                if gv is not None:
                    STATE.group_volumes[coord_uuid] = gv
            except Exception:
                pass

        await asyncio.gather(*(coord_for(g["coordinator_uuid"]) for g in groups if g["coordinator_uuid"]))

        # Prune vanished groups
        live_coords = {g["coordinator_uuid"] for g in groups}
        for k in list(STATE.transport.keys()):
            if k not in live_coords:
                del STATE.transport[k]
        for k in list(STATE.group_volumes.keys()):
            if k not in live_coords:
                del STATE.group_volumes[k]

        # Broadcast to SSE clients if track or play state changed
        transport_after = {k: _transport_sig(v) for k, v in STATE.transport.items()}
        if transport_before != transport_after:
            _broadcast_state()


def _get_lock() -> asyncio.Lock:
    """One refresh at a time: the poll loop and a manual rescan must not run together."""
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


async def refresh_loop() -> None:
    _log.info("Server started — polling Sonos every %.1fs", REFRESH_INTERVAL)
    while True:
        try:
            async with _get_lock():
                await refresh_state()
        except Exception as e:
            STATE.last_error = f"refresh loop: {e!r}"
            _log.error("Refresh loop crashed: %s", e)
        await asyncio.sleep(REFRESH_INTERVAL)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(refresh_loop())


# ---------- SSE push ----------
def _sse_snapshot() -> str:
    payload = {
        "devices": STATE.devices,
        "groups": STATE.groups,
        "transport": STATE.transport,
        "volumes": STATE.volumes,
        "group_volumes": STATE.group_volumes,
        "last_error": STATE.last_error,
        "https_port": HTTPS_PORT,
        "version": VERSION,
    }
    return f"data: {json.dumps(payload)}\n\n"


def _broadcast_state() -> None:
    if not _sse_clients:
        return
    snapshot = _sse_snapshot()
    dead = set()
    for q in _sse_clients:
        try:
            q.put_nowait(snapshot)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_clients.difference_update(dead)


async def refresh_coordinator(coord_uuid: str) -> dict | None:
    """Re-fetch transport state for one coordinator immediately after a command."""
    dev = STATE.devices.get(coord_uuid)
    if not dev:
        return None
    try:
        async with httpx.AsyncClient() as client:
            transport = await get_transport(client, dev["ip"])
        changed = _transport_sig(STATE.transport.get(coord_uuid, {})) != _transport_sig(transport)
        STATE.transport[coord_uuid] = transport
        if changed:
            _broadcast_state()   # other open pages should not wait for the next poll
        return transport
    except Exception:
        return None


async def _settle_refresh(coord_uuid: str) -> None:
    """Speakers report a new track a moment after Next/Previous. Re-read a couple of times so the
    page shows the new title and artwork promptly (refresh_coordinator pushes the change)."""
    for delay in (0.6, 0.9):
        await asyncio.sleep(delay)
        await refresh_coordinator(coord_uuid)


# ---------- API ----------
class VolReq(BaseModel):
    uuid: str
    volume: int


class MuteReq(BaseModel):
    uuid: str
    muted: bool


class JoinReq(BaseModel):
    uuid: str
    coordinator_uuid: str


class LeaveReq(BaseModel):
    uuid: str


class CoordReq(BaseModel):
    coordinator_uuid: str


class GroupVolReq(BaseModel):
    coordinator_uuid: str
    volume: int


def _ip(uuid: str) -> str:
    d = STATE.devices.get(uuid)
    if not d:
        raise HTTPException(404, "unknown device")
    return d["ip"]


@app.get("/state")
async def api_state() -> dict:
    return {
        "devices": STATE.devices,
        "groups": STATE.groups,
        "transport": STATE.transport,
        "volumes": STATE.volumes,
        "group_volumes": STATE.group_volumes,
        "last_error": STATE.last_error,
        "https_port": HTTPS_PORT,
        "version": VERSION,
    }


@app.post("/volume")
async def api_volume(req: VolReq) -> dict:
    async with httpx.AsyncClient() as c:
        await set_volume(c, _ip(req.uuid), req.volume)
    STATE.volumes.setdefault(req.uuid, {})["volume"] = max(0, min(100, req.volume))
    return {"ok": True}


@app.post("/mute")
async def api_mute(req: MuteReq) -> dict:
    async with httpx.AsyncClient() as c:
        await set_mute(c, _ip(req.uuid), req.muted)
    STATE.volumes.setdefault(req.uuid, {})["muted"] = req.muted
    return {"ok": True}


@app.post("/join")
async def api_join(req: JoinReq) -> dict:
    async with httpx.AsyncClient() as c:
        await join_group(c, _ip(req.uuid), req.coordinator_uuid)
    return {"ok": True}


@app.post("/leave")
async def api_leave(req: LeaveReq) -> dict:
    async with httpx.AsyncClient() as c:
        await leave_group(c, _ip(req.uuid))
    return {"ok": True}


def _transport_error(action: str, e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e  # e.g. 404 unknown device — don't mask as a 503 transport failure
    msg = str(e)
    # Extract Sonos UPnP error description if present
    m = re.search(r"<errorDescription>([^<]+)</errorDescription>", msg)
    detail = m.group(1) if m else msg[:120]
    _log.warning("%s failed: %s", action, detail)
    return HTTPException(503, f"{action} failed: {detail}")


@app.post("/play")
async def api_play(req: CoordReq) -> dict:
    try:
        async with httpx.AsyncClient() as c:
            await do_play(c, _ip(req.coordinator_uuid))
    except Exception as e:
        raise _transport_error("Play", e)
    transport = await refresh_coordinator(req.coordinator_uuid)
    return {"ok": True, "transport": transport}


@app.post("/pause")
async def api_pause(req: CoordReq) -> dict:
    try:
        async with httpx.AsyncClient() as c:
            await do_pause(c, _ip(req.coordinator_uuid))
    except Exception as e:
        raise _transport_error("Pause", e)
    transport = await refresh_coordinator(req.coordinator_uuid)
    return {"ok": True, "transport": transport}


@app.post("/next")
async def api_next(req: CoordReq) -> dict:
    try:
        async with httpx.AsyncClient() as c:
            await do_next(c, _ip(req.coordinator_uuid))
    except Exception as e:
        raise _transport_error("Next", e)
    await asyncio.sleep(0.15)
    transport = await refresh_coordinator(req.coordinator_uuid)
    asyncio.create_task(_settle_refresh(req.coordinator_uuid))
    return {"ok": True, "transport": transport}


@app.post("/prev")
async def api_prev(req: CoordReq) -> dict:
    try:
        async with httpx.AsyncClient() as c:
            await do_prev(c, _ip(req.coordinator_uuid))
    except Exception as e:
        raise _transport_error("Prev", e)
    await asyncio.sleep(0.15)
    transport = await refresh_coordinator(req.coordinator_uuid)
    asyncio.create_task(_settle_refresh(req.coordinator_uuid))
    return {"ok": True, "transport": transport}


@app.post("/rescan")
async def api_rescan() -> dict:
    """Forget remembered speakers and search the network the computer is on right now."""
    _log.info("Rescan requested")
    started = time.time()
    async with _get_lock():
        _reset_discovery()
        STATE.last_error = None
        await refresh_state(force_scan=True)
        STATE.last_scan = time.time()
    _broadcast_state()
    names = sorted(d["name"] for d in STATE.devices.values())
    _log.info("Rescan finished: %d speaker(s) found", len(names))
    return {
        "ok": not STATE.last_error,
        "found": len(names),
        "speakers": names,
        "seconds": round(time.time() - started, 1),
        "network": await asyncio.to_thread(get_network_info),
        "error": STATE.last_error,
    }


@app.get("/network")
async def api_network() -> dict:
    return await asyncio.to_thread(get_network_info)


@app.get("/events")
async def api_events():
    """SSE endpoint — pushes state snapshots when transport changes."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=5)
    _sse_clients.add(q)

    async def event_generator():
        yield _sse_snapshot()
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_clients.discard(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/group-volume")
async def api_group_volume(req: GroupVolReq) -> dict:
    async with httpx.AsyncClient() as c:
        await set_group_volume(c, _ip(req.coordinator_uuid), req.volume)
    STATE.group_volumes[req.coordinator_uuid] = max(0, min(100, req.volume))
    return {"ok": True}


# ---------- Static ----------
BASE = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")

# ---------- Attention (voice PA broadcast) ----------
RECORDINGS_DIR = os.path.join(BASE, "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)
# Served over plain HTTP so Sonos speakers can fetch the clip to play.
app.mount("/recordings", StaticFiles(directory=RECORDINGS_DIR), name="recordings")


def _safe_label(s: str) -> str:
    """Filesystem-safe device label for filenames."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-")[:40] or "device"


def _wav_duration(path: str) -> float:
    """Length of a WAV file in seconds (0 if unreadable)."""
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except Exception:
        return 0.0


def _fmt_duration(secs: float) -> str:
    """Seconds -> m:ss (or 0:ss)."""
    total = int(round(secs))
    return f"{total // 60}:{total % 60:02d}"


async def _broadcast_url(client, players: list, stream_url: str, volume: int) -> tuple[int, list[str]]:
    """Fire one audioClip (stream_url) at every player. Returns (played, failed-names)."""
    played = 0
    failed: list[str] = []

    async def one(uuid: str, dev: dict) -> None:
        nonlocal played
        try:
            await play_audio_clip(client, dev["ip"], uuid, stream_url, volume)
            played += 1
        except Exception as e:
            failed.append(dev.get("name", uuid))
            _log.warning("audioClip to %s failed: %s", dev.get("name", uuid), str(e)[:120])

    await asyncio.gather(*(one(u, d) for u, d in players))
    return played, failed


async def _broadcast_clip(fname: str, volume: int) -> dict:
    """Broadcast recordings/<fname> to every speaker at the given volume.

    The airplane chime (when enabled) is baked into the front of the recording
    client-side, so a single clip guarantees chime → voice ordering.
    """
    lan_ip = get_local_ip()
    if not lan_ip:
        raise HTTPException(503, "cannot determine LAN IP for broadcast")
    stream_url = f"http://{lan_ip}:{PUBLIC_HTTP_PORT}/recordings/{fname}"
    async with httpx.AsyncClient(verify=False) as client:
        played, failed = await _broadcast_url(client, list(STATE.devices.items()), stream_url, volume)
    return {"played": played, "failed": failed, "url": stream_url}


@app.post("/attention/upload")
async def attention_upload(request: Request) -> dict:
    """Receive a raw WAV body, store it named by date/time/IP/device, then broadcast to all."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > MAX_RECORDING_BYTES:
        raise HTTPException(413, "recording too large")
    device_name = _safe_label(urllib.parse.unquote(request.headers.get("X-Device-Name", "device")))
    try:
        volume = int(request.headers.get("X-Clip-Volume", "45"))
    except ValueError:
        volume = 45
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"{ts}_{device_name}.wav"
    with open(os.path.join(RECORDINGS_DIR, fname), "wb") as f:
        f.write(data)
    _log.info("Attention recording saved: %s (%d bytes)", fname, len(data))
    result = await _broadcast_clip(fname, volume)
    return {"ok": True, "file": fname, **result}


@app.get("/attention/list")
async def attention_list() -> dict:
    items = []
    for name in os.listdir(RECORDINGS_DIR):
        if not name.endswith(".wav"):
            continue
        try:
            st = os.stat(os.path.join(RECORDINGS_DIR, name))
        except OSError:
            continue
        dur = _wav_duration(os.path.join(RECORDINGS_DIR, name))
        items.append({"name": name, "size": st.st_size, "mtime": st.st_mtime, "duration": dur, "url": f"/recordings/{name}"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"recordings": items}


class AttentionBroadcastReq(BaseModel):
    file: str
    volume: int = 45


@app.post("/attention/broadcast")
async def attention_broadcast(req: AttentionBroadcastReq) -> dict:
    fname = os.path.basename(req.file)
    if not fname.endswith(".wav") or not os.path.exists(os.path.join(RECORDINGS_DIR, fname)):
        raise HTTPException(404, "recording not found")
    result = await _broadcast_clip(fname, req.volume)
    return {"ok": True, "file": fname, **result}


@app.delete("/attention/{name}")
async def attention_delete(name: str) -> dict:
    fname = os.path.basename(name)
    path = os.path.join(RECORDINGS_DIR, fname)
    if not fname.endswith(".wav") or not os.path.exists(path):
        raise HTTPException(404, "recording not found")
    os.remove(path)
    _log.info("Attention recording deleted: %s", fname)
    return {"ok": True}


@app.get("/art/{uuid}")
async def album_art(uuid: str):
    """Proxy the current album artwork for a coordinator, so HTTPS pages can show it
    (speakers serve artwork over plain HTTP) and the browser never needs to reach them."""
    from fastapi.responses import Response

    url = (STATE.transport.get(uuid) or {}).get("art")
    if not url:
        raise HTTPException(404, "no artwork")
    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            r = await c.get(url, timeout=5.0)
        r.raise_for_status()
    except Exception:
        raise HTTPException(404, "artwork unavailable")
    ctype = r.headers.get("content-type", "image/jpeg")
    if not ctype.startswith("image/"):
        raise HTTPException(404, "artwork unavailable")
    return Response(r.content, media_type=ctype, headers={"Cache-Control": "max-age=3600"})


@app.get("/")
async def root():
    # no-cache: browsers must revalidate, so an upgraded app is never shown with its old page code
    return FileResponse(os.path.join(BASE, "static", "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/dashboard")
async def dashboard():
    from fastapi.responses import HTMLResponse

    def level_color(level: str) -> str:
        return {"ERROR": "#ff5f5f", "WARNING": "#ffb84a", "INFO": "#45e07a", "DEBUG": "#8ab4f8"}.get(level, "#ccc")

    rows = ""
    for r in reversed(list(_log_ring)):
        color = level_color(r["level"])
        rows += (
            f'<tr><td class="ts">{html.escape(r["time"])}</td>'
            f'<td class="lv" style="color:{color}">{html.escape(r["level"])}</td>'
            f'<td class="ms">{html.escape(r["msg"])}</td></tr>\n'
        )
    if not rows:
        rows = '<tr><td colspan="3" class="empty">No events logged yet.</td></tr>'

    device_rows = ""
    for uuid, d in STATE.devices.items():
        vol = STATE.volumes.get(uuid, {})
        reachable = vol.get("reachable", True)
        status_color = "#45e07a" if reachable else "#ff5f5f"
        status = "Online" if reachable else "Unreachable"
        model = d.get("model") or "—"
        if d.get("model_number"):
            model += f' <span class="dim">({html.escape(d["model_number"])})</span>'
        else:
            model = html.escape(model) if not d.get("model") else html.escape(d["model"])
        ann = d.get("announcements")
        ann_html = ('<span style="color:#45e07a">✓ yes</span>' if ann
                    else '<span style="color:#ffb84a">✗ no</span>' if ann is False else "—")
        device_rows += (
            f'<tr><td>{html.escape(d["name"])}</td>'
            f'<td>{model}</td>'
            f'<td>{html.escape(d["ip"])}</td>'
            f'<td style="color:{status_color}">{status}</td>'
            f'<td>{vol.get("volume", "—")}</td>'
            f'<td>{ann_html}</td>'
            f'<td class="dim">{html.escape(d.get("software") or "—")}</td></tr>\n'
        )
    if not device_rows:
        device_rows = '<tr><td colspan="7" class="empty">No devices found. Try “Rescan for speakers”.</td></tr>'

    net = await asyncio.to_thread(get_network_info)
    if net["ssid"]:
        wifi_html = html.escape(net["ssid"])
    elif net["connection"] == "wifi":
        wifi_html = ('<span class="dim">Hidden by macOS</span> — apps need Location Services permission to read the '
                     'Wi-Fi name. The subnet below still tells networks apart.')
    elif net["connection"] == "ethernet":
        wifi_html = '<span class="dim">Wired (Ethernet), no Wi-Fi in use</span>'
    else:
        wifi_html = '<span class="dim">Not available</span>'
    net_rows = (
        f'<tr><th>Wi-Fi network</th><td>{wifi_html}</td></tr>'
        f'<tr><th>This computer</th><td>{html.escape(net["ip"] or "not connected")}'
        f'{" on " + html.escape(net["interface"]) if net["interface"] else ""}</td></tr>'
        f'<tr><th>Subnet · router</th><td>{html.escape(net["subnet"] or "—")} · {html.escape(net["gateway"] or "—")}</td></tr>'
    )

    error_html = ""
    if STATE.last_error:
        error_html = f'<div class="alert">⚠ {html.escape(STATE.last_error)}</div>'

    # Flight Log: recent announcements as compact line items (newest first).
    def _fmt_size(n: int) -> str:
        return f"{n / 1048576:.1f} MB" if n > 1048576 else f"{round(n / 1024)} KB"

    recs = []
    for rn in os.listdir(RECORDINGS_DIR):
        if not rn.endswith(".wav"):
            continue
        try:
            rst = os.stat(os.path.join(RECORDINGS_DIR, rn))
        except OSError:
            continue
        dur = _wav_duration(os.path.join(RECORDINGS_DIR, rn))
        recs.append((rn, rst.st_mtime, rst.st_size, dur))
    recs.sort(key=lambda x: x[1], reverse=True)

    announce_rows = ""
    for rn, mtime, size, dur in recs:
        when = datetime.datetime.fromtimestamp(mtime).strftime("%b %d · %H:%M:%S")
        device = rn[:-4].split("_")[-1] or "device"  # filenames are safe-labeled
        announce_rows += (
            '<div class="rec-item">'
            f'<span class="rtime">{html.escape(when)}</span>'
            f'<span class="rdev">📣 {html.escape(device)}</span>'
            f'<span class="rdur">{_fmt_duration(dur)}</span>'
            f'<span class="rsize">{_fmt_size(size)}</span>'
            '<span class="racts">'
            f'<button class="dbtn cast" onclick="rebroadcast(this,\'{rn}\')">▶ Broadcast</button>'
            f'<button class="dbtn del" onclick="delRec(this,\'{rn}\')">Delete</button>'
            "</span></div>\n"
        )
    if not announce_rows:
        announce_rows = '<div class="empty2">No announcements recorded yet.</div>'

    dash_extra_css = (
        ".dim{color:var(--parchment);opacity:0.7;}"
        "table.kv th{width:170px;white-space:nowrap;}"
        ".actions{display:flex;align-items:center;gap:14px;margin-top:14px;flex-wrap:wrap;}"
        ".rescan{font-family:'Courier New',monospace;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;"
        "padding:9px 18px;border-radius:4px;border:1px solid var(--ink);cursor:pointer;font-weight:bold;color:var(--ink);"
        "background:linear-gradient(180deg,var(--brass-hi),var(--brass-mid) 55%,var(--brass-lo));"
        "box-shadow:var(--bevel-raised);transition:transform var(--dur-press) var(--ease-out);}"
        ".rescan:active{transform:translateY(1px);}"
        ".rescan:disabled{opacity:0.6;cursor:wait;}"
        ".rescan:focus-visible{outline:2px solid var(--brass-hi);outline-offset:3px;}"
        "#rescan-msg{font-family:'Courier New',monospace;font-size:12px;color:var(--cream);}"
        "#rescan-msg.err{color:#ffb4a8;}"
        ".dtabs{display:flex;gap:6px;margin-bottom:20px;}"
        ".dtab{font-family:'Courier New',monospace;font-size:11px;letter-spacing:0.22em;text-transform:uppercase;"
        "padding:9px 20px;cursor:pointer;color:var(--parchment);"
        "background:linear-gradient(180deg,#3a2211,#241207);border:1px solid var(--ink);border-radius:7px;"
        "box-shadow:inset 0 1px 0 rgba(255,200,140,0.08);opacity:0.75;"
        "transition:transform var(--dur-press) var(--ease-out),opacity var(--dur-press) var(--ease-out);}"
        ".dtab.active{color:var(--ink);opacity:1;"
        "background:linear-gradient(180deg,var(--brass-hi),var(--brass-mid) 70%,var(--brass-lo));"
        "box-shadow:inset 0 1px 0 rgba(255,240,180,0.7),0 4px 12px rgba(0,0,0,0.4);}"
        ".dtab:focus-visible{outline:2px solid var(--brass-hi);outline-offset:3px;}"
        "@media (hover:hover) and (pointer:fine){.dtab:hover{opacity:1;}}"
        ".rec-list{max-height:60vh;overflow-y:auto;border:1px solid var(--ink);border-radius:8px;"
        "background:linear-gradient(180deg,#3a2211,#241207);box-shadow:var(--bevel-inset);}"
        ".rec-item{display:flex;align-items:center;gap:14px;padding:10px 14px;border-bottom:1px solid rgba(0,0,0,0.35);"
        "animation:row-in var(--dur-base) var(--ease-out) both;}"
        ".rec-item:last-child{border-bottom:none;}"
        ".rec-item:nth-child(2){animation-delay:40ms;}.rec-item:nth-child(3){animation-delay:80ms;}"
        ".rec-item:nth-child(4){animation-delay:120ms;}.rec-item:nth-child(5){animation-delay:160ms;}"
        ".rec-item:nth-child(n+6){animation-delay:200ms;}"
        "@keyframes row-in{from{opacity:0;transform:translateY(6px);}}"
        ".rec-item[data-leaving]{opacity:0;transform:translateY(-4px);"
        "transition:opacity var(--dur-fast) var(--ease-out),transform var(--dur-fast) var(--ease-out);}"
        "@media (hover:hover) and (pointer:fine){.rec-item:hover{background:rgba(246,220,131,0.06);}}"
        ".rtime{color:var(--brass-mid);white-space:nowrap;width:170px;font-variant-numeric:tabular-nums;}"
        ".rdev{flex:1;color:var(--cream);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}"
        ".rdur{color:var(--brass-hi);width:52px;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}"
        ".rsize{color:var(--parchment);opacity:0.7;width:70px;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}"
        ".racts{display:flex;gap:6px;flex:none;}"
        ".dbtn{font-family:'Courier New',monospace;font-size:10px;letter-spacing:0.12em;text-transform:uppercase;"
        "font-weight:bold;padding:6px 11px;border-radius:5px;cursor:pointer;border:1px solid var(--ink);color:var(--ink);"
        "box-shadow:var(--bevel-raised);"
        "transition:transform var(--dur-press) var(--ease-out),box-shadow var(--dur-press) var(--ease-out);}"
        ".dbtn:active{transform:translateY(1px);}"
        ".dbtn:focus-visible{outline:2px solid var(--brass-hi);outline-offset:3px;}"
        ".dbtn.cast{background:linear-gradient(180deg,#86c88a,#2c6b30);color:#0e2b12;}"
        ".dbtn.cast:disabled{opacity:0.6;cursor:default;}"
        ".dbtn.del{background:linear-gradient(180deg,#c47a5a,#7a1f14);color:#ffe4d8;}"
        ".empty2{color:var(--parchment);opacity:0.6;text-align:center;padding:24px;font-style:italic;}"
    )

    dash_script = """<script>
      var reloadTimer;
      function toast(msg,kind,ms){
        var wrap=document.getElementById('toast-wrap'); if(!wrap) return;
        var el=document.createElement('div'); el.className='toast '+(kind||'');
        el.textContent=msg; el.style.opacity='0'; el.style.transform='translateY(16px)';
        wrap.appendChild(el);
        requestAnimationFrame(function(){ el.style.opacity=''; el.style.transform=''; });
        var timer=setTimeout(leave, ms||3400);
        function leave(){ el.setAttribute('data-leaving',''); setTimeout(function(){ el.remove(); },300); }
        el.addEventListener('mouseenter',function(){ clearTimeout(timer); });
        el.addEventListener('mouseleave',function(){ timer=setTimeout(leave,1400); });
      }
      function tab(name){
        var ps=document.querySelectorAll('[data-panel]');
        for(var i=0;i<ps.length;i++){
          var show=ps[i].getAttribute('data-panel')===name;
          ps[i].hidden=!show;
          if(show){ ps[i].classList.remove('entering'); void ps[i].offsetWidth; ps[i].classList.add('entering'); }
        }
        var ts=document.querySelectorAll('.dtab');
        for(var j=0;j<ts.length;j++){ts[j].classList.toggle('active', ts[j].getAttribute('data-tab')===name);}
        try{localStorage.setItem('dash_tab',name);}catch(e){}
        clearTimeout(reloadTimer);
        if(name==='monitoring'){reloadTimer=setTimeout(function(){location.reload();},10000);}
      }
      async function rebroadcast(btn,name){
        btn.disabled=true; var o=btn.textContent; btn.textContent='… casting';
        try{
          var r=await fetch('/attention/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:name})});
          var d=await r.json().catch(function(){return {};});
          if(r.ok){ btn.textContent='\\u2713 '+(d.played||0)+' spk'; toast('\\u2713 Broadcast to '+(d.played||0)+' speakers','ok'); }
          else { btn.textContent='\\u2717 failed'; toast('\\u2717 Broadcast failed','err'); }
        }catch(e){ btn.textContent='\\u2717 failed'; toast('\\u2717 Broadcast failed','err'); }
        setTimeout(function(){ btn.textContent=o; btn.disabled=false; },2500);
      }
      async function delRec(btn,name){
        if(!confirm('Delete this announcement?\\n'+name)) return;
        try{
          await fetch('/attention/'+encodeURIComponent(name),{method:'DELETE'});
          var row=btn.closest('.rec-item');
          row.setAttribute('data-leaving',''); setTimeout(function(){ row.remove(); },200);
          toast('\\ud83d\\uddd1 Announcement deleted','ok');
        }catch(e){ toast('\\u2717 Delete failed','err'); }
      }
      (function(){
        var btn=document.getElementById('rescan'), msg=document.getElementById('rescan-msg');
        try{ var flash=sessionStorage.getItem('rescan_flash'); if(flash && msg){ msg.textContent=flash; sessionStorage.removeItem('rescan_flash'); } }catch(e){}
        if(!btn) return;
        btn.addEventListener('click', async function(){
          clearTimeout(reloadTimer);
          btn.disabled=true; var label=btn.textContent; btn.textContent='\u27f3 Scanning\u2026';
          msg.className=''; msg.textContent='Looking for speakers on this network (can take up to ~15 seconds)\u2026';
          try{
            var r=await fetch('/rescan',{method:'POST'}); var d=await r.json();
            var text = d.found ? 'Found '+d.found+' speaker'+(d.found===1?'':'s')+': '+d.speakers.join(', ') : 'No speakers found on this network.';
            if(!d.found && d.error) text += ' ('+d.error+')';
            try{ sessionStorage.setItem('rescan_flash', text); }catch(e){}
            toast(text, d.found?'ok':'err', 5000);
            setTimeout(function(){ location.reload(); }, 900);
          }catch(e){
            msg.className='err'; msg.textContent='Rescan failed: '+e; btn.disabled=false; btn.textContent=label;
          }
        });
      })();
      (function(){ var t='monitoring'; try{t=localStorage.getItem('dash_tab')||'monitoring';}catch(e){} tab(t); })();
    </script>"""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Captain Speaking · Dashboard</title>
<style>
  :root {{
    --wood-dark:#2b1608; --wood-mid:#4a2a14; --wood-lite:#6b3b1c;
    --brass-hi:#f6dc83; --brass-mid:#c39a3a; --brass-lo:#6b4a08;
    --cream:#f4e0b8; --parchment:#d9b781; --ink:#1a0d05;
    --led-green:#45e07a; --led-red:#ff4a3d; --led-amber:#ffb84a;
    --ease-out:cubic-bezier(0.23,1,0.32,1); --ease-in-out:cubic-bezier(0.77,0,0.175,1);
    --dur-press:140ms; --dur-fast:180ms; --dur-base:240ms;
    --bevel-raised:inset 0 1px 0 rgba(255,240,180,0.6),inset 0 -2px 2px rgba(0,0,0,0.35),0 2px 4px rgba(0,0,0,0.4);
    --bevel-inset:inset 0 2px 3px rgba(0,0,0,0.5);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    color: var(--cream); font-family: "Georgia","Times New Roman",serif; font-size: 14px;
    padding: 28px 24px 60px; min-height: 100vh; max-width: 1100px; margin: 0 auto;
    background-color: var(--wood-mid);
    background-image:
      radial-gradient(ellipse at 50% 15%, rgba(200,140,70,0.35), transparent 60%),
      radial-gradient(ellipse at 20% 80%, rgba(255,180,110,0.15), transparent 55%),
      repeating-linear-gradient(89deg, rgba(20,10,5,0.35) 0, rgba(20,10,5,0.35) 1px, transparent 1px, transparent 4px, rgba(20,10,5,0.18) 4px, rgba(20,10,5,0.18) 5px, transparent 5px, transparent 11px, rgba(20,10,5,0.28) 11px, rgba(20,10,5,0.28) 12px, transparent 12px, transparent 22px),
      linear-gradient(180deg, var(--wood-mid), var(--wood-dark));
    background-attachment: fixed;
  }}
  h1 {{ font-family:"Georgia",serif; color: var(--brass-hi); letter-spacing: 0.22em; font-size: 22px; margin-bottom: 4px;
    text-shadow: 0 1px 0 rgba(0,0,0,0.5), 0 0 14px rgba(246,220,131,0.35); }}
  .sub {{ color: var(--parchment); font-family:"Courier New",monospace; font-size: 11px; letter-spacing: 0.2em; margin-bottom: 24px; opacity: 0.8; }}
  .back {{ color: var(--brass-hi); text-decoration: none; font-family:"Courier New",monospace; font-size: 11px; letter-spacing: 0.15em; float: right; margin-top: 6px; }}
  .back:focus-visible {{ outline: 2px solid var(--brass-hi); outline-offset: 3px; }}
  @media (hover:hover) and (pointer:fine) {{ .back:hover {{ color: var(--cream); }} }}
  .alert {{ background: rgba(180,40,30,0.35); border: 1px solid #6b1b12; color: #ffd0c8; padding: 10px 14px; border-radius: 6px; margin-bottom: 20px; font-family:"Courier New",monospace; }}
  h2 {{ color: var(--brass-mid); font-family:"Courier New",monospace; font-size: 12px; letter-spacing: 0.28em; text-transform: uppercase; margin: 28px 0 10px; border-bottom: 1px solid rgba(246,220,131,0.25); padding-bottom: 6px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; color: var(--brass-mid); font-family:"Courier New",monospace; font-size: 11px; letter-spacing: 0.18em; padding: 6px 10px; border-bottom: 1px solid rgba(246,220,131,0.25); }}
  td {{ padding: 6px 10px; border-bottom: 1px solid rgba(0,0,0,0.3); vertical-align: top; font-family:"Courier New",monospace; font-size: 12px; color: var(--cream); }}
  @media (hover:hover) and (pointer:fine) {{ tr:hover td {{ background: rgba(246,220,131,0.06); }} }}
  td.ts {{ color: var(--parchment); opacity: 0.8; white-space: nowrap; width: 150px; font-variant-numeric: tabular-nums; }}
  td.lv {{ font-weight: bold; width: 80px; white-space: nowrap; }}
  td.ms {{ word-break: break-word; }}
  td.empty {{ color: var(--parchment); opacity: 0.6; text-align: center; padding: 20px; font-style: italic; }}
  .refresh {{ color: var(--parchment); opacity: 0.6; font-family:"Courier New",monospace; font-size: 10px; letter-spacing: 0.1em; margin-top: 20px; }}
  [data-panel][hidden] {{ display: none; }}
  [data-panel].entering {{ animation: view-in var(--dur-fast) var(--ease-out); }}
  @keyframes view-in {{ from {{ opacity: 0; transform: translateY(4px); }} }}
  .toast-wrap {{ position: fixed; left:0; right:0; bottom:22px; z-index:9999; display:flex; flex-direction:column; align-items:center; gap:10px; pointer-events:none; padding:0 12px; }}
  .toast {{ pointer-events:auto; min-width:220px; max-width:92vw; padding:11px 16px; border-radius:8px; font-family:"Courier New",monospace; font-size:12px; letter-spacing:0.04em; color:var(--cream); background:linear-gradient(180deg,#4a2810,#2b1608); border:1px solid var(--ink); box-shadow:var(--bevel-raised),0 8px 24px rgba(0,0,0,0.5); transition:opacity var(--dur-base) var(--ease-out), transform var(--dur-base) var(--ease-out); }}
  .toast.ok {{ border-color:#2c6b30; }}
  .toast.err {{ border-color:#7a1f14; color:#ffd9d0; }}
  .toast[data-leaving] {{ opacity:0; transform:translateY(10px); }}
  @media (prefers-reduced-motion: reduce) {{ [data-panel].entering, .rec-item {{ animation: none !important; }} .toast {{ transition-duration: 1ms; }} }}
  {dash_extra_css}
</style>
</head>
<body>
<a class="back" href="/">← Console</a>
<h1>CAPTAIN SPEAKING</h1>
<div class="sub">Dashboard · Monitoring · Flight Log</div>
{error_html}
<div class="dtabs">
  <button class="dtab" data-tab="monitoring" onclick="tab('monitoring')">Monitoring</button>
  <button class="dtab" data-tab="flightlog" onclick="tab('flightlog')">✈ Flight Log</button>
</div>
<div data-panel="monitoring">
  <h2>Network</h2>
  <table class="kv">{net_rows}</table>
  <div class="actions">
    <button class="rescan" id="rescan" type="button">⟳ Rescan for speakers</button>
    <span id="rescan-msg" role="status" aria-live="polite"></span>
  </div>
  <h2>Devices</h2>
  <table>
    <tr><th>Name</th><th>Model</th><th>IP</th><th>Status</th><th>Volume</th><th>Announcements</th><th>Firmware</th></tr>
    {device_rows}
  </table>
  <h2>Event Log</h2>
  <table>
    <tr><th>Time</th><th>Level</th><th>Message</th></tr>
    {rows}
  </table>
  <div class="refresh">Last rendered: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · {len(_log_ring)} events in buffer · auto-refreshes every 10s</div>
</div>
<div data-panel="flightlog" hidden>
  <h2>Recent Announcements</h2>
  <div class="rec-list">{announce_rows}</div>
  <div class="refresh">{len(recs)} announcement(s) · newest first</div>
</div>
<div id="toast-wrap" class="toast-wrap"></div>
{dash_script}
</body>
</html>"""
    return HTMLResponse(page)
