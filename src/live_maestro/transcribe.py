"""Run an automatic transcriber over an audio file and hand back note events.

The transcriber is ``basic-pitch``: Apache licensed, polyphonic, instrument agnostic and
CPU only. It supports Python 3.7 to 3.11 while this project runs on 3.14, so it is never
imported here. It lives in an environment of its own and is called as a subprocess,
which also keeps this project's own dependency count at zero.

What it is worth against a generator's MIDI export of the same five separated stems is
in ``measurements/spikes/spike-basic-pitch-against-suno-midi.json``. Its one clear
weakness is a bass, where a weak fundamental lets the model lock to a harmonic, and that
is what :mod:`live_maestro.music.transcription` is for.

Where the sidecar lives is a setting, read at call time rather than at import, so a
server started before the sidecar existed does not have to be restarted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from live_maestro.music.transcription import Event

__all__ = [
    "ENV_VAR",
    "TranscriberError",
    "default_python",
    "is_available",
    "transcribe",
]

#: Names the Python of the environment ``basic-pitch`` is installed into.
ENV_VAR = "LIVE_MAESTRO_BASIC_PITCH_PYTHON"

#: Looked at when the variable is unset, in order.
_FALLBACKS = (
    Path.home() / ".local" / "share" / "live-maestro" / "basic-pitch" / "bin" / "python",
    Path.home() / ".local" / "share" / "live-maestro" / "basic-pitch" / "Scripts" / "python.exe",
    Path(".venv-basic-pitch") / "bin" / "python",
    Path(".venv-basic-pitch") / "Scripts" / "python.exe",
)

#: How long one file may take. A four minute stem measured about two seconds, so this
#: is a stuck process rather than a slow one.
TIMEOUT_S = 600

#: Run inside the sidecar. Written here rather than shipped as a file because the
#: sidecar has no copy of this project and nothing to import from it.
_RUNNER = """
import json, sys
from basic_pitch.inference import predict
from basic_pitch import ICASSP_2022_MODEL_PATH
source, destination = sys.argv[1], sys.argv[2]
_, _, events = predict(source, model_or_model_path=str(ICASSP_2022_MODEL_PATH))
rows = [
    {"start": float(e[0]), "end": float(e[1]), "pitch": int(e[2]), "amp": float(e[3])}
    for e in events
]
with open(destination, "w") as handle:
    json.dump(rows, handle)
"""


class TranscriberError(RuntimeError):
    """Raised when the transcriber could not be reached or could not run.

    ``code`` is the machine-readable reason, ``error`` is the sentence for a person.
    """

    def __init__(self, code: str, error: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error


def default_python() -> Path | None:
    """The interpreter to run the transcriber with, or None where none was found."""
    named = os.environ.get(ENV_VAR)
    if named:
        candidate = Path(named).expanduser()
        return candidate if candidate.is_file() else None
    for candidate in _FALLBACKS:
        if candidate.expanduser().is_file():
            return candidate.expanduser()
    found = shutil.which("basic-pitch")
    if found:
        bp = Path(found)
        for name in (
            "python",
            "python.exe",
            "python3",
            "python3.exe",
            "python.cmd",
            "python.bat",
        ):
            py_cand = bp.parent / name
            if py_cand.is_file():
                return py_cand
    return None


def is_available() -> bool:
    """Return whether an environment to run the transcriber in was found."""
    return default_python() is not None


def transcribe(path: str | Path, *, python: Path | None = None) -> list[Event]:
    """Transcribe one audio file into note events, uncleaned.

    Args:
        path: Any audio file the transcriber reads.
        python: Interpreter of the environment holding ``basic-pitch``. Found from
            :data:`ENV_VAR` or the usual places when not given.

    Returns:
        Events in seconds, in the order the transcriber reported them. Cleaning is a
        separate step, in :mod:`live_maestro.music.transcription`, because what to
        remove depends on whether the part is played one note at a time.

    Raises:
        TranscriberError: No environment was found, the file is missing, the
            transcriber failed, timed out, or wrote something unreadable. Every
            failure leaves this as a TranscriberError, so a caller has one thing to
            catch.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise TranscriberError("no_such_file", f"{source} is not a file.")

    interpreter = python or default_python()
    if interpreter is None:
        raise TranscriberError(
            "no_transcriber",
            "No environment with basic-pitch was found. Create one with a Python "
            "between 3.7 and 3.11, install basic-pitch into it, and point "
            f"{ENV_VAR} at its interpreter.",
        )

    with tempfile.TemporaryDirectory(prefix="live-maestro-transcribe-") as folder:
        runner = Path(folder) / "run.py"
        runner.write_text(_RUNNER)
        target = Path(folder) / "notes.json"
        cmd = [str(interpreter), str(runner), str(source), str(target)]
        if sys.platform == "win32" and str(interpreter).lower().endswith(
            (".bat", ".cmd")
        ):
            cmd = ["cmd.exe", "/c", *cmd]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                timeout=TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TranscriberError(
                "transcriber_timed_out",
                f"basic-pitch did not finish with {source.name} inside "
                f"{TIMEOUT_S} seconds.",
            ) from exc
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            last = detail.splitlines()[-1] if detail else ""
            raise TranscriberError(
                "transcriber_failed",
                f"basic-pitch could not transcribe {source.name}"
                + (f": {last}" if last else "."),
            )
        try:
            rows = json.loads(target.read_text())
        except (OSError, ValueError) as exc:
            raise TranscriberError(
                "unreadable_result",
                f"basic-pitch wrote something this cannot read: {exc}",
            ) from exc

    return [
        Event(
            start_s=float(row["start"]),
            end_s=float(row["end"]),
            pitch=int(row["pitch"]),
            amplitude=float(row.get("amp") or 1.0),
        )
        for row in rows
    ]
