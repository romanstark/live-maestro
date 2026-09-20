"""Fold ``probe_paths.py --json`` runs into the report the catalog is checked against.

One run measures only the rows whose probe met an object of the right kind, and one open
set cannot present every kind at once. This reads every run, keeps the best evidence each
produced, and writes ``measurements/catalog-live-<version>.json``.
``tests/test_catalog.py`` fails when a row's ``proven`` disagrees with that report.

Evidence accumulates and verdicts compete: a read-only run proves the read and must not
displace a write run's result. ``proven`` lists ``get`` for a read, ``set`` for a write
that was restored, and ``call`` for a method invoked. ``observe`` and ``automate`` never
appear, because the probe subscribes to no listener and writes no envelope.

No value read out of the open set enters the report. A measured ``clip.file_path``
carried an absolute path into a commercial sample library, and a track name is somebody's
work. The report holds verdicts, not contents.

Usage:
    python scripts/write_measurements.py run1.json run2.json ...
    python scripts/write_measurements.py runs/*.json --out measurements
    python scripts/write_measurements.py runs/*.json --stdout
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from live_maestro.registry import default_registry

#: Where the report belongs. One report at a time: it describes the Live version
#: the catalog currently stands on, and ``tests/test_catalog.py`` fails on any
#: other count so that a rename cannot leave two versions claiming the same rows.
DEFAULT_OUT = REPO_ROOT / "measurements"

#: Live's own refusals that name the object it was handed rather than the path.
#: Each is a message Live raised during a measured run, kept verbatim so the
#: match is evidence rather than a guess. A row that answers one of these was
#: aimed at the wrong kind of object, and nothing was learned about the row.
WRONG_TARGET: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"only available for Audio Clips", re.IGNORECASE), "an audio clip"),
    (re.compile(r"MIDI tracks have no", re.IGNORECASE), "an audio track"),
    (
        re.compile(r"(Main|Master|Return|Group)[^.]{0,40}have no", re.IGNORECASE),
        "a different track kind",
    ),
    (re.compile(r"Only drum racks can have pads", re.IGNORECASE), "a Drum Rack"),
    (re.compile(r"don't have a chain selector", re.IGNORECASE), "a rack that is not a Drum Rack"),
    (
        re.compile(r"no default value available for this type", re.IGNORECASE),
        "a parameter that has a default",
    ),
    (
        re.compile(r"Device doesn't support AB Comparison", re.IGNORECASE),
        "a device with A and B comparison",
    ),
    (re.compile(r"View has no attribute", re.IGNORECASE), "another view object"),
    # Live names the class it was handed and says the member is absent. Under a
    # placeholder that selects among objects of different classes this says the
    # probe picked the wrong one. Under a singleton it is the row's own verdict,
    # which is why the path decides, below.
    (re.compile(r"\b[A-Z]\w* has no (attribute|method)\b"), "another object"),
)

#: A path holding one of these picks among objects that need not share a class,
#: so "class X has no such member" says the probe chose badly. A path without any
#: of them addresses a singleton: ``song.can_undo`` has exactly one Song to be
#: wrong about, and there the same sentence is the row's verdict.
CHOOSES_A_TARGET = re.compile(r"\{(device|nested|chain|pad|param|clip|slot|track|return)\}")

#: Best evidence wins. ``target_mismatch`` outranks ``inconclusive`` because it
#: names what to fix, and ``argument_gap`` sits with ``inconclusive`` because
#: both mean the probe could not form the question.
RANK = {
    "skipped": 0,
    "argument_gap": 1,
    "inconclusive": 1,
    "target_mismatch": 2,
    "broken": 3,
    "verified": 4,
}


def classify(record: dict[str, Any], path_template: str, declared_args: int) -> tuple[str, str]:
    """Return the outcome for one probe record, and what a mismatch needs.

    Only a failure is reclassified. A ``type_error`` on a row that declares call
    arguments means the probe called the method with none, because
    ``candidate_args`` fills path placeholders and never call arguments. That is
    the probe failing to ask, not the method failing to answer.
    """
    verdict = str(record.get("verdict") or "skipped")
    if verdict != "broken":
        return verdict, ""
    if record.get("error_code") == "type_error" and declared_args:
        return "argument_gap", "call arguments this probe does not supply"
    text = f"{record.get('detail') or ''} {record.get('note') or ''}"
    for pattern, wants in WRONG_TARGET:
        if not pattern.search(text):
            continue
        if wants == "another object" and not CHOOSES_A_TARGET.search(path_template):
            return "broken", ""
        return "target_mismatch", wants
    return "broken", ""


def fold(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, str]]:
    """Return one entry per row, the runs that were read, and the versions seen."""
    registry = default_registry()
    templates = {spec.id: spec.path for spec in registry.all()}
    declared = {spec.id: len(spec.args) for spec in registry.all()}

    best: dict[str, dict[str, Any]] = {}
    read: list[str] = []
    versions: dict[str, str] = {}

    for path in paths:
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  skipping {path.name}: {exc}", file=sys.stderr)
            continue
        # A glob catches whatever JSON sits beside the runs. Anything that is not
        # an object carrying a "rows" list is not a probe run, and saying so is
        # cheaper than making the caller curate the directory.
        rows = run.get("rows") if isinstance(run, dict) else None
        if not isinstance(rows, list):
            print(f"  skipping {path.name}: no 'rows' list, so it is not a probe run",
                  file=sys.stderr)
            continue
        read.append(path.name)
        for key in ("live_version", "script_version"):
            value = run.get(key)
            if value:
                versions.setdefault(key, str(value))

        for record in rows:
            if not isinstance(record, dict):
                continue
            row_id = str(record.get("id") or "")
            if row_id not in templates:
                # A run made before the row was removed still names it, and a
                # measurement of something that is gone measures nothing.
                continue
            outcome, wants = classify(record, templates[row_id], declared[row_id])
            entry = best.setdefault(
                row_id,
                {"outcome": "skipped", "wants": "", "error_code": None,
                 "readable": None, "writable": None, "restored": None, "called": None},
            )
            if RANK[outcome] > RANK[entry["outcome"]]:
                entry["outcome"] = outcome
                entry["wants"] = wants
                entry["error_code"] = record.get("error_code")
            # A proof holds whichever run produced it. A later run that did not
            # try cannot take it away.
            for proof in ("readable", "writable", "restored", "called"):
                if record.get(proof) is True:
                    entry[proof] = True
                elif record.get(proof) is False and entry[proof] is None:
                    entry[proof] = False

    return best, read, versions


def proven_for(entry: dict[str, Any]) -> list[str]:
    """The access kinds this row's evidence establishes.

    A write counts only where the restore succeeded. A write this tool cannot
    undo is a change to somebody's set rather than a measurement of it.
    """
    kinds: list[str] = []
    if entry.get("readable") is True:
        kinds.append("get")
    if entry.get("writable") is True and entry.get("restored") is not False:
        kinds.append("set")
    if entry.get("called") is True:
        kinds.append("call")
    return kinds


def build_report(best: dict[str, dict[str, Any]], versions: dict[str, str]) -> dict[str, Any]:
    """Reduce the folded evidence to what may be committed."""
    return {
        "live_version": versions.get("live_version", "unknown"),
        "script_version": versions.get("script_version", "unknown"),
        "rows": {
            row_id: {
                "outcome": entry["outcome"],
                "proven": proven_for(entry),
                **({"error_code": entry["error_code"]} if entry.get("error_code") else {}),
                **({"wants": entry["wants"]} if entry.get("wants") else {}),
            }
            for row_id, entry in sorted(best.items())
        },
    }


#: What must never reach the report. Each pattern matched something a probe read
#: out of a real set: an absolute path into a sample library, an audio file name,
#: a track or clip name. The report carries verdicts, so any hit is a bug here
#: rather than a judgement call.
LEAKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[A-Za-z]:[\\/]"), "an absolute Windows path"),
    (re.compile(r"/(Users|home)/"), "an absolute POSIX path"),
    (re.compile(r"\.(wav|aif|aiff|mp3|flac|ogg|als|adg|adv)\b", re.IGNORECASE), "a file name"),
)


def refuse_leaks(report: dict[str, Any]) -> list[str]:
    """Return every place the report carries something from somebody's set."""
    text = json.dumps(report, ensure_ascii=False)
    return [what for pattern, what in LEAKS if pattern.search(text)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/write_measurements.py",
        description=(
            "Fold probe_paths.py --json runs into the measurement report that "
            "tests/test_catalog.py checks every 'proven' field against."
        ),
    )
    parser.add_argument("runs", nargs="+", metavar="RUN",
                        help="one or more files written by probe_paths.py --json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, metavar="DIR",
                        help=f"directory for the report (default {DEFAULT_OUT.name}/)")
    parser.add_argument("--stdout", action="store_true",
                        help="print the report instead of writing it")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.runs]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        parser.error(f"no such run file(s): {missing}")

    best, read, versions = fold(paths)
    if not best:
        print("no catalog row was named by any run; nothing to write", file=sys.stderr)
        return 1

    report = build_report(best, versions)
    leaks = refuse_leaks(report)
    if leaks:
        print("refusing to write: the report carries " + ", ".join(leaks), file=sys.stderr)
        return 1

    body = json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    counts = collections.Counter(row["outcome"] for row in report["rows"].values())
    proven = collections.Counter(
        kind for row in report["rows"].values() for kind in row["proven"]
    )

    print(f"read {len(read)} run(s), {len(report['rows'])} row(s)")
    print(f"  Live {report['live_version']}, script {report['script_version']}")
    for outcome, count in counts.most_common():
        print(f"  {count:5d}  {outcome}")
    for kind, count in sorted(proven.items()):
        print(f"  {count:5d}  proven {kind}")

    if args.stdout:
        print()
        print(body, end="")
        return 0

    version = report["live_version"].replace(" ", "-")
    target = args.out / f"catalog-live-{version}.json"
    args.out.mkdir(parents=True, exist_ok=True)
    stale = [p for p in args.out.glob("catalog-live-*.json") if p != target]
    target.write_text(body, encoding="utf-8", newline="\n")
    print(f"\n-> {target}")
    if stale:
        print("  remove the superseded report(s) in the same commit: "
              + ", ".join(p.name for p in stale))
        print("  tests/test_catalog.py fails while more than one is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
