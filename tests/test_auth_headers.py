"""PWA-aligned AuthClient headers for /getSms."""

from __future__ import annotations

from melodify.auth import DeviceIdentity, _public_headers
from melodify.config import (
    PWA_USER_AGENT,
    PWA_USER_DEVICE_INFO,
    pwa_device_name,
    pwa_os_name,
    pwa_platform,
)


def test_public_headers_omit_auth_and_match_pwa_shape() -> None:
    h = _public_headers(
        DeviceIdentity(device_id="a" * 40, device_name=pwa_device_name())
    )
    assert "authorization" not in h
    assert "user-id" not in h
    assert h["device-token"] == ""
    assert h["User-Agent"] == PWA_USER_AGENT
    assert h["user-device-info"] == PWA_USER_DEVICE_INFO
    assert h["platform"] == pwa_platform()
    assert h["device-name"].endswith(" (os)")
    assert h["Origin"] == "https://desktop.melodify.app"
    assert h["Referer"] == "https://desktop.melodify.app/"


def test_mac_os_platform_string(monkeypatch) -> None:  # noqa: ANN001
    import melodify.config as config

    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.py_platform, "mac_ver", lambda: ("10.15.7", ("", "", ""), ""))
    assert config.pwa_os_name() == "Mac OS"
    assert config.pwa_platform() == "Mac OS-Desktop"
    assert config.pwa_device_name() == "Mac OS 10.15.7 (os)"
