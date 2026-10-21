"""Auth SMS errors: 502 friendly message + retries (no traceback)."""

from __future__ import annotations

from melodify.auth import AuthClient, AuthError, DeviceIdentity


class _FakeResp:
    def __init__(self, code: int, text: str = "Bad Gateway") -> None:
        self.status_code = code
        self.text = text

    def json(self):  # noqa: ANN201
        raise ValueError("not json")


def test_get_sms_502_retries_then_friendly_error() -> None:
    client = AuthClient(DeviceIdentity(device_id="a" * 40, device_name="test"))
    calls = {"n": 0}

    def fake_post(*_a, **_k):  # noqa: ANN001
        calls["n"] += 1
        return _FakeResp(502)

    client._client.post = fake_post  # type: ignore[method-assign]
    try:
        client.request_otp("09123456789")
        raise AssertionError("expected AuthError")
    except AuthError as exc:
        assert exc.status_code == 502
        assert "temporarily unavailable (502)" in str(exc)
        assert calls["n"] == 3
