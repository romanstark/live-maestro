"""Read container metadata out of an .aif/.aiff/.aifc/.wav file with the standard library.

A parser rather than a dependency: this repository has no numpy, soundfile or libsndfile,
and Python 3.14 removed ``aifc``. It reads IFF containers (``FORM`` plus ``AIFF``/``AIFC``,
big-endian, format in ``COMM`` and audio in ``SSND``) and RIFF containers (``RIFF`` plus
``WAVE``, little-endian, format in ``fmt `` and audio in ``data``).

:attr:`SampleHeader.audio_readable` answers for the samples, not for the file: metadata is
returned either way, and for an encrypted Ableton sample the metadata is all there is. Such
a file parses like any other, so channels, frames, bit depth and sample rate read out as
they do for a plain file while only the ``SSND`` payload is unreadable.
"""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

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

#: The ``COMM`` compressionType fourcc that marks an encrypted Ableton factory sample, the
#: form Ableton's downloadable Factory Packs ship in, compression name "Ableton Content".
#: Two chunks sit in front of ``COMM``: ``FVER``, the ordinary AIFF-C format version chunk
#: that unencrypted AIFF-C files carry too, and ``able``, 340 bytes of presumed key
#: material. A container holding several samples carries an ``abli`` index chunk as well.
ABLETON_ENCRYPTED_COMPRESSION = "able"

#: Verdict codes whose audio payload is plain uncompressed samples.
READABLE_CODES = frozenset({"pcm", "float"})

#: AIFF-C compressionType fourccs that are uncompressed integer samples under another name.
#: ``NONE``/``twos`` are big-endian, ``sowt`` is the same data byte-swapped, ``in24``/``in32``
#: are Apple's big-endian 24- and 32-bit forms.
_AIFC_PCM = frozenset({"NONE", "none", "twos", "sowt", "in24", "in32", "23ni", "42ni"})

#: AIFF-C compressionType fourccs that are uncompressed IEEE floating point samples.
_AIFC_FLOAT = frozenset({"fl32", "FL32", "fl64", "FL64"})

#: WAVE format tags this parser recognises. 0xFFFE is WAVE_FORMAT_EXTENSIBLE, whose real
#: format lives in the first four bytes of the SubFormat GUID in the fmt chunk's extension.
_WAVE_PCM = 0x0001
_WAVE_FLOAT = 0x0003
_WAVE_EXTENSIBLE = 0xFFFE

_WAVE_NAMES = {
    0x0001: "pcm",
    0x0003: "float",
    0x0006: "alaw",
    0x0007: "mulaw",
    0x0011: "ima-adpcm",
    0x0055: "mpeglayer3",
    0xFFFE: "extensible",
}

#: How many bytes are read before deciding a file is not a container this module knows.
_MAGIC_BYTES = 12


class SampleHeaderError(ValueError):
    """Raised when a file is not a container this module can read a header out of.

    ``code`` is the machine-readable reason, ``error`` is the sentence for a human.
    """

    def __init__(self, code: str, error: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error


@dataclass(frozen=True)
class Chunk:
    """One chunk of an IFF or RIFF file, as its own header declares itself.

    ``size`` is the declared payload size, which is not always the size that is there:
    compare it against ``offset`` and the file length to detect truncation.
    """

    chunk_id: str
    size: int
    offset: int


@dataclass(frozen=True)
class SampleHeader:
    """Everything about a sample file that can be known without decoding its audio.

    ``code`` and ``reason`` carry the verdict: ``code`` is for a caller to branch on,
    ``reason`` is the sentence to show a person. ``audio_readable`` is derived from
    ``code`` and restates it as the single question most callers actually ask.
    """

    path: str
    container: str
    compression: str
    compression_name: str
    channels: int
    frames: int
    bit_depth: int
    sample_rate: float
    duration_s: float
    payload_bytes: int
    audio_readable: bool
    code: str
    reason: str
    chunks: tuple[Chunk, ...]

    @property
    def expected_pcm_bytes(self) -> int:
        """Bytes this many frames would occupy as uncompressed samples.

        ``frames * channels * ceil(bit_depth / 8)``. Equal to :attr:`payload_bytes` exactly
        when the stored audio is the same size as plain samples, which is true of
        uncompressed files and of Ableton's encrypted ones, whose transform preserves size,
        and false of anything genuinely compressed.

        >>> SampleHeader(
        ...     path="x.aif", container="AIFC", compression="able",
        ...     compression_name="Ableton Content", channels=2, frames=255410,
        ...     bit_depth=16, sample_rate=44100.0, duration_s=5.79,
        ...     payload_bytes=1021640, audio_readable=False, code="encrypted",
        ...     reason="", chunks=(),
        ... ).expected_pcm_bytes
        1021640
        """
        return self.frames * self.channels * ((self.bit_depth + 7) // 8)

    @property
    def is_multisample_container(self) -> bool:
        """Whether the file carries an ``abli`` index chunk for multiple embedded zones.

        Ableton's monolithic multi-sample containers concatenate several sample zones into
        one ``SSND`` payload and index their byte boundaries in an ``abli`` chunk.
        """
        return any(chunk.chunk_id == "abli" for chunk in self.chunks)


def read_chunks(path: str | Path) -> tuple[str, tuple[Chunk, ...]]:
    """Return the container form type and every chunk header in file order.

    The form type is ``"AIFF"``, ``"AIFC"`` or ``"WAVE"``. Walking stops at the end of the
    file, so a chunk that declares more bytes than the file holds is the last one returned
    and its ``size`` exceeds what is there.

    Raises :class:`SampleHeaderError` when the first twelve bytes are not a container this
    module knows.
    """
    file_path = Path(path)
    try:
        total = file_path.stat().st_size
    except OSError as exc:
        raise SampleHeaderError(
            "unreadable_file", f"{file_path} could not be opened: {exc.strerror}."
        ) from exc
    with file_path.open("rb") as handle:
        magic = handle.read(_MAGIC_BYTES)
        if len(magic) < _MAGIC_BYTES:
            raise SampleHeaderError(
                "too_short",
                f"{file_path} is {len(magic)} bytes long and holds no container header.",
            )
        head, form_type = magic[0:4], magic[8:12].decode("latin-1")
        if head == b"FORM":
            big_endian = True
            if form_type not in ("AIFF", "AIFC"):
                raise SampleHeaderError(
                    "unsupported_form",
                    f"{file_path} is an IFF FORM of type {form_type!r}, not AIFF or AIFC.",
                )
        elif head == b"RIFF":
            big_endian = False
            if form_type != "WAVE":
                raise SampleHeaderError(
                    "unsupported_form",
                    f"{file_path} is a RIFF file of type {form_type!r}, not WAVE.",
                )
        else:
            raise SampleHeaderError(
                "unknown_container",
                f"{file_path} starts with {head!r}, which is neither FORM nor RIFF.",
            )
        return form_type, tuple(_walk(handle, total, big_endian))


def read_sample_header(path: str | Path) -> SampleHeader:
    """Read container metadata and a readability verdict for one audio file.

    Raises :class:`SampleHeaderError` when the file is not an AIFF, AIFF-C or WAVE
    container, or when the chunk that carries the metadata is missing.
    """
    file_path = Path(path)
    total = file_path.stat().st_size
    form_type, chunks = read_chunks(file_path)
    with file_path.open("rb") as handle:
        if form_type in ("AIFF", "AIFC"):
            return _aiff_header(handle, file_path, total, form_type, chunks)
        return _wave_header(handle, file_path, total, chunks)


def _walk(handle: BinaryIO, total: int, big_endian: bool) -> list[Chunk]:
    """Return the chunk headers of an already-identified container.

    ``handle`` is a binary file object positioned anywhere: this seeks for every chunk.
    """
    layout = ">I" if big_endian else "<I"
    out: list[Chunk] = []
    offset = _MAGIC_BYTES
    while offset + 8 <= total:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            break
        chunk_id = header[0:4].decode("latin-1")
        size = struct.unpack(layout, header[4:8])[0]
        out.append(Chunk(chunk_id=chunk_id, size=size, offset=offset))
        offset += 8 + size + (size & 1)
    return out


def _find(chunks: tuple[Chunk, ...], chunk_id: str) -> Chunk | None:
    """Return the first chunk with this id, or None."""
    for chunk in chunks:
        if chunk.chunk_id == chunk_id:
            return chunk
    return None


def _extended80(raw: bytes) -> float:
    """Decode an IEEE 754 80-bit extended float, which is how AIFF stores its sample rate.

    >>> _extended80(bytes.fromhex("400EAC44000000000000"))
    44100.0
    >>> _extended80(bytes.fromhex("400EBB80000000000000"))
    48000.0
    >>> _extended80(bytes(10))
    0.0
    """
    exponent = struct.unpack(">H", raw[0:2])[0]
    mantissa = struct.unpack(">Q", raw[2:10])[0]
    if exponent == 0 and mantissa == 0:
        return 0.0
    sign = -1.0 if exponent & 0x8000 else 1.0
    return sign * mantissa * 2.0 ** ((exponent & 0x7FFF) - 16383 - 63)


def _aiff_header(
    handle: BinaryIO,
    file_path: Path,
    total: int,
    form_type: str,
    chunks: tuple[Chunk, ...],
) -> SampleHeader:
    """Build a header from an AIFF or AIFF-C COMM chunk and its SSND payload size."""
    comm = _find(chunks, "COMM")
    if comm is None:
        raise SampleHeaderError(
            "no_comm_chunk",
            f"{file_path} is a {form_type} file with no COMM chunk, so it declares no format.",
        )
    handle.seek(comm.offset + 8)
    body = handle.read(comm.size)
    if len(body) < 18:
        raise SampleHeaderError(
            "short_comm_chunk",
            f"{file_path} has a COMM chunk of {len(body)} bytes, too short to hold a format.",
        )
    channels, frames, bit_depth = struct.unpack(">hIh", body[0:8])
    sample_rate = _extended80(body[8:18])
    compression, compression_name = _aifc_compression(body, comm.size, form_type)

    ssnd = _find(chunks, "SSND")
    payload_bytes = 0
    truncated = False
    if ssnd is not None:
        handle.seek(ssnd.offset + 8)
        head = handle.read(8)
        data_offset = struct.unpack(">I", head[0:4])[0] if len(head) >= 8 else 0
        payload_bytes = max(ssnd.size - 8 - data_offset, 0)
        truncated = ssnd.offset + 8 + ssnd.size > total

    code, reason = _aiff_verdict(
        file_path, form_type, compression, compression_name, ssnd, truncated
    )
    return SampleHeader(
        path=str(file_path),
        container=form_type,
        compression=compression,
        compression_name=compression_name,
        channels=channels,
        frames=frames,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        duration_s=frames / sample_rate if sample_rate else 0.0,
        payload_bytes=payload_bytes,
        audio_readable=code in READABLE_CODES,
        code=code,
        reason=reason,
        chunks=chunks,
    )


def _aifc_compression(body: bytes, size: int, form_type: str) -> tuple[str, str]:
    """Return the compressionType fourcc and its human name from a COMM body.

    A plain AIFF COMM carries neither, and is uncompressed by definition.
    """
    if form_type == "AIFF" or size < 22:
        return "NONE", ""
    compression = body[18:22].decode("latin-1")
    name = ""
    if size > 22:
        length = body[22]
        name = body[23 : 23 + length].decode("latin-1")
    return compression, name


def _aiff_verdict(
    file_path: Path,
    form_type: str,
    compression: str,
    compression_name: str,
    ssnd: Chunk | None,
    truncated: bool,
) -> tuple[str, str]:
    """Decide whether an AIFF-family audio payload can be decoded, and say why."""
    if ssnd is None:
        return (
            "no_ssnd_chunk",
            f"{file_path} is a {form_type} file with no SSND chunk, so it carries no audio.",
        )
    if truncated:
        return (
            "truncated",
            f"{file_path} declares more SSND bytes than the file holds and is incomplete.",
        )
    if compression == ABLETON_ENCRYPTED_COMPRESSION:
        label = compression_name or "Ableton Content"
        return (
            "encrypted",
            (
                f"{file_path} is an Ableton encrypted sample (compression {compression!r}, "
                f"{label!r}). Its metadata is readable and its audio is not: the payload is "
                f"the same size as plain samples, so no decoder can exist without the key."
            ),
        )
    if compression in _AIFC_PCM:
        return "pcm", f"{file_path} holds uncompressed samples (compression {compression!r})."
    if compression in _AIFC_FLOAT:
        return (
            "float",
            (
                f"{file_path} holds uncompressed floating point samples "
                f"(compression {compression!r})."
            ),
        )
    label = compression_name or "unknown"
    return (
        "codec",
        (
            f"{file_path} uses the AIFF-C codec {compression!r} ({label}), which this "
            f"reader cannot decode."
        ),
    )


def _wave_header(
    handle: BinaryIO,
    file_path: Path,
    total: int,
    chunks: tuple[Chunk, ...],
) -> SampleHeader:
    """Build a header from a WAVE fmt chunk and the size of its data chunk."""
    fmt = _find(chunks, "fmt ")
    if fmt is None:
        raise SampleHeaderError(
            "no_fmt_chunk",
            f"{file_path} is a WAVE file with no 'fmt ' chunk, so it declares no format.",
        )
    handle.seek(fmt.offset + 8)
    body = handle.read(fmt.size)
    if len(body) < 16:
        raise SampleHeaderError(
            "short_fmt_chunk",
            f"{file_path} has a 'fmt ' chunk of {len(body)} bytes, too short to hold a format.",
        )
    tag, channels, rate, _byte_rate, block_align, bit_depth = struct.unpack("<HHIIHH", body[0:16])
    compression, compression_name = _wave_compression(tag, body)

    data = _find(chunks, "data")
    payload_bytes = 0
    frames = 0
    truncated = False
    if data is not None:
        payload_bytes = data.size
        truncated = data.offset + 8 + data.size > total
        stride = block_align or channels * ((bit_depth + 7) // 8)
        frames = payload_bytes // stride if stride else 0

    code, reason = _wave_verdict(file_path, compression, compression_name, data, truncated)
    return SampleHeader(
        path=str(file_path),
        container="WAVE",
        compression=compression,
        compression_name=compression_name,
        channels=channels,
        frames=frames,
        bit_depth=bit_depth,
        sample_rate=float(rate),
        duration_s=frames / rate if rate else 0.0,
        payload_bytes=payload_bytes,
        audio_readable=code in READABLE_CODES,
        code=code,
        reason=reason,
        chunks=chunks,
    )


def _wave_compression(tag: int, body: bytes) -> tuple[str, str]:
    """Return a codec token and human name for a WAVE format tag.

    WAVE_FORMAT_EXTENSIBLE hides the real format in the first two bytes of the SubFormat
    GUID, which sits 24 bytes into the fmt body.
    """
    if tag == _WAVE_EXTENSIBLE and len(body) >= 26:
        sub_tag = struct.unpack("<H", body[24:26])[0]
        name = _WAVE_NAMES.get(sub_tag, f"0x{sub_tag:04x}")
        return name, f"WAVE_FORMAT_EXTENSIBLE, SubFormat {name}"
    return _WAVE_NAMES.get(tag, f"0x{tag:04x}"), ""


def _wave_verdict(
    file_path: Path,
    compression: str,
    compression_name: str,
    data: Chunk | None,
    truncated: bool,
) -> tuple[str, str]:
    """Decide whether a WAVE audio payload can be decoded, and say why."""
    if data is None:
        return (
            "no_data_chunk",
            f"{file_path} is a WAVE file with no 'data' chunk, so it carries no audio.",
        )
    if truncated:
        return (
            "truncated",
            f"{file_path} declares more 'data' bytes than the file holds and is incomplete.",
        )
    if compression == _WAVE_NAMES[_WAVE_PCM]:
        return "pcm", f"{file_path} holds uncompressed samples ({compression_name or 'WAVE PCM'})."
    if compression == _WAVE_NAMES[_WAVE_FLOAT]:
        return (
            "float",
            (
                f"{file_path} holds uncompressed floating point samples "
                f"({compression_name or 'WAVE IEEE float'})."
            ),
        )
    return (
        "codec",
        f"{file_path} uses the WAVE codec {compression!r}, which this reader cannot decode.",
    )


def convert_to_pcm_wav(source: str | Path, target: str | Path) -> SampleHeader:
    """Convert any readable AIFF, AIFF-C or WAVE file to a 16-bit or 24-bit PCM WAVE file.

    Uses only the standard library (``struct`` and ``wave``). Input files at 8-bit or
    16-bit write out as 16-bit ``WAVE_FORMAT_PCM``; input files at 24-bit, 32-bit integer
    or 32/64-bit IEEE float write out as 24-bit ``WAVE_FORMAT_PCM`` with the original
    channel count and sample rate preserved.

    Args:
        source: Path to the readable input ``.aif``, ``.aiff``, ``.aifc`` or ``.wav`` file.
        target: Destination path for the standard ``WAVE_FORMAT_PCM`` ``.wav`` file.

    Returns:
        The :class:`SampleHeader` of the newly written ``target`` file.

    Raises:
        SampleHeaderError: When the source file is not readable (for example, encrypted
            with ``able``, truncated, or an unsupported codec).
    """
    source_path = Path(source)
    target_path = Path(target)
    header = read_sample_header(source_path)
    if not header.audio_readable:
        raise SampleHeaderError(header.code, header.reason)
    raw = _read_raw_payload(source_path, header)
    target_width, pcm_bytes = _to_le_pcm(raw, header)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target_path), "wb") as wav_file:
        wav_file.setnchannels(header.channels)
        wav_file.setsampwidth(target_width)
        wav_file.setframerate(round(header.sample_rate))
        wav_file.writeframes(pcm_bytes)
    return read_sample_header(target_path)


def _read_raw_payload(file_path: Path, header: SampleHeader) -> bytes:
    """Read the raw uncompressed audio bytes described by ``header``."""
    with file_path.open("rb") as handle:
        if header.container in ("AIFF", "AIFC"):
            ssnd = _find(header.chunks, "SSND")
            if ssnd is None:
                raise SampleHeaderError("no_ssnd_chunk", header.reason)
            handle.seek(ssnd.offset + 8)
            head = handle.read(8)
            data_offset = struct.unpack(">I", head[0:4])[0] if len(head) >= 8 else 0
            handle.seek(ssnd.offset + 16 + data_offset)
            return handle.read(header.expected_pcm_bytes)
        data = _find(header.chunks, "data")
        if data is None:
            raise SampleHeaderError("no_data_chunk", header.reason)
        handle.seek(data.offset + 8)
        return handle.read(header.expected_pcm_bytes)


def _to_le_pcm(raw: bytes, header: SampleHeader) -> tuple[int, bytes]:
    """Convert raw audio payload bytes into little-endian 16-bit or 24-bit signed PCM."""
    little_endian = header.container == "WAVE" or header.compression in ("sowt", "23ni", "42ni")
    total_samples = header.frames * header.channels
    if header.code == "pcm":
        if header.bit_depth == 16:
            trimmed = raw[: total_samples * 2]
            if little_endian:
                return 2, trimmed
            out = bytearray(len(trimmed))
            out[0::2] = trimmed[1::2]
            out[1::2] = trimmed[0::2]
            return 2, bytes(out)
        if header.bit_depth == 24:
            trimmed = raw[: total_samples * 3]
            if little_endian:
                return 3, trimmed
            out = bytearray(len(trimmed))
            out[0::3] = trimmed[2::3]
            out[1::3] = trimmed[1::3]
            out[2::3] = trimmed[0::3]
            return 3, bytes(out)
        if header.bit_depth == 32:
            trimmed = raw[: total_samples * 4]
            out = bytearray(total_samples * 3)
            if little_endian:
                out[0::3] = trimmed[1::4]
                out[1::3] = trimmed[2::4]
                out[2::3] = trimmed[3::4]
            else:
                out[0::3] = trimmed[2::4]
                out[1::3] = trimmed[1::4]
                out[2::3] = trimmed[0::4]
            return 3, bytes(out)
        if header.bit_depth == 8:
            trimmed = raw[:total_samples]
            out = bytearray(total_samples * 2)
            for idx, byte in enumerate(trimmed):
                signed = (byte - 128) if header.container == "WAVE" else ((byte + 128) % 256 - 128)
                struct.pack_into("<h", out, idx * 2, signed << 8)
            return 2, bytes(out)
    if header.code == "float" and header.bit_depth in (32, 64):
        prefix = "<" if header.container == "WAVE" else ">"
        fmt = f"{prefix}{'f' if header.bit_depth == 32 else 'd'}"
        step = header.bit_depth // 8
        trimmed = raw[: total_samples * step]
        out = bytearray(total_samples * 3)
        scale = float((1 << 23) - 1)
        for idx, (val,) in enumerate(struct.iter_unpack(fmt, trimmed)):
            clean = 0.0 if not math.isfinite(val) else max(-1.0, min(1.0, val))
            sample_int = round(clean * scale)
            out[idx * 3 : idx * 3 + 3] = sample_int.to_bytes(3, "little", signed=True)
        return 3, bytes(out)
    raise SampleHeaderError(
        "unsupported_depth",
        (
            f"{header.path} has bit depth {header.bit_depth} ({header.code}), "
            "which cannot be converted."
        ),
    )
