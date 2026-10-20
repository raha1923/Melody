from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_API_BASE = "https://melodify.pw/api/desktop/v9"
DEFAULT_ORIGIN = "https://desktop.melodify.app"
DEFAULT_APP_VERSION = "9.1.3"
DEFAULT_PWA_VERSION = "9.2.6 Desktop"
DEFAULT_PLATFORM = "Mac-Desktop" if sys.platform == "darwin" else "Linux-Desktop"
DEFAULT_DEVICE_NAME = "Mac Desktop" if sys.platform == "darwin" else "Linux Desktop"
DEFAULT_STREAM_QUALITY = "320"


@dataclass(frozen=True)
class Settings:
    authorization: str
    user_id: str
    device_id: str
    device_token: str
    api_base: str = DEFAULT_API_BASE
    origin: str = DEFAULT_ORIGIN
    app_version: str = DEFAULT_APP_VERSION
    pwa_version: str = DEFAULT_PWA_VERSION
    platform: str = DEFAULT_PLATFORM
    device_name: str = DEFAULT_DEVICE_NAME
    stream_quality: str = DEFAULT_STREAM_QUALITY
    player: str = "mpv"

    def missing(self) -> list[str]:
        required = {
            "MELODIFY_AUTHORIZATION": self.authorization,
            "MELODIFY_USER_ID": self.user_id,
            "MELODIFY_DEVICE_ID": self.device_id,
            "MELODIFY_DEVICE_TOKEN": self.device_token,
        }
        return [name for name, value in required.items() if not value.strip()]


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file, override=False)

    return Settings(
        authorization=os.getenv("MELODIFY_AUTHORIZATION", "").strip(),
        user_id=os.getenv("MELODIFY_USER_ID", "").strip(),
        device_id=os.getenv("MELODIFY_DEVICE_ID", "").strip(),
        device_token=os.getenv("MELODIFY_DEVICE_TOKEN", "").strip(),
        api_base=os.getenv("MELODIFY_API_BASE", DEFAULT_API_BASE).rstrip("/"),
        origin=os.getenv("MELODIFY_ORIGIN", DEFAULT_ORIGIN).rstrip("/"),
        app_version=os.getenv("MELODIFY_APP_VERSION", DEFAULT_APP_VERSION),
        pwa_version=os.getenv("MELODIFY_PWA_VERSION", DEFAULT_PWA_VERSION),
        platform=os.getenv("MELODIFY_PLATFORM", DEFAULT_PLATFORM),
        device_name=os.getenv("MELODIFY_DEVICE_NAME", DEFAULT_DEVICE_NAME),
        stream_quality=os.getenv("MELODIFY_STREAM_QUALITY", DEFAULT_STREAM_QUALITY),
        player=os.getenv("MELODIFY_PLAYER", "mpv").strip().lower() or "mpv",
    )
