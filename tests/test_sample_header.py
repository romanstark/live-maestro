"""Tests for the standard-library sample header reader.

The first kind of test opens files that ship with Ableton Live 12 Suite on this
machine, by absolute path, and asserts the numbers read out of their bytes. Those
expected values were taken from the files on 2026-09-17 with a throwaway dumper
written separately from the module under test, so the two had to agree
independently. The second kind builds byte fixtures for shapes that occur nowhere in
the two local libraries: a truncated payload, a missing COMM chunk, an AIFF-C codec
that is neither PCM nor float, a WAVE codec this reader declines, and containers that
are not audio at all. Each fixture is assembled from the AIFF-C and RIFF/WAVE
specifications, not from the parser's internals.

The load-bearing claim is that Ableton's encrypted factory samples are ordinary AIFF-C
containers whose COMM and SSND chunks both parse and whose SSND payload is exactly the
size plain uncompressed samples occupy: metadata readable, audio permanently not.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from live_maestro.audio import (
    ABLETON_ENCRYPTED_COMPRESSION,
    Chunk,
    SampleHeader,
    SampleHeaderError,
    convert_to_pcm_wav,
    read_chunks,
    read_sample_header,
)

# --------------------------------------------------------------------------- #
# Real files on this machine, read off disk on 2026-09-17 from the Ableton Live 12 Suite
# installation and its downloaded Factory Packs. No Live version was queried for these
# numbers: they come out of the files' own bytes, and a Live upgrade does not move them.
# --------------------------------------------------------------------------- #

FACTORY_PACKS = Path.home() / "Music/Ableton/Factory Packs"
CORE_LIBRARY = Path(
    "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library"
)

#: Encrypted factory samples. The four differ in channel count, bit depth, sample rate
#: and length, and two of them carry the extra ``abli`` index chunk that multi-sample
#: containers use.
ENCRYPTED_FILES = {
    "larynx_pad": FACTORY_PACKS / "Synth Essentials/Samples/Larynx Pad.aif",
    "bass_check": FACTORY_PACKS / "Drum Essentials/Samples/Other/Bass/Bass C# Check.aif",
    "shaker": FACTORY_PACKS / "Latin Percussion/Samples/Combined/Shake-Shaker 1.aif",
    "grand_piano": FACTORY_PACKS / "Grand Piano/Samples/Combined/Normal.aif",
}

#: Unencrypted files, one per container shape that exists in these two libraries.
PLAIN_FILES = {
    "aiff_pcm": CORE_LIBRARY / "Samples/Multisamples/Grand Piano/GrandPiano A# 1 f.aif",
    "aifc_sowt": CORE_LIBRARY / "Samples/Multisamples/Synths/Dark Thought/Dark Thought - C0.aif",
    "aifc_fl32": CORE_LIBRARY / "Samples/One Shots/Drums/Wood/Clave Synth.aif",
    "wave_pcm": CORE_LIBRARY / "Samples/Multisamples/Mallet Guitar/Mallet Guitar Bb0.wav",
    "wave_extensible": CORE_LIBRARY / "Samples/One Shots/Instrument/Strings/Strings Ens C4.wav",
    "wave_float": (
        FACTORY_PACKS / "Convolution Reverb/IRs/11 Experimental/ABLCR Sweep 4 Med.wav"
    ),
}


def local(path: Path) -> Path:
    """Return the path, or skip the test when this machine does not have the file."""
    if not path.is_file():
        pytest.skip(f"not installed on this machine: {path}")
    return path


# --------------------------------------------------------------------------- #
# The encrypted factory files are ordinary AIFF-C containers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(ENCRYPTED_FILES))
def test_an_encrypted_factory_file_has_both_a_comm_and_an_ssnd_chunk(name: str) -> None:
    """COMM and SSND are present, not replaced by FVER/able/abli.

    FVER and able sit in front of COMM, and abli joins them in multi-sample containers,
    but the two chunks that carry the format and the audio are exactly where the AIFF-C
    specification puts them.
    """
    form_type, chunks = read_chunks(local(ENCRYPTED_FILES[name]))
    ids = [chunk.chunk_id for chunk in chunks]
    assert form_type == "AIFC"
    assert "COMM" in ids
    assert "SSND" in ids
    assert ids.index("COMM") < ids.index("SSND")
    assert ids[0] == "FVER"
    assert ids[1] == "able"
    assert ids.index("able") < ids.index("COMM")


@pytest.mark.parametrize("name", sorted(ENCRYPTED_FILES))
def test_an_encrypted_factory_file_names_able_inside_comm(name: str) -> None:
    """``able`` is a compressionType fourcc inside COMM, and it is not the whole story.

    The compression name Ableton writes beside it is the human half of the same field.
    """
    header = read_sample_header(local(ENCRYPTED_FILES[name]))
    assert header.container == "AIFC"
    assert header.compression == ABLETON_ENCRYPTED_COMPRESSION
    assert header.compression_name == "Ableton Content"


@pytest.mark.parametrize("name", sorted(ENCRYPTED_FILES))
def test_an_encrypted_factory_payload_is_exactly_the_size_of_plain_samples(name: str) -> None:
    """A same-size transform is encryption, not compression, so no decoder can exist.

    Checked on all four files, which differ in channel count, bit depth, sample rate and
    by three orders of magnitude in length, so that one coincidence cannot carry it.
    """
    header = read_sample_header(local(ENCRYPTED_FILES[name]))
    assert header.payload_bytes == header.expected_pcm_bytes
    assert header.payload_bytes > 0


@pytest.mark.parametrize("name", sorted(ENCRYPTED_FILES))
def test_an_encrypted_factory_file_yields_metadata_but_refuses_its_audio(name: str) -> None:
    """The verdict is the point of the module: metadata yes, audio no."""
    header = read_sample_header(local(ENCRYPTED_FILES[name]))
    assert header.audio_readable is False
    assert header.code == "encrypted"
    assert "able" in header.reason
    assert header.channels > 0
    assert header.frames > 0
    assert header.bit_depth in (16, 24)
    assert header.sample_rate in (44100.0, 48000.0)
    assert header.duration_s > 0.0


def test_the_encrypted_metadata_reads_out_to_the_values_measured_on_2026_09_17() -> None:
    """Exact numbers for two encrypted files, so a silent regression cannot hide.

    Larynx Pad is stereo 16-bit 44.1 kHz; Shake-Shaker 1 is stereo 24-bit 48 kHz and is one
    of the containers that carries an abli index chunk.
    """
    larynx = read_sample_header(local(ENCRYPTED_FILES["larynx_pad"]))
    assert (larynx.channels, larynx.frames, larynx.bit_depth) == (2, 255410, 16)
    assert larynx.sample_rate == 44100.0
    assert larynx.payload_bytes == 1021640
    assert round(larynx.duration_s, 3) == 5.792

    shaker = read_sample_header(local(ENCRYPTED_FILES["shaker"]))
    assert (shaker.channels, shaker.frames, shaker.bit_depth) == (2, 332480, 24)
    assert shaker.sample_rate == 48000.0
    assert shaker.payload_bytes == 1994880
    assert "abli" in [chunk.chunk_id for chunk in shaker.chunks]


def test_fver_alone_does_not_mean_encrypted() -> None:
    """An unencrypted AIFF-C file carries FVER too, so FVER is not the marker.

    Clave Synth is fl32 and perfectly readable, and its first chunk is FVER. Treating FVER
    as the signal would wrongly reject it.
    """
    header = read_sample_header(local(PLAIN_FILES["aifc_fl32"]))
    assert header.chunks[0].chunk_id == "FVER"
    assert header.compression == "fl32"
    assert header.audio_readable is True


# --------------------------------------------------------------------------- #
# The unencrypted shapes that exist in these libraries
# --------------------------------------------------------------------------- #


def test_a_plain_aiff_file_reads_as_readable_pcm() -> None:
    """A big-endian AIFF with no compressionType field at all."""
    header = read_sample_header(local(PLAIN_FILES["aiff_pcm"]))
    assert header.container == "AIFF"
    assert header.compression == "NONE"
    assert (header.channels, header.frames, header.bit_depth) == (2, 496628, 16)
    assert header.sample_rate == 44100.0
    assert round(header.duration_s, 3) == 11.261
    assert header.payload_bytes == header.expected_pcm_bytes == 1986512
    assert header.code == "pcm"
    assert header.audio_readable is True


def test_a_byte_swapped_aifc_file_reads_as_readable_pcm() -> None:
    """``sowt`` is little-endian PCM, which is uncompressed under another fourcc."""
    header = read_sample_header(local(PLAIN_FILES["aifc_sowt"]))
    assert header.container == "AIFC"
    assert header.compression == "sowt"
    assert (header.channels, header.frames, header.bit_depth) == (2, 35072, 16)
    assert header.payload_bytes == header.expected_pcm_bytes == 140288
    assert header.audio_readable is True
    assert header.code == "pcm"


def test_a_float_aifc_file_reads_as_readable_float() -> None:
    """``fl32`` is 32-bit IEEE float, uncompressed, and gets its own verdict code."""
    header = read_sample_header(local(PLAIN_FILES["aifc_fl32"]))
    assert (header.channels, header.frames, header.bit_depth) == (1, 2501, 32)
    assert header.sample_rate == 48000.0
    assert header.compression_name == "32-bit Floating Point"
    assert header.payload_bytes == header.expected_pcm_bytes == 10004
    assert header.code == "float"
    assert header.audio_readable is True


def test_a_wave_file_reads_its_frame_count_from_the_data_chunk() -> None:
    """WAVE declares no frame count, so it has to come out of data size and block align."""
    header = read_sample_header(local(PLAIN_FILES["wave_pcm"]))
    assert header.container == "WAVE"
    assert header.compression == "pcm"
    assert (header.channels, header.bit_depth) == (1, 24)
    assert header.payload_bytes == 877542
    assert header.frames == 877542 // 3 == 292514
    assert round(header.duration_s, 3) == 6.633
    assert header.audio_readable is True


def test_an_extensible_wave_file_resolves_its_subformat_guid() -> None:
    """WAVE_FORMAT_EXTENSIBLE hides PCM behind format tag 0xFFFE.

    Reading the tag alone would report an unknown codec for the 100 such files in Live's
    Core Library. The real format is the first field of the SubFormat GUID.
    """
    header = read_sample_header(local(PLAIN_FILES["wave_extensible"]))
    assert header.compression == "pcm"
    assert "EXTENSIBLE" in header.compression_name
    assert (header.channels, header.bit_depth) == (2, 24)
    assert header.frames == 351388
    assert header.audio_readable is True


def test_a_float_wave_file_is_readable_and_survives_a_junk_chunk() -> None:
    """Format tag 3 is IEEE float, and the fmt chunk is not the first chunk in this file."""
    header = read_sample_header(local(PLAIN_FILES["wave_float"]))
    assert header.chunks[0].chunk_id == "JUNK"
    assert header.compression == "float"
    assert (header.channels, header.bit_depth) == (2, 32)
    assert header.frames == 114687
    assert header.code == "float"
    assert header.audio_readable is True


def test_every_local_sample_reports_a_verdict_that_matches_its_readable_flag() -> None:
    """``audio_readable`` never disagrees with ``code`` on a real file."""
    readable = {"pcm", "float"}
    for path in [*ENCRYPTED_FILES.values(), *PLAIN_FILES.values()]:
        if not path.is_file():
            continue
        header = read_sample_header(path)
        assert header.audio_readable == (header.code in readable), header.path
        assert header.reason.startswith(str(path)), header.path


# --------------------------------------------------------------------------- #
# Byte fixtures for shapes that do not exist in either local library
# --------------------------------------------------------------------------- #


def extended80(value: float) -> bytes:
    """Encode a sample rate as the IEEE 754 80-bit extended float AIFF stores.

    Written from the format description rather than from the parser, so that the fixture
    and the module have to agree by construction and not by copying.
    """
    exponent = 16383 + 63
    mantissa = int(value)
    while mantissa and mantissa < (1 << 63):
        mantissa <<= 1
        exponent -= 1
    return struct.pack(">HQ", exponent, mantissa)


def iff_chunk(chunk_id: bytes, body: bytes) -> bytes:
    """One big-endian IFF chunk, padded to an even length as the specification requires."""
    return chunk_id + struct.pack(">I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")


def riff_chunk(chunk_id: bytes, body: bytes) -> bytes:
    """One little-endian RIFF chunk, padded to an even length."""
    return chunk_id + struct.pack("<I", len(body)) + body + (b"\x00" if len(body) & 1 else b"")


def comm_body(
    channels: int,
    frames: int,
    bit_depth: int,
    rate: float,
    compression: bytes | None = None,
    name: bytes = b"",
) -> bytes:
    """An AIFF or AIFF-C COMM payload."""
    body = struct.pack(">hIh", channels, frames, bit_depth) + extended80(rate)
    if compression is not None:
        body += compression + bytes([len(name)]) + name
        if len(body) & 1:
            body += b"\x00"
    return body


def aiff_file(form_type: bytes, chunks: bytes) -> bytes:
    """Wrap chunk bytes in a FORM header whose declared size matches what follows."""
    return b"FORM" + struct.pack(">I", 4 + len(chunks)) + form_type + chunks


def wave_file(chunks: bytes) -> bytes:
    """Wrap chunk bytes in a RIFF/WAVE header whose declared size matches what follows."""
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def fmt_body(tag: int, channels: int, rate: int, bit_depth: int, extension: bytes = b"") -> bytes:
    """A WAVE fmt payload with a correct byte rate and block align."""
    align = channels * ((bit_depth + 7) // 8)
    return struct.pack(
        "<HHIIHH", tag, channels, rate, rate * align, align, bit_depth
    ) + extension


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write fixture bytes to a file and return its path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_the_fixture_sample_rate_encoder_matches_the_bytes_of_a_real_file() -> None:
    """The 80-bit encoder the fixtures below rely on writes the bytes a real file has.

    Anchored against the ten COMM bytes of a real AIFF rather than against the module under
    test, so that a shared mistake in both would still show up here.
    """
    path = local(PLAIN_FILES["aiff_pcm"])
    with path.open("rb") as handle:
        handle.seek(12 + 8 + 8)
        assert handle.read(10) == extended80(44100.0)


def test_a_synthetic_encrypted_file_is_read_the_same_way_as_a_real_one(tmp_path: Path) -> None:
    """The factory layout, rebuilt from the specification, reads out identically.

    This is the one fixture with a real counterpart above, so the two can be compared: if
    the module ever started keying off the FVER or able chunks rather than off COMM, they
    would part company.
    """
    body = b"\x00" * 340
    chunks = (
        iff_chunk(b"FVER", struct.pack(">I", 0xA2805140))
        + iff_chunk(b"able", body)
        + iff_chunk(b"COMM", comm_body(2, 1000, 16, 44100.0, b"able", b"Ableton Content"))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * (1000 * 2 * 2))
    )
    header = read_sample_header(write(tmp_path, "fake.aif", aiff_file(b"AIFC", chunks)))
    assert header.code == "encrypted"
    assert header.audio_readable is False
    assert header.channels == 2
    assert header.frames == 1000
    assert header.sample_rate == 44100.0
    assert header.payload_bytes == header.expected_pcm_bytes == 4000


def test_an_unknown_aifc_codec_is_declined_rather_than_guessed(tmp_path: Path) -> None:
    """No file in either local library uses a real AIFF-C codec, so this shape is synthetic.

    ``ima4`` is 4:1 ADPCM: genuinely compressed, and its payload is nothing like the plain
    sample size. The reader must refuse it without calling it encrypted.
    """
    chunks = iff_chunk(
        b"COMM", comm_body(1, 4096, 16, 44100.0, b"ima4", b"IMA 4:1")
    ) + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 2048)
    header = read_sample_header(write(tmp_path, "ima.aif", aiff_file(b"AIFC", chunks)))
    assert header.code == "codec"
    assert header.audio_readable is False
    assert header.compression == "ima4"
    assert header.payload_bytes != header.expected_pcm_bytes


def test_an_unknown_wave_codec_is_declined_rather_than_guessed(tmp_path: Path) -> None:
    """Format tag 0x0011 is IMA ADPCM. Not present locally, and not decodable here."""
    chunks = riff_chunk(b"fmt ", fmt_body(0x0011, 1, 22050, 4)) + riff_chunk(
        b"data", b"\x00" * 1024
    )
    header = read_sample_header(write(tmp_path, "adpcm.wav", wave_file(chunks)))
    assert header.code == "codec"
    assert header.audio_readable is False
    assert header.compression == "ima-adpcm"


def test_a_truncated_aiff_payload_is_reported_as_incomplete(tmp_path: Path) -> None:
    """SSND declares more bytes than the file holds, which no local file does.

    A parser that trusted the declared size would hand a caller a duration for audio that
    is not there.
    """
    good = iff_chunk(b"COMM", comm_body(1, 1000, 16, 44100.0)) + iff_chunk(
        b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 2000
    )
    data = aiff_file(b"AIFF", good)[:-500]
    header = read_sample_header(write(tmp_path, "cut.aif", data))
    assert header.code == "truncated"
    assert header.audio_readable is False
    assert header.frames == 1000


def test_a_truncated_wave_payload_is_reported_as_incomplete(tmp_path: Path) -> None:
    """The same shape on the RIFF side, where the frame count is derived from the payload."""
    chunks = riff_chunk(b"fmt ", fmt_body(1, 2, 44100, 16)) + riff_chunk(
        b"data", b"\x00" * 4000
    )
    data = wave_file(chunks)[:-1000]
    header = read_sample_header(write(tmp_path, "cut.wav", data))
    assert header.code == "truncated"
    assert header.audio_readable is False


def test_an_aiff_without_an_ssnd_chunk_says_it_carries_no_audio(tmp_path: Path) -> None:
    """Metadata alone is still returned, because a caller may want the format anyway."""
    chunks = iff_chunk(b"COMM", comm_body(2, 500, 24, 48000.0))
    header = read_sample_header(write(tmp_path, "meta.aif", aiff_file(b"AIFF", chunks)))
    assert header.code == "no_ssnd_chunk"
    assert header.audio_readable is False
    assert header.channels == 2
    assert header.sample_rate == 48000.0
    assert header.payload_bytes == 0


def test_a_wave_without_a_data_chunk_says_it_carries_no_audio(tmp_path: Path) -> None:
    """The RIFF counterpart, where a missing payload also zeroes the frame count."""
    chunks = riff_chunk(b"fmt ", fmt_body(1, 1, 44100, 16))
    header = read_sample_header(write(tmp_path, "meta.wav", wave_file(chunks)))
    assert header.code == "no_data_chunk"
    assert header.frames == 0
    assert header.duration_s == 0.0


def test_an_aiff_without_a_comm_chunk_raises_rather_than_inventing_a_format(
    tmp_path: Path,
) -> None:
    """There is no honest default for channels or sample rate, so the reader refuses."""
    chunks = iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 16)
    with pytest.raises(SampleHeaderError) as caught:
        read_sample_header(write(tmp_path, "nocomm.aif", aiff_file(b"AIFF", chunks)))
    assert caught.value.code == "no_comm_chunk"
    assert "COMM" in caught.value.error


def test_a_wave_without_a_fmt_chunk_raises_rather_than_inventing_a_format(
    tmp_path: Path,
) -> None:
    """Same refusal on the RIFF side."""
    chunks = riff_chunk(b"data", b"\x00" * 16)
    with pytest.raises(SampleHeaderError) as caught:
        read_sample_header(write(tmp_path, "nofmt.wav", wave_file(chunks)))
    assert caught.value.code == "no_fmt_chunk"


def test_a_non_audio_iff_container_is_refused_by_form_type(tmp_path: Path) -> None:
    """FORM/AIFF and FORM/AIFC are the only IFF types this reader claims."""
    with pytest.raises(SampleHeaderError) as caught:
        read_sample_header(write(tmp_path, "ilbm.iff", aiff_file(b"ILBM", b"")))
    assert caught.value.code == "unsupported_form"


def test_a_non_wave_riff_container_is_refused_by_form_type(tmp_path: Path) -> None:
    """An AVI is a RIFF file and is not a sample."""
    body = b"RIFF" + struct.pack("<I", 4) + b"AVI "
    with pytest.raises(SampleHeaderError) as caught:
        read_sample_header(write(tmp_path, "movie.avi", body))
    assert caught.value.code == "unsupported_form"


def test_a_file_that_is_not_a_container_at_all_is_refused(tmp_path: Path) -> None:
    """An mp3 renamed to .wav must not be answered with numbers."""
    with pytest.raises(SampleHeaderError) as caught:
        read_sample_header(write(tmp_path, "song.wav", b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00"))
    assert caught.value.code == "unknown_container"


def test_a_file_too_short_to_hold_a_header_is_refused(tmp_path: Path) -> None:
    """Four bytes cannot declare anything, and must not be read as a zero-length sample."""
    with pytest.raises(SampleHeaderError) as caught:
        read_sample_header(write(tmp_path, "stub.aif", b"FORM"))
    assert caught.value.code == "too_short"


def test_a_missing_file_is_refused_with_a_sentence_naming_it(tmp_path: Path) -> None:
    """The error carries a machine code and a sentence, as this repository requires."""
    with pytest.raises(SampleHeaderError) as caught:
        read_chunks(tmp_path / "absent.wav")
    assert caught.value.code == "unreadable_file"
    assert "absent.wav" in caught.value.error


def test_an_odd_length_chunk_is_followed_by_its_pad_byte(tmp_path: Path) -> None:
    """IFF pads odd chunks to even boundaries, and skipping the pad desynchronises the walk.

    The odd chunk here is 3 bytes. Without the pad byte every later chunk id would be read
    one byte early, so COMM would never be found.
    """
    chunks = (
        iff_chunk(b"ANNO", b"odd")
        + iff_chunk(b"COMM", comm_body(1, 100, 16, 44100.0))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 200)
    )
    header = read_sample_header(write(tmp_path, "odd.aif", aiff_file(b"AIFF", chunks)))
    assert [chunk.chunk_id for chunk in header.chunks] == ["ANNO", "COMM", "SSND"]
    assert header.code == "pcm"
    assert header.frames == 100


def test_a_zero_length_chunk_does_not_stall_the_walk(tmp_path: Path) -> None:
    """A chunk of size zero still advances the cursor by its own eight header bytes."""
    chunks = (
        iff_chunk(b"NULL", b"")
        + iff_chunk(b"COMM", comm_body(1, 10, 8, 8000.0))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 10)
    )
    header = read_sample_header(write(tmp_path, "zero.aif", aiff_file(b"AIFF", chunks)))
    assert [chunk.chunk_id for chunk in header.chunks] == ["NULL", "COMM", "SSND"]
    assert header.bit_depth == 8
    assert header.payload_bytes == header.expected_pcm_bytes == 10


def test_an_ssnd_offset_is_not_counted_as_audio(tmp_path: Path) -> None:
    """SSND begins with offset and blockSize, and an offset shifts where the samples start.

    Every local file has offset 0, so this shape is synthetic. Counting the offset bytes as
    audio would inflate the payload and break the size comparison that identifies ``able``.
    """
    chunks = iff_chunk(b"COMM", comm_body(1, 100, 16, 44100.0)) + iff_chunk(
        b"SSND", struct.pack(">II", 8, 0) + b"\x00" * 8 + b"\x00" * 200
    )
    header = read_sample_header(write(tmp_path, "offset.aif", aiff_file(b"AIFF", chunks)))
    assert header.payload_bytes == header.expected_pcm_bytes == 200


def test_a_bare_dataclass_computes_its_own_pcm_size() -> None:
    """24-bit rounds up to three bytes per sample, which is the case ``able`` files use."""
    header = SampleHeader(
        path="x.aif",
        container="AIFC",
        compression="able",
        compression_name="Ableton Content",
        channels=1,
        frames=40488,
        bit_depth=24,
        sample_rate=44100.0,
        duration_s=0.918,
        payload_bytes=121464,
        audio_readable=False,
        code="encrypted",
        reason="",
        chunks=(),
    )
    assert header.expected_pcm_bytes == 121464
    assert header.is_multisample_container is False


def test_is_multisample_container_detects_abli_chunk(tmp_path: Path) -> None:
    """Monolithic multi-sample files carry an ``abli`` index chunk beside ``able``."""
    single_chunks = (
        iff_chunk(b"FVER", struct.pack(">I", 0xA2805140))
        + iff_chunk(b"able", b"\x00" * 340)
        + iff_chunk(b"COMM", comm_body(2, 100, 16, 44100.0, b"able", b"Ableton Content"))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 400)
    )
    multi_chunks = (
        iff_chunk(b"FVER", struct.pack(">I", 0xA2805140))
        + iff_chunk(b"able", b"\x00" * 340)
        + iff_chunk(b"COMM", comm_body(2, 100, 16, 44100.0, b"able", b"Ableton Content"))
        + iff_chunk(b"abli", b"\x00" * 64)
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 400)
    )
    single = read_sample_header(write(tmp_path, "single.aif", aiff_file(b"AIFC", single_chunks)))
    multi = read_sample_header(write(tmp_path, "multi.aif", aiff_file(b"AIFC", multi_chunks)))
    assert single.is_multisample_container is False
    assert multi.is_multisample_container is True
    assert Chunk(chunk_id="abli", size=64, offset=multi.chunks[3].offset) in multi.chunks


def test_convert_to_pcm_wav_converts_16bit_and_24bit_aiff_and_float(tmp_path: Path) -> None:
    """Standard-library conversion turns 16-bit AIFF, 24-bit AIFF and 32-bit float into PCM WAV."""
    samples_16 = struct.pack(">hhhh", -32768, -1000, 1000, 32767)
    aiff_16 = aiff_file(
        b"AIFF",
        iff_chunk(b"COMM", comm_body(2, 2, 16, 44100.0))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + samples_16),
    )
    src_16 = write(tmp_path, "in16.aif", aiff_16)
    dst_16 = tmp_path / "out16.wav"
    hdr_16 = convert_to_pcm_wav(src_16, dst_16)
    assert (hdr_16.container, hdr_16.compression, hdr_16.bit_depth) == ("WAVE", "pcm", 16)
    assert (hdr_16.channels, hdr_16.frames, hdr_16.sample_rate) == (2, 2, 44100.0)
    assert dst_16.read_bytes()[-8:] == struct.pack("<hhhh", -32768, -1000, 1000, 32767)

    s24_a = (123456).to_bytes(3, "big", signed=True)
    s24_b = (-654321).to_bytes(3, "big", signed=True)
    aiff_24 = aiff_file(
        b"AIFF",
        iff_chunk(b"COMM", comm_body(1, 2, 24, 48000.0))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + s24_a + s24_b),
    )
    src_24 = write(tmp_path, "in24.aif", aiff_24)
    dst_24 = tmp_path / "out24.wav"
    hdr_24 = convert_to_pcm_wav(src_24, dst_24)
    assert (hdr_24.container, hdr_24.compression, hdr_24.bit_depth) == ("WAVE", "pcm", 24)
    assert (hdr_24.channels, hdr_24.frames, hdr_24.sample_rate) == (1, 2, 48000.0)
    assert dst_24.read_bytes()[-6:] == (
        (123456).to_bytes(3, "little", signed=True)
        + (-654321).to_bytes(3, "little", signed=True)
    )

    fl32_payload = struct.pack(">ff", 0.5, -0.5)
    aifc_fl32 = aiff_file(
        b"AIFC",
        iff_chunk(b"COMM", comm_body(1, 2, 32, 44100.0, b"fl32", b"32-bit Floating Point"))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + fl32_payload),
    )
    src_fl32 = write(tmp_path, "infl32.aif", aifc_fl32)
    dst_fl32 = tmp_path / "outfl32.wav"
    hdr_fl32 = convert_to_pcm_wav(src_fl32, dst_fl32)
    assert (hdr_fl32.container, hdr_fl32.compression, hdr_fl32.bit_depth) == ("WAVE", "pcm", 24)
    assert (hdr_fl32.channels, hdr_fl32.frames) == (1, 2)


def test_convert_to_pcm_wav_refuses_encrypted_sample(tmp_path: Path) -> None:
    """An encrypted ``able`` file is refused: Live Maestro never decodes protected samples."""
    chunks = (
        iff_chunk(b"FVER", struct.pack(">I", 0xA2805140))
        + iff_chunk(b"able", b"\x00" * 340)
        + iff_chunk(b"COMM", comm_body(1, 10, 16, 44100.0, b"able", b"Ableton Content"))
        + iff_chunk(b"SSND", struct.pack(">II", 0, 0) + b"\x00" * 20)
    )
    src = write(tmp_path, "enc.aif", aiff_file(b"AIFC", chunks))
    with pytest.raises(SampleHeaderError) as caught:
        convert_to_pcm_wav(src, tmp_path / "out.wav")
    assert caught.value.code == "encrypted"

