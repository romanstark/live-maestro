"""Channel C: on-demand acoustic measurement from a Max for Live companion device.

Channels A and B read metadata; Live's Remote Script interpreter cannot reach audio
buffers, so neither can hear clipping, loudness or what survives a fold to mono.
``devices/LiveMaestro_Analyzer.amxd`` sits in a track's device chain and answers one
OSC request with one OSC reply. ``scripts/build_analyzer_device.py`` generates it from
:mod:`live_maestro.analyzer.protocol`, so the contract exists once. Only the socket
layer needs Live running. The layers, in the order a call passes through them:

* :mod:`~live_maestro.analyzer.protocol` the wire contract and its OSC codec
* :mod:`~live_maestro.analyzer.client` the socket, and nothing else
* :mod:`~live_maestro.analyzer.metrics` the arithmetic, as pure functions
* :mod:`~live_maestro.analyzer.report` the choosing, judging and wording
* :mod:`~live_maestro.analyzer.models` what the caller receives
"""

from __future__ import annotations

from live_maestro.analyzer.client import (
    DEFAULT_ANALYZER_HOST,
    DEFAULT_ANALYZER_PORT,
    DEFAULT_TIMEOUT_SECONDS,
    AnalyzerClient,
    Reply,
)
from live_maestro.analyzer.models import (
    AudioMeasurementResult,
    DynamicsMetrics,
    LoudnessMetrics,
    MeasurementStatus,
    SpectralBalance,
    StereoMetrics,
    TrackAnswer,
)
from live_maestro.analyzer.report import build

__all__ = [
    "DEFAULT_ANALYZER_HOST",
    "DEFAULT_ANALYZER_PORT",
    "DEFAULT_TIMEOUT_SECONDS",
    "AnalyzerClient",
    "AudioMeasurementResult",
    "DynamicsMetrics",
    "LoudnessMetrics",
    "MeasurementStatus",
    "Reply",
    "SpectralBalance",
    "StereoMetrics",
    "TrackAnswer",
    "build",
]
