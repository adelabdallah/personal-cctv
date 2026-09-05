from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class CameraConfig(BaseModel):
    id: str
    name: str
    backend: Literal["opencv", "picamera2"] = "opencv"
    device_index: int = 0
    width: int = 854
    height: int = 480
    fps: int = 12
    jpeg_quality: int = Field(default=70, ge=1, le=100)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    cctv_password: str
    session_secret: str
    host: str = "127.0.0.1"
    port: int = 8000
    config_path: Path = Field(default=DEFAULT_CONFIG_PATH)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_camera_configs(config_path: Path | None = None) -> list[CameraConfig]:
    path = config_path or get_settings().config_path
    with path.open(encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}

    cameras = data.get("cameras", [])
    if not cameras:
        raise ValueError(f"No cameras defined in {path}")

    return [CameraConfig.model_validate(camera) for camera in cameras]
