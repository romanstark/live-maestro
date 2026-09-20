"""Wire contract for the Max for Live device the probe scripts drive.

Two measurements need Max running inside Live's process, and the device answers both.

Whether ``buffer~`` reads Ableton's encrypted factory samples. It loads a file, reports
what ``info~`` says and writes the buffer out as a plain WAV, so a caller can see
whether the samples are audio, silence or noise. Length alone decides nothing: a header
parsed before a failed payload still gives a length.

How far ``sfrecord~`` lags the message that starts it. Impulses at known global
positions go into the recorder while the free running sample counter is read at the
start, so the first impulse's offset in the file fixes where its sample zero really was.

``scripts/build_probe_device.py`` generates the device and ``scripts/spike_probe.py``
drives it. Both read this module, so neither can disagree about a port, an address or a
field order.
"""

from __future__ import annotations

__all__ = [
    "BUFFER_ADDRESS",
    "BUFFER_NAME",
    "BUFFER_REPLY_ADDRESS",
    "BUFFER_REPLY_FIELDS",
    "DEVICE_PORT",
    "DUMP_ADDRESS",
    "IMPULSE_PERIOD",
    "PING_ADDRESS",
    "PONG_ADDRESS",
    "REC_MARK_FIELDS",
    "REC_MARK_REPLY_ADDRESS",
    "REC_OPEN_ADDRESS",
    "REC_START_ADDRESS",
    "REC_STOP_ADDRESS",
    "REPLY_PORT",
    "TRACK_OPEN_ADDRESS",
    "TRACK_START_ADDRESS",
    "TRACK_STOP_ADDRESS",
]

#: UDP port the device listens on. Chosen two above the analyzer's 9882 so both devices
#: can sit in the same set without either hearing the other's requests.
DEVICE_PORT = 9884

#: UDP port the harness listens on. The device's reply target is fixed rather than named
#: per request, because a throwaway device with one caller does not need the indirection.
REPLY_PORT = 9885

#: Name of the buffer the device holds. ``info~`` addresses it by name.
BUFFER_NAME = "lmprobe"

PING_ADDRESS = "/lm/probe/ping"
PONG_ADDRESS = "/lm/probe/pong"

#: ``<request_id> <absolute path>``. Replaces the buffer's contents from that file.
#: ``replace`` rather than ``read`` because ``read`` keeps the buffer's current length
#: and would silently truncate every file longer than it.
BUFFER_ADDRESS = "/lm/probe/buffer"

#: ``<request_id> <absolute path>``. Writes the buffer out as a WAV. The same reply
#: carries the completion, because ``buffer~`` bangs the same outlet for read and write.
DUMP_ADDRESS = "/lm/probe/dump"

BUFFER_REPLY_ADDRESS = "/lm/probe/buffer_reply"

#: What :data:`BUFFER_REPLY_ADDRESS` carries, in order. ``duration_ms`` and ``channels``
#: come from ``info~``, which reports what the buffer holds after the read, not what the
#: file claimed. A failed read leaves the previous contents in place, so a request id
#: that never comes back is the only proof that a read failed.
BUFFER_REPLY_FIELDS: tuple[str, ...] = (
    "request_id",
    "samplerate",
    "duration_ms",
    "channels",
)

#: ``<request_id> <absolute path>``. Opens a WAV for recording; does not start it.
REC_OPEN_ADDRESS = "/lm/probe/rec_open"

#: ``<request_id>``. Starts the recorder and reports the sample counter in the same
#: breath.
REC_START_ADDRESS = "/lm/probe/rec_start"

#: ``<request_id>``. Stops the recorder, closes the file and reports the counter again.
REC_STOP_ADDRESS = "/lm/probe/rec_stop"

#: A second recorder, fed by the track's own signal rather than by the impulse train.
#: The two are kept apart on purpose: the impulse recorder measures when recording
#: starts and must not have anything else in it, and this one captures what an
#: instrument on the same track actually plays. ``<request_id> <absolute path>`` to
#: open, ``<request_id>`` to start and to stop.
TRACK_OPEN_ADDRESS = "/lm/probe/track_open"
TRACK_START_ADDRESS = "/lm/probe/track_start"
TRACK_STOP_ADDRESS = "/lm/probe/track_stop"

REC_MARK_REPLY_ADDRESS = "/lm/probe/rec_mark"

#: ``phase`` is 1 for the start mark and 0 for the stop mark. ``count`` is ``count~``
#: read through ``snapshot~``, which reports the value at the last signal vector
#: boundary: the reading is therefore quantised to one vector, and that quantisation is
#: the floor under any latency this device can measure.
REC_MARK_FIELDS: tuple[str, ...] = ("request_id", "phase", "count")

#: Samples between impulses in the signal fed to the recorder. One second at 48 kHz, and
#: longer than any plausible start latency, so the offset of the first impulse in the
#: recording fixes the latency without an integer ambiguity.
IMPULSE_PERIOD = 48000
