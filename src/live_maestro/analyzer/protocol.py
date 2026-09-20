"""The wire contract between ``analyze_audio`` and ``LiveMaestro_Analyzer.amxd``.

``scripts/build_analyzer_device.py`` generates the Max patcher from the constants below
and ``live_maestro.analyzer.client`` decodes replies with them, so device and client
cannot disagree about a port number, a field order or a field count. None of these
constants may be repeated anywhere else.

The format is Open Sound Control, which is what Max's ``udpsend`` serializes to and
what ``udpreceive`` decodes.

Only int32, float32, string and the address pattern are handled, which is everything
Max emits from a message box carrying ints, floats and symbols.

>>> packet = encode(REQUEST_ADDRESS, [7, 500, 0, 54321])
>>> decode(packet)
('/maestro/measure', [7, 500, 0, 54321])
"""

from __future__ import annotations

import struct

__all__ = [
    "BANDS",
    "CALIBRATION_AMPLITUDE",
    "CALIBRATION_HZ",
    "DEFAULT_DEVICE_PORT",
    "LOW_BAND_HZ",
    "MAX_AVERAGING_SAMPLES",
    "NUMERIC_REPLY_FIELDS",
    "REPLY_ADDRESS",
    "REPLY_SCHEMA",
    "REQUEST_ADDRESS",
    "REQUEST_FIELDS",
    "STRING_REPLY_FIELDS",
    "OscError",
    "decode",
    "encode",
]

#: The port every device instance listens on. Two analyzers in one Live set therefore
#: both hear every request and both answer. The client gathers all the answers and
#: picks by track, which is also how it reports which tracks can measure.
DEFAULT_DEVICE_PORT = 9882

#: Where the client asks and where the device answers. The address carries the
#: project name so an unrelated OSC sender on the same port cannot be mistaken
#: for a reply.
REQUEST_ADDRESS = "/maestro/measure"
REPLY_ADDRESS = "/maestro/report"

#: Bumped whenever the reply layout changes. The client refuses a reply carrying
#: any other number instead of reading the wrong field, so an old device in
#: somebody's set reports "wrong version" rather than a plausible lie.
REPLY_SCHEMA = 3

#: The request, in order. ``window_ms`` is what the averaged measurements cover.
#: ``reset`` restarts the integrated loudness of ``loudness~``. ``reply_port`` is where
#: the answer goes: the client binds an ephemeral port and the device aims its
#: ``udpsend`` at it per request, so neither side holds a hard-coded reply port.
REQUEST_FIELDS: tuple[str, ...] = ("request_id", "window_ms", "reset", "reply_port")

#: The numeric half of the reply, in order. Every entry is one value the device
#: measures and hands over raw. Nothing here is a dB value, a correlation or a
#: verdict: those are derived in ``analyzer.metrics``.
#:
#: ``loudness~`` supplies the first six, in its own outlet order: momentary (400 ms),
#: short term (3 s), integrated since the last reset, loudness range, sample peak in
#: dB, true peak in dB with oversampling.
#:
#: ``window_samples`` is what the averaged measurements actually cover, reported back
#: rather than echoed: the device clamps the requested window to what its averaging
#: buffers hold at the current sample rate.
#:
#: The averaged entries are amplitudes, not powers, because ``average~ @mode rms``
#: reports the root of the mean square. ``mean_lr`` is the one exception: it is a
#: bipolar average of the product of the two channels, which keeps the sign a
#: correlation needs and an RMS average would drop.
NUMERIC_REPLY_FIELDS: tuple[str, ...] = (
    "schema",
    "request_id",
    "samplerate",
    "window_samples",
    "momentary_lufs",
    "short_term_lufs",
    "integrated_lufs",
    "lra_lu",
    "sample_peak_db",
    "true_peak_db",
    "rms_l",
    "rms_r",
    "mean_lr",
    "rms_low_l",
    "rms_low_r",
    "mean_low_lr",
    "band_sub",
    "band_bass",
    "band_low_mid",
    "band_mid",
    "band_high_mid",
    "band_presence",
    "band_air",
    "cal_momentary_lufs",
    "cal_sample_peak_db",
    "cal_true_peak_db",
    "track_id",
)

#: The device carries its own reference signal: a sine of this frequency at this
#: amplitude, wired into a second ``loudness~`` and never into the audio output. Its
#: peak is known exactly, so every reply reports what this build of Max makes of a
#: signal the reader already knows, at the sample rate Live is running. That is what
#: the sample peak correction is taken from, rather than from a fixed constant.
#:
#: 1 kHz is where the K-weighting curve is nearest unity and where an interpolating
#: peak meter has the least to find: at 48 kHz a 1 kHz sine is sampled 48 times per
#: cycle, so a correct true peak sits within about 0.02 dB of the sample peak.
#: ``docs/limits.md`` records what each outlet does with it.
CALIBRATION_HZ = 1000.0
CALIBRATION_AMPLITUDE = 0.5

#: The band edges the device filters at, in the order the reply carries them.
#: ``None`` as the upper edge means the band is a high pass with no top: at 44.1 kHz a
#: 20 kHz low pass sits close enough to Nyquist that a second order section is no
#: longer trustworthy there. The generator reads this table, so a band moved here
#: moves in the device as well.
BANDS: tuple[tuple[str, float, float | None], ...] = (
    ("band_sub", 20.0, 60.0),
    ("band_bass", 60.0, 250.0),
    ("band_low_mid", 250.0, 500.0),
    ("band_mid", 500.0, 2000.0),
    ("band_high_mid", 2000.0, 6000.0),
    ("band_presence", 6000.0, 10000.0),
    ("band_air", 10000.0, None),
)

#: How many samples of history every ``average~`` in the device holds. It fixes
#: the longest window the averaged measurements can cover: 96000 samples is one
#: second at 96 kHz and 2.18 seconds at 44.1 kHz. ``loudness~`` is unaffected and
#: keeps its own EBU windows, which is why the long measurements come from there.
MAX_AVERAGING_SAMPLES = 96000

#: The frequency the low band correlation is measured below. 120 Hz is where the
#: mono compatibility question is usually asked, because that is roughly where a
#: club system stops reproducing a stereo image and starts summing.
LOW_BAND_HZ = 120.0

#: The string half, always last. A track name is a symbol the user typed, it can hold
#: spaces, and an OSC serializer is free to split it there. Keeping the strings behind
#: every number lets the client rejoin a split tail instead of losing the numbers that
#: would otherwise follow it.
#:
#: The name is the only string, and the reply carries no canonical path. Measured
#: 2026-09-07 against Live 12.4.5: ``live.path`` answers both
#: ``path this_device canonical_parent`` and ``path this_device`` with an id and
#: with no path, on any of its three outlets. ``track_id`` is that id, which
#: distinguishes two tracks that share a name from each other. Turning a caller's
#: index or "master" into a name is Channel A's job, in ``server.analyze_audio``,
#: because the Remote Script can read the set and this device cannot.
STRING_REPLY_FIELDS: tuple[str, ...] = ("track_name",)


class OscError(ValueError):
    """A datagram that is not an OSC message this module can read."""


def _pad(size: int) -> int:
    """Bytes of NUL padding that bring ``size`` up to a multiple of four.

    >>> [_pad(n) for n in (0, 1, 3, 4, 5)]
    [4, 3, 1, 4, 3]
    """
    # A four-byte-aligned string still takes a terminator, so the padding is
    # never zero. OSC 1.0 requires at least one NUL after every string.
    return 4 - (size % 4)


def _encode_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return raw + b"\0" * _pad(len(raw))


def encode(address: str, args: list[int | float | str]) -> bytes:
    """Serialize one OSC message.

    The address, the type tag string and every string argument are NUL padded to
    a four byte boundary, so "/a" with one int is three four byte words.

    >>> encode("/a", [1]).hex(" ", 4)
    '2f610000 2c690000 00000001'
    """
    tags = ","
    body = b""
    for arg in args:
        # bool is an int subclass and would silently encode as 0 or 1. Nothing in
        # this protocol sends one, so refusing is cheaper than deciding.
        if isinstance(arg, bool):
            raise OscError(f"bool is not an OSC type: {arg!r}")
        if isinstance(arg, int):
            tags += "i"
            body += struct.pack(">i", arg)
        elif isinstance(arg, float):
            tags += "f"
            body += struct.pack(">f", arg)
        elif isinstance(arg, str):
            tags += "s"
            body += _encode_string(arg)
        else:
            raise OscError(f"cannot encode {type(arg).__name__} as an OSC argument")
    return _encode_string(address) + _encode_string(tags) + body


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\0", offset)
    if end < 0:
        raise OscError("an OSC string is not terminated")
    text = data[offset:end].decode("utf-8", errors="replace")
    return text, end + _pad(end - offset)


def decode(data: bytes) -> tuple[str, list[int | float | str]]:
    """Read one OSC message into its address and arguments.

    Types this module does not send are refused rather than skipped, because a
    reply holding an unexpected type is a device that does not match this schema.

    >>> decode(encode("/maestro/report", [1, 2.5, "master"]))
    ('/maestro/report', [1, 2.5, 'master'])
    """
    if len(data) < 8 or not data.startswith(b"/"):
        raise OscError("not an OSC message: no address pattern")
    address, offset = _read_string(data, 0)
    tags, offset = _read_string(data, offset)
    if not tags.startswith(","):
        raise OscError(f"not an OSC type tag string: {tags!r}")

    args: list[int | float | str] = []
    for tag in tags[1:]:
        if tag in "if":
            if offset + 4 > len(data):
                raise OscError(f"the packet ends inside a {tag!r} argument")
            chunk = data[offset : offset + 4]
            # Max rounds a float that happens to be integral, so a field declared
            # numeric arrives as either tag. Both are kept as read and normalised
            # by the caller that knows what the field means.
            args.append(struct.unpack(">i", chunk)[0] if tag == "i" else
                        round(struct.unpack(">f", chunk)[0], 6))
            offset += 4
        elif tag == "s":
            text, offset = _read_string(data, offset)
            args.append(text)
        else:
            raise OscError(f"unsupported OSC type tag {tag!r} in {tags!r}")
    return address, args
