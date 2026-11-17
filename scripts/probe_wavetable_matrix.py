"""Measure Wavetable's modulation matrix on a running Live, in a scratch set only.

Run after the Remote Script is installed and Live restarted, with Wavetable on a
fresh MIDI track of a new, unsaved or named scratch set. The probe records the
target rows, the source columns and Live's names for them, then writes one route
(a source column to the filter frequency row), reads it back at +0.5, -0.5 and a
deliberately out-of-range 1.5, and sets the cell back to its first value. It
refuses any saved set whose file name was not passed with --allow-set.

    python scripts/probe_wavetable_matrix.py --track 3 --device 0 --source 2 --go
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from live_maestro import wavetable
from live_maestro.client import LiveClient
from live_maestro.lom import introspect

#: Values written to the probed cell: two inside the range, one beyond it.
PROBE_VALUES = (0.5, -0.5, 1.5)


def main() -> int:
    """Run the probe and write its JSON record; return the process exit code."""
    parser = argparse.ArgumentParser(description="Measure Wavetable's modulation matrix.")
    parser.add_argument("--track", type=int, required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--source", type=int, default=None,
                        help="source column to route; default: the first LFO Live names")
    parser.add_argument("--target", default="Filter 1 Freq",
                        help="row name, added to the matrix when it is missing")
    parser.add_argument("--allow-set", default="",
                        help="file name of the scratch set when it is saved")
    parser.add_argument("--try-insert", action="store_true",
                        help="also call Track.insert_device('Wavetable', 0) on the track")
    parser.add_argument("--out", type=Path,
                        default=_REPO_ROOT / "scratch" / "probe-wavetable-matrix.json")
    parser.add_argument("--go", action="store_true", help="without it, print the plan only")
    args = parser.parse_args()
    if not args.go:
        print(f"Plan: track {args.track}, device {args.device}, target {args.target!r}, "
              f"values {PROBE_VALUES}; pass --go to run.")
        return 0

    client = LiveClient()
    head = f"song.tracks[{args.track}].devices[{args.device}]"
    record: dict = {"recorded": datetime.now().astimezone().isoformat(),
                    "script": client.script_info()}
    path = str(client.get("song.file_path").get("value") or "")
    if path and Path(path).name != args.allow_set:
        print(f"Refused: the open set is {Path(path).name!r}. Use a scratch set.")
        return 2
    record["set"] = Path(path).name or "(unsaved)"
    if args.try_insert:
        record["insert_device"] = client.call(f"song.tracks[{args.track}]", "insert_device",
                                              ["Wavetable", 0])
    record["class_name"] = client.get(f"{head}.class_name").get("value")
    before = wavetable.read(client, head)
    record["before"] = before
    if args.target not in before["targets"]:
        found = introspect.find_parameter(client, f"song.tracks[{args.track}]",
                                          args.device, args.target)
        if len(found) != 1:
            print(f"Parameter {args.target!r} matched {len(found)} parameters; be exact.")
            return 3
        reference = {"__path__": f"{head}.parameters[{found[0].index}]"}
        record["modulatable"] = client.call(head, "is_parameter_modulatable", [reference])
        record["added"] = client.call(head, "add_parameter_to_modulation_matrix", [reference])
        before = wavetable.read(client, head)
        record["after_add"] = before
    row = before["targets"].index(args.target)
    column = args.source
    if column is None:
        named = [i for i, name in enumerate(before["sources"]) if "lfo" in name.lower()]
        if not named:
            print(f"Live names no LFO column ({before['sources']}); pass --source.")
            return 4
        column = named[0]
    original = client.call(head, "get_modulation_value", [row, column]).get("result")
    readings = []
    try:
        for value in PROBE_VALUES:
            client.call(head, "set_modulation_value", [row, column, value])
            got = client.call(head, "get_modulation_value", [row, column]).get("result")
            readings.append({"written": value, "read_back": got})
    finally:
        client.call(head, "set_modulation_value", [row, column, original])
    record.update(target_row=row, source_column=column,
                  source_name=before["sources"][column], original=original,
                  readings=readings,
                  restored=client.call(head, "get_modulation_value", [row, column]).get("result"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"readings": readings, "sources": before["sources"],
                      "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
