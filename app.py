"""
Sonos wooden-panel console — local web app.
Run: uvicorn app:app --host 127.0.0.1 --port 8787
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import subprocess
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

SOAP_TIMEOUT = 4.0
REFRESH_INTERVAL = 2.0

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


STATE = State()
app = FastAPI(title="Sonos Wooden Console")
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
    ti = await soap(client, ip, AV_PATH, AV_SVC, "GetTransportInfo", "<InstanceID>0</InstanceID>")
    mi = await soap(client, ip, AV_PATH, AV_SVC, "GetMediaInfo", "<InstanceID>0</InstanceID>")
    pi = await soap(client, ip, AV_PATH, AV_SVC, "GetPositionInfo", "<InstanceID>0</InstanceID>")
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
def get_local_prefix() -> str | None:
    for iface in ("en0", "en1"):
        try:
            ip = subprocess.check_output(["ipconfig", "getifaddr", iface], stderr=subprocess.DEVNULL).decode().strip()
            if ip:
                return ".".join(ip.split(".")[:3]) + "."
        except Exception:
            continue
    return None


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
    cands = list(STATE.ip_pool)
    for ip in arp_ips():
        if ip not in cands:
            cands.append(ip)
    for ip in cands:
        try:
            await soap(client, ip, ZGT_PATH, ZGT_SVC, "GetZoneGroupState")
            return ip
        except Exception:
            continue
    return None


# ---------- Refresh loop ----------
async def refresh_state() -> None:
    async with httpx.AsyncClient() as client:
        seed = await find_seed(client)
        if not seed:
            prefix = get_local_prefix()
            if prefix:
                await asyncio.to_thread(prime_arp, prefix)
                seed = await find_seed(client)
        if not seed:
            STATE.last_error = "No Sonos device reachable"
            return

        try:
            devices, groups = await fetch_topology(client, seed)
        except Exception as e:
            STATE.last_error = f"topology error: {e!r}"
            return

        STATE.devices = devices
        STATE.groups = groups
        STATE.ip_pool.update(d["ip"] for d in devices.values())
        STATE.last_error = None

        # Volumes/mute in parallel
        async def vol_for(uuid: str, ip: str) -> None:
            try:
                v, m = await asyncio.gather(get_volume(client, ip), get_mute(client, ip))
                STATE.volumes[uuid] = {"volume": v, "muted": m, "reachable": True}
            except Exception:
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


async def refresh_loop() -> None:
    while True:
        try:
            await refresh_state()
        except Exception as e:
            STATE.last_error = f"refresh loop: {e!r}"
        await asyncio.sleep(REFRESH_INTERVAL)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(refresh_loop())


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


@app.post("/play")
async def api_play(req: CoordReq) -> dict:
    async with httpx.AsyncClient() as c:
        await do_play(c, _ip(req.coordinator_uuid))
    return {"ok": True}


@app.post("/pause")
async def api_pause(req: CoordReq) -> dict:
    async with httpx.AsyncClient() as c:
        await do_pause(c, _ip(req.coordinator_uuid))
    return {"ok": True}


@app.post("/next")
async def api_next(req: CoordReq) -> dict:
    async with httpx.AsyncClient() as c:
        await do_next(c, _ip(req.coordinator_uuid))
    return {"ok": True}


@app.post("/prev")
async def api_prev(req: CoordReq) -> dict:
    async with httpx.AsyncClient() as c:
        await do_prev(c, _ip(req.coordinator_uuid))
    return {"ok": True}


@app.post("/group-volume")
async def api_group_volume(req: GroupVolReq) -> dict:
    async with httpx.AsyncClient() as c:
        await set_group_volume(c, _ip(req.coordinator_uuid), req.volume)
    STATE.group_volumes[req.coordinator_uuid] = max(0, min(100, req.volume))
    return {"ok": True}


# ---------- Static ----------
BASE = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(BASE, "static", "index.html"))
