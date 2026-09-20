"""Turn any audio file macOS can read into mono samples at the analysis rate.

The decoder is ``afconvert``, which ships with macOS rather than as a dependency: it
reads Ogg Vorbis, MP3, AIFF, WAVE, CAF and ALAC and resamples and downmixes in one pass.

It cannot read Ableton's encrypted factory samples, and nothing outside Live can. Nor
can anything inside it: a Max for Live ``buffer~`` in Live's own process read 0 of 11
encrypted files and every plain control in the same run, dated in
``measurements/spikes/spike-buffer-encrypted-live-12.4.6.json``. Those sounds are
reached through the preview audio Ableton renders for its browser.

:mod:`live_maestro.audio.sample_header` reads a header without decoding anything. Use
that to ask what a file is, and this to get at what is in it.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from live_maestro.audio.descriptor import SAMPLE_RATE

__all__ = ["TOOL", "DecodeError", "decode_to_samples", "tool_available"]

#: The converter. Part of every macOS install, so a missing one means a machine this
#: module was not written for rather than a missing optional extra.
TOOL = "afconvert"

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


def tool_available() -> bool:
    """Return whether the decoder is installed."""
    return shutil.which(TOOL) is not None


def decode_to_samples(
    path: str | Path, *, sample_rate: int = SAMPLE_RATE
) -> list[float]:
    """Decode a file to mono samples between -1.0 and 1.0.

    Args:
        path: Any audio file the system decoder reads.
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

    Note:
        Decoding happens through a temporary file rather than a pipe. ``afconvert``
        writes a WAVE header with a length in it, which it cannot do to a stream.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise DecodeError("no_such_file", f"{source} is not a file.")
    if not tool_available():
        raise DecodeError(
            "no_decoder",
            f"{TOOL} is part of macOS and is not on this machine. Sound matching and "
            f"the sound index need it, and nothing else here does.",
        )

    with tempfile.TemporaryDirectory(prefix="live-maestro-decode-") as folder:
        target = Path(folder) / "decoded.wav"
        try:
            completed = subprocess.run(
                [TOOL, "-f", "WAVE", "-d", f"LEI16@{sample_rate}", "-c", "1",
                 str(source), str(target)],
                capture_output=True,
                timeout=TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DecodeError(
                "decoder_timed_out",
                f"{TOOL} did not finish with {source.name} inside {TIMEOUT_S} seconds.",
            ) from exc
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise DecodeError(
                "decoder_refused",
                f"{TOOL} could not decode {source.name}"
                + (f": {detail.splitlines()[-1]}" if detail else "."),
            )
        try:
            with wave.open(str(target), "rb") as handle:
                if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                    raise DecodeError(
                        "unexpected_decode",
                        f"{TOOL} wrote {handle.getnchannels()} channels at "
                        f"{handle.getsampwidth() * 8} bits, not mono 16 bit.",
                    )
                raw = handle.readframes(handle.getnframes())
        except wave.Error as exc:
            raise DecodeError(
                "unreadable_decode", f"what {TOOL} wrote is not a readable WAVE: {exc}"
            ) from exc

    return [value / 32768.0 for (value,) in struct.iter_unpack("<h", raw)]
