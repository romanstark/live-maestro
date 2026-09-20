"""The UDP side of ``analyze_audio``: ask every analyzer in the set, collect answers.

The socket must stay unconnected. ``create_datagram_endpoint(remote_addr=...)`` calls
``connect`` underneath, and a connected UDP socket drops every datagram whose source
is not the address it connected to, while Max's ``udpsend`` answers from its own
ephemeral source port. The reply address travels in the request, so the device sends
to the port this client bound. A fake that answers over the socket the client connected
to matches by construction, so a test has to answer from a different one.

Every reply is collected, not the first one: the device can sit on several tracks,
they all bind the same port and they all answer, and choosing among them is
:mod:`live_maestro.analyzer.report`'s job. Nothing here waits for audio, because the
device reports rolling averages of audio that has already played, so the timeout
covers one round trip and nothing else.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
from dataclasses import dataclass, field

from live_maestro.analyzer.protocol import (
    DEFAULT_DEVICE_PORT,
    NUMERIC_REPLY_FIELDS,
    REPLY_ADDRESS,
    REQUEST_ADDRESS,
    STRING_REPLY_FIELDS,
    OscError,
    decode,
    encode,
)

__all__ = [
    "DEFAULT_ANALYZER_HOST",
    "DEFAULT_ANALYZER_PORT",
    "DEFAULT_SETTLE_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "AnalyzerClient",
    "Reply",
]

logger = logging.getLogger(__name__)

DEFAULT_ANALYZER_HOST = "127.0.0.1"
DEFAULT_ANALYZER_PORT = DEFAULT_DEVICE_PORT

#: How long to wait for the first answer. Max answers in the low milliseconds, so
#: the margin covers a machine under load. Nothing received within it is reported
#: to the caller as the device not being found.
DEFAULT_TIMEOUT_SECONDS = 1.5

#: How much longer to listen once the first answer is in, so a second and third
#: analyzer in the same set are not missed. They all answer the same request at
#: the same time, so this only has to cover the spread between them.
DEFAULT_SETTLE_SECONDS = 0.15

#: Request ids only have to tell one in-flight request from the previous one, so
#: that a late answer to an abandoned request is not read as an answer to this one.
_REQUEST_IDS = itertools.count(1)


@dataclass(frozen=True)
class Reply:
    """One analyzer instance's answer to one request."""

    numbers: dict[str, float] = field(default_factory=dict)
    strings: dict[str, str] = field(default_factory=dict)

    @property
    def track_name(self) -> str:
        return self.strings.get("track_name", "")

    @property
    def track_id(self) -> int | None:
        """Return the Live Object Model id of the track this instance sits on.

        It tells two tracks that share a name apart. It does not say where the
        track is: nothing maps an id back to an index over this channel.
        """
        raw = self.numbers.get("track_id")
        return int(raw) if raw else None


class _Collector(asyncio.DatagramProtocol):
    """Put every datagram on a queue. Filtering happens where it can be tested."""

    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self.queue = queue

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        # An ICMP port-unreachable for the request arrives here. It means nothing
        # is listening on the device port, which is the same outcome as silence
        # and is reported by the caller finding no replies.
        logger.debug("analyzer socket reported %s", exc)


def parse_reply(data: bytes) -> Reply | None:
    """Turn one datagram into a :class:`Reply`, or ``None`` if it is not one.

    A datagram on another OSC address is dropped rather than guessed at: port 9882 is
    not reserved and another sender on the machine may use it.

    A track name holding spaces may arrive split across several string arguments. The
    numbers come first, so the tail can be rejoined without losing them.
    """
    try:
        address, args = decode(data)
    except OscError as exc:
        logger.debug("ignoring a datagram that is not OSC: %s", exc)
        return None
    if address != REPLY_ADDRESS:
        logger.debug("ignoring OSC address %s", address)
        return None

    numbers: dict[str, float] = {}
    for name, value in zip(NUMERIC_REPLY_FIELDS, args, strict=False):
        if not isinstance(value, (int, float)):
            logger.debug("reply field %s carried %r, not a number", name, value)
            return None
        numbers[name] = float(value)
    if len(numbers) < len(NUMERIC_REPLY_FIELDS):
        logger.debug("reply carried %d of %d numbers", len(numbers), len(NUMERIC_REPLY_FIELDS))
        return None

    tail = [str(value) for value in args[len(NUMERIC_REPLY_FIELDS) :]]
    strings: dict[str, str] = {}
    for index, name in enumerate(STRING_REPLY_FIELDS):
        if index == len(STRING_REPLY_FIELDS) - 1:
            # The last declared string absorbs whatever is left, because that is
            # where a symbol split by the serializer ends up.
            strings[name] = " ".join(tail[index:]).strip()
        elif index < len(tail):
            strings[name] = tail[index].strip()
    return Reply(numbers=numbers, strings=strings)


class AnalyzerClient:
    """Send one request to the analyzer port and return every answer to it."""

    def __init__(
        self,
        host: str = DEFAULT_ANALYZER_HOST,
        port: int | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        settle: float = DEFAULT_SETTLE_SECONDS,
    ) -> None:
        self.host = host
        self.port = port if port is not None else _port_from_environment()
        self.timeout = timeout
        self.settle = settle

    async def measure(self, *, window_ms: int = 400, reset: bool = False) -> list[Reply]:
        """Ask every analyzer in the open set and return the replies that arrived.

        An empty list means nothing answered, whatever the cause. Wording the causes
        is :mod:`live_maestro.analyzer.report`'s job.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Collector(queue), local_addr=(DEFAULT_ANALYZER_HOST, 0)
            )
        except OSError as exc:
            logger.debug("could not bind a local UDP port: %s", exc)
            return []

        try:
            reply_port = int(transport.get_extra_info("sockname")[1])
            request_id = next(_REQUEST_IDS) % 2_000_000_000
            payload = encode(
                REQUEST_ADDRESS, [request_id, window_ms, int(reset), reply_port]
            )
            transport.sendto(payload, (self.host, self.port))
            return await self._collect(queue, request_id)
        finally:
            transport.close()

    async def _collect(self, queue: asyncio.Queue[bytes], request_id: int) -> list[Reply]:
        """Wait for the first answer, then keep listening briefly for the others."""
        replies: list[Reply] = []
        deadline = self.timeout
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=deadline)
            except TimeoutError:
                return replies
            reply = parse_reply(data)
            if reply is None:
                continue
            if int(reply.numbers.get("request_id", -1)) != request_id:
                # A late answer to a request that already gave up. Reading it as
                # this request's answer would report a measurement of a different
                # moment, so it is dropped.
                logger.debug("dropping a reply for request %s", reply.numbers.get("request_id"))
                continue
            replies.append(reply)
            deadline = self.settle


def _port_from_environment() -> int:
    """Return the device port from the environment, defaulting to 9882.

    A bad value is ignored rather than raising, because a typo in an environment
    variable should not stop the rest of the MCP server from starting.
    """
    raw = os.getenv("LIVE_MAESTRO_ANALYZER_PORT")
    if not raw:
        return DEFAULT_ANALYZER_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning("LIVE_MAESTRO_ANALYZER_PORT is not a number: %r", raw)
        return DEFAULT_ANALYZER_PORT
    if not 1 <= port <= 65535:
        logger.warning("LIVE_MAESTRO_ANALYZER_PORT is out of range: %d", port)
        return DEFAULT_ANALYZER_PORT
    return port
