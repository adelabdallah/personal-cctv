# Personal CCTV

Self-hosted home CCTV: USB webcams on a macOS laptop stream live MJPEG footage to a password-protected web page, reachable from anywhere via a Cloudflare Tunnel.

## Features (MVP)

- Multi-camera capable (config-driven), start with one camera
- 480p streaming by default (854x480), bump to 720p via config
- Password-protected web viewer
- MJPEG over HTTP (works in any browser, no plugins)
- Cloudflare Tunnel for remote access without port forwarding

## Requirements

- macOS with Python 3.9+ (3.11+ recommended)
- USB webcam(s)
- [Homebrew](https://brew.sh/) (for `cloudflared`)



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

## Remote access (Cloudflare Tunnel)



### Quick tunnel (testing)

In a second terminal, with `./run.sh` already running:

```bash
./tunnel.sh
```

`cloudflared` prints a boxed public URL like `https://random-words.trycloudflare.com`. **Use only that link.** It also appears again under `OPEN THIS URL IN YOUR BROWSER` when `tunnel.sh` detects it.

Do **not** open `region1.v2.argotunnel.com` or other `*.argotunnel.com` addresses. Those are internal Cloudflare tunnel endpoints, not your public viewer URL, and they will show Cloudflare Error 1000.

If you do not see a `trycloudflare.com` URL:

1. Scroll up in the `tunnel.sh` terminal — it appears within a few seconds in a bordered box.
2. Confirm `./run.sh` is running and listening on the same `PORT` as `.env`.
3. Make sure you are not using a named tunnel config. Quick tunnels do not work if `~/.cloudflared/config.yml` exists — temporarily rename that file.
4. If the tunnel fails to connect, retry with HTTP/2: `cloudflared tunnel --url http://127.0.0.1:8000 --protocol http2`

Open the `trycloudflare.com` URL, sign in, and view the stream from any network.

### Named tunnel (permanent URL)

For a stable URL on your own domain:

1. Install cloudflared: `brew install cloudflared`
2. Log in: `cloudflared tunnel login`
3. Create a tunnel: `cloudflared tunnel create personal-cctv`
4. Route DNS to the tunnel (Cloudflare dashboard or `cloudflared tunnel route dns`)
5. Create a config file (e.g. `~/.cloudflared/config.yml`) pointing to `http://localhost:8000`
6. Run: `cloudflared tunnel run personal-cctv`

See [Cloudflare Tunnel docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) for full setup.

## Bumping to 720p

Once 480p works end-to-end, update each camera in `config.yaml`:

```yaml
width: 1280
height: 720
fps: 10
jpeg_quality: 65
```

Restart `./run.sh` and re-test over the tunnel. Lower fps/quality if bandwidth is tight on cellular.

## API routes


| Route                       | Auth | Description        |
| --------------------------- | ---- | ------------------ |
| `GET /`                     | Yes  | Camera grid viewer |
| `GET /login`, `POST /login` | No   | Password login     |
| `GET /logout`               | No   | Clear session      |
| `GET /stream/{camera_id}`   | Yes  | MJPEG live stream  |
| `GET /snapshot/{camera_id}` | Yes  | Single JPEG frame  |
| `GET /healthz`              | No   | Liveness check     |




## Future roadmap

- Recording with automatic deletion after N days
- Motion / object detection
- SMS alerts via on detected movement
- HLS streaming for lower bandwidth on mobile

