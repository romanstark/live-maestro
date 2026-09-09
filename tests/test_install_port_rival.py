"""Tests for spotting an earlier install of this script under another folder name.

The folder name is not evidence of anything. A release installed before a rename
sits in the same Remote Scripts directory under whatever it was called then, and
Live loads every folder a Control Surface slot names. Two of them bound to
``OUR_PORT`` fail in a way neither reports, so what these tests hold down is that
the collision is found by reading what a folder declares rather than by
recognising a name the installer would have to be told in advance.
"""

from __future__ import annotations

from pathlib import Path

from live_maestro import installer as install_script

OURS = install_script.SCRIPT_FOLDER_NAME


def make_script(library: Path, name: str, body: str) -> Path:
    """One folder under Remote Scripts holding one ``__init__.py``."""
    folder = library / "Remote Scripts" / name
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(body, encoding="utf-8")
    return folder


def only(library: Path, name: str) -> install_script.InstalledScript:
    scripts = [s for s in install_script.scan_remote_scripts(library) if s.name == name]
    assert len(scripts) == 1, f"expected exactly one {name!r}, got {len(scripts)}"
    return scripts[0]


def test_a_differently_named_folder_binding_our_port_is_a_rival(tmp_path: Path) -> None:
    # The upgrade case: the pre-rename folder is still there and still binds 9878.
    make_script(tmp_path, "SomethingElse", f"DEFAULT_PORT = {install_script.OUR_PORT}\n")
    assert only(tmp_path, "SomethingElse").is_port_rival(OURS) is True


def test_our_own_folder_is_never_its_own_rival(tmp_path: Path) -> None:
    # It declares the port because it is supposed to. Reporting it would be noise.
    make_script(tmp_path, OURS, f"DEFAULT_PORT = {install_script.OUR_PORT}\n")
    assert only(tmp_path, OURS).is_port_rival(OURS) is False


def test_the_match_ignores_folder_name_casing(tmp_path: Path) -> None:
    # Windows hands back whatever case is on disk; ours is ours either way.
    make_script(tmp_path, OURS.upper(), f"DEFAULT_PORT = {install_script.OUR_PORT}\n")
    assert only(tmp_path, OURS.upper()).is_port_rival(OURS) is False


def test_a_known_foreign_script_is_reported_as_foreign_not_as_ours(tmp_path: Path) -> None:
    # abletonmcp is somebody else's project. It already has its own notice, and
    # calling it an earlier install of this one would be a false accusation.
    foreign = min(install_script.FOREIGN_FOLDER_NAMES)
    make_script(tmp_path, foreign, f"DEFAULT_PORT = {install_script.OUR_PORT}\n")
    script = only(tmp_path, foreign)
    assert script.is_foreign is True
    assert script.is_port_rival(OURS) is False


def test_the_neighbouring_port_is_not_a_collision(tmp_path: Path) -> None:
    make_script(tmp_path, "Neighbour", f"DEFAULT_PORT = {install_script.FOREIGN_PORT}\n")
    assert only(tmp_path, "Neighbour").is_port_rival(OURS) is False


def test_the_port_number_in_prose_is_not_a_binding(tmp_path: Path) -> None:
    # The regex is anchored on the assignment precisely so that a docstring
    # mentioning the port does not read as a folder claiming it.
    body = f'"""This script talks to port {install_script.OUR_PORT} somewhere else."""\n'
    make_script(tmp_path, "Mentions", body)
    script = only(tmp_path, "Mentions")
    assert script.declares_our_port is False
    assert script.is_port_rival(OURS) is False


def test_a_longer_number_starting_with_our_port_is_not_a_match(tmp_path: Path) -> None:
    make_script(tmp_path, "Longer", f"SOME_VALUE = {install_script.OUR_PORT}0\n")
    assert only(tmp_path, "Longer").declares_our_port is False


def test_a_folder_without_an_init_file_declares_nothing(tmp_path: Path) -> None:
    # Live ignores such a folder, and "could not tell" is not "does not clash".
    (tmp_path / "Remote Scripts" / "Empty").mkdir(parents=True)
    script = only(tmp_path, "Empty")
    assert script.loadable is False
    assert script.declares_our_port is None
    assert script.is_port_rival(OURS) is False


def test_the_installed_remote_script_would_be_recognised_by_this_check() -> None:
    """The check is worthless if it does not match this project's own script.

    Read off the file that actually ships, so a future edit to how the port is
    declared fails here rather than silently turning the upgrade notice off.
    """
    shipped = Path(__file__).resolve().parent.parent / "live-remote-script" / "__init__.py"
    assert shipped.is_file()
    assert install_script._declares_our_port(shipped) is True
