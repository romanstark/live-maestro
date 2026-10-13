"""Reject local environment payloads before clean artifact installation."""

from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_package import validate_artifacts


@pytest.mark.parametrize("extra", [None, ".validation-venv/lib/scipy/tests/data.wav"])
def test_sdist_gate_confines_the_actual_archive(tmp_path: Path, extra: str | None) -> None:
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("live_maestro/__init__.py", "")
    sdist = tmp_path / "package.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in ["src/live_maestro/__init__.py"] + ([extra] if extra else []):
            member = tarfile.TarInfo("live_maestro-0.1.2/" + name)
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    if extra:
        with pytest.raises(ValueError, match="unlisted sdist roots"):
            validate_artifacts(wheel, sdist)
    else:
        validate_artifacts(wheel, sdist)
