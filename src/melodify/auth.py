"""Melodify phone + OTP login (same path as desktop.melodify.app)."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from getpass import getpass
from typing import Any

import httpx

from .config import (
    DEFAULT_API_BASE,
    DEFAULT_APP_VERSION,
    DEFAULT_DEVICE_NAME,
    DEFAULT_ORIGIN,
    DEFAULT_PLATFORM,
    DEFAULT_PWA_VERSION,
    Settings,
)
from .session import (
    DeviceIdentity,
    Session,
    ensure_data_dir_private,
    load_or_create_device,
    save_session,
)

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

class AuthError(RuntimeError):
    pass


@dataclass
class VerifyResult:
    token: str
    user: dict[str, Any]
    verified: bool


def to_english_digits(value: str) -> str:
    return (value or "").translate(_PERSIAN_DIGITS)


def normalize_phone(raw: str) -> str:
    """Normalize to ``09xxxxxxxxx`` the way the Melodify PWA ``fixedPhone`` does."""
    digits = re.sub(r"\D", "", to_english_digits(raw).strip())
    # PWA regex: /^(98|0)?9\d{9}$/
    if not re.fullmatch(r"(98|0)?9\d{9}", digits):
        raise AuthError(
            "Invalid phone. Use an Iranian mobile like 09xxxxxxxxx (or +989xxxxxxxxx)."
        )
    if len(digits) == 13:  # 9809xxxxxxxxx
        return "0" + digits[3:]
    if len(digits) == 12:  # 989xxxxxxxxx
        return "0" + digits[2:]
    if len(digits) == 11:  # 09xxxxxxxxx or similar → 0 + rest
        return "0" + digits[1:]
    # 10 digits: 9xxxxxxxxx
    return "0" + digits

def _public_headers(device: DeviceIdentity, *, platform: str = DEFAULT_PLATFORM) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "authorization": "",
        "user-id": "",
        "device-id": device.device_id,
        "device-token": "",
        "device-name": device.device_name,
        "platform": platform,
        "app-version": DEFAULT_APP_VERSION,
        "pwa-version": DEFAULT_PWA_VERSION,
        "Origin": DEFAULT_ORIGIN,
        "Referer": f"{DEFAULT_ORIGIN}/",
        "user-device-info": "screen_w:1280-screen_h:720-terminal:1",
    }


def _api_error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str):
        return err
    return str(payload.get("message") or payload)


class AuthClient:
    """Unauthenticated / mid-login Melodify API calls."""

    def __init__(
        self,
        device: DeviceIdentity,
        *,
        api_base: str = DEFAULT_API_BASE,
        platform: str = DEFAULT_PLATFORM,
        timeout: float = 60.0,
    ):
        self.device = device
        self.platform = platform
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            timeout=timeout,
            headers=_public_headers(device, platform=platform),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AuthClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(
        self,
        path: str,
        json: dict[str, Any],
        *,
        authorization: str = "",
        user_id: str = "",
        device_token: str = "",
    ) -> dict[str, Any]:
        headers = {
            "authorization": authorization,
            "user-id": str(user_id or ""),
            "device-token": device_token or "",
            "device-id": self.device.device_id,
            "device-name": self.device.device_name,
        }
        try:
            response = self._client.post(path, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise AuthError("Melodify timed out — try again.") from exc
        except httpx.HTTPError as exc:
            raise AuthError(f"Network error: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:300]}

        if response.status_code == 406:
            raise AuthError(
                "Session/device rejected (406). Run `melodify login` again."
            )
        if response.status_code >= 400:
            raise AuthError(
                f"HTTP {response.status_code}: {_api_error_message(payload)}"
            )
        if isinstance(payload, dict) and payload.get("done") is False:
            raise AuthError(_api_error_message(payload))
        if not isinstance(payload, dict):
            raise AuthError("Unexpected API response.")
        return payload

    def request_otp(self, phone: str) -> str:
        phone = normalize_phone(phone)
        payload = self._post("/getSms", {"phone": phone})
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            return result
        return "Verification code sent."

    def verify_otp(self, phone: str, code: str) -> VerifyResult:
        phone = normalize_phone(phone)
        code = to_english_digits(code).strip()
        if not re.fullmatch(r"\d{4,8}", code):
            raise AuthError("Enter the SMS code (digits only).")
        payload = self._post("/verifyCode", {"phone": phone, "code": code})
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            raise AuthError("verifyCode returned an unexpected result.")
        token = str(result.get("token") or "").strip()
        user = result.get("user") if isinstance(result.get("user"), dict) else {}
        verified = bool(result.get("verified"))
        if not token:
            raise AuthError("Login succeeded but no token was returned.")
        if not verified:
            raise AuthError(_api_error_message(payload) or "Code not verified.")
        return VerifyResult(token=token, user=user, verified=verified)

    def register(self, *, full_name: str, authorization: str, user_id: str) -> dict[str, Any]:
        name = full_name.strip()
        if not name:
            raise AuthError("Name cannot be empty.")
        payload = self._post(
            "/register",
            {
                "full_name": name,
                "app_version": DEFAULT_APP_VERSION,
                "ref_number": "",
            },
            authorization=authorization,
            user_id=user_id,
        )
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("user"), dict):
            return result["user"]
        if isinstance(result, dict):
            return result
        return {}

    def initialize(
        self,
        *,
        authorization: str,
        user_id: str,
        device_token: str = "",
    ) -> dict[str, Any]:
        payload = self._post(
            "/initializeRequest",
            {
                "app_version": DEFAULT_APP_VERSION,
                "device_id": self.device.device_id,
                "device_name": self.device.device_name,
            },
            authorization=authorization,
            user_id=user_id,
            device_token=device_token,
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AuthError("initializeRequest returned an unexpected result.")
        return result


def finalize_login(
    verified: VerifyResult,
    *,
    phone: str,
    full_name: str | None = None,
    device: DeviceIdentity | None = None,
    prompt_name: bool = False,
) -> Session:
    """Register (if needed) → initialize → persist session.json (0600)."""
    ensure_data_dir_private()
    device = device or load_or_create_device(default_name=DEFAULT_DEVICE_NAME)
    phone = normalize_phone(phone)
    user = dict(verified.user)
    user_id = str(user.get("id") or "").strip()
    if not user_id:
        raise AuthError("verifyCode did not return a user id.")

    with AuthClient(device) as client:
        if not user.get("is_registered"):
            name = (full_name or "").strip()
            if not name and prompt_name and sys.stdin.isatty():
                name = input("Display name (new account): ").strip()
            if not name:
                raise AuthError(
                    "This phone is not registered yet. Provide a display name "
                    "(--name) to finish signup."
                )
            user = client.register(
                full_name=name,
                authorization=verified.token,
                user_id=user_id,
            ) or user
            user_id = str(user.get("id") or user_id).strip()

        init = client.initialize(
            authorization=verified.token,
            user_id=user_id,
            device_token="",
        )
        device_token = str(init.get("device_token") or "").strip()
        if not device_token:
            raise AuthError(
                "Login verified but Melodify did not issue a device_token. "
                "Try `melodify login` again."
            )
        init_user = init.get("user") if isinstance(init.get("user"), dict) else user

    session = Session(
        authorization=verified.token,
        user_id=str(init_user.get("id") or user_id),
        device_id=device.device_id,
        device_token=device_token,
        device_name=device.device_name,
        platform=DEFAULT_PLATFORM,
        full_name=str(init_user.get("full_name") or user.get("full_name") or ""),
        phone=str(init_user.get("phone") or phone),
        logged_in_at=datetime.now(timezone.utc).isoformat(),
    )
    save_session(session)
    return session


def complete_login(
    *,
    phone: str,
    code: str,
    full_name: str | None = None,
    device: DeviceIdentity | None = None,
    prompt_name: bool = False,
) -> Session:
    """OTP verify → optional register → initialize → persist session.json (0600)."""
    ensure_data_dir_private()
    device = device or load_or_create_device(default_name=DEFAULT_DEVICE_NAME)
    phone = normalize_phone(phone)
    with AuthClient(device) as client:
        verified = client.verify_otp(phone, code)
    return finalize_login(
        verified,
        phone=phone,
        full_name=full_name,
        device=device,
        prompt_name=prompt_name,
    )


def interactive_login(
    *,
    phone: str | None = None,
    name: str | None = None,
) -> Session:
    """CLI prompt flow: phone → SMS → OTP → session file."""
    ensure_data_dir_private()
    device = load_or_create_device(default_name=DEFAULT_DEVICE_NAME)
    if not phone:
        phone = input("Phone (09xxxxxxxxx): ").strip()
    phone = normalize_phone(phone)

    with AuthClient(device) as client:
        msg = client.request_otp(phone)
        print(msg)

    code = getpass("SMS code: ").strip() if sys.stdin.isatty() else input("SMS code: ").strip()
    if not code:
        code = input("SMS code: ").strip()

    return complete_login(
        phone=phone,
        code=code,
        full_name=name,
        device=device,
        prompt_name=True,
    )


def session_to_settings(session: Session, base: Settings | None = None) -> Settings:
    """Build Settings from a persisted session (env overrides applied by load_settings)."""
    b = base
    return Settings(
        authorization=session.authorization,
        user_id=session.user_id,
        device_id=session.device_id,
        device_token=session.device_token,
        api_base=b.api_base if b else DEFAULT_API_BASE,
        origin=b.origin if b else DEFAULT_ORIGIN,
        app_version=b.app_version if b else DEFAULT_APP_VERSION,
        pwa_version=b.pwa_version if b else DEFAULT_PWA_VERSION,
        platform=session.platform or (b.platform if b else DEFAULT_PLATFORM),
        device_name=session.device_name or (b.device_name if b else DEFAULT_DEVICE_NAME),
        stream_quality=b.stream_quality if b else "320",
        player=b.player if b else "mpv",
    )
