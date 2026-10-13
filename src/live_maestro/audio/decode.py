"""Decode local audio to mono at the descriptor's analysis rate.

Prefer macOS's afconvert, then FFmpeg, then the optional SoundFile/SciPy runtime.
The control server does not import scientific dependencies until that fallback runs.

It cannot read Ableton's encrypted factory samples, and nothing outside Live can. Nor
can anything inside it: a Max for Live ``buffer~`` in Live's own process read 0 of 11
encrypted files and every plain control in the same run, dated in
``measurements/spikes/spike-buffer-encrypted-live-12.4.6.json``. Those sounds are
reached through the preview audio Ableton renders for its browser.

:mod:`live_maestro.audio.sample_header` reads a header without decoding anything. Use
that to ask what a file is, and this to get at what is in it.
"""

from __future__ import annotations

import importlib.util
import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from live_maestro.audio.descriptor import SAMPLE_RATE

__all__ = ["TOOL", "DecodeError", "decode_to_samples", "tool_available"]

TOOL = "afconvert, FFmpeg or live-maestro[audio]"

#: Longest a single decode may take. A preview is seconds long and a stem is minutes;
#: anything past this is a converter that has stopped rather than one that is working.
TIMEOUT_S = 120


class DecodeError(RuntimeError):
    """Raised when a file could not be decoded.

    ``code`` is the machine-readable reason, ``error`` is the sentence for a person.
    """

    def __init__(self, code: str, error: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error


def _converter() -> tuple[str, str] | None:
    """Select an installed executable; an explicit FFmpeg path takes precedence."""
    override = os.environ.get("LIVE_MAESTRO_FFMPEG")
    if override:
        path = Path(override).expanduser()
        return ("ffmpeg", str(path)) if path.is_file() else None
    for name in ("afconvert", "ffmpeg"):
        executable = shutil.which(name)
        if executable:
            return name, executable
    return None


def tool_available() -> bool:
    """Inspect availability without importing NumPy or opening audio."""
    return _converter() is not None or all(
        importlib.util.find_spec(name) is not None for name in ("soundfile", "scipy")
    )


def decode_to_samples(
    path: str | Path, *, sample_rate: int = SAMPLE_RATE
) -> list[float]:
    """Decode a file to mono samples between -1.0 and 1.0.

    Args:
        path: Any audio file the selected local decoder reads.
        sample_rate: Rate to resample to. The default is what
            :mod:`live_maestro.audio.descriptor` expects, and a descriptor taken at a
            different rate is not comparable with one taken at this one.

    Returns:
        The samples, mono, in file order. An empty file decodes to an empty list rather
        than raising, because a zero length file is a fact about the file.

    Raises:
        DecodeError: The file is missing, the decoder is not installed, the decoder
            refused the file, timed out, or wrote back something unreadable. Every
            failure leaves this as a DecodeError, so a caller has one thing to catch
            and a long index run is not ended by one bad file.

    Subprocess decoders write a temporary PCM WAVE, keeping their duration header
    readable. SoundFile's fallback resamples with SciPy and returns finite samples.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise DecodeError("no_such_file", f"{source} is not a file.")
    if sample_rate <= 0:
        raise DecodeError("invalid_sample_rate", "sample_rate must be positive.")
    converter = _converter()
    if converter is None and not tool_available():
        raise DecodeError(
            "no_decoder",
            "Install FFmpeg on PATH, set LIVE_MAESTRO_FFMPEG to its executable, "
            "or install live-maestro[audio] for SoundFile/SciPy decoding.",
        )
    if converter is None:
        return _soundfile_samples(source, sample_rate)

    name, executable = converter

    with tempfile.TemporaryDirectory(prefix="live-maestro-decode-") as folder:
        target = Path(folder) / "decoded.wav"
        try:
            command = (
                [executable, "-f", "WAVE", "-d", f"LEI16@{sample_rate}", "-c", "1",
                 str(source), str(target)]
                if name == "afconvert" else
                [executable, "-nostdin", "-v", "error", "-y", "-i", str(source),
                 "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(target)]
            )
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DecodeError(
                "decoder_timed_out",
                f"{name} did not finish with {source.name} inside {TIMEOUT_S} seconds.",
            ) from exc
        except OSError as exc:
            raise DecodeError("decoder_unavailable", f"Cannot execute {name}: {exc}") from exc
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise DecodeError(
                "decoder_refused",
                f"{name} could not decode {source.name}"
                + (f": {detail.splitlines()[-1]}" if detail else "."),
            )
        try:
            with wave.open(str(target), "rb") as handle:
                if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                    raise DecodeError(
                        "unexpected_decode",
                        f"{name} wrote {handle.getnchannels()} channels at "
                        f"{handle.getsampwidth() * 8} bits, not mono 16 bit.",
                    )
                raw = handle.readframes(handle.getnframes())
        except (wave.Error, OSError, EOFError) as exc:
            raise DecodeError(
                "unreadable_decode", f"what {name} wrote is not a readable WAVE: {exc}"
            ) from exc

    return [value / 32768.0 for (value,) in struct.iter_unpack("<h", raw)]


def _soundfile_samples(source: Path, sample_rate: int) -> list[float]:
    """Decode ordinary unencrypted audio with the separately installed audio extra."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    try:
        audio, rate = sf.read(source, dtype="float64", always_2d=True)
        if not np.isfinite(audio).all():
            raise ValueError("audio contains non-finite samples")
        mono = audio.mean(axis=1)
        if rate != sample_rate and len(mono):
            divisor = math.gcd(rate, sample_rate)
            mono = resample_poly(mono, sample_rate // divisor, rate // divisor)
        return mono.tolist()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DecodeError("decoder_refused", f"SoundFile could not decode {source.name}: {exc}") \
            from exc
