import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import LoginRequired, login, logout, verify_password
from app.camera import CameraManager
from app.config import get_settings, load_camera_configs

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    camera_configs = load_camera_configs(settings.config_path)
    manager = CameraManager(camera_configs)
    manager.start_all()
    app.state.camera_manager = manager

    logger.info("Started %d camera(s)", len(camera_configs))
    yield
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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
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
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    manager: CameraManager = request.app.state.camera_manager
    cameras = [
        {
            "id": camera.id,
            "name": camera.name,
            "width": camera.actual_width or camera.config.width,
            "height": camera.actual_height or camera.config.height,
        }
        for camera in manager.list_cameras()
    ]
    return templates.TemplateResponse(request, "index.html", {"cameras": cameras})


@app.get("/stream/{camera_id}")
async def stream_camera(request: Request, camera_id: str, _: LoginRequired):
    manager: CameraManager = request.app.state.camera_manager
    camera = manager.get(camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")

    return StreamingResponse(
        camera.mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


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


@app.get("/snapshot/{camera_id}")
async def snapshot_camera(request: Request, camera_id: str, _: LoginRequired):
    manager: CameraManager = request.app.state.camera_manager
    camera = manager.get(camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
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


@app.get("/cameras")
async def list_cameras_local(request: Request):
    if not _local_client(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local only")
    manager: CameraManager = request.app.state.camera_manager
    return [{"id": camera.id, "name": camera.name} for camera in manager.list_cameras()]
