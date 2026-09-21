# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the "is a monitor actually plugged in?" probes.

When every HDMI connector is ``disconnected`` cage starts with no output,
XWayland's root is 0x0 and pi3d aborts inside Xlib before Python can log a
reason.  These probes are the only honest "no screen" signal, so they must
be right in both directions: an unplugged monitor must be reported, and a
machine with no DRM connectors at all (desktop) must read as *unknown*, not
as unplugged.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from metixel.display import hardware
from metixel.display.dispmanx_backend import Pi3dBackend
from metixel.display.hardware import WlrOutput, drm_connector_status


def _connector(root: Path, name: str, status: str, edid: bytes = b"") -> None:
    d = root / f"card1-{name}"
    d.mkdir(parents=True)
    (d / "status").write_text(status + "\n")
    (d / "edid").write_bytes(edid)


class TestDrmConnectorStatus:
    def test_reports_unplugged_when_every_connector_is_disconnected(self, tmp_path: Path):
        _connector(tmp_path, "HDMI-A-1", "disconnected")
        _connector(tmp_path, "HDMI-A-2", "disconnected")
        (tmp_path / "card1").mkdir()  # the card node itself is not a connector
        (tmp_path / "renderD128").mkdir()

        result = drm_connector_status(str(tmp_path))

        assert result["available"] is True
        assert result["connected"] is False
        assert [c["name"] for c in result["connectors"]] == ["HDMI-A-1", "HDMI-A-2"]
        assert all(c["edid"] is False for c in result["connectors"])

    def test_reports_connected_when_one_port_has_a_monitor(self, tmp_path: Path):
        _connector(tmp_path, "HDMI-A-1", "disconnected")
        _connector(tmp_path, "HDMI-A-2", "connected", edid=b"\x00" * 128)

        result = drm_connector_status(str(tmp_path))

        assert result["connected"] is True
        by_name = {c["name"]: c for c in result["connectors"]}
        assert by_name["HDMI-A-2"] == {"name": "HDMI-A-2", "status": "connected", "edid": True}

    def test_writeback_connectors_are_ignored(self, tmp_path: Path):
        _connector(tmp_path, "Writeback-1", "unknown")
        _connector(tmp_path, "Writeback-2", "unknown")

        result = drm_connector_status(str(tmp_path))

        # Only virtual connectors → nothing physical to report on.
        assert result == {"available": False, "connected": None, "connectors": []}

    def test_missing_sysfs_is_unknown_not_unplugged(self, tmp_path: Path):
        result = drm_connector_status(str(tmp_path / "does-not-exist"))

        assert result["available"] is False
        assert result["connected"] is None


def _wlr(monkeypatch: pytest.MonkeyPatch, stdout: str | None, returncode: int = 0) -> None:
    """Fake an installed wlr-randr that prints *stdout* (None → binary absent)."""
    monkeypatch.setattr(hardware.os.path, "exists", lambda p: stdout is not None)

    def fake_run(*_a, **_k):
        return SimpleNamespace(returncode=returncode, stdout=(stdout or "").encode(), stderr=b"")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)


class TestEnabledOutputs:
    def test_no_outputs_at_all_is_an_empty_list(self, monkeypatch):
        _wlr(monkeypatch, "[]")
        assert WlrOutput.enabled_outputs() == []

    def test_only_enabled_outputs_are_listed(self, monkeypatch):
        _wlr(
            monkeypatch,
            json.dumps(
                [
                    {"name": "HDMI-A-1", "enabled": False},
                    {"name": "HDMI-A-2", "enabled": True},
                ]
            ),
        )
        assert WlrOutput.enabled_outputs() == ["HDMI-A-2"]

    def test_missing_binary_is_unknown(self, monkeypatch):
        _wlr(monkeypatch, None)
        assert WlrOutput.enabled_outputs() is None

    def test_failed_command_is_unknown(self, monkeypatch):
        _wlr(monkeypatch, "", returncode=1)
        assert WlrOutput.enabled_outputs() is None


class TestRequireOutput:
    """``Pi3dBackend._require_output`` turns the silent Xlib abort into a logged error."""

    @staticmethod
    def _backend(enabled):
        backend = Pi3dBackend.__new__(Pi3dBackend)
        backend._wlr_output_mgr = SimpleNamespace(enabled_outputs=lambda: enabled)
        return backend

    def test_raises_with_a_clear_message_when_nothing_is_enabled(self, caplog):
        with pytest.raises(RuntimeError, match="No display output is enabled"):
            self._backend([])._require_output()
        assert any(
            r.levelname == "ERROR" and "no monitor on any HDMI port" in r.getMessage()
            for r in caplog.records
        )

    def test_passes_when_an_output_is_enabled(self):
        self._backend(["HDMI-A-2"])._require_output()

    def test_unknown_never_blocks_startup(self):
        self._backend(None)._require_output()
