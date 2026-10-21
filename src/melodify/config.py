from __future__ import annotations

import os
import platform as py_platform
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

from .session import load_or_create_device, load_session

DEFAULT_API_BASE = "https://melodify.pw/api/desktop/v9"
DEFAULT_ORIGIN = "https://desktop.melodify.app"
DEFAULT_APP_VERSION = "9.1.3"
DEFAULT_PWA_VERSION = "9.2.6 Desktop"
DEFAULT_STREAM_QUALITY = "320"

# Browser UA — Melodify edge/WAF is happier when getSms looks like the desktop PWA.
PWA_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    if sys.platform == "darwin"
    else (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
)

# PWA quirk: `user-device-info` uses screen_w twice (width then height).
PWA_USER_DEVICE_INFO = "screen_w:1280-screen_w:720"


def pwa_os_name() -> str:
    """Match react-device-detect / PWA ``osName`` (``Mac OS``, ``Linux``, …)."""
    if sys.platform == "darwin":
        return "Mac OS"
    if sys.platform.startswith("win"):
        return "Windows"
    return "Linux"


def pwa_os_version() -> str:
    """Best-effort OS version string for ``DEVICE_NAME`` (``… (os)``)."""
    if sys.platform == "darwin":
        ver = py_platform.mac_ver()[0] or py_platform.release()
        return ver or "10.15.7"
    return py_platform.release() or "osVersion"


def pwa_platform() -> str:
    """PWA ``platform`` header: ``{osName}-Desktop`` e.g. ``Mac OS-Desktop``."""
    return f"{pwa_os_name()}-Desktop"


def pwa_device_name() -> str:
    """PWA ``device-name``: ``{osName} {osVersion} (os)``."""
    return f"{pwa_os_name()} {pwa_os_version()} (os)"


DEFAULT_PLATFORM = pwa_platform()
DEFAULT_DEVICE_NAME = pwa_device_name()


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
    # Where auth fields came from (for doctor / whoami).
    auth_source: str = "none"

    def missing(self) -> list[str]:
        required = {
            "authorization": self.authorization,
            "user-id": self.user_id,
            "device-id": self.device_id,
            "device-token": self.device_token,
        }
        return [name for name, value in required.items() if not value.strip()]

    @property
    def authenticated(self) -> bool:
        return not self.missing()


def load_settings(env_file: str | None = ".env") -> Settings:
    """Load settings: env vars override session.json; else session; else empty.

    Device identity is always ensured under the OS data dir so new logins do not
    reuse a hardcoded sample device-id.
    """
    if env_file:
        load_dotenv(env_file, override=False)

    session = load_session()
    device = load_or_create_device(
        default_name=os.getenv("MELODIFY_DEVICE_NAME", DEFAULT_DEVICE_NAME).strip()
        or DEFAULT_DEVICE_NAME
    )

    env_auth = os.getenv("MELODIFY_AUTHORIZATION", "").strip()
    env_uid = os.getenv("MELODIFY_USER_ID", "").strip()
    env_did = os.getenv("MELODIFY_DEVICE_ID", "").strip()
    env_dt = os.getenv("MELODIFY_DEVICE_TOKEN", "").strip()

    authorization = env_auth or (session.authorization if session else "")
    user_id = env_uid or (session.user_id if session else "")
    device_id = env_did or (session.device_id if session else "") or device.device_id
    device_token = env_dt or (session.device_token if session else "")

    if env_auth or env_uid or env_did or env_dt:
        # Any explicit env credential counts as env/legacy override source.
        if env_auth and env_uid and (env_did or device_id) and (env_dt or device_token):
            auth_source = "env"
        elif session and session.authorization:
            auth_source = "env+session"
        else:
            auth_source = "env"
    elif session and session.authorization:
        auth_source = "session"
    else:
        auth_source = "none"

    device_name = (
        os.getenv("MELODIFY_DEVICE_NAME", "").strip()
        or device.device_name
        or DEFAULT_DEVICE_NAME
    )
    # Prefer live PWA-shaped platform; remap legacy CLI values (Mac-Desktop → Mac OS-Desktop).
    env_platform = os.getenv("MELODIFY_PLATFORM", "").strip()
    session_platform = (session.platform if session else "").strip()
    platform = env_platform or DEFAULT_PLATFORM
    if not env_platform and session_platform in {"Mac-Desktop", "Darwin-Desktop"}:
        platform = "Mac OS-Desktop"
    elif not env_platform and session_platform:
        # Keep non-legacy session platform only if it already looks PWA-shaped.
        platform = session_platform if session_platform.endswith("-Desktop") else DEFAULT_PLATFORM

    return Settings(
        authorization=authorization,
        user_id=user_id,
        device_id=device_id,
        device_token=device_token,
        api_base=os.getenv("MELODIFY_API_BASE", DEFAULT_API_BASE).rstrip("/"),
        origin=os.getenv("MELODIFY_ORIGIN", DEFAULT_ORIGIN).rstrip("/"),
        app_version=os.getenv("MELODIFY_APP_VERSION", DEFAULT_APP_VERSION),
        pwa_version=os.getenv("MELODIFY_PWA_VERSION", DEFAULT_PWA_VERSION),
        platform=platform,
        device_name=device_name,
        stream_quality=os.getenv("MELODIFY_STREAM_QUALITY", DEFAULT_STREAM_QUALITY),
        player=os.getenv("MELODIFY_PLAYER", "mpv").strip().lower() or "mpv",
        auth_source=auth_source,
    )
