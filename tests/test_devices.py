"""The properties every generated Max for Live device has to have.

A device is a file, so all of this is read out of the committed files and none of it
needs Max. Each test runs once per entry in :data:`DEVICES`:

* the ``.amxd`` container carries a device type code, or Live loads nothing
* every object name is one Max has, or Max draws a broken box and reports no error
* every measurement reaches the reply: an outlet wired nowhere reports nothing
* no patch cord names an inlet or outlet past the stored count, which Max can drop
  on load without saying so
* the port and the addresses are the ones the client uses

What a particular device measures is tested beside it: the analyzer's bands, averages
and audio path are in ``tests/test_analyzer_device.py``. To register a device, add a
:class:`DeviceUnderTest` naming its ``DeviceBuild`` and the contract its client reads.
"""

from __future__ import annotations

import collections
import json
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_analyzer_device
import build_probe_device

from live_maestro import maxgen, probe_protocol
from live_maestro.analyzer import protocol as analyzer_protocol


@dataclass(frozen=True)
class Osc:
    """What a device's client sends and where it listens for the answer."""

    #: The UDP port the device's ``udpreceive`` binds.
    port: int
    #: Every OSC address the device accepts, as the client sends them.
    request_addresses: tuple[str, ...]
    #: Every OSC address the device answers with.
    reply_addresses: tuple[str, ...]
    #: True when the reply port is set per request by a ``port`` message rather
    #: than fixed by the ``udpsend`` creation argument.
    aims_reply_port: bool = True


@dataclass(frozen=True)
class Reply:
    """A device that answers in one ``pack``, and how wide that pack is."""

    #: Total number of slots, which is every field the contract declares.
    slots: int
    #: The schema number the client matches on, sent as a message box.
    schema: int


@dataclass(frozen=True)
class DeviceUnderTest:
    """One committed device: its generator, its two files and its contract."""

    spec: maxgen.DeviceBuild
    osc: Osc
    reply: Reply | None = None

    @property
    def amxd(self) -> Path:
        return self.spec.out_default / f"{self.spec.name}.amxd"

    @property
    def maxpat(self) -> Path:
        return self.spec.out_default / f"{self.spec.name}.maxpat"


ANALYZER = DeviceUnderTest(
    spec=build_analyzer_device.DEVICE,
    osc=Osc(
        port=analyzer_protocol.DEFAULT_DEVICE_PORT,
        request_addresses=(analyzer_protocol.REQUEST_ADDRESS,),
        reply_addresses=(analyzer_protocol.REPLY_ADDRESS,),
    ),
    reply=Reply(
        slots=(
            len(analyzer_protocol.NUMERIC_REPLY_FIELDS)
            + len(analyzer_protocol.STRING_REPLY_FIELDS)
        ),
        schema=analyzer_protocol.REPLY_SCHEMA,
    ),
)

PROBE = DeviceUnderTest(
    spec=build_probe_device.DEVICE,
    osc=Osc(
        port=probe_protocol.DEVICE_PORT,
        request_addresses=(
            probe_protocol.BUFFER_ADDRESS,
            probe_protocol.DUMP_ADDRESS,
            probe_protocol.REC_OPEN_ADDRESS,
            probe_protocol.REC_START_ADDRESS,
            probe_protocol.REC_STOP_ADDRESS,
            probe_protocol.PING_ADDRESS,
            probe_protocol.TRACK_OPEN_ADDRESS,
            probe_protocol.TRACK_START_ADDRESS,
            probe_protocol.TRACK_STOP_ADDRESS,
        ),
        reply_addresses=(
            probe_protocol.BUFFER_REPLY_ADDRESS,
            probe_protocol.REC_MARK_REPLY_ADDRESS,
            probe_protocol.PONG_ADDRESS,
        ),
        aims_reply_port=False,
    ),
)

#: Every device this repository generates and commits.
DEVICES: tuple[DeviceUnderTest, ...] = (ANALYZER, PROBE)

#: The subset that answers in a single packed reply.
REPLYING: tuple[DeviceUnderTest, ...] = tuple(d for d in DEVICES if d.reply is not None)

_IDS = [device.spec.name for device in DEVICES]
_REPLYING_IDS = [device.spec.name for device in REPLYING]

everydevice = pytest.mark.parametrize("device", DEVICES, ids=_IDS)
everyreplyingdevice = pytest.mark.parametrize("device", REPLYING, ids=_REPLYING_IDS)


@dataclass(frozen=True)
class Chunk:
    """One ``.amxd`` chunk: its name, the length it declares and the bytes present.

    ``declared`` and ``body`` are kept apart on purpose. A length that overruns the
    end of the file reads back as a short body, and a reader that only slices sees
    a chunk it can parse where Live sees a truncated one.
    """

    name: bytes
    declared: int
    body: bytes


def chunks_of(amxd: Path) -> list[Chunk]:
    """Walk an ``.amxd`` chunk by chunk, in file order, clipping nothing."""
    raw = amxd.read_bytes()
    out: list[Chunk] = []
    offset = 0
    while offset + 8 <= len(raw):
        name = raw[offset : offset + 4]
        declared = struct.unpack("<I", raw[offset + 4 : offset + 8])[0]
        out.append(Chunk(name, declared, raw[offset + 8 : offset + 8 + declared]))
        offset += 8 + declared
    if offset != len(raw):
        out.append(Chunk(b"", 0, raw[offset:]))
    return out


def patcher_of(device: DeviceUnderTest) -> dict[str, Any]:
    """The patcher as it sits inside the committed ``.amxd``, not as rebuilt here."""
    bodies = {chunk.name: chunk.body for chunk in chunks_of(device.amxd)}
    assert set(bodies) == {b"ampf", b"meta", b"ptch"}, sorted(bodies)
    loaded = json.loads(bodies[b"ptch"][:-1].decode("utf-8"))["patcher"]
    assert isinstance(loaded, dict)
    return loaded


def boxes_of(patcher: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["box"]["id"]: entry["box"] for entry in patcher["boxes"]}


def text_of(box: dict[str, Any]) -> str:
    return str(box.get("text") or box.get("maxclass") or "")


def texts_of(patcher: dict[str, Any]) -> list[str]:
    return [text_of(entry["box"]) for entry in patcher["boxes"]]


@dataclass(frozen=True)
class Graph:
    """A patcher read as a graph: the boxes, and which ports carry a cord."""

    boxes: dict[str, dict[str, Any]]
    outgoing: dict[str, set[int]]
    incoming: dict[str, set[int]]


def graph_of(patcher: dict[str, Any]) -> Graph:
    outgoing: dict[str, set[int]] = collections.defaultdict(set)
    incoming: dict[str, set[int]] = collections.defaultdict(set)
    for line in patcher["lines"]:
        source = line["patchline"]["source"]
        target = line["patchline"]["destination"]
        outgoing[source[0]].add(source[1])
        incoming[target[0]].add(target[1])
    return Graph(boxes_of(patcher), outgoing, incoming)


def test_every_generator_in_the_repository_is_registered() -> None:
    """Every ``scripts/build_*_device.py`` has to appear in :data:`DEVICES`.

    A generator that is not listed gets none of the checks below, and nothing else
    in the suite would notice.
    """
    generators = sorted(path.name for path in (REPO_ROOT / "scripts").glob("build_*_device.py"))
    registered = sorted(Path(device.spec.generator).name for device in DEVICES)
    assert generators == registered, (
        "every scripts/build_*_device.py belongs in DEVICES here: add a "
        "DeviceUnderTest naming that generator's DeviceBuild, the port and OSC "
        "addresses its client uses, and its reply pack if it answers in one"
    )


@everydevice
def test_the_committed_device_is_what_the_generator_writes(device: DeviceUnderTest) -> None:
    """A hand edit to either file, or a contract change without a rebuild, fails here."""
    finished = subprocess.run(
        [sys.executable, str(REPO_ROOT / device.spec.generator), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr


@everydevice
def test_the_container_is_the_one_live_loads(device: DeviceUnderTest) -> None:
    """A file that is only JSON has no type code, and Live does not show it at all."""
    raw = device.amxd.read_bytes()
    assert raw[:4] == b"ampf"
    assert struct.unpack("<I", raw[4:8])[0] == len(device.spec.device_type)
    assert raw[8:12] == device.spec.device_type
    assert raw[12:16] == b"meta"
    assert raw[24:28] == b"ptch"
    assert raw[-1:] == b"\0", "the patcher chunk is NUL terminated and counted with it"
    project = patcher_of(device)["project"]
    assert isinstance(project, dict)
    # The code is written twice, once for Live's container reader and once inside
    # the patcher Max reads. Two spellings of one fact can disagree.
    assert project["amxdtype"] == maxgen.type_code_int(device.spec.device_type)


@everydevice
def test_the_chunk_lengths_describe_the_whole_file(device: DeviceUnderTest) -> None:
    """A length that overruns or stops short leaves Live reading a truncated patcher."""
    chunks = chunks_of(device.amxd)
    assert [chunk.name for chunk in chunks] == [b"ampf", b"meta", b"ptch"], (
        "three chunks in this order, and no trailing bytes that are not one of them"
    )
    for chunk in chunks:
        assert len(chunk.body) == chunk.declared, (
            f"{chunk.name!r} declares {chunk.declared} bytes and "
            f"{len(chunk.body)} are there"
        )
    total = sum(8 + chunk.declared for chunk in chunks)
    assert total == device.amxd.stat().st_size, (
        "chunk headers and bodies must account for every byte of the file"
    )


@everydevice
def test_the_reviewable_copy_holds_the_same_graph(device: DeviceUnderTest) -> None:
    """The ``.maxpat`` exists so a diff can show what changed in the device."""
    reviewable = json.loads(device.maxpat.read_text(encoding="utf-8"))["patcher"]
    assert reviewable == patcher_of(device)


@everydevice
def test_every_object_is_one_this_project_verified_exists(device: DeviceUnderTest) -> None:
    """Max draws an object it does not have as a broken box and reports no error."""
    for box in boxes_of(patcher_of(device)).values():
        if box["maxclass"] != "newobj":
            continue
        head = text_of(box).split()[0]
        assert head in maxgen.SHAPES, (
            f"{head!r} is not in live_maestro.maxgen.SHAPES. Every entry there was "
            f"looked up in Max's refpages; an object Max does not have measures "
            f"nothing and reports no error."
        )


@everydevice
def test_no_measurement_ends_in_a_void(device: DeviceUnderTest) -> None:
    """An outlet wired nowhere is a measurement that reaches no reply."""
    graph = graph_of(patcher_of(device))
    stranded = [
        text_of(box)
        for box_id, box in graph.boxes.items()
        if box["maxclass"] == "newobj"
        and not text_of(box).startswith(device.spec.terminal)
        and not graph.outgoing[box_id]
    ]
    assert stranded == [], f"these objects produce a value nobody reads: {stranded}"


@everydevice
def test_nothing_is_left_unfed(device: DeviceUnderTest) -> None:
    """Everything that is not a declared source has to be reachable, or it never runs."""
    graph = graph_of(patcher_of(device))
    orphans = [
        text_of(box)
        for box_id, box in graph.boxes.items()
        if box["maxclass"] in ("newobj", "message")
        and not text_of(box).startswith(device.spec.free_running)
        and not graph.incoming[box_id]
    ]
    assert orphans == [], f"these objects can never run: {orphans}"


@everydevice
def test_the_declared_sources_and_sinks_are_all_in_the_graph(device: DeviceUnderTest) -> None:
    """A stale exception is an assertion that quietly stopped applying.

    ``free_running`` and ``terminal`` are the two tests above written down as
    exceptions. A name in either that no box carries any more excuses a box that
    is no longer there, and the day the graph grows one back it is excused again
    without anybody deciding that.
    """
    texts = texts_of(patcher_of(device))
    for name in device.spec.free_running + device.spec.terminal:
        assert any(text.startswith(name) for text in texts), (
            f"{name!r} is declared in {device.spec.generator} but no box carries it"
        )


@everydevice
def test_no_patch_cord_names_an_inlet_or_outlet_the_box_does_not_have(
    device: DeviceUnderTest,
) -> None:
    """Max can drop a cord whose index is past the stored count, silently."""
    graph = graph_of(patcher_of(device))
    for box_id, box in graph.boxes.items():
        for index in graph.outgoing[box_id]:
            assert index < box["numoutlets"], f"{text_of(box)!r} outlet {index}"
        for index in graph.incoming[box_id]:
            assert index < box["numinlets"], f"{text_of(box)!r} inlet {index}"


@everydevice
def test_every_patch_cord_names_a_box_the_patcher_holds(device: DeviceUnderTest) -> None:
    """A cord to an id nobody has is a connection Max cannot make."""
    patcher = patcher_of(device)
    known = set(boxes_of(patcher))
    for line in patcher["lines"]:
        source = line["patchline"]["source"][0]
        target = line["patchline"]["destination"][0]
        assert source in known, f"cord from unknown box {source}"
        assert target in known, f"cord to unknown box {target}"


@everydevice
def test_the_device_listens_where_the_client_sends(device: DeviceUnderTest) -> None:
    """Both ends take the port and the addresses from one contract."""
    texts = texts_of(patcher_of(device))
    assert f"udpreceive {device.osc.port}" in texts, (
        f"nothing binds UDP {device.osc.port}, so no request arrives"
    )
    routes = [text for text in texts if text.split()[0] == "route"]
    for address in device.osc.request_addresses:
        assert any(address in route.split()[1:] for route in routes), (
            f"nothing routes {address}, so a request carrying it is dropped"
        )
    for address in device.osc.reply_addresses:
        assert f"prepend {address}" in texts
    # The sender's creation argument is only a placeholder when the reply port is
    # aimed: every request carries the port to answer on.
    assert any(text.startswith("udpsend 127.0.0.1 ") for text in texts)
    if device.osc.aims_reply_port:
        assert "prepend port" in texts


@everyreplyingdevice
def test_the_reply_has_a_slot_for_every_declared_field(device: DeviceUnderTest) -> None:
    assert device.reply is not None
    graph = graph_of(patcher_of(device))
    packs = [
        (box_id, box)
        for box_id, box in graph.boxes.items()
        if text_of(box).startswith("pack ")
    ]
    assert len(packs) == 1, "one reply, one pack"
    box_id, box = packs[0]
    assert box["numinlets"] == device.reply.slots
    assert graph.incoming[box_id] == set(range(device.reply.slots)), (
        "every slot must be written before the packet goes out, or the reply "
        "carries a pack default where a measurement belongs"
    )


@everyreplyingdevice
def test_the_schema_the_device_sends_is_the_one_the_client_reads(
    device: DeviceUnderTest,
) -> None:
    assert device.reply is not None
    messages = {
        text_of(entry["box"])
        for entry in patcher_of(device)["boxes"]
        if entry["box"]["maxclass"] == "message"
    }
    assert str(device.reply.schema) in messages, (
        f"no message box sends schema {device.reply.schema}, which the client matches on"
    )
