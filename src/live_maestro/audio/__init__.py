"""Offline audio file inspection that needs neither Ableton Live nor an external codec.

Standard library only: it runs where numpy, soundfile and libsndfile are absent, and on
a Python that no longer ships ``aifc``.
"""

from __future__ import annotations

from live_maestro.audio.sample_header import (
    ABLETON_ENCRYPTED_COMPRESSION,
    READABLE_CODES,
    Chunk,
    SampleHeader,
    SampleHeaderError,
    convert_to_pcm_wav,
    read_chunks,
    read_sample_header,
)

__all__ = [
    "ABLETON_ENCRYPTED_COMPRESSION",
    "READABLE_CODES",
    "Chunk",
    "SampleHeader",
    "SampleHeaderError",
    "convert_to_pcm_wav",
    "read_chunks",
    "read_sample_header",
]
