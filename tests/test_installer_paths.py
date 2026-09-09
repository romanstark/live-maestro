"""Tests for locating Live's per-version preferences on each platform.

Pure filesystem shape against temporary directories: nothing here starts Live or
touches a real installation. What these hold down is the one difference that
fails silently. Live keeps ``Library.cfg`` and ``Log.txt`` in a ``Preferences``
subfolder of the version folder on Windows, and directly in the version folder
on macOS, where that folder already sits inside ``~/Library/Preferences``.
Appending the segment on both platforms made the installer look one level too
deep on every Mac: it reported the User Library as missing while the file was
there all along, and named a Windows path in the fix it offered.

The last group covers the hints the installer prints. They are not decoration.
A user who installed the wheel has no ``scripts/`` directory, so a hint naming
one sends them looking for a file that is not on their disk, and a PowerShell
command is no use on a Mac.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from live_maestro import installer as install_script

CFG = """<?xml version="1.0" encoding="UTF-8"?>
<Ableton MajorVersion="5" MinorVersion="12.0_12049">
  <ContentLibrary>
    <UserLibrary>
      <LibraryProject Id="2">
        <ProjectLocation />
        <ProjectName Value="{name}" />
        <ProjectPath Value="{path}" />
      </LibraryProject>
    </UserLibrary>
  </ContentLibrary>
</Ableton>
"""


def write_cfg(folder: Path, *, path: str, name: str = "User Library") -> Path:
    """Write a Library.cfg of the shape Live writes, into ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    cfg = folder / "Library.cfg"
    cfg.write_text(CFG.format(path=path, name=name), encoding="utf-8")
    return cfg


# ----------------------------------------------------------------- the layout


def test_macos_keeps_the_files_in_the_version_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """No nested Preferences exists on macOS; the version folder is the folder."""
    monkeypatch.setattr(sys, "platform", "darwin")
    folder = Path("/Users/username/Library/Preferences/Ableton/Live 12.4.5")
    assert install_script.preferences_dir(folder) == folder


def test_windows_nests_them_under_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows layout is the one that was already measured. Keep it."""
    monkeypatch.setattr(sys, "platform", "win32")
    folder = Path("C:/Users/username/AppData/Roaming/Ableton/Live 12.1.10")
    assert install_script.preferences_dir(folder) == folder / "Preferences"


# -------------------------------------------------------------- discovery


def test_discovery_reads_a_macos_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: a cfg directly in the version folder has to be found."""
    monkeypatch.setattr(sys, "platform", "darwin")
    root = tmp_path / "Ableton"
    version = root / "Live 12.4.5"
    write_cfg(version, path="/Users/username/Music/Ableton")

    installs = install_script.find_live_installs([root])

    assert len(installs) == 1
    assert installs[0].error is None
    assert installs[0].library_cfg == version / "Library.cfg"
    assert installs[0].project_path == Path("/Users/username/Music/Ableton")
    assert installs[0].project_name == "User Library"


def test_discovery_reads_a_windows_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same discovery, one level deeper, still works."""
    monkeypatch.setattr(sys, "platform", "win32")
    root = tmp_path / "Ableton"
    version = root / "Live 12.1.10"
    write_cfg(version / "Preferences", path="D:/Music/Ableton")

    installs = install_script.find_live_installs([root])

    assert len(installs) == 1
    assert installs[0].error is None
    assert installs[0].library_cfg == version / "Preferences" / "Library.cfg"
    assert installs[0].project_path == Path("D:/Music/Ableton")


def test_a_cfg_in_the_other_platforms_place_is_reported_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A miss says where it looked, so the next reader can see the mistake.

    The old message guessed instead ("never fully started?"), which is what let
    a path bug read as a user error for as long as it did.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    root = tmp_path / "Ableton"
    version = root / "Live 12.4.5"
    write_cfg(version / "Preferences", path="/Users/username/Music/Ableton")

    installs = install_script.find_live_installs([root])

    assert installs[0].project_path is None
    assert installs[0].error is not None
    assert str(version / "Library.cfg") in installs[0].error


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_log_txt_and_library_cfg_come_from_one_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """scripts/inventory.py reads Log.txt from ``.preferences``.

    It has to name the folder discovery just read the cfg out of. When these two
    disagreed, the Control Surface readout was broken on macOS for the same
    reason discovery was, and neither had a test.
    """
    monkeypatch.setattr(sys, "platform", platform)
    root = tmp_path / "Ableton"
    version = root / "Live 12.4.5"
    write_cfg(install_script.preferences_dir(version), path="/Music/Ableton")

    install = install_script.find_live_installs([root])[0]

    assert install.library_cfg.parent == install.preferences


def test_linux_has_no_root_and_finds_nothing() -> None:
    """Live does not run there, and an invented path misleads later."""
    assert install_script.find_live_installs([]) == []


# ------------------------------------------------------------- platform hints


def test_invocation_detects_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detects whether running via console script or direct python invocation."""
    monkeypatch.setattr(sys, "argv", ["live-maestro-install"])
    assert install_script.invocation() == "live-maestro-install"
    monkeypatch.setattr(sys, "argv", ["scripts/install_script.py"])
    assert install_script.invocation() == "python scripts/install_script.py"


def test_platform_hints_render_for_windows_and_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both platforms produce valid, copyable commands for library and folder deletion."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert install_script.delete_folder_hint(Path("C:/tmp")).startswith("Remove-Item")
    assert "Preferences" in install_script.library_cfg_hint()

    monkeypatch.setattr(sys, "platform", "darwin")
    assert install_script.delete_folder_hint(Path("/tmp")).startswith("rm -rf")
    assert "Preferences" not in install_script.library_cfg_hint()

