# Sonos Console 🎛️📣

*Last updated: 16 October 2026*

**Sonos makes great speakers with a so-so control experience.**

When music is playing, you can't tell who started it or what's on. To turn the volume up or down you have to track down whoever is controlling it, and half the time they tap the wrong thing and the music stops. If you've ever lost a party playlist that way, this app is for you.

**Sonos Console** is a small web app that runs on your own computer. Nothing to install on your phone, no account, no cloud. Open it in any browser on your home network and you get:

- 🔊 **Volume for every speaker**, one slider each, plus a group volume.
- 🔇 **Mute and unmute** any speaker.
- 🔗 **Add or remove speakers** from whatever is playing, so a room can join or leave the music in one tap.
- ▶️ **See what's playing** on each group (AirPlay, Spotify and more), with play, pause, next and previous.
- 📣 **Voice announcements on all your speakers at once.** The fun part.

### The announcement feature (Attention / Flight Attendant tab)

Record a message in your browser, optionally wrapped in an airplane chime, and it plays through every speaker at the same time. Think "Pizza's here!", "Last call!" or "Dinner in five."

**It never interrupts your music.** The announcement plays *over* whatever is playing. The music dips for a moment while the message plays, then carries on right where it was. Nothing stops, restarts or loses its place, which makes it perfect for parties.

---

## Contents

- [Is my setup supported?](#is-my-setup-supported)
- [Quick start](#quick-start)
- [Everything you need to install](#everything-you-need-to-install)
- [Running on your local network](#running-on-your-local-network)
- [How it works](#how-it-works)
- [Recording from a phone](#recording-from-a-phone)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Deploying as a service](#deploying-as-a-service)
- [Tests](#tests)
- [Project layout](#project-layout)
- [Security note](#security-note)

---

## Is my setup supported?

### Computer

| OS | Status |
|---|---|
| **macOS** | ✅ Supported. This is what the app is built and tested on. |
| **Linux** | ❌ Not yet. |
| **Windows** | ❌ Not yet. |

The app is macOS-only today because speaker discovery and some settings call macOS tools (`ipconfig getifaddr`, `arp -a`, `ping` with macOS options), and the deploy scripts use `launchctl`. Linux and Windows would need those parts rewritten. Contributions are welcome.

### The computer and the speakers must be on the same network

**The computer running this app must be on the same local network (same Wi-Fi or router, same subnet) as your Sonos speakers.** The app finds speakers by scanning your computer's subnet. It can't see speakers on another network, a guest Wi-Fi, a separate VLAN or the internet. A VPN that reroutes local traffic can also hide them.

Devices that only *open the web page*, such as phones and laptops, don't have to be on the same subnet as the speakers. They only need to reach the computer running the app.

### Sonos speakers

The app uses two parts of Sonos's local network interface:

| Feature | Uses | Needs |
|---|---|---|
| Volume, mute, grouping, play/pause/next/previous, now playing | Standard UPnP/SOAP on port `1400` | Any Sonos speaker on the network |
| **Voice announcements** | Sonos local **audioClip** API on port `1443` | A speaker that reports the `AUDIO_CLIP` capability |

In short, **announcements need an S2 speaker.** Sonos documents audio clips under an `AUDIO_CLIP` capability flag ([Sonos docs: audioClip](https://docs.sonos.com/reference/audioclip), [groups object](https://docs.sonos.com/reference/groups-object)), and the community reports that S1 firmware doesn't support the local audioClip interface ([jrule-sonos](https://github.com/seaside1/jrule-sonos)). I haven't tested S1 speakers.

Sonos sorts its products by app generation ([Sonos app version compatibility](https://support.sonos.com/en/article/sonos-s2-compatibility)):

| Group | Products |
|---|---|
| **S2 only** (announcements ✅) | Arc, Arc SL, Arc Ultra, Beam (Gen 2), Era 100, Era 100 Pro, Era 300, Five, Move 2, One SL, Ray, Roam, Roam 2, Roam SL, Sub (Gen 3), Sub 4, Sub Mini, Ace, SYMFONISK (Gen 2) |
| **S1 or S2** (announcements ✅ only when running **S2**) | Amp, Beam (Gen 1), Boost, Connect (Gen 2), Connect:Amp (Gen 2), Move, One (Gen 1 and 2), Play:1, Play:3, Play:5 (Gen 2), Playbar, Playbase, Port, Sub (Gen 1 and 2), SYMFONISK Bookshelf and Table lamp (Gen 1) |
| **S1 only** (controls ✅, announcements ❌) | Connect (Gen 1), Connect:Amp (Gen 1), Play:5 (Gen 1), Bridge, CR200, ZonePlayer models |

Tested on the author's system: Beam and three Era 100 speakers on firmware `97.2-81220`, all S2.

**Check your own speakers.** Run this against any speaker's IP. If the output contains `AUDIO_CLIP`, announcements will work on it.

```bash
curl -sk -H "X-Sonos-Api-Key: 123e4567-e89b-12d3-a456-426655440000" \
  https://<speaker-ip>:1443/api/v1/players/local/info
```

Sonos can change its firmware at any time, so this list reflects the date at the top of this file.

### Speaker names

**Room names come from Sonos, and you can't change them in this app.** The app reads each speaker's name (the "room name") straight from your Sonos system. To rename a speaker, use the official Sonos app (open the room's settings and change its name). The new name shows up here automatically.

---

## Quick start

```bash
git clone https://github.com/rkgits/sonos-app.git
cd sonos-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787
```

Open <http://localhost:8787>, or `http://<computer-ip>:8787` from your phone or another computer on the network. To find the computer's IP on macOS:

```bash
ipconfig getifaddr en0   # Wi-Fi; try en1 if this prints nothing
```

---

## Everything you need to install

### To run the app

| What | Why | How to get it |
|---|---|---|
| **macOS** | Speaker discovery uses macOS network tools | n/a |
| **Python 3.9 or newer** | Runs the server | Preinstalled with Xcode Command Line Tools, or `brew install python` |
| **`fastapi` ≥ 0.110** | Web framework | `pip`, via `requirements.txt` |
| **`uvicorn` ≥ 0.29** | Web server | `pip`, via `requirements.txt` |
| **`httpx` ≥ 0.27** | Talks to the speakers | `pip`, via `requirements.txt` |

`ipconfig`, `arp` and `ping`, which the app calls for discovery, ship with macOS. Recording uses your browser's microphone, so there's nothing to install for it.

The Python packages are pinned only as minimums in [requirements.txt](requirements.txt). The author's tested versions were `fastapi 0.128`, `uvicorn 0.39`, `httpx 0.28` and `pydantic 2.13` (installed automatically with FastAPI).

### To run the tests

| What | How |
|---|---|
| `pytest` ≥ 8.0, `pytest-asyncio` ≥ 0.23 | `pip install -r requirements-dev.txt` |

### To deploy as a background service (optional)

These are used by `scripts/deploy.sh` and are all part of macOS unless noted:

| What | Used for |
|---|---|
| `rsync` | Copies code into the deploy folder |
| `openssl` | Creates the self-signed HTTPS certificate |
| `launchctl` (launchd) | Starts the server at login and keeps it running |
| `caffeinate` | Keeps the Mac awake while serving |
| `curl` | Health check after a deploy |
| `sha256sum` | Detects changes in `requirements.txt` |
| `python3` + the packages above | Builds the deploy `.venv` |
| **A GitHub Actions self-hosted runner** (not included in macOS) | Runs the deploy workflows |
| **Two launchd plist files** you create yourself | Not in this repo. See [Deploying as a service](#deploying-as-a-service). |

---

## Running on your local network

Announcements work differently from the other controls, so read this part if you want them to work.

The volume and playback buttons go one way, from the app to the speaker. An announcement goes both ways. The app saves your recording, then tells each speaker to **download it from your computer over HTTP**. If the speakers can't reach the app, the request to the speaker still succeeds, but nothing plays.

Three things must be true:

1. **Bind to all interfaces.** Use `--host 0.0.0.0`, not `127.0.0.1`. With `127.0.0.1` the controls still work from your own browser, but the speakers can't fetch the recording.
2. **Keep `PUBLIC_HTTP_PORT` equal to the port you serve on.** The app gives speakers the URL `http://<computer-ip>:$PUBLIC_HTTP_PORT/recordings/<file>`. It defaults to `8787`. If you run on another port, set it to match:
   ```bash
   PUBLIC_HTTP_PORT=8788 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8788
   ```
3. **Allow incoming connections.** If macOS asks whether Python may accept incoming network connections, choose **Allow**. A firewall that blocks the port also silences announcements.

Speakers can't use the app's self-signed HTTPS certificate, so a **plain-HTTP listener must always be running**, even if you also run HTTPS.

### Check that the speakers can reach the app

After recording something, request it the way a speaker would:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<computer-ip>:8787/recordings/<file>.wav
```

`200` means the speakers can download it. A connection error or timeout means one of the three points above is wrong. The server log and the `/dashboard` page show `audioClip to <room> failed` when a speaker rejects the request.

---

## How it works

- **Discovery.** The app scans your subnet, asks the speakers it finds for the Sonos zone-group layout, and refreshes every 2 seconds. Room names, groups and now-playing all come from the speakers themselves.
- **Controls.** Standard Sonos UPnP calls on port `1400`.
- **Announcements.** The browser records audio and uploads it as WAV. The server saves it in `recordings/` and asks every speaker, via the Sonos audioClip API, to play it from the server's URL. The speaker plays the clip over the current audio, lowers the music while the clip plays, and then restores it. Your queue, AirPlay session and playback position are untouched.
- **Chime.** If enabled, the airplane chime is mixed into the recording itself, so the order is always chime → voice → chime.

---

## Recording from a phone

Browsers only allow microphone access on `localhost` or over HTTPS. To record from a phone, run a second, HTTPS listener alongside the HTTP one:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=sonos.local" \
  -addext "subjectAltName=DNS:sonos.local,DNS:localhost,IP:<computer-ip>,IP:127.0.0.1"

# plain HTTP, which the speakers fetch from
PUBLIC_HTTP_PORT=8787 HTTPS_PORT=8443 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787 &
# HTTPS, which the phone uses
PUBLIC_HTTP_PORT=8787 HTTPS_PORT=8443 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
```

Open `https://<computer-ip>:8443` on the phone and accept the self-signed certificate warning. If you open the page over plain HTTP, the Attention tab links you to the HTTPS address. `scripts/deploy.sh` sets all this up for you.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_HTTP_PORT` | `8787` | Port speakers use to download recordings. Must be the plain-HTTP port. |
| `HTTPS_PORT` | `0` (off) | HTTPS port shown in the UI for phone recording. |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No speakers found | The computer isn't on the same network or subnet as the speakers. Check Wi-Fi, guest networks, VLANs and VPNs. |
| Announcement is sent but nothing plays | The app is bound to `127.0.0.1`, `PUBLIC_HTTP_PORT` doesn't match the real port, or a firewall blocks the port. Use the `curl` check above. |
| `audioClip to <room> failed` in the log | That speaker is unreachable or doesn't support audioClip. Check for `AUDIO_CLIP` (see [Sonos speakers](#sonos-speakers)). |
| Announcement is too quiet or loud | Change the announcement volume in the Attention tab. The default is 45. |
| `Play failed: 500 Internal Server Error` | The ▶ button was pressed on a speaker streaming over AirPlay. Sonos doesn't accept play and pause there. Control playback from the AirPlay source (your phone or Mac). |
| Microphone permission never appears on a phone | The page is on plain HTTP. Use the HTTPS address. |
| A room has the wrong name | Rename it in the Sonos app. This app can't rename speakers. |

---

## Deploying as a service

`scripts/deploy.sh` installs the app as a launchd service on a Mac, using a self-hosted GitHub Actions runner. Install the [prerequisites above](#to-deploy-as-a-background-service-optional) first.

| Environment | Trigger | Directory | HTTP | HTTPS |
|---|---|---|---|---|
| staging | push to `develop` | `~/sonos/staging` | 8788 | 8444 |
| production | manual run of the **Deploy to Production** workflow | `~/sonos/production` | 8787 | 8443 |

On each deploy the script:

1. Snapshots production (keeping the last 3) into `~/sonos/releases/production`.
2. Syncs the code with `rsync`, leaving `.venv`, `certs/` and `recordings/` untouched.
3. Creates a self-signed certificate on first run.
4. Writes `run_server.sh`, which runs HTTP and HTTPS listeners on `0.0.0.0` with matching `PUBLIC_HTTP_PORT` and `HTTPS_PORT`, and restarts both if either dies.
5. Rebuilds the `.venv` when `requirements.txt` changes, restarts the launchd agent and waits for `/state`.
6. On staging, runs the test suite.

**One-time setup:** register a self-hosted runner on the Mac, and create launchd agents `com.sonos-app` (production) and `com.sonos-app-staging` (staging). Each should run that environment's `run_server.sh` with `KeepAlive` enabled. The plist files aren't part of this repo.

### Rollback

```bash
bash scripts/rollback.sh
```

Restores the most recent production snapshot and restarts the service.

---

## Tests

The tests check the HTTP API of a running server. They don't need Sonos hardware.

```bash
.venv/bin/pip install -r requirements-dev.txt
TEST_URL=http://localhost:8787 .venv/bin/pytest tests/ -v
```

---

## Project layout

```
app.py               FastAPI app: Sonos control, discovery, announcements
static/index.html    Single-page UI
static/              Chime and hero image
scripts/deploy.sh    Deploy to staging or production
scripts/rollback.sh  Restore the last production snapshot
tests/               API tests
recordings/          Saved announcements (created at runtime, git-ignored)
certs/               Self-signed TLS cert (created by deploy, git-ignored)
```

---

## Security note

The app has **no login**. Anyone who can reach its port can control your speakers and broadcast audio through them. Run it only on a trusted home network, and never expose it to the internet.
