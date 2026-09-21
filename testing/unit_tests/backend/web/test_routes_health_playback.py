# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the ``playback`` verdict on ``GET /api/health``.

The dashboard used to say only "No media playing".  After a reboot with the
monitor off that left the user guessing between an unplugged cable, a
crash-looping renderer and an empty folder — while the backend could tell the
difference all along.  These tests pin the ordering of the verdict: no
monitor first, then no renderer, then no media.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from metixel.backend.frontend_liveness import FrontendLiveness
from metixel.backend.web.routes import health as health_module
from metixel.backend.web.server import create_app


class FakeDaemon:
    def __init__(self, tracker: FrontendLiveness) -> None:
        self._display_on = True
        self.frontend_liveness = tracker


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)  # mock_state already creates it
    monkeypatch.setenv("METIXEL_RUN_DIR", str(d))
    return d


@pytest.fixture
def make_client(mock_state, mock_ipc, mock_update_manager, run_dir, monkeypatch):
    def _factory(*, monitor: bool | None, frontend_alive: bool, playlist: int = 5) -> Any:
        monkeypatch.setattr(
            health_module,
            "_monitor_status",
            lambda: {
                "available": monitor is not None,
                "connected": monitor,
                "connectors": []
                if monitor is None
                else [
                    {
                        "name": "HDMI-A-1",
                        "status": "connected" if monitor else "disconnected",
                        "edid": bool(monitor),
                    },
                    {"name": "HDMI-A-2", "status": "disconnected", "edid": False},
                ],
            },
        )
        heartbeat = run_dir / "frontend_heartbeat.json"
        if frontend_alive:
            heartbeat.write_text(json.dumps({"pid": 4321, "boot_id": "", "uptime": 90.0}))
            tracker = FrontendLiveness(heartbeat, stale_after=300.0, stable_after=0.0)
            tracker.snapshot()
        else:
            tracker = FrontendLiveness(heartbeat, stale_after=5.0, stable_after=2.0)
        monkeypatch.setattr(
            mock_state,
            "get_system_health",
            lambda: {"playlist_image_count": playlist, "playlist_video_count": 0},
        )
        app = create_app(
            mock_state,
            mock_ipc,
            opt_queue=None,
            update_mgr=mock_update_manager,
            daemon=FakeDaemon(tracker),
        )
        return app.test_client()

    return _factory


def _playback(client) -> dict[str, str]:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    return resp.get_json()["playback"]


class TestPlaybackVerdict:
    def test_no_monitor_wins_over_a_dead_frontend(self, make_client):
        # The real failure: monitor off at boot → renderer can never start.
        pb = _playback(make_client(monitor=False, frontend_alive=False))
        assert pb["state"] == "no_monitor"
        assert "HDMI-A-1, HDMI-A-2" in pb["reason"]
        assert "cable" in pb["reason"]

    def test_dead_frontend_with_a_monitor_names_the_renderer(self, make_client):
        pb = _playback(make_client(monitor=True, frontend_alive=False))
        assert pb["state"] == "frontend_down"
        assert "journalctl -u metixel-cage" in pb["reason"]
        assert "never run" in pb["reason"]

    def test_unknown_monitor_does_not_claim_unplugged(self, make_client):
        # Desktop / no sysfs: fall through to the liveness verdict.
        pb = _playback(make_client(monitor=None, frontend_alive=False))
        assert pb["state"] == "frontend_down"

    def test_empty_playlist_when_renderer_is_alive(self, make_client):
        pb = _playback(make_client(monitor=True, frontend_alive=True, playlist=0))
        assert pb["state"] == "no_media"

    def test_alive_with_media_but_no_slide_yet(self, make_client):
        pb = _playback(make_client(monitor=True, frontend_alive=True))
        assert pb["state"] == "waiting"

    def test_playing_and_paused_come_from_current_media(self, make_client, run_dir):
        client = make_client(monitor=True, frontend_alive=True)
        current = {"file": "a.jpg", "index": 0, "total": 5, "media_type": "image"}
        (run_dir / "current_media.json").write_text(json.dumps({**current, "paused": False}))
        assert _playback(client)["state"] == "playing"
        (run_dir / "current_media.json").write_text(json.dumps({**current, "paused": True}))
        assert _playback(client)["state"] == "paused"

    def test_monitor_block_is_published(self, make_client):
        resp = make_client(monitor=False, frontend_alive=False).get("/api/health")
        monitor = resp.get_json()["monitor"]
        assert monitor["connected"] is False
        assert [c["name"] for c in monitor["connectors"]] == ["HDMI-A-1", "HDMI-A-2"]
