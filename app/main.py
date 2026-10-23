import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from json import JSONDecodeError
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import LoginRequired, login, logout, verify_password
from app.camera import CameraManager
from app.cluster import Cluster, cluster_enabled
from app.config import get_settings, load_app_name, load_camera_configs, load_thermal_enabled
from app.feeds import RemoteFeeds, fresh, mjpeg_frames, valid_jpeg
from app.host import HOT_C, LOW_BATTERY_PERCENT, read_host_status

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
CAMERA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _local_frames(manager: CameraManager) -> list[tuple[str, str, bytes | None, bytes | None, bool]]:
    return [
        (camera.id, camera.name, camera.get_latest_jpeg(), camera.thermal_jpeg(), camera.slow)
        for camera in manager.list_cameras()
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    camera_configs = load_camera_configs(settings.config_path)
    manager = CameraManager(camera_configs)
    manager.start_all()
    remote = RemoteFeeds()

    def apply_pace(camera_id: str, slow: bool) -> None:
        camera = manager.get(camera_id)
        if camera is not None:
            camera.set_slow(slow)

    cluster = Cluster(
        port=settings.port,
        token=settings.ingest_token,
        frames=lambda: _local_frames(manager),
        on_pace=apply_pace,
    )
    app.state.camera_manager = manager
    app.state.remote_feeds = remote
    app.state.thermal_enabled = load_thermal_enabled(settings.config_path)
    app.state.cluster = cluster
    if cluster_enabled():
        cluster.start()
    else:
        cluster.hold_host()

    logger.info("Started %d camera(s)", len(camera_configs))
    yield
    cluster.stop()
    manager.stop_all()
    logger.info("Stopped all cameras")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Personal CCTV", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="cctv_session",
        https_only=False,
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    return app


app = create_app()


def _local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1"}


def _jpeg_or_503(camera) -> Response:
    jpeg = camera.get_latest_jpeg()
    if jpeg is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No frame available yet",
        )
    return Response(content=jpeg, media_type="image/jpeg")


def _check_camera_id(camera_id: str) -> None:
    if not CAMERA_ID.fullmatch(camera_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid camera id")


def _bearer_ok(request: Request, expected: str) -> bool:
    header = request.headers.get("authorization", "")
    if not expected or not header.startswith("Bearer "):
        return False
    try:
        return secrets.compare_digest(header[7:], expected)
    except (TypeError, ValueError):
        return False


def _require_host_ingest(request: Request) -> None:
    if not _bearer_ok(request, get_settings().ingest_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ingest token required")
    cluster: Cluster = request.app.state.cluster
    if not cluster.accepts_ingest():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this device is not the host")


def _posted_name(request: Request, camera_id: str) -> str:
    cleaned = " ".join(request.headers.get("x-camera-name", "").split())[:80]
    return cleaned or camera_id


def _declared_slow(request: Request) -> bool:
    return request.headers.get("x-slow", "").strip() == "1"


def _read_jpeg(body: bytes) -> bytes:
    if not valid_jpeg(body):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="expected a jpeg")
    return body


def _viewer_feeds(request: Request) -> list[dict[str, object]]:
    manager: CameraManager = request.app.state.camera_manager
    remote: RemoteFeeds = request.app.state.remote_feeds
    now = time.monotonic()
    local_ids: set[str] = set()
    rows: list[dict[str, object]] = []
    for camera in manager.list_cameras():
        local_ids.add(camera.id)
        rows.append(
            {
                "id": camera.id,
                "name": camera.name,
                "width": camera.actual_width or camera.config.width,
                "height": camera.actual_height or camera.config.height,
                "fresh": fresh(camera.arrived_at(), now),
                "thermal": camera.thermal_jpeg() is not None,
                "slow": camera.slow,
            }
        )
    for row in remote.rows(now):
        if row["id"] not in local_ids:
            rows.append(row)
    return rows


def _follow(request: Request) -> RedirectResponse | None:
    cluster: Cluster = request.app.state.cluster
    target = cluster.viewer_redirect()
    if target:
        return RedirectResponse(url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    return None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/cluster")
async def cluster_status(request: Request) -> JSONResponse:
    cluster: Cluster = request.app.state.cluster
    return JSONResponse(cluster.snapshot(), headers={"Cache-Control": "no-store"})


@app.post("/cluster/takeover")
async def cluster_takeover(request: Request) -> dict[str, bool]:
    if not _local_client(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local only")
    cluster: Cluster = request.app.state.cluster
    cluster.request_takeover()
    cluster.step()
    return {"ok": True}


@app.get("/login")
async def login_page(request: Request):
    followed = _follow(request)
    if followed is not None:
        return followed
    if request.session.get("authenticated"):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    followed = _follow(request)
    if followed is not None:
        return followed
    if not verify_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    login(request)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout_route(request: Request):
    return logout(request)


@app.get("/")
async def index(request: Request):
    followed = _follow(request)
    if followed is not None:
        return followed
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": load_app_name(),
            "cameras": _viewer_feeds(request),
            "host": read_host_status(),
            "hot_c": HOT_C,
            "low_battery_percent": LOW_BATTERY_PERCENT,
        },
    )


@app.get("/host")
async def host_status(_: LoginRequired) -> JSONResponse:
    return JSONResponse(
        read_host_status().as_dict(),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/status")
async def feed_status(request: Request, _: LoginRequired) -> JSONResponse:
    return JSONResponse({"feeds": _viewer_feeds(request)}, headers={"Cache-Control": "no-store"})


@app.get("/stream/{camera_id}")
async def stream_camera(request: Request, camera_id: str, _: LoginRequired):
    manager: CameraManager = request.app.state.camera_manager
    camera = manager.get(camera_id)
    if camera is not None:
        return StreamingResponse(
            camera.mjpeg_frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )
    remote: RemoteFeeds = request.app.state.remote_feeds
    if remote.video(camera_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    return StreamingResponse(
        mjpeg_frames(lambda: remote.video(camera_id)),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/snapshot/{camera_id}")
async def snapshot_camera(request: Request, camera_id: str, _: LoginRequired):
    manager: CameraManager = request.app.state.camera_manager
    camera = manager.get(camera_id)
    if camera is None:
        remote: RemoteFeeds = request.app.state.remote_feeds
        jpeg = remote.video(camera_id)
        if jpeg is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
        return Response(content=jpeg, media_type="image/jpeg")
    return _jpeg_or_503(camera)


@app.get("/preview/{camera_id}")
async def preview_camera(request: Request, camera_id: str):
    """Unauthenticated JPEG for the local kiosk only."""
    if not _local_client(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local only")
    manager: CameraManager = request.app.state.camera_manager
    camera = manager.get(camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    return _jpeg_or_503(camera)


@app.post("/ingest/{camera_id}")
async def ingest_video(request: Request, camera_id: str) -> dict[str, bool]:
    _check_camera_id(camera_id)
    _require_host_ingest(request)
    if request.app.state.camera_manager.get(camera_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="local camera")
    body = _read_jpeg(await request.body())
    request.app.state.remote_feeds.store_video(
        camera_id,
        _posted_name(request, camera_id),
        body,
        time.monotonic(),
    )
    slow = request.app.state.remote_feeds.note_slow(camera_id, _declared_slow(request))
    return {"ok": True, "slow": slow}


@app.post("/ingest/{camera_id}/thermal")
async def ingest_thermal(request: Request, camera_id: str) -> dict[str, bool]:
    _check_camera_id(camera_id)
    _require_host_ingest(request)
    if request.app.state.camera_manager.get(camera_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="local camera")
    body = _read_jpeg(await request.body())
    request.app.state.remote_feeds.store_thermal(camera_id, body, time.monotonic())
    slow = request.app.state.remote_feeds.note_slow(camera_id, _declared_slow(request))
    return {"ok": True, "slow": slow}


@app.post("/cameras/{camera_id}/slow")
async def set_camera_slow(request: Request, camera_id: str, _: LoginRequired) -> dict[str, bool]:
    _check_camera_id(camera_id)
    try:
        payload = await request.json()
    except JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="expected json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("slow"), bool):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="expected slow true or false")
    slow = payload["slow"]
    camera = request.app.state.camera_manager.get(camera_id)
    if camera is not None:
        camera.set_slow(slow)
        return {"slow": slow}
    if request.app.state.remote_feeds.video(camera_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown camera")
    return {"slow": request.app.state.remote_feeds.set_slow(camera_id, slow)}


@app.post("/thermal")
async def post_thermal(request: Request) -> dict[str, bool]:
    if not _local_client(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local only")
    if not getattr(request.app.state, "thermal_enabled", False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thermal is off")
    body = _read_jpeg(await request.body())
    manager: CameraManager = request.app.state.camera_manager
    cameras = manager.list_cameras()
    if not cameras:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no camera")
    cameras[0].store_thermal(body)
    return {"ok": True}


@app.get("/thermal/{camera_id}")
async def thermal_stream(request: Request, camera_id: str, _: LoginRequired):
    manager: CameraManager = request.app.state.camera_manager
    camera = manager.get(camera_id)
    remote: RemoteFeeds = request.app.state.remote_feeds
    if camera is not None:
        if camera.thermal_jpeg() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no thermal frame")
        return StreamingResponse(
            mjpeg_frames(camera.thermal_jpeg),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )
    if remote.thermal(camera_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no thermal frame")
    return StreamingResponse(
        mjpeg_frames(lambda: remote.thermal(camera_id)),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/cameras")
async def list_cameras_local(request: Request):
    if not _local_client(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local only")
    manager: CameraManager = request.app.state.camera_manager
    return [{"id": camera.id, "name": camera.name} for camera in manager.list_cameras()]
