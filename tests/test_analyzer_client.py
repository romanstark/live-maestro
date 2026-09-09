"""Tests for the socket half of Channel C.

One detail decides whether this file is worth anything. The fake analyzer answers
from a *different* socket than the one that received the request, because that is
what Max does: ``udpreceive`` listens and ``udpsend`` answers, and ``udpsend``
holds its own ephemeral source port.

A client that connects its UDP socket, which
``create_datagram_endpoint(remote_addr=...)`` does, cannot hear such a reply: a
connected socket drops every datagram whose source is not the address it
connected to. A mock answering over the socket the client connected to matches by
construction and proves nothing about that, which is why this one does not.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Generator

import pytest

from live_maestro.analyzer import protocol
from live_maestro.analyzer.client import AnalyzerClient, parse_reply

#: A complete, well formed reply body, in the declared order, with recognisable
#: values so a field read out of the wrong slot is visible rather than plausible.
NUMBERS: dict[str, float] = {
    "schema": float(protocol.REPLY_SCHEMA),
    "request_id": 0.0,
    "samplerate": 48000.0,
    "window_samples": 19200.0,
    "momentary_lufs": -12.5,
    "short_term_lufs": -13.0,
    "integrated_lufs": -14.2,
    "lra_lu": 4.5,
    "sample_peak_db": -1.2,
    "true_peak_db": -0.8,
    "rms_l": 0.15,
    "rms_r": 0.15,
    "mean_lr": 0.0200,
    "rms_low_l": 0.10,
    "rms_low_r": 0.10,
    "mean_low_lr": 0.0095,
    "band_sub": 0.02,
    "band_bass": 0.09,
    "band_low_mid": 0.05,
    "band_mid": 0.04,
    "band_high_mid": 0.02,
    "band_presence": 0.01,
    "band_air": 0.005,
    "cal_momentary_lufs": -3.7,
    "cal_sample_peak_db": -6.02,
    "cal_true_peak_db": -6.0,
    "track_id": 12.0,
}


def test_the_reply_fixture_covers_every_declared_field() -> None:
    # Without this, a field added to the protocol would make every reply built
    # here raise a KeyError, or worse, be quietly defaulted.
    assert set(NUMBERS) == set(protocol.NUMERIC_REPLY_FIELDS)


def reply_packet(request_id: int, name: str, track_id: int = 12) -> bytes:
    """One OSC reply, built from the declared field order rather than by hand."""
    numbers = dict(NUMBERS, request_id=float(request_id), track_id=float(track_id))
    args: list[int | float | str] = [numbers[field] for field in protocol.NUMERIC_REPLY_FIELDS]
    args += [name]
    return protocol.encode(protocol.REPLY_ADDRESS, args)


class FakeAnalyzer:
    """A stand-in for the device that behaves the way Max's objects behave.

    ``udpreceive`` listens on one socket and ``udpsend`` answers from another, so
    the reply reaches the client from a source port the client never named.
    """

    def __init__(self, instances: list[tuple[str, int]]) -> None:
        self.instances = instances
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.setblocking(False)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender.bind(("127.0.0.1", 0))
        self.requests: list[list[int | float | str]] = []

    @property
    def port(self) -> int:
        return int(self.listener.getsockname()[1])

    async def serve_one(self) -> None:
        loop = asyncio.get_running_loop()
        data = await loop.sock_recv(self.listener, 4096)
        address, args = protocol.decode(data)
        assert address == protocol.REQUEST_ADDRESS
        self.requests.append(args)
        request_id = int(args[0])
        reply_port = int(args[3])
        for name, track_id in self.instances:
            self.sender.sendto(
                reply_packet(request_id, name, track_id), ("127.0.0.1", reply_port)
            )

    def close(self) -> None:
        self.listener.close()
        self.sender.close()


async def _measure(analyzer: FakeAnalyzer, **kwargs: object) -> list[object]:
    client = AnalyzerClient(port=analyzer.port, timeout=2.0, settle=0.2)
    serving = asyncio.create_task(analyzer.serve_one())
    replies = await client.measure(**kwargs)  # type: ignore[arg-type]
    await serving
    return list(replies)


@pytest.fixture
def analyzer() -> Generator[FakeAnalyzer, None, None]:
    made = FakeAnalyzer([("Main", 12)])
    yield made
    made.close()


def test_a_reply_from_another_source_port_is_accepted(analyzer: FakeAnalyzer) -> None:
    """A connected socket would drop this reply, so the client must not connect."""
    replies = asyncio.run(_measure(analyzer))
    assert len(replies) == 1, (
        "no reply arrived. A connected UDP socket drops datagrams whose source is "
        "not the address it connected to, which is exactly what a real device sends."
    )
    assert analyzer.sender.getsockname()[1] != analyzer.port, (
        "the fake analyzer must answer from a different port than it listens on, "
        "or this test cannot see the bug it exists for"
    )


def test_the_request_carries_the_port_the_client_actually_bound(
    analyzer: FakeAnalyzer,
) -> None:
    asyncio.run(_measure(analyzer, window_ms=250, reset=True))
    request = analyzer.requests[0]
    assert list(protocol.REQUEST_FIELDS) == ["request_id", "window_ms", "reset", "reply_port"]
    assert request[1] == 250
    assert request[2] == 1
    # The reply arrived, so the port in the request was the one being listened on.
    assert 1 <= int(request[3]) <= 65535


def test_every_instance_that_answers_is_returned() -> None:
    fake = FakeAnalyzer([("Main", 12), ("Drums", 34), ("Bass", 56)])
    try:
        replies = asyncio.run(_measure(fake))
    finally:
        fake.close()
    assert {reply.track_name for reply in replies} == {"Main", "Drums", "Bass"}  # type: ignore[attr-defined]


def test_nothing_listening_returns_no_replies_instead_of_raising() -> None:
    # Port 1 is not bound by this process and needs no privileges to send to.
    client = AnalyzerClient(port=1, timeout=0.2, settle=0.05)
    assert asyncio.run(client.measure()) == []


def test_a_reply_to_an_older_request_is_not_read_as_this_one(
    analyzer: FakeAnalyzer,
) -> None:
    async def run() -> list[object]:
        client = AnalyzerClient(port=analyzer.port, timeout=0.4, settle=0.1)

        async def answer_the_wrong_id() -> None:
            loop = asyncio.get_running_loop()
            data = await loop.sock_recv(analyzer.listener, 4096)
            _, args = protocol.decode(data)
            stale = int(args[0]) - 1
            analyzer.sender.sendto(
                reply_packet(stale, "Main"), ("127.0.0.1", int(args[3]))
            )

        serving = asyncio.create_task(answer_the_wrong_id())
        replies = await client.measure()
        await serving
        return list(replies)

    assert asyncio.run(run()) == []


def test_a_datagram_that_is_not_a_reply_is_ignored() -> None:
    assert parse_reply(b"not osc at all") is None
    assert parse_reply(protocol.encode("/something/else", [1, 2, 3])) is None
    # Right address, too few numbers: reading it would leave fields unset and the
    # report would present defaults as measurements.
    short = protocol.encode(protocol.REPLY_ADDRESS, [protocol.REPLY_SCHEMA, 1, 2.0])
    assert parse_reply(short) is None


def test_a_track_name_split_by_the_serializer_is_rejoined() -> None:
    args: list[int | float | str] = [
        NUMBERS[field] for field in protocol.NUMERIC_REPLY_FIELDS
    ]
    # The one declared string arrives as three, which is what a serializer that
    # splits a symbol on spaces produces. The numbers must survive it, which is
    # why the string comes last.
    args += ["01", "SC", "Trigger"]
    reply = parse_reply(protocol.encode(protocol.REPLY_ADDRESS, args))
    assert reply is not None
    assert reply.numbers["true_peak_db"] == pytest.approx(-0.8)
    assert reply.track_name == "01 SC Trigger"


def test_the_track_id_comes_back_as_an_integer() -> None:
    reply = parse_reply(reply_packet(1, "Main", 34))
    assert reply is not None
    assert reply.track_id == 34
    # An id of zero is what live.path answers for a path it could not resolve, so
    # it is reported as absent rather than as track zero.
    assert parse_reply(reply_packet(1, "Main", 0)) is not None
    absent = parse_reply(reply_packet(1, "Main", 0))
    assert absent is not None and absent.track_id is None


def test_the_environment_can_move_the_port_and_a_bad_value_cannot_break_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_MAESTRO_ANALYZER_PORT", "9999")
    assert AnalyzerClient().port == 9999
    for broken in ("not-a-number", "0", "70000", ""):
        monkeypatch.setenv("LIVE_MAESTRO_ANALYZER_PORT", broken)
        assert AnalyzerClient().port == protocol.DEFAULT_DEVICE_PORT
