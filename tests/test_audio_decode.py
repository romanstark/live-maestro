"""Cross-platform decoder failures and actual sample-rate conversion."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from live_maestro.audio import decode


def test_soundfile_fallback_resamples_stereo_and_refuses_bad_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(decode, "_converter", lambda: None)
    source = tmp_path / "stereo.wav"
    t = np.arange(48000) / 48000
    signal = np.sin(2 * np.pi * 440 * t) * 0.4
    sf.write(source, np.column_stack((signal, signal)), 48000, subtype="FLOAT")
    mono = np.asarray(decode.decode_to_samples(source, sample_rate=16000))
    assert len(mono) == 16000
    assert abs(np.sqrt(np.mean(mono**2)) - 0.4 / np.sqrt(2)) < 0.001
    bad = tmp_path / "bad.ogg"
    bad.write_bytes(b"not audio")
    with pytest.raises(decode.DecodeError, match="could not decode"):
        decode.decode_to_samples(bad)


@pytest.mark.parametrize("name", ["ffmpeg", "afconvert"])
def test_converter_arguments_and_pcm_readback(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input with spaces.ogg"
    source.write_bytes(b"stub")
    monkeypatch.setattr(decode, "_converter", lambda: (name, name))

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert str(source) in command
        if name == "ffmpeg":
            assert "-nostdin" in command
            assert command[command.index("-ar") + 1] == "16000"
        with wave.open(command[-1], "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            target.writeframes(b"\x00\x40\x00\xc0")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(decode.subprocess, "run", run)
    assert decode.decode_to_samples(source, sample_rate=16000) == [0.5, -0.5]


def test_no_decoder_and_spawn_failure_have_structured_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.wav"
    source.write_bytes(b"stub")
    monkeypatch.setattr(decode, "_converter", lambda: None)
    monkeypatch.setattr(decode, "tool_available", lambda: False)
    with pytest.raises(decode.DecodeError) as missing:
        decode.decode_to_samples(source)
    assert missing.value.code == "no_decoder"
    monkeypatch.setattr(decode, "_converter", lambda: ("ffmpeg", "missing.exe"))

    def run(*args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(decode.subprocess, "run", run)
    with pytest.raises(decode.DecodeError) as unavailable:
        decode.decode_to_samples(source)
    assert unavailable.value.code == "decoder_unavailable"


def test_windows_library_discovery_uses_configured_user_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from live_maestro import installer, sound_index

    user = tmp_path / "custom library"
    core = tmp_path / "Ableton/Live 12 Suite/Resources/Core Library"
    user.mkdir()
    core.mkdir(parents=True)
    install = installer.LiveInstall((12, 4, 6), "Live 12.4.6", tmp_path, tmp_path, user)
    monkeypatch.setattr(installer, "find_live_installs", lambda: [install])
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    roots = sound_index.default_roots()
    assert user in roots and core in roots
