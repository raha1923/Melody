"""Unit tests for EOF detection (auto-advance lag root cause)."""

from __future__ import annotations

from pathlib import Path

from melodify.player import PlayerError, StreamPlayer


class _FakeBackend:
    name = "mpv"
    binary = "mpv"


class _AlivePlayer(StreamPlayer):
    def __init__(self) -> None:
        self.backend = _FakeBackend()
        self._proc = type("P", (), {"poll": lambda self: None})()
        self._ipc_path = Path("/tmp/melodify-test.sock")
        self._paused = False
        self._volume = 80
        self.now_playing = ""

    @property
    def is_alive(self) -> bool:  # type: ignore[override]
        return True


def test_reached_eof_when_process_gone() -> None:
    p = StreamPlayer.__new__(StreamPlayer)
    p.backend = _FakeBackend()
    p._proc = None
    p._ipc_path = None
    assert p.reached_eof() is True


def test_reached_eof_via_ipc_flag() -> None:
    class EofPlayer(_AlivePlayer):
        def _ipc_get(self, prop: str):  # noqa: ANN001
            if prop == "eof-reached":
                return True
            raise PlayerError("unexpected")

    assert EofPlayer().reached_eof() is True


def test_reached_eof_via_position_near_duration() -> None:
    class NearEnd(_AlivePlayer):
        def _ipc_get(self, prop: str):  # noqa: ANN001
            if prop == "eof-reached":
                return False
            raise PlayerError("unexpected")

        def get_position(self) -> float:
            return 199.8

        def get_duration(self) -> float:
            return 200.0

    assert NearEnd().reached_eof() is True


def test_not_eof_mid_track() -> None:
    class Mid(_AlivePlayer):
        def _ipc_get(self, prop: str):  # noqa: ANN001
            if prop == "eof-reached":
                return False
            raise PlayerError("unexpected")

        def get_position(self) -> float:
            return 50.0

        def get_duration(self) -> float:
            return 200.0

    assert Mid().reached_eof() is False
