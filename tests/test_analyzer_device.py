"""Tests for the generated Max for Live device.

None of this needs Max, and that is the point. Four properties of a working
device are readable in the file itself, and each one is a test below:

* the ``.amxd`` container carries a device type code, or Live loads nothing
* every object name is one Max has, because Max draws an unknown one as a broken
  box and reports no error
* every measurement reaches the reply, because an outlet wired nowhere is a
  measurement nobody receives
* the port and the reply layout are the ones the client uses, which holds because
  the device is generated from the same contract

The device is generated, so these run against the graph the generator writes.
"""

from __future__ import annotations

import collections
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_analyzer_device as builder

from live_maestro.analyzer import protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
AMXD = REPO_ROOT / "devices" / "LiveMaestro_Analyzer.amxd"
MAXPAT = REPO_ROOT / "devices" / "LiveMaestro_Analyzer.maxpat"

#: Boxes that are supposed to be the end of a chain. Everything else feeds
#: something, or it is measuring into a void.
TERMINAL = ("plugout~", "udpsend")


@pytest.fixture(scope="module")
def patcher() -> dict[str, object]:
    """The patcher as it sits inside the committed ``.amxd``."""
    raw = AMXD.read_bytes()
    chunks: dict[bytes, bytes] = {}
    offset = 0
    while offset < len(raw):
        name = raw[offset : offset + 4]
        size = struct.unpack("<I", raw[offset + 4 : offset + 8])[0]
        chunks[name] = raw[offset + 8 : offset + 8 + size]
        offset += 8 + size
    assert set(chunks) == {b"ampf", b"meta", b"ptch"}, sorted(chunks)
    return json.loads(chunks[b"ptch"][:-1].decode("utf-8"))["patcher"]


Graph = tuple[dict[str, dict], dict[str, set[int]], dict[str, set[int]]]


@pytest.fixture(scope="module")
def graph(patcher: dict[str, object]) -> Graph:
    boxes = {box["box"]["id"]: box["box"] for box in patcher["boxes"]}  # type: ignore[index]
    outgoing: dict[str, set[int]] = collections.defaultdict(set)
    incoming: dict[str, set[int]] = collections.defaultdict(set)
    for line in patcher["lines"]:  # type: ignore[index]
        source = line["patchline"]["source"]
        target = line["patchline"]["destination"]
        outgoing[source[0]].add(source[1])
        incoming[target[0]].add(target[1])
    return boxes, outgoing, incoming


def text_of(box: dict) -> str:
    return str(box.get("text") or box.get("maxclass") or "")


def test_the_committed_device_is_what_the_generator_writes() -> None:
    """A hand edit to either file, or a protocol change without a rebuild, fails here."""
    finished = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_analyzer_device.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr


def test_the_container_is_the_one_live_loads(patcher: dict[str, object]) -> None:
    raw = AMXD.read_bytes()
    # Read out of Live's own devices: 'ampf', a length, then the type code, and
    # the audio effects carry 'aaaa'. A file that is only JSON has none of this.
    assert raw[:4] == b"ampf"
    assert struct.unpack("<I", raw[4:8])[0] == 4
    assert raw[8:12] == b"aaaa"
    assert raw[12:16] == b"meta"
    assert raw[24:28] == b"ptch"
    assert raw[-1:] == b"\0", "the patcher chunk is NUL terminated and counted with it"
    project = patcher["project"]
    assert isinstance(project, dict)
    assert project["amxdtype"] == int.from_bytes(b"aaaa", "big")


def test_the_reviewable_copy_holds_the_same_graph(patcher: dict[str, object]) -> None:
    """The ``.maxpat`` exists so a diff can show what changed in the device."""
    assert json.loads(MAXPAT.read_text(encoding="utf-8"))["patcher"] == patcher


def test_every_object_is_one_this_project_verified_exists(patcher: dict[str, object]) -> None:
    """``ebur128~`` was not a Max object, and Max draws a missing one as a broken box."""
    for entry in patcher["boxes"]:  # type: ignore[index]
        box = entry["box"]
        if box["maxclass"] != "newobj":
            continue
        head = text_of(box).split()[0]
        assert head in builder.SHAPES, (
            f"{head!r} is not in build_analyzer_device.SHAPES. Every entry there "
            f"was looked up in Max's refpages; an object Max does not have "
            f"measures nothing and reports no error."
        )


def test_the_loudness_object_is_the_one_that_exists(patcher: dict[str, object]) -> None:
    heads = {text_of(entry["box"]).split()[0] for entry in patcher["boxes"]}  # type: ignore[index]
    assert "loudness~" in heads
    assert "ebur128~" not in heads


def test_no_measurement_ends_in_a_void(
    graph: Graph,
) -> None:
    """The defect that made the first device unable to report anything."""
    boxes, outgoing, _ = graph
    stranded = [
        text_of(box)
        for box_id, box in boxes.items()
        if box["maxclass"] == "newobj"
        and not text_of(box).startswith(TERMINAL)
        and not outgoing[box_id]
    ]
    assert stranded == [], f"these objects produce a value nobody reads: {stranded}"


def test_nothing_is_left_unfed(
    graph: Graph,
) -> None:
    boxes, _, incoming = graph
    # Objects that produce without being asked: the audio input, the socket, the
    # load notification, and the reference oscillator.
    sources = ("plugin~", "udpreceive", "live.thisdevice", "cycle~")
    orphans = [
        text_of(box)
        for box_id, box in boxes.items()
        if box["maxclass"] in ("newobj", "message")
        and not text_of(box).startswith(sources)
        and not incoming[box_id]
    ]
    assert orphans == [], f"these objects can never run: {orphans}"


def test_no_patch_cord_names_an_inlet_or_outlet_the_box_does_not_have(
    graph: Graph,
) -> None:
    """Max can drop a cord whose index is past the stored count, silently."""
    boxes, outgoing, incoming = graph
    for box_id, box in boxes.items():
        for index in outgoing[box_id]:
            assert index < box["numoutlets"], f"{text_of(box)!r} outlet {index}"
        for index in incoming[box_id]:
            assert index < box["numinlets"], f"{text_of(box)!r} inlet {index}"


def test_the_device_listens_where_the_client_sends(patcher: dict[str, object]) -> None:
    """Both ends take the port and both addresses from one contract."""
    texts = [text_of(entry["box"]) for entry in patcher["boxes"]]  # type: ignore[index]
    assert f"udpreceive {protocol.DEFAULT_DEVICE_PORT}" in texts
    assert f"route {protocol.REQUEST_ADDRESS}" in texts
    assert f"prepend {protocol.REPLY_ADDRESS}" in texts
    # The sender's creation argument is only a placeholder: every request carries
    # the port to answer on, and a 'port' message aims it before it is used.
    assert any(text.startswith("udpsend 127.0.0.1 ") for text in texts)
    assert "prepend port" in texts


def test_the_reply_has_a_slot_for_every_declared_field(
    graph: Graph,
) -> None:
    boxes, _, incoming = graph
    packs = [
        (box_id, box) for box_id, box in boxes.items() if text_of(box).startswith("pack ")
    ]
    assert len(packs) == 1, "one reply, one pack"
    box_id, box = packs[0]
    declared = len(protocol.NUMERIC_REPLY_FIELDS) + len(protocol.STRING_REPLY_FIELDS)
    assert box["numinlets"] == declared
    assert incoming[box_id] == set(range(declared)), (
        "every slot must be written before the packet goes out, or the reply "
        "carries a pack default where a measurement belongs"
    )


def test_the_schema_the_device_sends_is_the_one_the_client_reads(
    patcher: dict[str, object],
) -> None:
    messages = {
        text_of(entry["box"])
        for entry in patcher["boxes"]  # type: ignore[index]
        if entry["box"]["maxclass"] == "message"
    }
    assert str(protocol.REPLY_SCHEMA) in messages


def test_each_band_edge_is_two_sections_at_the_declared_frequency(
    patcher: dict[str, object],
) -> None:
    filters = collections.Counter(
        text_of(entry["box"])  # type: ignore[index]
        for entry in patcher["boxes"]  # type: ignore[index]
        if text_of(entry["box"]).startswith("svf~ ")
    )
    for _, low, high in protocol.BANDS:
        assert filters[f"svf~ {low:g} 0"] >= 2, f"no high pass pair at {low} Hz"
        if high is not None:
            assert filters[f"svf~ {high:g} 0"] >= 2, f"no low pass pair at {high} Hz"
    assert filters[f"svf~ {protocol.LOW_BAND_HZ:g} 0"] == 4, (
        "the low band correlation needs two sections on each of the two channels"
    )


def test_one_average_and_one_snapshot_per_averaged_field(
    patcher: dict[str, object],
) -> None:
    heads = collections.Counter(
        text_of(entry["box"]).split()[0]  # type: ignore[index]
        for entry in patcher["boxes"]  # type: ignore[index]
    )
    # Three broadband figures, three below 120 Hz, one per band.
    expected = 3 + 3 + len(protocol.BANDS)
    assert heads["average~"] == expected
    assert heads["snapshot~"] == expected


def test_every_average_holds_the_declared_ceiling(patcher: dict[str, object]) -> None:
    for entry in patcher["boxes"]:  # type: ignore[index]
        text = text_of(entry["box"])
        if text.startswith("average~ "):
            assert text.split()[1] == str(protocol.MAX_AVERAGING_SAMPLES)


def test_the_audio_passes_through_untouched(
    graph: Graph,
) -> None:
    """A meter that colours the signal is not a meter."""
    boxes, _, _ = graph
    source = next(i for i, b in boxes.items() if text_of(b).startswith("plugin~"))
    sink = next(i for i, b in boxes.items() if text_of(b).startswith("plugout~"))
    cords = {
        (line["patchline"]["source"][1], line["patchline"]["destination"][1])
        for line in _lines(boxes, source, sink)
    }
    assert cords == {(0, 0), (1, 1)}, "left to left and right to right, nothing between"


def _lines(boxes: dict[str, dict], source: str, sink: str) -> list[dict]:
    patcher = json.loads(MAXPAT.read_text(encoding="utf-8"))["patcher"]
    return [
        line
        for line in patcher["lines"]
        if line["patchline"]["source"][0] == source
        and line["patchline"]["destination"][0] == sink
    ]
