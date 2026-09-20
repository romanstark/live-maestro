"""Tests for installing a Max for Live device into a User Library.

The installer writes into a folder Live reads, so these run against a temporary
directory rather than a real library. What they hold down is the part that has no
second chance: the device has to land in the one folder Live's browser looks in,
under the name that folder already knows it by, and neither a file that was already
there nor a device installed earlier may be lost to a later install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from live_maestro import installer as install_script

REPO_ROOT = Path(__file__).resolve().parent.parent

ANALYZER = "LiveMaestro_Analyzer.amxd"
PROBE = "LiveMaestro_Probe.amxd"


def target_in(library: Path, name: str = install_script.DEVICE_TARGET_NAME) -> Path:
    return library / install_script.DEVICE_SUBFOLDER / name


def make_source(
    tmp_path: Path, name: str = ANALYZER, body: bytes = b"ampf\x04\x00\x00\x00aaaa"
) -> Path:
    source = tmp_path / name
    source.write_bytes(body)
    return source


def test_the_default_source_is_the_committed_device() -> None:
    """The installer must not ship pointing at a file that is not there."""
    assert install_script.DEVICE_SOURCE.is_file()
    assert install_script.DEVICE_SOURCE.read_bytes()[:4] == b"ampf"


def test_the_shipped_analyzer_keeps_the_name_lives_browser_resolved() -> None:
    """The derived name has to be the name the browser already indexes.

    Recorded 2026-09-17: the analyzer's browser uri ends
    ``...Max%20Audio%20Effect:LiveMaestro%20Analyzer.amxd``, so the file in that
    folder is called ``LiveMaestro Analyzer.amxd`` with a space. Deriving the
    target from the source must reproduce that byte for byte, or every cached uri
    and item_path stops resolving.
    """
    assert install_script.DEVICE_TARGET_NAME == "LiveMaestro Analyzer.amxd"
    assert install_script.device_target_name(install_script.DEVICE_SOURCE) == (
        "LiveMaestro Analyzer.amxd"
    )


def test_the_default_install_lands_on_exactly_todays_path(tmp_path: Path) -> None:
    """Installing the shipped device, with the shipped flags, writes today's path."""
    library = tmp_path / "User Library"
    assert install_script.install_device(library, install_script.DEVICE_SOURCE, dry_run=False)
    landed = library / "Presets" / "Audio Effects" / "Max Audio Effect"
    landed = landed / "LiveMaestro Analyzer.amxd"
    assert landed.is_file()
    assert landed.read_bytes() == install_script.DEVICE_SOURCE.read_bytes()


@pytest.mark.parametrize(
    ("source_name", "expected"),
    [
        ("LiveMaestro_Analyzer.amxd", "LiveMaestro Analyzer.amxd"),
        ("LiveMaestro_Probe.amxd", "LiveMaestro Probe.amxd"),
        ("Already Spaced.amxd", "Already Spaced.amxd"),
        ("one_two_three.amxd", "one two three.amxd"),
    ],
)
def test_the_target_name_follows_the_source(source_name: str, expected: str) -> None:
    """The name comes from the source and from nothing else."""
    assert install_script.device_target_name(Path("/wherever") / source_name) == expected


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


def test_a_second_device_does_not_land_on_the_first(tmp_path: Path) -> None:
    """Two devices through this path are two files, and the first one survives.

    Until the target name was derived from the source it was the constant
    ``LiveMaestro Analyzer.amxd``, so ``--device-source`` pointing at any second
    device backed the analyzer up and wrote the second device under the analyzer's
    name. A second generated device sits beside the analyzer, so this is the case
    that has to stay broken-proof.
    """
    library = tmp_path / "User Library"
    analyzer = make_source(tmp_path, ANALYZER, b"ampf\x04\x00\x00\x00analyzer-bytes")
    probe = make_source(tmp_path, PROBE, b"ampf\x04\x00\x00\x00probe-bytes")

    assert install_script.install_device(library, analyzer, dry_run=False) is True
    assert install_script.install_device(library, probe, dry_run=False) is True

    folder = target_in(library).parent
    assert target_in(library).read_bytes() == analyzer.read_bytes()
    assert (folder / "LiveMaestro Probe.amxd").read_bytes() == probe.read_bytes()
    assert sorted(path.name for path in folder.iterdir()) == [
        "LiveMaestro Analyzer.amxd",
        "LiveMaestro Probe.amxd",
    ], "a second device must add a file, not replace one and leave a backup"


def test_a_second_device_in_a_dry_run_reports_the_first_as_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = tmp_path / "User Library"
    install_script.install_device(library, make_source(tmp_path, ANALYZER), dry_run=False)
    capsys.readouterr()

    probe = make_source(tmp_path, PROBE, b"ampf\x04\x00\x00\x00probe-bytes")
    assert install_script.install_device(library, probe, dry_run=True) is True

    out = capsys.readouterr().out
    assert "already there  LiveMaestro Analyzer.amxd  (left alone)" in out
    assert "would replace" not in out, "adding a device beside another replaces nothing"
    assert not (target_in(library).parent / "LiveMaestro Probe.amxd").exists()


def test_replacing_a_device_says_so_before_it_does_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one remaining collision is an update, and it is not allowed to be silent."""
    library = tmp_path / "User Library"
    install_script.install_device(library, make_source(tmp_path, ANALYZER), dry_run=False)
    capsys.readouterr()

    newer = make_source(tmp_path, ANALYZER, b"ampf\x04\x00\x00\x00a-newer-analyzer")
    assert install_script.install_device(library, newer, dry_run=False) is True

    out = capsys.readouterr().out
    assert "replacing      LiveMaestro Analyzer.amxd" in out
    assert "backed up" in out
    assert target_in(library).read_bytes() == newer.read_bytes()


def test_a_source_that_is_not_a_device_is_refused(tmp_path: Path) -> None:
    """Live's browser lists only .amxd there, so anything else installs invisibly.

    The generator writes a ``.maxpat`` twin beside every ``.amxd``; naming it by
    mistake used to copy it in under the analyzer's name, which replaced a working
    device with a file Live's browser does not show at all.
    """
    library = tmp_path / "User Library"
    twin = make_source(tmp_path, "LiveMaestro_Analyzer.maxpat", b'{"patcher":{}}')
    assert install_script.install_device(library, twin, dry_run=False) is False
    assert not library.exists()


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
