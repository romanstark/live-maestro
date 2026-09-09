"""Channel C: on-demand acoustic measurement from a Max for Live companion device.

Channels A and B read metadata. The Remote Script can report that a fader sits at
-6 dB and the ``.als`` reader can report the same number off disk, but neither can
hear whether the result clips, how loud it is, or whether the low end survives
being summed to mono. Live's Remote Script interpreter has no access to audio
buffers at all.

This package asks a device that does. ``devices/LiveMaestro_Analyzer.amxd``
sits in a track's device chain, measures the signal passing through it, and
answers one OSC request with one OSC reply. The device is generated from
:mod:`live_maestro.analyzer.protocol` by ``scripts/build_analyzer_device.py``,
so the contract exists once.

The layers, in the order a call passes through them:

* :mod:`~live_maestro.analyzer.protocol` the wire contract and its OSC codec
* :mod:`~live_maestro.analyzer.client` the socket, and nothing else
* :mod:`~live_maestro.analyzer.metrics` the arithmetic, as pure functions
* :mod:`~live_maestro.analyzer.report` the choosing, judging and wording
* :mod:`~live_maestro.analyzer.models` what the caller receives

Only the socket layer needs Live to be running, which is why everything a caller
is told can be tested without it.
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
