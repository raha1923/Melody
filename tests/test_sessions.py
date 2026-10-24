"""Listening-session API: inactive_session errors and device removal bodies."""

from __future__ import annotations

import json

from melodify.api import InactiveSessionError, MelodifyAPIError, MelodifyClient
from melodify.config import Settings
from melodify.models import DeviceSessions


def _settings() -> Settings:
    return Settings(
        authorization="tok",
        user_id="9",
        device_id="dev",
        device_token="dt",
    )


class _Resp:
    def __init__(self, code: int, payload: dict | str) -> None:
        self.status_code = code
        if isinstance(payload, str):
            self.text = payload
            self._payload: dict | str = payload
        else:
            self.text = json.dumps(payload)
            self._payload = payload

    def json(self) -> dict:
        if not isinstance(self._payload, dict):
            raise ValueError("not json")
        return self._payload


def _client(handler) -> tuple[MelodifyClient, list]:  # noqa: ANN001
    client = MelodifyClient(_settings())
    calls: list[tuple] = []

    def fake_request(method, path, **kwargs):  # noqa: ANN001
        calls.append((method, path, kwargs.get("json")))
        return handler(method, path, kwargs.get("json"))

    client._client.request = fake_request  # type: ignore[method-assign]
    return client, calls


def test_406_inactive_session_json_string() -> None:
    action = {
        "type": "inactive_session",
        "text": "<b>Choose a device</b>",
        "btn_1_text": "Devices",
        "btn_2_text": "Later",
    }

    def handler(method, path, body):  # noqa: ANN001
        return _Resp(406, {"message": json.dumps(action)})

    client, calls = _client(handler)
    try:
        client.list_sessions()
        raise AssertionError("expected InactiveSessionError")
    except InactiveSessionError as exc:
        assert exc.status_code == 406
        assert exc.text == "Choose a device"
        assert str(exc) == "Choose a device"
        assert exc.btn_1_text == "Devices"
        assert exc.btn_2_text == "Later"
        assert len(calls) == 1


def test_406_inactive_session_message_object_and_490() -> None:
    def handler(method, path, body):  # noqa: ANN001
        return _Resp(
            490,
            {"message": {"type": "inactive_session", "text": "Switch device"}},
        )

    client, _calls = _client(handler)
    try:
        client.search("x")
        raise AssertionError("expected InactiveSessionError")
    except InactiveSessionError as exc:
        assert exc.status_code == 490
        assert exc.text == "Switch device"


def test_plain_406_stays_a_login_error() -> None:
    def handler(method, path, body):  # noqa: ANN001
        return _Resp(406, {"message": "nope"})

    client, calls = _client(handler)
    try:
        client.list_sessions()
        raise AssertionError("expected MelodifyAPIError")
    except InactiveSessionError:
        raise AssertionError("plain 406 must not be an inactive session")
    except MelodifyAPIError as exc:
        assert exc.status_code == 406
        assert "login" in str(exc).lower()
        assert len(calls) == 1


def test_list_and_remove_session_payloads() -> None:
    def handler(method, path, body):  # noqa: ANN001
        if path == "/getAllSessions":
            return _Resp(
                200,
                {
                    "done": True,
                    "result": {
                        "my_session": {"name": "Linux (os)"},
                        "other_sessions": [
                            {
                                "name": "Phone",
                                "device_token": "abc",
                                "last_activity": "today",
                            },
                            "skip-me",
                        ],
                    },
                },
            )
        return _Resp(200, {"done": True, "result": {}})

    client, calls = _client(handler)
    sessions = client.list_sessions()
    assert sessions.current is not None
    assert sessions.current.name == "Linux (os)"
    assert sessions.current.is_current
    assert len(sessions.others) == 1
    assert sessions.others[0].device_token == "abc"
    assert sessions.others[0].label() == "Phone · today"

    client.remove_session("abc")
    client.remove_other_sessions()
    assert calls[0] == ("POST", "/getAllSessions", None)
    assert calls[1] == ("POST", "/removeSingleSession", {"device_token": "abc"})
    assert calls[2] == ("POST", "/removeAllSessions", None)

    try:
        client.remove_session("  ")
        raise AssertionError("expected MelodifyAPIError")
    except MelodifyAPIError as exc:
        assert "device token" in str(exc).lower()
    assert len(calls) == 3


def test_device_sessions_from_payload_empty() -> None:
    parsed = DeviceSessions.from_payload({"result": {}})
    assert parsed.current is None
    assert parsed.others == []


def test_cli_use_this_removes_other_sessions(monkeypatch, capsys) -> None:  # noqa: ANN001
    from melodify.cli import main
    from melodify.models import DeviceSession

    seen: dict[str, bool] = {}

    class FakeClient:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def remove_other_sessions(self) -> None:
            seen["removed"] = True

        def list_sessions(self) -> DeviceSessions:
            return DeviceSessions(
                current=DeviceSession(name="This Linux", is_current=True)
            )

    monkeypatch.setattr("melodify.cli.MelodifyClient", FakeClient)
    monkeypatch.setattr("melodify.cli.load_settings", lambda _env=None: _settings())
    assert main(["devices", "--use-this", "--yes"]) == 0
    assert seen.get("removed") is True
    out = capsys.readouterr().out
    assert "now the listening device" in out
    assert "This Linux" in out

    assert main(["devices", "--remove", "abc", "--use-this", "--yes"]) == 2
