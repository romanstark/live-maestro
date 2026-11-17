"""Refuse a test run that would silently skip the audio tests.

The audio extra (numpy, scipy, soundfile) and the plot extra (matplotlib) are part
of the dev install. Without them every audio test skips and the run still looks
green. Set LIVE_MAESTRO_TESTS_WITHOUT_AUDIO=1 to skip them on purpose; the run
then says so in its summary.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

AUDIO_MODULES = ("numpy", "scipy", "soundfile", "matplotlib")
OPT_OUT = "LIVE_MAESTRO_TESTS_WITHOUT_AUDIO"


def _missing() -> list[str]:
    return [name for name in AUDIO_MODULES if importlib.util.find_spec(name) is None]


def pytest_configure(config: pytest.Config) -> None:
    """Stop before collection when the audio stack is absent and nobody opted out."""
    missing = _missing()
    if missing and os.environ.get(OPT_OUT) != "1":
        raise pytest.UsageError(
            f"audio test dependencies missing: {', '.join(missing)}. Install the dev extra "
            f"(pip install -e '.[dev]' or uv sync), or set {OPT_OUT}=1 to skip those tests "
            "on purpose.")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Name the deliberately skipped audio tests at the end of the run."""
    missing = _missing()
    if missing:
        terminalreporter.write_sep(
            "!", f"AUDIO TESTS SKIPPED ON PURPOSE ({OPT_OUT}=1): missing {', '.join(missing)}",
            red=True)
