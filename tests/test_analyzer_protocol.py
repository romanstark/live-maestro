"""Tests for the OSC codec and for the contract it encodes.

Open Sound Control is what Max's ``udpsend`` and ``udpreceive`` speak, so this is
the format the device is reachable in. Forty lines of ``struct`` is cheaper than a
dependency, and the cost of writing it by hand is that the padding rules need
holding down.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from live_maestro.analyzer import protocol

PROTOCOL_DOC = Path(__file__).resolve().parent.parent / "docs" / "protocol.md"

#: A numbered field row in one of the tables of docs/protocol.md section 11.
_DOC_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`(\w+)`\s*\|", re.MULTILINE)


def documented_fields(heading: str) -> list[str]:
    """The field names one table of section 11 lists, in the order it lists them."""
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.find("\n### ", start + len(heading))
    rows = _DOC_ROW.findall(text[start : end if end > 0 else len(text)])
    assert rows, f"no numbered field rows found under {heading!r}"
    assert [int(index) for index, _ in rows] == list(range(len(rows))), (
        f"the rows under {heading!r} are not numbered from zero without gaps"
    )
    return [name for _, name in rows]


@pytest.mark.parametrize(
    "args",
    [
        [],
        [0],
        [1, 2, 3, 4],
        [-1, 2147483647, -2147483648],
        [0.0, -0.5, 1.5],
        ["master"],
        ["", "a", "ab", "abc", "abcd", "abcde"],
        [1, 2.5, "01 SC Trigger", -3, "live_set tracks 12"],
    ],
)
def test_every_message_survives_a_round_trip(args: list[int | float | str]) -> None:
    address, decoded = protocol.decode(protocol.encode("/maestro/test", args))
    assert address == "/maestro/test"
    assert decoded == args


@pytest.mark.parametrize("text", ["", "a", "ab", "abc", "abcd", "abcdefg", "abcdefgh"])
def test_a_packet_is_always_a_whole_number_of_four_byte_words(text: str) -> None:
    # OSC requires four byte alignment throughout, and a string that already ends
    # on a boundary still takes a terminator, so the padding is never nothing.
    packet = protocol.encode("/x", [text])
    assert len(packet) % 4 == 0
    assert packet.endswith(b"\0")


def test_the_type_tag_string_records_what_was_sent() -> None:
    packet = protocol.encode("/x", [1, 2.0, "three"])
    # Address takes one word, then the tag string, which is ",ifs" plus its NUL.
    assert packet[4:12] == b",ifs\0\0\0\0"


def test_a_float_keeps_the_precision_a_float32_has() -> None:
    _, decoded = protocol.decode(protocol.encode("/x", [0.1]))
    assert decoded[0] == pytest.approx(0.1, abs=1e-6)
    assert isinstance(decoded[0], float)


def test_an_integer_stays_an_integer_and_a_float_stays_a_float() -> None:
    # The device reports some fields as ints and some as floats, and a caller
    # reading a count out of a float is a different bug to chase.
    _, decoded = protocol.decode(protocol.encode("/x", [7, 7.0]))
    assert isinstance(decoded[0], int)
    assert isinstance(decoded[1], float)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"abc",
        b"not an address at all",
        b"/x\0\0",  # an address with no type tag string
        b"/x\0\0nope\0\0\0\0",  # a tag string that does not start with a comma
    ],
)
def test_a_datagram_that_is_not_an_osc_message_is_refused(data: bytes) -> None:
    with pytest.raises(protocol.OscError):
        protocol.decode(data)


def test_a_type_this_codec_does_not_send_is_refused_rather_than_skipped() -> None:
    # A blob or a timetag would need a length this codec never computes. Skipping
    # it would leave the read offset wrong and every later field misaligned.
    forged = protocol._encode_string("/x") + protocol._encode_string(",b") + b"\0\0\0\4data"
    with pytest.raises(protocol.OscError, match="unsupported OSC type tag"):
        protocol.decode(forged)


def test_a_packet_that_ends_inside_an_argument_is_refused() -> None:
    truncated = protocol.encode("/x", [1, 2, 3])[:-6]
    with pytest.raises(protocol.OscError):
        protocol.decode(truncated)


def test_a_bool_is_refused_because_it_has_no_osc_type_here() -> None:
    # bool is an int subclass, so it would silently encode as 0 or 1. Nothing in
    # this protocol sends one, and the reset flag is converted at the call site.
    with pytest.raises(protocol.OscError, match="bool"):
        protocol.encode("/x", [True])


def test_a_type_python_cannot_place_is_refused() -> None:
    with pytest.raises(protocol.OscError, match="cannot encode"):
        protocol.encode("/x", [None])  # type: ignore[list-item]


def test_the_declared_fields_are_unique_and_do_not_overlap() -> None:
    numbers = protocol.NUMERIC_REPLY_FIELDS
    strings = protocol.STRING_REPLY_FIELDS
    assert len(set(numbers)) == len(numbers)
    assert len(set(strings)) == len(strings)
    assert not set(numbers) & set(strings)
    # The strings go last so a symbol split by the serializer cannot swallow a
    # number that followed it.
    assert set(strings).isdisjoint(numbers)


def test_the_reply_declares_a_field_for_every_band() -> None:
    for name, low, high in protocol.BANDS:
        assert name in protocol.NUMERIC_REPLY_FIELDS
        assert low > 0
        assert high is None or high > low
    edges = [low for _, low, _ in protocol.BANDS]
    assert edges == sorted(edges), "the bands are declared from low to high"
    for (_, _, high), (_, next_low, _) in zip(protocol.BANDS, protocol.BANDS[1:], strict=False):
        assert high == next_low, "the bands meet without a gap or an overlap"


def test_the_schema_and_request_id_lead_the_reply() -> None:
    # The client reads the schema before anything else, so it has to arrive
    # before any field whose meaning depends on it.
    assert protocol.NUMERIC_REPLY_FIELDS[0] == "schema"
    assert protocol.NUMERIC_REPLY_FIELDS[1] == "request_id"


def test_the_documented_request_is_the_declared_request() -> None:
    """docs/protocol.md section 11.2 is the contract, so it cannot drift from it."""
    assert documented_fields("### 11.2 Request") == list(protocol.REQUEST_FIELDS)


def test_the_documented_reply_is_the_declared_reply() -> None:
    """The reply table is what a reader builds a decoder from.

    A field added to the protocol and not to the table would leave the table
    describing the wrong offsets for everything after it.
    """
    documented = documented_fields("### 11.3 Reply")
    declared = list(protocol.NUMERIC_REPLY_FIELDS) + list(protocol.STRING_REPLY_FIELDS)
    assert documented == declared


def test_the_documented_port_and_addresses_are_the_declared_ones() -> None:
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert f"| `{protocol.DEFAULT_DEVICE_PORT}` |" in text
    assert f"Address `{protocol.REQUEST_ADDRESS}`" in text
    assert f"Address `{protocol.REPLY_ADDRESS}`" in text
