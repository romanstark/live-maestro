"""Generate the Max for Live device that answers ``analyze_audio``.

The device is generated rather than drawn, for two reasons.

The first is that the protocol lives in
``src/live_maestro/analyzer/protocol.py`` and this script reads it. The reply
field order, the band edges, the listening port and the averaging ceiling all come
from there, so a device and a client that disagree about any of them cannot be
built. A patcher drawn by hand carries its own copy of every number in it, and a
copy is free to be wrong.

The second is that a ``.amxd`` is a container around patcher JSON, and a container
is not reviewable. The generated ``.maxpat`` beside it is the same graph in a form
``git diff`` can show, and ``--check`` fails when either file stopped matching this
script. A test runs that, so the device in the repository is always the device this
script describes.

Every object used here was verified to exist in the Max that ships with Ableton
Live 12 Suite, by looking it up in ``Resources/Max/resources/docs/refpages``. Max
draws an object name it does not know as a broken box, reports no error, and
measures nothing, so an unverified name is a silent failure. ``loudness~`` is the
object that reports EBU R128 loudness, and its six outlets are the whole loudness
and peak section of the reply. There is no ``ebur128~`` in Max.

Usage:
    python scripts/build_analyzer_device.py
    python scripts/build_analyzer_device.py --check
    python scripts/build_analyzer_device.py --out some/other/dir
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from live_maestro.analyzer.protocol import (
    BANDS,
    CALIBRATION_AMPLITUDE,
    CALIBRATION_HZ,
    DEFAULT_DEVICE_PORT,
    LOW_BAND_HZ,
    MAX_AVERAGING_SAMPLES,
    NUMERIC_REPLY_FIELDS,
    REPLY_ADDRESS,
    REPLY_SCHEMA,
    REQUEST_ADDRESS,
    STRING_REPLY_FIELDS,
)

DEVICE_NAME = "LiveMaestro_Analyzer"
DEFAULT_OUT = REPO_ROOT / "devices"

#: The four byte code in an ``.amxd``'s first chunk that tells Live what kind of
#: device this is. Read out of Live's own factory devices: the audio effects carry
#: ``aaaa`` and the instruments carry ``iiii``.
AMXD_AUDIO_EFFECT = b"aaaa"

#: The same code as the integer Max stores in the patcher's ``project`` entry.
#: 0x61616161 is ``aaaa`` read as a big endian integer.
AMXD_TYPE_INT = int.from_bytes(AMXD_AUDIO_EFFECT, "big")

#: ``svf~``'s resonance inlet takes 0 to 1, not a Q. Zero is the flattest setting
#: it offers, and two sections at zero are what each band edge is built from. This
#: is a 24 dB per octave state variable slope, not a calibrated Linkwitz-Riley
#: crossover, and ``docs/`` says so where the band values are described.
EDGE_RESONANCE = 0.0

#: How many inlets and outlets each object class draws with, and what its outlets
#: carry. Max re-derives all of this when it instantiates the object, but a stored
#: count lower than an index a patch cord uses can lose that cord on load, so the
#: counts here are floors that :func:`Patch.link` raises when it has to.
SHAPES: dict[str, tuple[int, int, str]] = {
    "plugin~": (2, 2, "signal"),
    "plugout~": (2, 2, "signal"),
    "loudness~": (2, 6, "float"),
    "svf~": (3, 4, "signal"),
    "*~": (2, 1, "signal"),
    "+~": (2, 1, "signal"),
    "average~": (1, 1, "signal"),
    "snapshot~": (1, 1, "float"),
    "udpreceive": (1, 1, ""),
    "udpsend": (1, 0, ""),
    "route": (1, 2, ""),
    "unpack": (1, 4, "int"),
    "prepend": (1, 1, ""),
    "select": (2, 2, ""),
    "expr": (2, 1, ""),
    "dspstate~": (1, 4, ""),
    "cycle~": (2, 1, "signal"),
    "pack": (1, 1, ""),
    "trigger": (1, 3, ""),
    "t": (1, 3, ""),
    "live.thisdevice": (1, 3, "bang"),
    "live.path": (1, 3, ""),
    "live.object": (2, 2, ""),
    "tosymbol": (1, 1, ""),
}


class Patch:
    """A Max patcher under construction.

    Boxes are added in reading order and laid out in columns. The layout carries
    no meaning for Max, but a patch nobody can open is a patch nobody can debug.
    """

    def __init__(self) -> None:
        self.boxes: list[dict[str, Any]] = []
        self.lines: list[dict[str, Any]] = []
        self._next = 1
        self._column = 0
        self._row = 0

    def _place(self, width: float) -> list[float]:
        x = 24.0 + self._column * 232.0
        y = 40.0 + self._row * 28.0
        self._row += 1
        if self._row > 22:
            self._row = 0
            self._column += 1
        return [x, y, width, 20.0]

    def obj(self, text: str, *, inlets: int | None = None, outlets: int | None = None) -> str:
        """Add an object box and return its id."""
        head = text.split()[0]
        shape = SHAPES.get(head)
        if shape is None:
            raise KeyError(
                f"{head!r} has no entry in SHAPES. Add it only after looking the "
                f"object up in Max's refpages: an object Max does not have draws "
                f"as a broken box and measures nothing."
            )
        base_in, base_out, kind = shape
        box_id = f"obj-{self._next}"
        self._next += 1
        self.boxes.append(
            {
                "box": {
                    "id": box_id,
                    "maxclass": "newobj",
                    "numinlets": inlets if inlets is not None else base_in,
                    "numoutlets": outlets if outlets is not None else base_out,
                    "outlettype": [kind] * (outlets if outlets is not None else base_out),
                    "patching_rect": self._place(max(72.0, 7.0 * len(text))),
                    "text": text,
                }
            }
        )
        return box_id

    def message(self, text: str) -> str:
        """Add a message box and return its id."""
        box_id = f"obj-{self._next}"
        self._next += 1
        self.boxes.append(
            {
                "box": {
                    "id": box_id,
                    "maxclass": "message",
                    "numinlets": 2,
                    "numoutlets": 1,
                    "outlettype": [""],
                    "patching_rect": self._place(max(56.0, 7.0 * len(text))),
                    "text": text,
                }
            }
        )
        return box_id

    def comment(self, text: str) -> str:
        box_id = f"obj-{self._next}"
        self._next += 1
        self.boxes.append(
            {
                "box": {
                    "id": box_id,
                    "maxclass": "comment",
                    "numinlets": 1,
                    "numoutlets": 0,
                    "patching_rect": self._place(max(120.0, 6.5 * len(text))),
                    "text": text,
                }
            }
        )
        return box_id

    def _box(self, box_id: str) -> dict[str, Any]:
        for entry in self.boxes:
            if entry["box"]["id"] == box_id:
                return entry["box"]
        raise KeyError(box_id)

    def link(self, source: str, outlet: int, target: str, inlet: int) -> None:
        """Connect an outlet to an inlet, widening either box if the index needs it."""
        src = self._box(source)
        dst = self._box(target)
        if outlet + 1 > src["numoutlets"]:
            src["numoutlets"] = outlet + 1
            kind = src.get("outlettype") or [""]
            src["outlettype"] = (list(kind) + [kind[-1]] * (outlet + 1))[: outlet + 1]
        dst["numinlets"] = max(dst["numinlets"], inlet + 1)
        self.lines.append(
            {"patchline": {"destination": [target, inlet], "source": [source, outlet]}}
        )

    def to_patcher(self, description: str) -> dict[str, Any]:
        return {
            "fileversion": 1,
            "appversion": {
                "major": 8,
                "minor": 5,
                "revision": 0,
                "architecture": "x64",
                "modernui": 1,
            },
            "classnamespace": "box",
            "rect": [40.0, 90.0, 1000.0, 700.0],
            "bglocked": 0,
            "openinpresentation": 0,
            "default_fontsize": 10.0,
            "default_fontface": 0,
            "default_fontname": "Arial Bold",
            "gridonopen": 1,
            "gridsize": [8.0, 8.0],
            "gridsnaponopen": 1,
            "objectsnaponopen": 1,
            "statusbarvisible": 2,
            "toolbarvisible": 1,
            "lefttoolbarpinned": 0,
            "toptoolbarpinned": 0,
            "righttoolbarpinned": 0,
            "bottomtoolbarpinned": 0,
            "toolbars_unpinned_last_save": 0,
            "tallnewobj": 0,
            "boxanimatetime": 200,
            "enablehscroll": 1,
            "enablevscroll": 1,
            "devicewidth": 0.0,
            "description": description,
            "digest": "",
            "tags": "",
            "style": "",
            "subpatcher_template": "",
            "title": "LiveMaestro Analyzer",
            "assistshowspatchername": 0,
            "dependency_cache": [],
            "autosave": 0,
            "boxes": self.boxes,
            "lines": self.lines,
            # Max writes this block into every device it saves. amxdtype is the
            # audio effect code, and Live reads it to know what the device is.
            "project": {
                "version": 1,
                "creationdate": 3605254137,
                "modificationdate": 3605254137,
                "viewrect": [0.0, 0.0, 300.0, 500.0],
                "autoorganize": 1,
                "hideprojectwindow": 1,
                "showdependencies": 1,
                "autolocalize": 0,
                "contents": {"patchers": {}},
                "layout": {},
                "searchpath": {},
                "detailsvisible": 0,
                "amxdtype": AMXD_TYPE_INT,
                "readonly": 0,
                "devpathtype": 0,
                "devpath": ".",
                "sortmode": 0,
            },
        }


def _edge(patch: Patch, source: str, outlet: int, hz: float, wanted: str) -> tuple[str, int]:
    """Two cascaded ``svf~`` sections forming one band edge, and where it comes out.

    ``wanted`` is ``"low"`` or ``"high"``, naming which of ``svf~``'s four outlets
    to take. Its reference page lists them as low pass, high pass, band pass and
    notch, in that order.
    """
    index = 0 if wanted == "low" else 1
    for _ in range(2):
        section = patch.obj(f"svf~ {hz:g} {EDGE_RESONANCE:g}")
        patch.link(source, outlet, section, 0)
        source, outlet = section, index
    return source, outlet


def build() -> dict[str, Any]:
    """Assemble the whole device graph."""
    patch = Patch()
    slot_of = {
        name: index
        for index, name in enumerate(NUMERIC_REPLY_FIELDS + STRING_REPLY_FIELDS)
    }

    patch.comment(
        "LiveMaestro Analyzer. Generated by scripts/build_analyzer_device.py. "
        "Edit that script, not this patch."
    )
    patch.comment(
        f"Listens for OSC {REQUEST_ADDRESS} on UDP {DEFAULT_DEVICE_PORT} and answers "
        f"{REPLY_ADDRESS} to the port the request names."
    )

    # ---- audio path. The device measures and must not colour anything.
    plugin = patch.obj("plugin~ 1 2")
    plugout = patch.obj("plugout~ 1 2")
    patch.link(plugin, 0, plugout, 0)
    patch.link(plugin, 1, plugout, 1)

    # ---- the reply. pack fires on its leftmost inlet only, which is what makes
    # one request produce exactly one packet: every measurement lands in a slot
    # first, and the schema arrives last.
    slots: list[str] = []
    for name in NUMERIC_REPLY_FIELDS:
        integral = name in (
            "schema", "request_id", "samplerate", "window_samples", "track_id",
        )
        slots.append("0" if integral else "0.")
    slots += ["unknown"] * len(STRING_REPLY_FIELDS)
    reply = patch.obj("pack " + " ".join(slots), inlets=len(slots))
    address = patch.obj(f"prepend {REPLY_ADDRESS}")
    sender = patch.obj(f"udpsend 127.0.0.1 {DEFAULT_DEVICE_PORT + 1}")
    patch.link(reply, 0, address, 0)
    patch.link(address, 0, sender, 0)

    # ---- loudness and peaks. Six outlets, in the order the refpage lists them.
    loudness = patch.obj("loudness~ 2")
    patch.link(plugin, 0, loudness, 0)
    patch.link(plugin, 1, loudness, 1)
    for outlet, name in enumerate(
        (
            "momentary_lufs",
            "short_term_lufs",
            "integrated_lufs",
            "lra_lu",
            "sample_peak_db",
            "true_peak_db",
        )
    ):
        patch.link(loudness, outlet, reply, slot_of[name])

    # ---- averaged measurements. Each one is an average~ read by a snapshot~ on
    # the measurement bang, so every value in a reply is taken at one instant.
    banged: list[str] = [loudness]

    def averaged(source: str, outlet: int, mode: str, field: str) -> None:
        average = patch.obj(f"average~ {MAX_AVERAGING_SAMPLES} {mode}")
        patch.link(source, outlet, average, 0)
        reader = patch.obj("snapshot~")
        patch.link(average, 0, reader, 0)
        patch.link(reader, 0, reply, slot_of[field])
        banged.append(reader)
        windowed.append(average)

    windowed: list[str] = []

    averaged(plugin, 0, "rms", "rms_l")
    averaged(plugin, 1, "rms", "rms_r")
    product = patch.obj("*~")
    patch.link(plugin, 0, product, 0)
    patch.link(plugin, 1, product, 1)
    averaged(product, 0, "bipolar", "mean_lr")

    # ---- the same three below LOW_BAND_HZ, which is the mono compatibility
    # question. Correlation needs the sign of the product, so that one average
    # stays bipolar while the two levels are RMS.
    low_left = _edge(patch, plugin, 0, LOW_BAND_HZ, "low")
    low_right = _edge(patch, plugin, 1, LOW_BAND_HZ, "low")
    averaged(*low_left, "rms", "rms_low_l")
    averaged(*low_right, "rms", "rms_low_r")
    low_product = patch.obj("*~")
    patch.link(*low_left, low_product, 0)
    patch.link(*low_right, low_product, 1)
    averaged(low_product, 0, "bipolar", "mean_low_lr")

    # ---- band energies, measured on the mono sum. A band level is a statement
    # about the spectrum, and halving the filter count costs nothing there.
    total = patch.obj("+~")
    patch.link(plugin, 0, total, 0)
    patch.link(plugin, 1, total, 1)
    mono = patch.obj("*~ 0.5")
    patch.link(total, 0, mono, 0)
    for field, low, high in BANDS:
        tail = _edge(patch, mono, 0, low, "high")
        if high is not None:
            tail = _edge(patch, *tail, high, "low")
        averaged(*tail, "rms", field)

    # ---- the device's own reference. A sine of known amplitude into a second
    # loudness~, wired nowhere near plugout~, so every reply carries what this
    # build of Max reports for a peak whose value is already known. That turns a
    # claim about what a peak outlet means into a measurement taken on the same
    # call, at whatever sample rate Live is running.
    tone = patch.obj(f"cycle~ {CALIBRATION_HZ:g}")
    scaled = patch.obj(f"*~ {CALIBRATION_AMPLITUDE:g}")
    patch.link(tone, 0, scaled, 0)
    reference = patch.obj("loudness~ 2")
    patch.link(scaled, 0, reference, 0)
    patch.link(scaled, 0, reference, 1)
    for outlet, field in (
        (0, "cal_momentary_lufs"),
        (4, "cal_sample_peak_db"),
        (5, "cal_true_peak_db"),
    ):
        patch.link(reference, outlet, reply, slot_of[field])
    banged.append(reference)

    # ---- request handling. unpack outputs right to left, so the fields arrive in
    # the reverse of their declared order: the reply port is set before the window
    # is applied, and the window before anything is read.
    receiver = patch.obj(f"udpreceive {DEFAULT_DEVICE_PORT}")
    filter_address = patch.obj(f"route {REQUEST_ADDRESS}")
    fields = patch.obj("unpack 0 0 0 0")
    patch.link(receiver, 0, filter_address, 0)
    patch.link(filter_address, 0, fields, 0)

    # reply_port: aim the sender, then refresh the sample rate the window needs.
    aim = patch.obj("t b i")
    patch.link(fields, 3, aim, 0)
    port_message = patch.obj("prepend port")
    patch.link(aim, 1, port_message, 0)
    patch.link(port_message, 0, sender, 0)
    # dspstate~ answers a bang with the audio state on four outlets, of which
    # outlet 1 is the current sample rate. It is asked on every request rather
    # than cached, because Live can change the rate while the device is loaded.
    rate = patch.obj("dspstate~")
    patch.link(aim, 0, rate, 0)
    patch.link(rate, 1, reply, slot_of["samplerate"])

    # reset: restart the integrated loudness. The reply that carries a reset
    # reports an integration that just began, and the client drops it.
    reset_gate = patch.obj("select 1")
    patch.link(fields, 2, reset_gate, 0)
    reset_message = patch.message("reset")
    patch.link(reset_gate, 0, reset_message, 0)
    patch.link(reset_message, 0, loudness, 0)
    patch.link(reset_message, 0, reference, 0)

    # window_ms: to samples, clamped to what the averaging buffers hold, then out
    # to every average~ and into the reply so the caller learns what it got.
    window = patch.obj(
        f"expr int(min(max($f1*$f2/1000.,64.),{MAX_AVERAGING_SAMPLES}.))"
    )
    patch.link(fields, 1, window, 0)
    patch.link(rate, 1, window, 1)
    patch.link(window, 0, reply, slot_of["window_samples"])
    for average in windowed:
        patch.link(window, 0, average, 0)

    # request_id: the last field out of unpack, and the one that triggers the
    # measurement. Right to left again: the id is stored, then everything is read,
    # then the schema fires the packet.
    finish = patch.obj("t b b i")
    patch.link(fields, 0, finish, 0)
    patch.link(finish, 2, reply, slot_of["request_id"])
    for target in banged:
        patch.link(finish, 1, target, 0)
    schema = patch.message(str(REPLY_SCHEMA))
    patch.link(finish, 0, schema, 0)
    patch.link(schema, 0, reply, slot_of["schema"])

    # ---- which track this instance sits on. Reported, never matched here: the
    # client gathers every reply and chooses, so a set with three analyzers
    # answers three times and the client says which is which.
    #
    # The order matters and a single outlet does not fix it. Max does not define
    # which of two cords from one outlet fires first, so asking for the name and
    # setting the object it names have to be sequenced: trigger fires right to
    # left, so the path is resolved before the name is requested.
    identity = patch.obj("t b b")
    loaded = patch.obj("live.thisdevice")
    patch.link(loaded, 0, identity, 0)

    ask_path = patch.message("path this_device canonical_parent")
    path = patch.obj("live.path")
    patch.link(identity, 1, ask_path, 0)
    patch.link(ask_path, 0, path, 0)

    # live.path answers on three outlets and its own messages carry a selector,
    # so the selector decides which value is which. An outlet index would be a
    # guess about a layout that is not documented outlet by outlet.
    sort = patch.obj("route path id")
    for outlet in range(3):
        patch.link(path, outlet, sort, 0)

    # The id is what live.path gives. Measured 2026-09-07 against Live 12.4.5: no
    # outlet of it carries a canonical path, for either "canonical_parent" or
    # "this_device", so the reply reports the id and the name and leaves turning a
    # caller's index into a name to Channel A, which can read the set.
    #
    # The id goes two ways: into live.object, which is how the name is fetched,
    # and into the reply, where it tells two tracks of the same name apart.
    patch.link(sort, 1, reply, slot_of["track_id"])

    track = patch.obj("live.object")
    as_id = patch.obj("prepend id")
    patch.link(sort, 1, as_id, 0)
    patch.link(as_id, 0, track, 1)

    ask_name = patch.message("get name")
    patch.link(identity, 0, ask_name, 0)
    patch.link(ask_name, 0, track, 0)
    name_out = patch.obj("route name")
    patch.link(track, 0, name_out, 0)
    name_symbol = patch.obj("tosymbol")
    patch.link(name_out, 0, name_symbol, 0)
    patch.link(name_symbol, 0, reply, slot_of["track_name"])

    # Asked again on every measurement, so a renamed track is not reported under
    # the name it carried when the device was loaded.
    patch.link(finish, 1, identity, 0)

    return patch.to_patcher(
        "LiveMaestro Analyzer: on-demand loudness, peak, phase and band "
        "measurement for Live Maestro's analyze_audio tool."
    )


def maxpat_bytes(patcher: dict[str, Any]) -> bytes:
    """The patcher as a ``.maxpat`` file: the same JSON Max reads and writes."""
    return (json.dumps({"patcher": patcher}, indent=1) + "\n").encode("utf-8")


def amxd_bytes(patcher: dict[str, Any]) -> bytes:
    """Wrap the patcher in the chunk container Live loads.

    Read out of Live's own devices and confirmed against
    ``Resources/Max/examples/resources/fourin_fourout.amxd``, which is the one
    unencrypted device that ships with Live 12 Suite:

    * ``ampf`` + uint32 little endian length + the device type code
    * ``meta`` + length + four zero bytes
    * ``ptch`` + length + the patcher JSON, NUL terminated and counted with it

    A file holding only the JSON has no type code, so Live cannot tell an audio
    effect from an instrument, and the device does not appear at all.
    """
    body = json.dumps({"patcher": patcher}, indent=1).encode("utf-8") + b"\0"
    return b"".join(
        (
            b"ampf",
            struct.pack("<I", len(AMXD_AUDIO_EFFECT)),
            AMXD_AUDIO_EFFECT,
            b"meta",
            struct.pack("<I", 4),
            b"\0\0\0\0",
            b"ptch",
            struct.pack("<I", len(body)),
            body,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/build_analyzer_device.py",
        description=(
            "Generate LiveMaestro_Analyzer.maxpat and .amxd from the protocol "
            "in src/live_maestro/analyzer/protocol.py."
        ),
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, metavar="DIR",
                        help=f"where to write (default {DEFAULT_OUT.name}/)")
    parser.add_argument("--check", action="store_true",
                        help="verify the files on disk match this script, write nothing")
    args = parser.parse_args(argv)

    patcher = build()
    written = {
        args.out / f"{DEVICE_NAME}.maxpat": maxpat_bytes(patcher),
        args.out / f"{DEVICE_NAME}.amxd": amxd_bytes(patcher),
    }

    if args.check:
        stale = [
            path.name
            for path, content in written.items()
            if not path.is_file() or path.read_bytes() != content
        ]
        if stale:
            print(
                "out of date with scripts/build_analyzer_device.py: "
                + ", ".join(sorted(stale))
                + "\nrun: python scripts/build_analyzer_device.py",
                file=sys.stderr,
            )
            return 1
        print(f"{len(written)} file(s) match the generator")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for path, content in written.items():
        path.write_bytes(content)
    boxes = len(patcher["boxes"])
    lines = len(patcher["lines"])
    print(f"{boxes} boxes, {lines} patch cords")
    for path in sorted(written):
        print(f"  -> {path.relative_to(REPO_ROOT)}  {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
