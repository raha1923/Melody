"""TUI: devices screen and logout return to the login form (no network)."""

from __future__ import annotations

import asyncio

from melodify.api import InactiveSessionError
from melodify.config import Settings
from melodify.models import DeviceSession, DeviceSessions, ListeningHistory
from melodify.tui import (
    DevicesScreen,
    InactiveSessionPrompt,
    LoginScreen,
    MelodifyApp,
)


def _settings() -> Settings:
    return Settings(
        authorization="tok",
        user_id="9",
        device_id="dev",
        device_token="dt",
        auth_source="session",
    )


def test_devices_screen_and_logout(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("MELODIFY_MPRIS", "0")
    monkeypatch.setenv("MELODIFY_NOWPLAYING", "0")
    monkeypatch.setattr("melodify.tui.clear_session", lambda: True)
    seen: dict[str, bool] = {}
    sessions = DeviceSessions(
        current=DeviceSession(name="This Linux", is_current=True),
        others=[
            DeviceSession(name="Phone", device_token="abc", last_activity="today")
        ],
    )

    app = MelodifyApp(_settings())
    app.client.list_sessions = lambda: sessions  # type: ignore[method-assign]
    app.client.get_listening_history = (  # type: ignore[method-assign]
        lambda offset=0: ListeningHistory(tracks=[], source="most_listened", end=True)
    )

    def remove_others() -> None:
        seen["removed"] = True

    app.client.remove_other_sessions = remove_others  # type: ignore[method-assign]

    async def body() -> None:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            await pilot.click("#btn-devices")
            for _ in range(30):
                await pilot.pause(0.05)
                if isinstance(app.screen, DevicesScreen):
                    current = app.screen.query_one("#device-current")
                    if "This Linux" in str(current.render()):
                        break
            assert isinstance(app.screen, DevicesScreen)
            assert "This Linux" in str(app.screen.query_one("#device-current").render())
            labels = [
                str(widget.render())
                for widget in app.screen.query("#device-list Label")
            ]
            assert any("Phone" in text for text in labels)

            await pilot.click("#btn-use-this")
            for _ in range(30):
                await pilot.pause(0.05)
                if seen.get("removed") and not isinstance(app.screen, DevicesScreen):
                    break
            assert seen.get("removed") is True
            assert not isinstance(app.screen, DevicesScreen)

            await pilot.click("#btn-logout")
            await pilot.pause()
            await pilot.click("#confirm-yes")
            for _ in range(30):
                await pilot.pause(0.05)
                if isinstance(app.screen, LoginScreen):
                    break
            assert isinstance(app.screen, LoginScreen)
            assert app.settings.authenticated is False

    asyncio.run(body())


def test_inactive_session_stops_playback_and_offers_listen_here(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("MELODIFY_MPRIS", "0")
    monkeypatch.setenv("MELODIFY_NOWPLAYING", "0")
    seen: dict[str, int] = {"removed": 0, "stopped": 0}

    app = MelodifyApp(_settings())
    app.client.get_listening_history = (  # type: ignore[method-assign]
        lambda offset=0: ListeningHistory(tracks=[], source="most_listened", end=True)
    )
    app.client.list_sessions = lambda: DeviceSessions(  # type: ignore[method-assign]
        current=DeviceSession(name="This Linux", is_current=True)
    )

    def remove_others() -> None:
        seen["removed"] += 1

    app.client.remove_other_sessions = remove_others  # type: ignore[method-assign]
    original_stop = app.player.stop

    def stop() -> float | None:
        seen["stopped"] += 1
        return original_stop()

    app.player.stop = stop  # type: ignore[method-assign]

    error = InactiveSessionError(
        "This account is playing on another device.",
        text="This account is playing on another device.",
    )

    async def body() -> None:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            app.note_api_error(error)
            app.note_api_error(error)
            await pilot.pause()
            assert isinstance(app.screen, InactiveSessionPrompt)
            assert seen["stopped"] >= 1
            assert "another device" in str(
                app.screen.query_one("#inactive-text").render()
            ).lower()
            prompts = [
                screen
                for screen in app.screen_stack
                if isinstance(screen, InactiveSessionPrompt)
            ]
            assert len(prompts) == 1

            await pilot.click("#btn-manage-devices")
            for _ in range(20):
                await pilot.pause(0.05)
                if isinstance(app.screen, DevicesScreen):
                    break
            assert isinstance(app.screen, DevicesScreen)

            app.note_api_error(error)
            await pilot.pause()
            assert isinstance(app.screen, InactiveSessionPrompt)
            await pilot.click("#btn-listen-here")
            for _ in range(30):
                await pilot.pause(0.05)
                if seen["removed"]:
                    break
            assert seen["removed"] == 1
            assert not isinstance(app.screen, InactiveSessionPrompt)

    asyncio.run(body())
