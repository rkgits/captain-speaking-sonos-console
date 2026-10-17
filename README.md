# Sonos Console

A local web app for controlling Sonos speakers on your home network: group and ungroup rooms, adjust volume, control playback, and broadcast voice announcements to every speaker with the **Attention** (Flight Attendant) tab.

It is a single FastAPI service (`app.py`) with a static front end (`static/index.html`). It talks to the speakers directly over your LAN, with no cloud account or Sonos developer key.

> **macOS only, for now.** Speaker discovery uses `ipconfig`, `arp` and `ping`, and the deploy scripts use `launchctl`.

## Features

- Auto-discovers speakers and groups on your subnet.
- Per-room and group volume, mute, join and leave group.
- Play, pause, next and previous, with live now-playing for AirPlay and other sources.
- **Attention tab:** record a message in the browser, optionally wrap it in an airplane chime, and play it over every speaker. Music is ducked and then restored.
- **Flight Log:** history of announcements, with replay and delete.
- `/dashboard`: device status and recent server log.

## Requirements

- macOS with Python 3.9 or newer
- Speakers on the same subnet as the machine running the app
- For recording from a phone: the HTTPS listener (see [Recording from a phone](#recording-from-a-phone))

## Quick start

```bash
git clone https://github.com/rkgits/sonos-app.git
cd sonos-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787
```

Open <http://localhost:8787>, or `http://<your-mac-ip>:8787` from another device on the network.

## Running on your local network (read this for Attention)

The Attention tab behaves differently from the other controls. The playback buttons go from the app to the speakers. An announcement goes the other way too: the app saves your recording and tells each speaker to **download it from your computer over HTTP**. If the speakers cannot reach the app, the request to the speaker succeeds but nothing is played.

Three settings must be right for announcements to work:

1. **Bind to all interfaces.** Use `--host 0.0.0.0`, not `127.0.0.1`. With `127.0.0.1` the controls still work from your own browser, but the speakers cannot fetch the recording.
2. **Keep `PUBLIC_HTTP_PORT` equal to the port you serve on.** The app builds the URL it gives to the speakers as `http://<lan-ip>:$PUBLIC_HTTP_PORT/recordings/<file>`. It defaults to `8787`, so if you run on a different port, set it to match:
   ```bash
   PUBLIC_HTTP_PORT=8788 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8788
   ```
3. **Allow incoming connections.** If macOS asks whether Python may accept incoming network connections, choose **Allow**. A firewall that blocks the port also stops announcements.

The speakers must fetch the recording over plain HTTP, because they cannot use the app's self-signed HTTPS certificate. Always keep a plain-HTTP listener running, even if you also run HTTPS.

### Check that the speakers can reach the app

After making a recording, request it through your LAN address, which is the URL the speakers use:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<your-mac-ip>:8787/recordings/<file>.wav
```

`200` means the speakers can download it. A connection error or timeout means that one of the three settings above is wrong. The server log (or `/dashboard`) prints `audioClip to <room> failed` when the request to a speaker is rejected.

### Recording from a phone

Browsers only allow microphone access on `localhost` or over HTTPS. To record from a phone, run a second listener with a certificate and tell the app its port:

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=sonos.local" \
  -addext "subjectAltName=DNS:sonos.local,DNS:localhost,IP:<your-mac-ip>,IP:127.0.0.1"

# plain HTTP, which the speakers fetch from
PUBLIC_HTTP_PORT=8787 HTTPS_PORT=8443 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787 &
# HTTPS, which the phone uses
PUBLIC_HTTP_PORT=8787 HTTPS_PORT=8443 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
```

Open `https://<your-mac-ip>:8443` on the phone and accept the self-signed certificate warning. When the page is opened over insecure HTTP, the Attention tab links to the HTTPS address for you. `scripts/deploy.sh` does all of this automatically.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_HTTP_PORT` | `8787` | Port the speakers use to download recordings. Must be the plain-HTTP port. |
| `HTTPS_PORT` | `0` (off) | HTTPS port shown in the UI for phone recording. |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Announcement is sent but nothing plays | The app is bound to `127.0.0.1`, `PUBLIC_HTTP_PORT` does not match the real port, or a firewall blocks the port. Use the `curl` check above. |
| `audioClip to <room> failed` in the log | That speaker is unreachable or does not support the local audioClip API (port 1443). |
| `Play failed: 500 Internal Server Error` | The ▶ button was pressed while the speaker is streaming over AirPlay. Sonos does not accept play and pause for AirPlay, so control playback from the AirPlay source (your phone or Mac). |
| Microphone permission never appears on a phone | The page is on plain HTTP. Use the HTTPS address. |
| No speakers found | The machine is on a different subnet or VLAN than the speakers. |
| Recording ends up quiet | Raise the announcement volume in the Attention tab. The default is 45. |

## Deploying as a service

`scripts/deploy.sh` installs the app as a launchd service, using a self-hosted GitHub Actions runner on the Mac:

| Environment | Trigger | Directory | HTTP | HTTPS |
|---|---|---|---|---|
| staging | push to `develop` | `~/sonos/staging` | 8788 | 8444 |
| production | manual run of the **Deploy to Production** workflow | `~/sonos/production` | 8787 | 8443 |

On each deploy the script:

1. Snapshots production (keeping the last 3) into `~/sonos/releases/production`.
2. Syncs the code with `rsync`, leaving `.venv`, `certs/` and `recordings/` in place.
3. Creates a self-signed certificate on first run.
4. Writes `run_server.sh`, which runs the HTTP and HTTPS listeners on `0.0.0.0` with the matching `PUBLIC_HTTP_PORT` and `HTTPS_PORT`. It restarts both if either dies.
5. Rebuilds the venv when `requirements.txt` changes, restarts the launchd agent, and waits for `/state`.
6. On staging, runs the test suite.

**One-time setup:** register a self-hosted runner on the Mac, and create the launchd agents `com.sonos-app` (production) and `com.sonos-app-staging` (staging), each running that environment's `run_server.sh` with `KeepAlive` enabled. The plist files are not part of this repo.

### Rollback

```bash
bash scripts/rollback.sh
```

Restores the most recent production snapshot and restarts the service.

## Tests

The tests check the HTTP API against a running server. They do not need Sonos hardware.

```bash
.venv/bin/pip install -r requirements-dev.txt
TEST_URL=http://localhost:8787 .venv/bin/pytest tests/ -v
```

## Project layout

```
app.py              FastAPI app: Sonos SOAP/UPnP control, discovery, Attention API
static/index.html   Single-page UI
static/             Chime and hero image
scripts/deploy.sh   Deploy to staging or production
scripts/rollback.sh Restore the last production snapshot
tests/              API contract tests
recordings/         Saved announcements (created at runtime, git-ignored)
certs/              Self-signed TLS cert (created by deploy, git-ignored)
```

## Security note

The app has no authentication. Anyone who can reach the port can control your speakers and broadcast audio. Run it only on a trusted home network, and do not expose it to the internet.
