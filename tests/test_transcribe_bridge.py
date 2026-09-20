"""The bridge to the transcriber, tested without one being installed.

Everything here is about finding the sidecar and failing legibly when it is not there.
What the transcriber is worth is a measurement and lives in ``measurements/spikes/``;
that it is reachable is a property of this code and belongs here.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from live_maestro.transcribe import (
    ENV_VAR,
    TranscriberError,
    default_python,
    is_available,
    transcribe,
)


@pytest.fixture(autouse=True)
def no_inherited_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own sidecar must not decide what these tests see."""
    monkeypatch.delenv(ENV_VAR, raising=False)


def fake_python(folder: Path, py_code: str) -> Path:
    """An executable standing in for the sidecar interpreter."""
    if sys.platform == "win32":
        py_file = folder / "_fake_runner.py"
        py_file.write_text(py_code, encoding="utf-8")
        script = folder / "python.cmd"
        script.write_text(
            f'@echo off\n"{sys.executable}" -B "{py_file}" %*\n', encoding="utf-8"
        )
        return script
    script = folder / "python"
    script.write_text(f"#!{sys.executable}\n" + py_code)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class TestFindingIt:
    def test_the_variable_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        here = fake_python(tmp_path, "import sys\nsys.exit(0)\n")
        monkeypatch.setenv(ENV_VAR, str(here))
        assert default_python() == here

    def test_a_variable_pointing_nowhere_finds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Naming a path that is not there is a mistake worth seeing, not falling back."""
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "absent"))
        assert default_python() is None

    def test_availability_follows_the_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_VAR, str(fake_python(tmp_path, "import sys\nsys.exit(0)\n")))
        assert is_available() is True
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "absent"))
        assert is_available() is False

    def test_the_variable_is_read_at_call_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server started before the sidecar existed must not need restarting."""
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "absent"))
        assert default_python() is None
        monkeypatch.setenv(ENV_VAR, str(fake_python(tmp_path, "import sys\nsys.exit(0)\n")))
        assert default_python() is not None

    def test_which_basic_pitch_locates_companion_python(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bp = tmp_path / ("basic-pitch.exe" if sys.platform == "win32" else "basic-pitch")
        bp.write_text("")
        py = fake_python(tmp_path, "import sys\nsys.exit(0)\n")
        monkeypatch.setattr("shutil.which", lambda cmd: str(bp) if cmd == "basic-pitch" else None)
        monkeypatch.setattr("live_maestro.transcribe._FALLBACKS", ())
        assert default_python() == py

    def test_fallback_finds_windows_scripts_python(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        py = scripts / "python.exe"
        py.write_text("")
        monkeypatch.setattr("live_maestro.transcribe._FALLBACKS", (py,))
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        assert default_python() == py


class TestFailing:
    def test_a_missing_file_says_so_before_anything_runs(self, tmp_path: Path) -> None:
        with pytest.raises(TranscriberError) as caught:
            transcribe(tmp_path / "nothing.wav", python=tmp_path / "unused")
        assert caught.value.code == "no_such_file"

    def test_no_sidecar_says_how_to_make_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        monkeypatch.setenv(ENV_VAR, str(tmp_path / "absent"))
        with pytest.raises(TranscriberError) as caught:
            transcribe(audio)
        assert caught.value.code == "no_transcriber"
        assert "basic-pitch" in caught.value.error
        assert ENV_VAR in caught.value.error

    def test_a_transcriber_that_fails_carries_its_last_line(self, tmp_path: Path) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        broken = fake_python(
            tmp_path,
            "import sys\nsys.stderr.write('boom: cannot read that\\n')\nsys.exit(1)\n",
        )
        with pytest.raises(TranscriberError) as caught:
            transcribe(audio, python=broken)
        assert caught.value.code == "transcriber_failed"
        assert "cannot read that" in caught.value.error

    def test_a_transcriber_that_writes_nothing_is_a_failure(self, tmp_path: Path) -> None:
        """Exit zero and no output is the quiet failure worth catching."""
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        silent = fake_python(tmp_path, "import sys\nsys.exit(0)\n")
        with pytest.raises(TranscriberError) as caught:
            transcribe(audio, python=silent)
        assert caught.value.code == "transcriber_failed"


class TestReadingItBack:
    def test_events_come_back_as_events(self, tmp_path: Path) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        rows = [
            {"start": 0.0, "end": 0.5, "pitch": 60, "amp": 0.8},
            {"start": 1.0, "end": 2.0, "pitch": 64, "amp": 0.2},
        ]
        body = (
            "import sys, pathlib\n"
            f"pathlib.Path(sys.argv[3]).write_text({json.dumps(rows)!r}, encoding='utf-8')\n"
        )
        writer = fake_python(tmp_path, body)
        got = transcribe(audio, python=writer)
        assert [(e.pitch, e.start_s, e.end_s, e.amplitude) for e in got] == [
            (60, 0.0, 0.5, 0.8),
            (64, 1.0, 2.0, 0.2),
        ]

    def test_a_missing_amplitude_reads_as_full(self, tmp_path: Path) -> None:
        """A transcriber that reports no loudness must not make everything silent."""
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        rows = [{"start": 0.0, "end": 1.0, "pitch": 60}]
        body = (
            "import sys, pathlib\n"
            f"pathlib.Path(sys.argv[3]).write_text({json.dumps(rows)!r}, encoding='utf-8')\n"
        )
        writer = fake_python(tmp_path, body)
        assert transcribe(audio, python=writer)[0].amplitude == 1.0

    def test_an_empty_transcription_is_an_empty_list(self, tmp_path: Path) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        writer = fake_python(
            tmp_path,
            "import sys, pathlib\npathlib.Path(sys.argv[3]).write_text('[]', encoding='utf-8')\n",
        )
        assert transcribe(audio, python=writer) == []

    def test_nothing_is_left_behind_in_the_temporary_folder(self, tmp_path: Path) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        writer = fake_python(
            tmp_path,
            "import sys, pathlib\npathlib.Path(sys.argv[3]).write_text('[]', encoding='utf-8')\n",
        )
        before = set(os.listdir(tmp_path))
        transcribe(audio, python=writer)
        assert set(os.listdir(tmp_path)) == before
