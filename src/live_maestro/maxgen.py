"""Generate Max for Live devices: the patcher graph, the container and the CLI.

An ``.amxd`` is a chunk container around patcher JSON: a list of boxes, a list of patch
cords and a block of editor settings. The parts every generated device needs live here,
and a generator script supplies only its own graph and a :class:`DeviceBuild`:

* :data:`SHAPES`, the inlet and outlet counts of every object class used,
* :class:`Patch`, which adds boxes, connects them and writes the patcher dictionary,
* :func:`maxpat_bytes` and :func:`amxd_bytes`, the two files a device is committed as,
* :class:`DeviceBuild` and :func:`main`, the ``--out`` and ``--check`` command line.

It ships in the wheel rather than under ``scripts/`` because a build script cannot be
imported. Object names are the one thing unchecked here: Max draws a name it does not
know as a broken box and reports no error, so every :data:`SHAPES` entry was looked up
in the refpages of the Max that ships with Ableton Live 12 Suite.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The four byte code in an ``.amxd``'s first chunk that tells Live what kind of
#: device this is. Read out of Live's own factory devices: the audio effects carry
#: ``aaaa`` and the instruments carry ``iiii``.
AMXD_AUDIO_EFFECT = b"aaaa"

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
    # Counts read out of Max's refpages under Ableton Live 12 Suite: buffer~ bangs its
    # right outlet when a read or a write finishes, info~ reports ten values right to
    # left with the sample rate last, and the operator objects are documented under
    # their spelled out file names (equals~, modulo~) rather than "==~" and "%~".
    "buffer~": (1, 2, ""),
    "info~": (1, 10, ""),
    "sfrecord~": (1, 1, "signal"),
    "count~": (2, 1, "signal"),
    "%~": (2, 1, "signal"),
    "==~": (2, 1, "signal"),
    "zl": (2, 2, ""),
    "int": (2, 1, "int"),
    "loadbang": (1, 1, "bang"),
    "plugsync~": (1, 9, ""),
    "sig~": (1, 1, "signal"),
    "==": (2, 1, "int"),
}


def type_code_int(device_type: bytes) -> int:
    """The device type code as the integer Max stores in the patcher's ``project``.

    0x61616161 is ``aaaa`` read as a big endian integer.
    """
    return int.from_bytes(device_type, "big")


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
        """Add a comment box and return its id."""
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

    def to_patcher(
        self,
        description: str,
        *,
        title: str,
        device_type: bytes = AMXD_AUDIO_EFFECT,
    ) -> dict[str, Any]:
        """The finished patcher dictionary, boxes and cords inside the editor block."""
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
            "title": title,
            "assistshowspatchername": 0,
            "dependency_cache": [],
            "autosave": 0,
            "boxes": self.boxes,
            "lines": self.lines,
            # Max writes this block into every device it saves. amxdtype is the
            # device type code, and Live reads it to know what the device is.
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
                "amxdtype": type_code_int(device_type),
                "readonly": 0,
                "devpathtype": 0,
                "devpath": ".",
                "sortmode": 0,
            },
        }


def maxpat_bytes(patcher: dict[str, Any]) -> bytes:
    """The patcher as a ``.maxpat`` file: the same JSON Max reads and writes."""
    return (json.dumps({"patcher": patcher}, indent=1) + "\n").encode("utf-8")


def amxd_bytes(patcher: dict[str, Any], device_type: bytes = AMXD_AUDIO_EFFECT) -> bytes:
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
            struct.pack("<I", len(device_type)),
            device_type,
            b"meta",
            struct.pack("<I", 4),
            b"\0\0\0\0",
            b"ptch",
            struct.pack("<I", len(body)),
            body,
        )
    )


@dataclass(frozen=True)
class DeviceBuild:
    """One generated device: what it is called, what writes it and how it is built.

    ``free_running`` and ``terminal`` are the graph's two deliberate exceptions and
    the generator is the only place that knows them. ``free_running`` names the boxes
    that produce without being asked, such as the audio input and the socket, and
    ``terminal`` names the boxes a chain is allowed to end at. Everything else in a
    working device is both fed and read, and the device tests check exactly that.
    """

    #: File stem of both output files, and the name Live shows.
    name: str
    #: Returns the finished patcher dictionary. Takes no arguments on purpose: a
    #: generated device has no settings, or the file on disk would not be one thing.
    build: Callable[[], dict[str, Any]]
    #: The script that owns this device, repository relative, for error messages.
    generator: str
    #: The module the ports, addresses and field orders are read from, repository
    #: relative, so ``--help`` can name the file a caller would go and read.
    contract: str
    #: Where the two files are written unless ``--out`` says otherwise.
    out_default: Path
    #: Printed paths are shortened against this root when they sit inside it.
    display_root: Path | None = None
    device_type: bytes = AMXD_AUDIO_EFFECT
    free_running: tuple[str, ...] = ()
    terminal: tuple[str, ...] = ()

    def files(self, patcher: dict[str, Any], out: Path) -> dict[Path, bytes]:
        """The two files this device is committed as, by path."""
        return {
            out / f"{self.name}.maxpat": maxpat_bytes(patcher),
            out / f"{self.name}.amxd": amxd_bytes(patcher, self.device_type),
        }


def _shown(path: Path, root: Path | None) -> str:
    """The path as a reader wants to see it: relative to the repository, or whole.

    ``Path.relative_to`` raises for a path outside the root, and a build that wrote
    both files correctly must not report failure because of how its own output line
    is spelled.
    """
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            pass
    return str(path)


def main(spec: DeviceBuild, argv: list[str] | None = None) -> int:
    """Run the ``--out`` and ``--check`` command line for one device.

    Returns 0 when the files were written, or when ``--check`` found both of them
    matching the generator. Returns 1 only when ``--check`` found a difference,
    which is the one thing here that is a failure.
    """
    parser = argparse.ArgumentParser(
        prog=f"python {spec.generator}",
        description=(
            f"Generate {spec.name}.maxpat and {spec.name}.amxd from the contract "
            f"in {spec.contract}."
        ),
    )
    parser.add_argument("--out", type=Path, default=spec.out_default, metavar="DIR",
                        help=f"where to write (default {spec.out_default.name}/)")
    parser.add_argument("--check", action="store_true",
                        help="verify the files on disk match this script, write nothing")
    args = parser.parse_args(argv)

    patcher = spec.build()
    written = spec.files(patcher, args.out)

    if args.check:
        stale = [
            path.name
            for path, content in written.items()
            if not path.is_file() or path.read_bytes() != content
        ]
        if stale:
            print(
                f"out of date with {spec.generator}: "
                + ", ".join(sorted(stale))
                + f"\nrun: python {spec.generator}",
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
        print(f"  -> {_shown(path, spec.display_root)}  {path.stat().st_size} bytes")
    return 0
