"""Tests for installing the analyzer device into a User Library.

The installer writes into a folder Live reads, so these run against a temporary
directory rather than a real library. What they hold down is the part that has no
second chance: the device has to land in the one folder Live's browser looks in,
and a file that was already there has to survive as a backup.
"""

from __future__ import annotations

from pathlib import Path

from live_maestro import installer as install_script

REPO_ROOT = Path(__file__).resolve().parent.parent


def target_in(library: Path) -> Path:
    return library / install_script.DEVICE_SUBFOLDER / install_script.DEVICE_TARGET_NAME


def make_source(tmp_path: Path, body: bytes = b"ampf\x04\x00\x00\x00aaaa") -> Path:
    source = tmp_path / "device.amxd"
    source.write_bytes(body)
    return source


def test_the_default_source_is_the_committed_device() -> None:
    """The installer must not ship pointing at a file that is not there."""
    assert install_script.DEVICE_SOURCE.is_file()
    assert install_script.DEVICE_SOURCE.read_bytes()[:4] == b"ampf"


def test_the_device_lands_where_lives_browser_looks(tmp_path: Path) -> None:
    # Presets/Audio Effects/Max Audio Effect is the only folder Live lists a user
    # audio effect from. A device anywhere else never appears, with no error.
    library = tmp_path / "User Library"
    source = make_source(tmp_path)
    assert install_script.install_device(library, source, dry_run=False) is True
    landed = target_in(library)
    assert landed.is_file()
    assert landed.read_bytes() == source.read_bytes()
    assert landed.parent.relative_to(library) == Path("Presets/Audio Effects/Max Audio Effect")


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    library = tmp_path / "User Library"
    source = make_source(tmp_path)
    assert install_script.install_device(library, source, dry_run=True) is True
    assert not target_in(library).exists()
    assert not library.exists()


def test_installing_twice_copies_once_and_leaves_no_backup(tmp_path: Path) -> None:
    library = tmp_path / "User Library"
    source = make_source(tmp_path)
    install_script.install_device(library, source, dry_run=False)
    install_script.install_device(library, source, dry_run=False)
    backups = list(target_in(library).parent.glob(f"*{install_script.BACKUP_PREFIX}*"))
    assert backups == [], f"an identical file needs no backup: {backups}"


def test_a_different_file_already_there_is_backed_up_and_not_lost(tmp_path: Path) -> None:
    library = tmp_path / "User Library"
    target = target_in(library)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"a device somebody else put here")
    source = make_source(tmp_path)

    install_script.install_device(library, source, dry_run=False)

    assert target.read_bytes() == source.read_bytes()
    backups = list(target.parent.glob(f"*{install_script.BACKUP_PREFIX}*"))
    assert len(backups) == 1, f"expected one backup, found {backups}"
    assert backups[0].read_bytes() == b"a device somebody else put here"


def test_a_missing_source_is_reported_and_installs_nothing(tmp_path: Path) -> None:
    """An installation that cannot measure audio is still a working installation.

    Only ``analyze_audio`` needs the device, so a source file that is not there
    is worth saying out loud and not worth raising over.
    """
    library = tmp_path / "User Library"
    assert install_script.install_device(library, tmp_path / "gone.amxd", dry_run=False) is False
    assert not target_in(library).exists()


def test_the_flags_that_control_it_exist(tmp_path: Path) -> None:
    args = install_script._build_parser().parse_args([])
    assert args.no_device is False
    assert Path(args.device_source) == install_script.DEVICE_SOURCE
    skipped = install_script._build_parser().parse_args(["--no-device"])
    assert skipped.no_device is True
    elsewhere = install_script._build_parser().parse_args(
        ["--device-source", str(tmp_path / "other.amxd")]
    )
    assert Path(elsewhere.device_source) == tmp_path / "other.amxd"
