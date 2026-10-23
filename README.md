# Personal CCTV

Self-hosted home CCTV. Cameras on your tailnet post live MJPEG to one host, and a password-protected page on that host is reachable through Tailscale Funnel.

## Features (MVP)

- Multi-camera capable (config-driven), start with one camera
- 480p streaming by default (854x480), bump to 720p via config
- Password-protected web viewer
- MJPEG over HTTP (works in any browser, no plugins)
- Tailscale Funnel for remote access without port forwarding

## Requirements

- macOS with Python 3.9+ (3.11+ recommended)
- USB webcam(s)
- [Tailscale](https://tailscale.com/) on each device that streams or hosts



## Setup



### 1. Clone and configure

```bash
cd personal-cctv
cp .env.example .env
```

Edit `.env`:

- `CCTV_PASSWORD` — password for viewing footage
- `SESSION_SECRET` — long random string for session cookies
- `HOST` / `PORT` — server bind address (default `127.0.0.1:8000`)



### 2. Configure cameras

Edit `[config.yaml](config.yaml)`. Each camera entry:

```yaml
cameras:
  - id: front-door
    name: Front Door
    device_index: 0
    width: 854
    height: 480
    fps: 12
    jpeg_quality: 70
```


| Field              | Description                                         |
| ------------------ | --------------------------------------------------- |
| `id`               | URL-safe identifier used in stream routes           |
| `name`             | Display name in the viewer                          |
| `device_index`     | OpenCV camera index (usually `0` for first USB cam) |
| `width` / `height` | Target resolution (480p: 854x480, 720p: 1280x720)   |
| `fps`              | Capture and stream rate                             |
| `jpeg_quality`     | JPEG compression (1–100)                            |
| `slow`             | Start at one JPEG per second. The page can change it per camera. |




### 3. macOS camera permission

On first run, macOS will prompt for camera access. Grant it to **Terminal** (or whichever app runs Python).

If you see OpenCV authorization errors when starting from a script, run the server interactively once from Terminal so macOS can show the permission dialog. You can also set `OPENCV_AVFOUNDATION_SKIP_AUTH=1` in `.env` / `run.sh` after permission has already been granted.

If the wrong camera opens, try changing `device_index` (`0`, `1`, etc.).

Check the server logs on startup — they show the **actual** negotiated resolution and fps, which may differ from requested values depending on the webcam.

### 4. Install and run locally

```bash
chmod +x run.sh tunnel.sh
./run.sh
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), sign in, and confirm the live stream.

## Remote access (Tailscale)

Devices that stream find each other on the tailnet. The first one to start is the host. The others post JPEGs to it. A later device can take the page with `cctv start --takeover` (or Become host on the deck) so a smaller machine can hand off to a stronger one.

The host turns on Tailscale Funnel. The public page is `https://<host>.<tailnet>.ts.net`. `cctv link` prints that address. A bookmark to another device that is on redirects to the current host.

Every device needs the same `CCTV_PASSWORD` and `INGEST_TOKEN`. The server listens on `0.0.0.0` so tailnet peers can post frames. The viewer password still guards the page, and the ingest token guards frame uploads.

Funnel has an unpublished bandwidth cap. The public name is up only while Tailscale is up on the current host. The Mac App Store build of Tailscale cannot turn Funnel on. If that Mac is the host, other tailnet devices can still open `http://<magicdns>:8000`, and a public link returns when a Linux device takes over.

## Bumping to 720p

Once 480p works end-to-end, update each camera in `config.yaml`:

```yaml
width: 1280
height: 720
fps: 10
jpeg_quality: 65
```

Restart `./run.sh` and open the page again. Lower fps or quality if bandwidth is tight on cellular.

## API routes


| Route                       | Auth | Description        |
| --------------------------- | ---- | ------------------ |
| `GET /`                     | Yes  | Scrollable stack of live feeds |
| `GET /login`, `POST /login` | No   | Password login     |
| `GET /logout`               | No   | Clear session      |
| `GET /stream/{camera_id}`   | Yes  | MJPEG live stream  |
| `GET /snapshot/{camera_id}` | Yes  | Single JPEG frame  |
| `GET /thermal/{camera_id}`  | Yes  | Thermal overlay for that feed |
| `GET /status`               | Yes  | Freshness, thermal, and slow mode |
| `POST /cameras/{camera_id}/slow` | Yes | Set that camera to one JPEG per second |
| `POST /ingest/{camera_id}`  | Bearer | JPEG from another device. `X-Slow: 1` asks to start at one frame a second |
| `GET /cluster`              | No   | Which device is the host |
| `GET /healthz`              | No   | Liveness check     |




## Raspberry Pi panel

On the Pi, the touchscreen UI starts on tty1 at boot. The same actions are available over SSH.

| Command | What it does |
| --- | --- |
| `cctv start` | Start the camera server. It joins the tailnet host, or becomes the host, and publishes Tailscale Funnel |
| `cctv start --takeover` | Become the host when another device is already hosting |
| `cctv camera` | Show the camera on the panel |
| `cctv reboot` | Restart the Pi |
| `cctv shutdown` | Shut the Pi down |
| `cctv link` | Print the public stream URL |

The viewer password is `pass123`. On the panel, keys `1` to `4` match the four tiles, and Esc goes back. The backlight turns off after 10 minutes without a touch or key. The next touch or key turns it back on and does not activate a tile.

After boot finishes and a default route exists, the Pi runs `cctv start` on its own. A check every 30 seconds repairs Wi-Fi, Tailscale, or the stream when one of them stays down, and reboots the Pi if the repair does not hold. Thresholds and which checks count are in `~/.local/share/cctv/supervisor.conf`, created the first time the check runs. The hardware watchdog also resets the Pi if the system itself locks up. That part takes effect on the next reboot after install.

Install on the Pi from a checkout of this repo. sudo will ask for a password.

```bash
./scripts/install-pi.sh
```

The touch controller needs I2C. The installer enables it and will tell you if a reboot is required. If the panel UI will not quit, run `touch ~/.local/share/cctv/skip-ui` from SSH. The UI exits and tty1 drops to a shell.

## Future roadmap

- Recording with automatic deletion after N days
- Motion / object detection
- SMS alerts via on detected movement
- HLS streaming for lower bandwidth on mobile

