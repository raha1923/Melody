"""Phone normalization + session file permissions."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from melodify.auth import AuthError, normalize_phone, to_english_digits
from melodify.session import Session, save_session


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("09123456789", "09123456789"),
        ("9123456789", "09123456789"),
        ("989123456789", "09123456789"),
        ("+98 912 345 6789", "09123456789"),
        ("۰۹۱۲۳۴۵۶۷۸۹", "09123456789"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


def test_normalize_phone_rejects_short() -> None:
    with pytest.raises(AuthError):
        normalize_phone("09")


def test_persian_digits() -> None:
    assert to_english_digits("۱۲۳۴") == "1234"


def test_session_file_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from melodify import paths

    monkeypatch.setattr(paths, "_LEGACY_LINUX", tmp_path / ".local" / "share" / "melodify")
    monkeypatch.setattr(paths, "is_macos", lambda: False)

    s = Session(
        authorization="a" * 32,
        user_id="1",
        device_id="d" * 40,
        device_token="00000000-0000-0000-0000-000000000001",
        full_name="Test",
        phone="09123456789",
    )
    path = save_session(s)
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
