"""The Wavetable probe plans without Live and refuses an open, real set."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import probe_wavetable_matrix as probe


class SavedSet:
    """A Live whose open set is a saved project of the user's."""

    def script_info(self) -> dict[str, Any]:
        return {}

    def get(self, path: str) -> dict[str, Any]:
        assert path == "song.file_path"
        return {"value": "Song Project/Song.als"}


def test_plan_only_without_go(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["probe", "--track", "1", "--device", "0"])
    monkeypatch.setattr(probe, "LiveClient", lambda: pytest.fail("Live contacted"))
    assert probe.main() == 0
    assert "pass --go" in capsys.readouterr().out


def test_refuses_a_saved_set_that_was_not_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["probe", "--track", "1", "--device", "0", "--go"])
    monkeypatch.setattr(probe, "LiveClient", SavedSet)
    assert probe.main() == 2
