"""Local Melodify session + device identity (replaces hand-copied .env tokens)."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from .paths import data_dir, data_file

SESSION_FILE = "session.json"
DEVICE_FILE = "device.json"


@dataclass
class DeviceIdentity:
    device_id: str
    device_name: str


@dataclass
class Session:
    authorization: str
    user_id: str
    device_id: str
    device_token: str
    device_name: str = ""
    platform: str = ""
    full_name: str = ""
    phone: str = ""
    logged_in_at: str = ""

    def is_complete(self) -> bool:
        return bool(
            self.authorization.strip()
            and self.user_id.strip()
            and self.device_id.strip()
            and self.device_token.strip()
        )


def session_path() -> Path:
    return data_file(SESSION_FILE)


def device_path() -> Path:
    return data_file(DEVICE_FILE)


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError:
        pass


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _chmod_private(path)


def load_or_create_device(*, default_name: str) -> DeviceIdentity:
    """Stable per-install device-id (not the old shared sample forever).

    ``device_name`` is refreshed to the current PWA-shaped default so login
    headers stay aligned (e.g. ``Mac OS 14.x (os)``) even if an older CLI
    name was stored.
    """
    path = device_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            device_id = str(raw.get("device_id") or "").strip()
            if device_id:
                identity = DeviceIdentity(
                    device_id=device_id, device_name=default_name
                )
                if str(raw.get("device_name") or "") != default_name:
                    _write_json(
                        path,
                        {
                            **raw,
                            "device_id": identity.device_id,
                            "device_name": identity.device_name,
                        },
                    )
                return identity
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    device_id = sha1(f"melodify-cli:{uuid.uuid4()}".encode("utf-8")).hexdigest()
    identity = DeviceIdentity(device_id=device_id, device_name=default_name)
    _write_json(
        path,
        {
            "device_id": identity.device_id,
            "device_name": identity.device_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return identity


def load_session() -> Session | None:
    path = session_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    session = Session(
        authorization=str(raw.get("authorization") or "").strip(),
        user_id=str(raw.get("user_id") or "").strip(),
        device_id=str(raw.get("device_id") or "").strip(),
        device_token=str(raw.get("device_token") or "").strip(),
        device_name=str(raw.get("device_name") or "").strip(),
        platform=str(raw.get("platform") or "").strip(),
        full_name=str(raw.get("full_name") or "").strip(),
        phone=str(raw.get("phone") or "").strip(),
        logged_in_at=str(raw.get("logged_in_at") or "").strip(),
    )
    if not session.authorization or not session.user_id:
        return None
    return session


def save_session(session: Session) -> Path:
    path = session_path()
    payload = asdict(session)
    if not payload.get("logged_in_at"):
        payload["logged_in_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, payload)
    return path


def clear_session() -> bool:
    """Remove session.json. Keeps device.json so the install identity is stable."""
    path = session_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def ensure_data_dir_private() -> None:
    """Best-effort: data dir not world-readable."""
    try:
        os.chmod(data_dir(), stat.S_IRWXU)  # 0o700
    except OSError:
        pass
