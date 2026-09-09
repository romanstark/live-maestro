"""FastMCP server surface for Live Maestro.

Exposes high-level musical intent tools (note editing, mixing, automation, arrangement),
generic Live Object Model tools (lom_get, lom_set, lom_call, lom_batch, lom_describe,
lom_enums), and reference resources.

All catalogued operations route through the executor against the catalog registry,
enforcing type validation, parameter range checks, confirmation guards for destructive
actions, and post-write verification.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import MISSING
from dataclasses import fields as dataclass_fields
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from live_maestro import automation as auto
from live_maestro import lookup, toolargs
from live_maestro.als import read as als_reader
from live_maestro.als import write as als_writer
from live_maestro.analyzer import AnalyzerClient
from live_maestro.analyzer import report as analyzer_report
from live_maestro.client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LiveClient,
    LiveConnectionError,
    LiveError,
    LiveTimeoutError,
)
from live_maestro.executor import Result, UnknownSpecError, execute, execute_batch
from live_maestro.lom import introspect, paths
from live_maestro.models import Access, Note, PathStatus
from live_maestro.music import notes as note_ops
from live_maestro.registry import CatalogError, Registry, area_of, default_registry
from live_maestro.spec import PathSpec
from live_maestro.toolargs import (
    BatchOpArg,
    ConfirmArg,
    KindArg,
    NoteArg,
    ParameterPathArg,
    SlotArg,
    TrackArg,
)

# stderr, never stdout: stdout is the stdio transport and one stray print there
# corrupts the JSON-RPC stream.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("live_maestro.server")

REPO_ROOT = Path(__file__).resolve().parents[2]
LIMITS_DOC = REPO_ROOT / "docs" / "limits.md"

# --------------------------------------------------------------------------- #
# Tool annotations: the machine-readable half of behavioural disclosure.
# --------------------------------------------------------------------------- #
# A client reads these before it reads a word of prose, and one of the four
# carries real risk. ``idempotentHint`` is what tells a client a failed call is
# safe to send again, and a write that timed out may already have landed in Live
# (docs/limits.md, 'Write timeouts and retry policy'). So it is true only where
# repeating the same arguments cannot apply twice, which means an absolute setter
# storing the value it was handed. Anything relative (transpose_clip), additive
# (arrange, create_clip) or toggling (set_locator) declares false, however safe a
# retry happens to look in practice.
#
# ``openWorldHint`` is false throughout. The domain is one running copy of Live
# and the project files it saves, which is closed in the sense the MCP spec means.

#: Reads Live and changes nothing.
READS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

#: Stores the value it was given, so writing it twice stores it once.
SETS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

#: A write whose effect accumulates, toggles, or depends on what is already there.
ADDS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)

#: Can discard something the caller cannot get back. Gated on confirm=True.
DESTROYS = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)

# --------------------------------------------------------------------------- #
# Where a measurement's provenance goes.
# --------------------------------------------------------------------------- #
# A tool docstring is the description FastMCP ships to the client, so its reader is
# a model, mid-session, with no way to re-run a probe. It needs the finding: that a
# quantized parameter snaps, that inserting a track renumbers the ones after it. The
# citation that backs the finding is for the person who can go and measure again, and
# it lives in a comment above the tool it belongs to. Same fact, same file, same
# distance from the code. Only the reader changes.
#
# Everywhere else the citation stays inline. A catalog row's doc is itself the record
# own probe, and a code comment sits next to the line the measurement justifies, where
# deleting one without the other is the mistake worth making hard.

# --------------------------------------------------------------------------- #
# Technical finding notices repeated across tool responses.
# --------------------------------------------------------------------------- #

STORED_NOT_AUDIBLE = (
    "The read-back proves the value is stored in Live. It does not prove the change is "
    "audible (the device may be disabled, the track muted, or the clip inactive), "
    "that it survives an undo or reload, or that the saved .als agrees."
)

RAN_NOT_EFFECTIVE = (
    "The method executed without raising an exception. A method call has no read-back of "
    "its own the way a parameter write does, so unless the catalog row names a verify hook "
    "that checks a change, verify the resulting state with a read operation."
)

ENVELOPE_KEEPS_ITS_LAST_VALUE = (
    "A Session clip envelope lets go of its parameter wherever it stopped, and nothing puts "
    "the earlier value back: Live keeps no static underneath the envelope and no read reports "
    "the value it covered. After a playthrough, re-read every volume, send and device "
    "parameter the scene automated, and write back the value you meant "
    "(docs/limits.md, section 2, 'A clip envelope leaves its last value behind (LOM)')."
)

#: What a call row with a change-checking hook actually proves. Distinct from
#: STORED_NOT_AUDIBLE, which is about a value written to a property: a quantize has
#: no stored value, it has a note count that did or did not move.
CHECKED_NOT_AUDIBLE = (
    "The before and after readings prove that the post-condition this catalog row declares "
    "held one round trip after the call. They do not prove the change is audible (the device "
    "may be disabled, the track muted, or the clip inactive), that it survives an undo or "
    "reload, or that the saved .als agrees. A method can also satisfy its post-condition "
    "without having done everything the caller wanted: the hook checks what the row declares, "
    "not the caller's intent."
)

NORMALISED_NOTE = (
    "Parameter values lie between min and max (typically 0.0..1.0 on native devices, where "
    "0.85 corresponds to 0 dB). Values represent internal normalized units rather than "
    "the physical display units shown by Live. Measured 2026-08-31 against Live 12.4.5: "
    "Max devices and VSTs define independent parameter ranges."
)

GROUP_TRACK_NOTE = (
    "Group tracks do not support arming and have no arrangement clips. Grouping or ungrouping "
    "inserts or removes a container track, shifting all subsequent track indices. "
    "Measured 2026-08-30 against Live 12.4.5: Re-read session state after grouping changes."
)

#: Track indices represent position offsets rather than stable identities.
#: Creating, deleting, or grouping tracks renumbers all subsequent tracks.
STALE_INDEX_NOTE = (
    "A track index represents a position offset rather than a stable identity. Creating a "
    "track anywhere but the end, deleting one, and grouping or ungrouping all renumber every "
    "track after the change, and nothing in the LOM detects that a path built earlier now "
    "points somewhere else. Measured 2026-08-30 against Live 12.4.5: after one insert and one "
    "delete, song.tracks[1] answered a different track's name with ok: true and no warning, "
    "and a slot the earlier survey called an empty audio track held a synth. The name is not a "
    "fallback either: Live prefixes its own track names with a position number, so inserting "
    "one track at index 0 renamed all six others in the same breath, and 2-Kit-OP 808 became "
    "3-Kit-OP 808 on down the set. Only a name you chose yourself survives that. Re-survey "
    "after any structural change, and compare on a name you set yourself if you compare on "
    "anything."
)

#: Methods excluded from the Remote Script allowlist, mapped to safer tool alternatives.
_TOOL_FOR_METHOD: dict[str, str] = {
    "value_at_time": "read_automation samples an envelope (Session clips only).",
    "insert_step": "write_automation builds and verifies curves with read-back comparison.",
    "clear_envelope": "clear_automation reads the curve before removing it.",
    "clear_all_envelopes": "clear_automation with all_envelopes=True.",
    "set_notes": "write_clip_notes (uses extended note API).",
    "remove_notes": "write_clip_notes (uses extended note API).",
    "get_notes": "read_clip_notes (uses extended note API with per-note metadata).",
}

SESSION_ONLY_SURVEY_NOTE = (
    "The per-track 'session_clips' list and 'session_clip_count' cover the Session grid "
    "only. Clips on the Arrangement timeline are a second, independent set and are not "
    "in either. 'has_arrangement_clips' per track says whether there is anything there: "
    "false means the Arrangement is empty for that track, true means it is not, and null "
    "means the question does not apply (a group track) or could not be read. Read "
    "song.tracks[N].arrangement_clips for the clips themselves."
)

CONFIGURE_NOTE = (
    "Plugins reporting only 'Device On' have not had parameters exposed via Live Configure "
    "mode (up to 128 parameter slots per instance, measured against Live 12.4.5)."
)

SESSION_ONLY_NOTE = auto.SESSION_ONLY_NOTE

NOTES_REPLACE_NOTE = (
    "mode='replace' removes existing notes within the range before writing new notes. "
    "LOM note writes use add_new_notes. Use mode='append' to retain existing notes."
)

BATCH_NOT_ATOMIC = (
    "Batch operations execute sequentially. The Live Object Model does not support "
    "transactional rollbacks. atomic=True halts execution at the first encountered error."
)

SAVED_STATE_NOTE = (
    "Reads the saved .als project file on disk. Changes made in an open Live session are "
    "only reflected after saving the project (Ctrl+S)."
)


def _port() -> int:
    """Return Remote Script port from LIVE_MAESTRO_PORT or default."""
    raw = os.environ.get("LIVE_MAESTRO_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("LIVE_MAESTRO_PORT=%r is not an integer; using %d", raw, DEFAULT_PORT)
    return DEFAULT_PORT


def _host() -> str:
    """Return the Remote Script host from LIVE_MAESTRO_HOST or default.

    The default is loopback, which is where Live is when the server runs on the
    same machine, and that is the only arrangement this project has measured.
    A host is settable because the CLI in ``client.py`` has always taken one and
    the MCP server had no way to pass it on, not because reaching Live across a
    network is supported: the Remote Script binds loopback and speaks no
    authentication, so anything else is the operator's own arrangement.
    """
    host = os.environ.get("LIVE_MAESTRO_HOST", "")
    raw = host.strip()
    return raw or DEFAULT_HOST


def _connection_fix() -> list[str]:
    """Return troubleshooting checklist for Remote Script connection issues."""
    port = _port()
    return [
        "Is Ableton Live running at all?",
        (
            "Preferences -> Link, Tempo & MIDI: one of the Control Surface slots must be set to "
            "'LiveMaestro'. The folder name under Remote Scripts matches the dropdown entry."
        ),
        (
            "The script must sit at <User Library>/Remote Scripts/LiveMaestro/__init__.py. "
            "The User Library path is specified in Preferences/Library.cfg under <ProjectPath>."
        ),
        (
            "After changing the script, delete its __pycache__ and restart Live. "
            "Live loads Remote Scripts at startup and can use cached bytecode."
        ),
        (
            f"This server connects to port {port} (127.0.0.1). Port 9877 belongs to another "
            "Ableton bridge service."
        ),
        f"Cold-start check from a shell: python -m live_maestro.client ping --port {port}",
    ]


# --------------------------------------------------------------- client singleton
_client: LiveClient | None = None


def _client_instance() -> LiveClient:
    """Return lazily initialized process-wide LiveClient singleton."""
    global _client
    if _client is None:
        _client = LiveClient(_host(), _port())
    return _client


mcp = FastMCP(
    name="live-maestro",
    instructions=(
        "Produce music inside a running Ableton Live over the Live Object Model. "
        "Start with get_session: it reports the script handshake and the set. Writes report "
        "before/after/clamped, a result never claims an effect it did not read back, and a "
        "read-back proves the stored value, never audibility. Parameter values are normalised, "
        "not dB or Hz. Automation exists only in Session clips. To find anything, call "
        "find_path with the intent in your own words: it answers with the rows that apply, "
        "or says the answer is a runtime device parameter or impossible. Then reach it "
        "through lom_get/lom_set/lom_call/lom_batch/lom_describe. Do not read the whole "
        "live://catalog resource to look a path up. live://limits says what is "
        "impossible and why."
    ),
)


# --------------------------------------------------------------------- helpers
def _means_for_path(path: str, value: Any) -> str | None:
    """Return catalog description for value at the given path, or None."""
    if value is None:
        return None
    registry, _err = _registry_or_error()
    if registry is None:
        return None
    found = [
        (row.id, meaning)
        for row in registry.rows_for_path(path)
        if (meaning := row.meaning_of(value)) is not None
    ]
    if not found:
        return None
    if len({meaning for _rid, meaning in found}) == 1:
        return found[0][1]
    return " | ".join(f"{rid}: {meaning}" for rid, meaning in found)


#: Longest row doc a catalog hint carries inline. The hint is rare by design, so the
#: cap is set high. The full row is one resource read away.
_HINT_DOC_CAP = 600


def _catalog_hint(path: str, *, writing: bool) -> dict[str, Any] | None:
    """Return catalog hints about a raw path when informative.

    ``lom_get`` and ``lom_set`` take a path, never a row id, so nothing the catalog has
    measured about a property would otherwise reach the call that touches it. The cost is
    concrete: the ``-2`` of ``playing_slot_index`` and the fact that muting a sidechain
    source does not cut a Post FX sidechain are both measured and both written on their
    row, yet neither is reachable from the call that touches the property.

    Silence is the default. A note that arrives on every result becomes noise, and then
    the notes that were working stop being read: the stale-index warning already rides
    on every ``get_session``. So this returns ``None`` unless the catalog actively
    disagrees with what the caller is doing:

    * the row is not ``verified``, meaning Live is known to refuse this property, e.g.
      ``song.master_track.mute``, an ordinary-looking write and one of three
      measured-broken property paths.
    * a write is aimed at a row that does not grant ``set``. That is the
      DeviceParameter trap: ``song.tracks[0].mixer_device.volume`` grants
      ``get``/``observe``/``automate`` and the value lives one level down at
      ``…volume.value``.

    A ``verified`` row being read normally adds nothing and says nothing. Neither
    does an uncatalogued path: the dynamic surface of a loaded plug-in is not in
    the catalog by design, so "no row" is the expected answer there rather than a
    finding.
    """
    registry, _err = _registry_or_error()
    if registry is None:
        return None
    rows = registry.rows_for_path(path)
    if not rows:
        return None

    unverified = [row for row in rows if row.status is not PathStatus.VERIFIED]
    unsettable = (
        [row for row in rows if Access.SET not in row.access] if writing else []
    )
    if not unverified and not unsettable:
        return None

    hint: dict[str, Any] = {"rows": [row.id for row in rows]}
    notes: list[str] = []
    if unverified:
        hint["status"] = {row.id: row.status.value for row in unverified}
        notes.append(
            "The catalog has measured this path and its status is not `verified`, so a "
            "clean answer here is not evidence that the property works."
        )
    if unsettable:
        hint["not_settable_per_catalog"] = [row.id for row in unsettable]
        # No "write X instead" here on purpose. For a DeviceParameter the script
        # already says it, with the concrete index (measured 2026-08-31):
        # "DeviceParameter is a Live object, not a value - write
        # song.tracks[5].mixer_device.volume.value instead". Repeating that from
        # up here could only offer the template form with {track} still in it,
        # which is worse than what the layer below already returned.
        # What the script cannot know is that the catalog measured the access, so
        # that is all this says, and it is the only warning for a row granting
        # no set whose property the script would otherwise write happily.
        notes.append(
            "The catalog grants no `set` on this path: the access was measured, and a "
            "write here is outside what any row documents."
        )
    # The doc of the row that fired, not of every row that matched, and not a
    # pointer to a resource: a pointer costs a second fetch the caller may not
    # make. Capped because a few docs run long, with the full row still one
    # resource read away.
    fired = unverified + [row for row in unsettable if row not in unverified]
    hint["doc"] = {
        row.id: (row.doc if len(row.doc) <= _HINT_DOC_CAP else row.doc[:_HINT_DOC_CAP] + " […]")
        for row in fired
        if row.doc
    }
    notes.append(
        "Full rows: read the `live://catalog/" + fired[0].id + "` resource."
    )
    hint["note"] = " ".join(notes)
    return hint


def _registry_or_error() -> tuple[Registry | None, dict[str, Any] | None]:
    """Load the path catalog, or return the failure as a tool result."""
    try:
        return default_registry(), None
    except (CatalogError, OSError) as exc:
        return None, {
            "ok": False,
            "error": f"the path catalog could not be loaded: {exc}",
            "hint": (
                "The catalog is in src/live_maestro/catalog/*.yaml with schema "
                "docs/catalog.md. Intent tools route through the catalog; generic "
                "lom_* tools operate directly without catalog validation."
            ),
        }


def _connection_error(exc: LiveConnectionError) -> dict[str, Any]:
    """Return connection failure dictionary with troubleshooting checklist."""
    return {
        "ok": False,
        "error": str(exc),
        "reachable": False,
        "host": _host(),
        "port": _port(),
        "fix": _connection_fix(),
    }


def _timeout_error(exc: LiveTimeoutError) -> dict[str, Any]:
    """Return timeout error dictionary with handler and uncertainty details."""
    return {
        "ok": False,
        "error": str(exc),
        "timed_out": True,
        "handler": exc.handler,
        "timeout_seconds": exc.timeout,
        "may_have_landed": exc.may_have_landed,
        "note": (
            "A write that timed out may have landed and is not retried "
            "automatically. Verify with a read before re-sending."
            if exc.may_have_landed
            else "A read timed out; nothing was changed."
        ),
    }


def _live_error(exc: LiveError) -> dict[str, Any]:
    """Convert an LiveError into a standardized error response dictionary."""
    if isinstance(exc, LiveConnectionError):
        return _connection_error(exc)
    if isinstance(exc, LiveTimeoutError):
        return _timeout_error(exc)
    payload: dict[str, Any] = {"ok": False, "error": str(exc)}
    code = getattr(exc, "code", None)
    if code is not None:
        payload["code"] = str(code)
    path = getattr(exc, "path", None)
    if path is not None:
        payload["path"] = path
    return payload


def _enum_value(value: Any) -> Any:
    """Unwrap Enum instances for JSON serialization recursively."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_enum_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _enum_value(v) for k, v in value.items()}
    return value


def _result_dict(result: Result, spec: PathSpec | None = None) -> dict[str, Any]:
    """Format an executor Result into a standardized tool response dictionary."""
    ok = result.ok and not result.blocked
    out: dict[str, Any] = {"ok": ok, "id": result.id, "path": result.path}
    if result.code:
        out["code"] = result.code
    if result.message:
        out["message"] = result.message
    if result.blocked:
        out["blocked"] = True
    for name in ("value", "before", "after", "display"):
        found = getattr(result, name)
        if found is not None:
            out[name] = found
    for name in ("clamped", "changed"):
        found = getattr(result, name)
        if found is not None:
            out[name] = found
    if result.read_back is not None:
        out["read_back"] = result.read_back
    if result.after is not None or result.verified is not None:
        out["verified"] = result.verified
        if result.verified is None:
            out["verified_note"] = (
                "the re-read was too early to see this write; read the value again to settle it"
                if result.read_back == "not_observed"
                else "not checked: verification was not executed"
            )
    if spec is not None:
        for field_name, key in (("value", "means"), ("after", "after_means")):
            got = getattr(result, field_name)
            meaning = spec.meaning_of(got) if got is not None else None
            if meaning is not None:
                out[key] = meaning
        out["catalog_status"] = spec.status.value
        # A method row has no unit. PathSpec.unit defaults to 'normalized' for every
        # row, which is harmless noise next to nothing and actively misleading next
        # to the before/after a change-checking verify hook now reports: a note count
        # is not a normalised value.
        if spec.method is None:
            out["unit"] = spec.unit.value
        if spec.quantized:
            out["quantized"] = True
            out["quantized_note"] = (
                "This parameter takes discrete steps, so a written value can snap "
                "to the nearest step."
            )
        if spec.status.value != "verified":
            out["caveat"] = (
                f"catalog row {spec.id!r} is '{spec.status.value}': unverified against Live."
            )
    if ok and result.after is not None:
        # A call row reaches this with a before/after taken by the executor's baseline
        # step, not with a value it stored, so it gets the wording that fits it.
        out["proves"] = (
            CHECKED_NOT_AUDIBLE if spec is not None and spec.method else STORED_NOT_AUDIBLE
        )
    if result.code == "not_settable" and ".value" in (result.message or ""):
        out["hint"] = (
            "This path addresses a Live object (such as DeviceParameter) rather than a value. "
            "Write to the '.value' companion path instead (e.g. param.value_raw)."
        )
    return out


def _run(
    spec_id: str, *, confirm: bool = False, verify: bool = True, **args: Any
) -> dict[str, Any]:
    """Execute one catalog row and return its ``{ok, …}`` result dict."""
    registry, err = _registry_or_error()
    if err is not None:
        return err
    assert registry is not None
    try:
        spec: PathSpec | None = registry.get(spec_id)
    except KeyError:
        spec = None
    try:
        result = execute(
            _client_instance(), registry, spec_id, confirm=confirm, verify=verify, **args
        )
    except UnknownSpecError as exc:
        return {"ok": False, "error": str(exc)}
    except LiveError as exc:
        return _live_error(exc)
    except (ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "error": f"invalid arguments for {spec_id}: {exc}"}
    return _result_dict(result, spec)


def _run_batch(
    ops: Sequence[Mapping[str, Any]], *, atomic: bool = False, verify: bool = False
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Execute multiple catalog operations in a single round trip."""
    registry, err = _registry_or_error()
    if err is not None:
        return None, err
    assert registry is not None
    try:
        results = execute_batch(
            _client_instance(), registry, list(ops), atomic=atomic, verify=verify
        )
    except UnknownSpecError as exc:
        return None, {"ok": False, "error": str(exc)}
    except LiveError as exc:
        return None, _live_error(exc)
    except (ValueError, TypeError, KeyError) as exc:
        return None, {"ok": False, "error": f"invalid batch: {exc}"}
    specs: list[PathSpec | None] = []
    for op in ops:
        try:
            specs.append(registry.get(str(op.get("id"))))
        except KeyError:
            specs.append(None)
    return [_result_dict(r, s) for r, s in zip(results, specs, strict=False)], None


def _read_fields(
    wanted: Sequence[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Batch-read catalog rows and format into a dictionary keyed by field label."""
    ops = [{"id": spec_id, **args} for spec_id, args in wanted]
    results, err = _run_batch(ops)
    if err is not None:
        return None, err
    assert results is not None
    fields: dict[str, Any] = {}
    for (spec_id, _args), row in zip(wanted, results, strict=False):
        label = spec_id.split(".", 1)[1] if "." in spec_id else spec_id
        entry: dict[str, Any] = {"id": spec_id, "ok": row.get("ok", False)}
        for key in ("value", "display", "unit", "code", "message"):
            if key in row:
                entry[key] = row[key]
        fields[label] = entry
    return fields, None


def _value_of(fields: Mapping[str, Any], label: str, default: Any = None) -> Any:
    """Return extracted field value or default if read failed."""
    entry = fields.get(label)
    if isinstance(entry, Mapping) and entry.get("ok"):
        return entry.get("value", default)
    return default


#: Live collections are Vector objects that do not all expose len().
#: Probing indices sequentially in batches provides reliable count determination.
SCAN_CAP = 128
SCAN_LIMIT = 1024

COLLECTION_NOTE = (
    "Collection sizes are measured by probing indices until Live reports index_out_of_range. "
    "Live collection Vector objects do not all expose len() directly. "
    "'exact': False indicates the probe reached its ceiling."
)


def _scan(
    scans: Sequence[tuple[str, str, str, Mapping[str, Any]]], cap: int = SCAN_CAP
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Query indexed catalog collections in a single batch probe."""
    state: dict[str, dict[str, Any]] = {
        label: {"id": spec_id, "values": [], "count": 0, "exact": False, "stopped_by": None}
        for label, spec_id, _index_arg, _base in scans
    }
    pending: dict[str, int] = {label: 0 for label, _s, _i, _b in scans}

    while pending:
        ops: list[dict[str, Any]] = []
        spans: list[tuple[str, int, int]] = []
        for label, spec_id, index_arg, base in scans:
            start = pending.get(label)
            if start is None:
                continue
            spans.append((label, len(ops), cap))
            ops.extend({"id": spec_id, **dict(base), index_arg: start + i} for i in range(cap))
        if not ops:
            break
        results, err = _run_batch(ops)
        if err is not None:
            return state, err
        assert results is not None
        for label, offset, size in spans:
            entry = state[label]
            chunk = results[offset : offset + size]
            stopped = False
            for row in chunk:
                if not row.get("ok"):
                    entry["stopped_by"] = row.get("code") or "error"
                    stopped = True
                    break
                entry["values"].append(row.get("value"))
            entry["count"] = len(entry["values"])
            if stopped:
                entry["exact"] = True
                pending.pop(label, None)
            elif len(chunk) < size or entry["count"] >= SCAN_LIMIT:
                entry["exact"] = False
                pending.pop(label, None)
            else:
                pending[label] = entry["count"]
    return state, None


def _count(
    spec_id: str, index_arg: str, base: Mapping[str, Any], cap: int = SCAN_CAP
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return item count and items for an indexed catalog collection."""
    state, err = _scan([("n", spec_id, index_arg, base)], cap=cap)
    return state.get("n", {"count": 0, "exact": False, "values": []}), err


def _grew(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool | None:
    """Return True if collection count increased, or None if indeterminate."""
    if not before.get("exact") or not after.get("exact"):
        return None
    return int(after.get("count", 0)) > int(before.get("count", 0))


def _shrank(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool | None:
    """Return True if collection count decreased, or None if indeterminate."""
    if not before.get("exact") or not after.get("exact"):
        return None
    return int(after.get("count", 0)) < int(before.get("count", 0))


def _verdict(call_ok: bool, moved: bool | None) -> tuple[bool, dict[str, Any]]:
    """Combine operation status and count verification into result dictionary."""
    if moved is None:
        return call_ok, {
            "verified": None,
            "verified_note": (
                "not checked: the count could not be measured. "
                + COLLECTION_NOTE
            ),
        }
    return call_ok and moved, {"verified": moved}


def _clip_path(track: int, slot: int) -> str:
    """Return canonical LOM path for a Session clip slot."""
    return paths.clip(track, slot)


#: Metadata keys attached by Live note queries that are excluded from Note models.
UNMODELLED_NOTE_KEYS = frozenset({"note_id"})

#: Per-note warning codes excluded from duplicate issue aggregation.
_PER_NOTE_WARNINGS = frozenset({"ignored_key"})


def _merged_warnings(*reports: note_ops.ValidationReport) -> list[dict[str, Any]]:
    """Merge and deduplicate validation warnings from note validation stages."""
    seen: set[tuple[str, tuple[int, ...]]] = set()
    merged: list[dict[str, Any]] = []
    for report in reports:
        for issue in report.warnings:
            if issue.code in _PER_NOTE_WARNINGS:
                continue
            fingerprint = (issue.code, tuple(issue.indices or ()))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(issue.to_dict())
    return merged


def _parse_notes(raw: Sequence[Any]) -> tuple[list[Note], list[str]]:
    """Parse note dictionaries returned by Live, filtering unmodelled metadata keys."""
    dropped: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            cleaned.append(entry)
            continue
        keep = {}
        for key, value in entry.items():
            if key in UNMODELLED_NOTE_KEYS:
                dropped.add(key)
                continue
            keep[key] = value
        cleaned.append(keep)
    return note_ops.from_dicts(cleaned), sorted(dropped)


def _track_path_for(kind: str, index: int) -> str:
    """Return canonical LOM path for track, return track, or master track."""
    if kind == "master":
        return paths.master()
    if kind == "return":
        return paths.return_track(index)
    return paths.track(index)


def _device_parameter_names(
    track_path: str, device: int, cap: int = SCAN_CAP
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Probe parameter names for a device by index."""
    if track_path == paths.master():
        return _count("master_device.parameter_name", "param", {"device": device}, cap=cap)
    if track_path.startswith("song.return_tracks["):
        index = _trailing_index(track_path)
        if index is None:
            return {"count": 0, "exact": False, "values": []}, None
        return _count(
            "return_device.parameter_name", "param", {"return": index, "device": device}, cap=cap
        )
    index = _trailing_index(track_path)
    if index is None:
        return {"count": 0, "exact": False, "values": []}, None
    return _count("param.name", "param", {"track": index, "device": device}, cap=cap)


def _trailing_index(path: str) -> int | None:
    """Extract trailing integer index from LOM path segment, or None."""
    match = re.search(r"\[(\d+)\]$", path)
    return int(match.group(1)) if match else None


def _view_from_names(track_path: str, device: int, names: Sequence[Any]) -> introspect.DeviceView:
    """Construct DeviceView instance from probed parameter names."""
    device_path = f"{track_path}.devices[{device}]"
    parameters = tuple(
        introspect.ParameterView(index, f"{device_path}.parameters[{index}]", str(name))
        for index, name in enumerate(names)
    )
    return introspect.DeviceView(
        path=device_path,
        name=device_path,
        class_name="",
        index=device,
        track=track_path,
        parameters=parameters,
    )


def _send_index(key: str | int) -> int:
    """Convert send identifier ('A', 'B', or numeric index) to integer index."""
    if isinstance(key, int):
        return key
    text = key.strip()
    if text.isdigit():
        return int(text)
    if len(text) == 1 and text.upper().isalpha():
        return ord(text.upper()) - ord("A")
    return -1


# Catalog mapping specifications per track kind.
_TRACK_KINDS: dict[str, dict[str, Any]] = {
    "track": {
        "param": "track",
        "fields": (
            "track.name", "track.color", "track.mute", "track.solo", "track.is_frozen",
            "track.can_be_armed", "track.is_foldable", "track.is_grouped",
            "track.current_monitoring_state", "track.volume", "track.panning",
            "track.sends", "track.devices", "track.clip_slots", "track.arrangement_clips",
        ),
        "volume": "track.volume_value",
        "panning": "track.panning_value",
        "send": "track.send_value",
        "mute": "track.mute",
        "solo": "track.solo",
        "devices": "device.list",
        "device_name": "device.name",
        "device_class": "device.class_name",
        "device_active": "device.is_active",
        "device_parameters": "device.parameters",
        "device_param_name": "param.name",
        "parameter": "param.value_raw",
        "param_str_for_value": "param.str_for_value",
        "device_param": "track",
        "name": "track.name",
        "device_delete": "track.delete_device",
    },
    "return": {
        "param": "ret",
        "fields": (
            "return.name", "return.color", "return.mute", "return.solo",
            "return.volume", "return.panning", "return.sends", "return.devices",
            "return.clip_slots",
        ),
        "volume": "return.volume_value",
        "panning": "return.panning_value",
        "send": "return.send_value",
        "mute": "return.mute",
        "solo": "return.solo",
        "devices": "return_device.list",
        "device_name": "return_device.name",
        "device_class": "return_device.class_name",
        "device_active": "return_device.is_active",
        "device_parameters": "return_device.parameters",
        "device_param_name": "return_device.parameter_name",
        "parameter": "return_device.parameter_value",
        "param_str_for_value": "return_device.str_for_value",
        "device_param": "return",
        "name": "return.name",
        "device_delete": "return_device.delete",
    },
    "master": {
        "param": None,
        "fields": (
            "master.name", "master.color", "master.volume",
            "master.panning", "master.cue_volume", "master.crossfader", "master.devices",
        ),
        "volume": "master.volume_value",
        "panning": "master.panning_value",
        "send": None,
        "mute": None,
        "solo": None,
        "devices": "master_device.list",
        "device_name": "master_device.name",
        "device_class": "master_device.class_name",
        "device_active": "master_device.is_active",
        "device_parameters": "master_device.parameters",
        "device_param_name": "master_device.parameter_name",
        "parameter": "master_device.parameter_value",
        "param_str_for_value": "master_device.str_for_value",
        "device_param": None,
        "name": "master.name",
        "device_delete": "master_device.delete",
    },
}


def _kind_args(kind: str, index: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return path arguments for track kind or error if unsupported."""
    profile = _TRACK_KINDS.get(kind)
    if profile is None:
        return {}, {
            "ok": False,
            "error": f"kind must be one of {sorted(_TRACK_KINDS)}, not {kind!r}",
        }
    param = profile["param"]
    return ({} if param is None else {param: index}), None


def _clip_fields(track: int, slot: int) -> Sequence[tuple[str, dict[str, Any]]]:
    """Return catalog specifications queried by get_clip."""
    args = {"track": track, "slot": slot}
    ids = (
        "clip.name", "clip.is_midi_clip", "clip.is_audio_clip", "clip.length",
        "clip.looping", "clip.loop_start", "clip.loop_end", "clip.start_marker",
        "clip.end_marker", "clip.muted", "clip.color", "clip.gain", "clip.warping",
        "clip.warp_mode", "clip.pitch_coarse", "clip.pitch_fine",
        "clip.signature_numerator", "clip.signature_denominator", "clip.has_envelopes",
        "clip.velocity_amount", "clip.legato", "clip.launch_mode",
    )
    return [(spec_id, dict(args)) for spec_id in ids]


# --------------------------------------------------------------------- state
# Measured 2026-08-29 against Live 12.4.5: the round-trip handshake takes about
# 450 ms.
@mcp.tool(annotations=READS)
def get_session(
    clips: Annotated[
        bool,
        Field(
            description=(
                "Sweep the Session grid for slots that hold a clip. Off, the answer "
                "covers tracks and devices but says nothing about clips, and comes "
                "back faster because no slots are probed."
            )
        ),
    ] = True,
    devices: Annotated[
        bool,
        Field(description="Include each track device chain in the answer."),
    ] = True,
    max_scenes: Annotated[
        int,
        Field(
            description=(
                "How many scenes deep to probe for Session clips. A cap rather than "
                "a count: Live collections report no length, so the sweep stops here "
                "instead of at the last scene. Ignored when clips is false."
            )
        ),
    ] = 64,
) -> dict[str, Any]:
    """Survey the running set: script handshake, song fields, tracks, clips, devices.

    The call to make first. It proves the Remote Script is answering before anything
    else is attempted, and it hands back the track and scene indices every other
    tool here is addressed by.

    Returns:
        Dictionary with script handshake info, song fields, the track list with its
        indices, group track indices, and clip and device detail where asked for.

    Note:
        The round-trip handshake takes about 450 ms, so this is the expensive read. Take
        it once at the start and narrow with get_track, get_clip and get_devices
        afterwards rather than calling this again between edits. Group tracks have no arm
        state ('armed' returns null). If Live collection introspection reports zero
        tracks, index probing is used as a fallback.
    """
    client = _client_instance()
    try:
        info = client.script_info()
    except LiveError as exc:
        return _live_error(exc)
    try:
        snap = introspect.snapshot(
            client, clips=clips, devices=devices, max_scenes=max(0, max_scenes)
        )
    except LiveError as exc:
        return _live_error(exc)
    except introspect.IntrospectionError as exc:
        return {"ok": False, "error": str(exc), "script": info}

    groups = [t.index for t in snap.group_tracks]
    result: dict[str, Any] = {
        "ok": True,
        "script": info,
        "session": snap.to_dict(),
        "track_count": snap.track_count,
        "session_clip_count": snap.session_clip_count,
        "group_track_indices": groups,
        "notes": [
            (
                "A snapshot is a photograph of Live's memory and goes stale the moment a "
                "human touches the mouse. It says nothing about the .als on disk."
            ),
            GROUP_TRACK_NOTE if groups else "No group tracks in this set.",
            SESSION_ONLY_SURVEY_NOTE,
            STALE_INDEX_NOTE,
        ],
    }

    if snap.track_count == 0:
        probed, err = _count("track.name", "track", {})
        if err is None and probed.get("count"):
            result["track_count"] = probed["count"]
            result["track_names"] = probed["values"]
            result["track_count_exact"] = probed["exact"]
            result["diagnostics"] = [
                (
                    "The snapshot came back with no tracks while probing found "
                    f"{probed['count']}. The snapshot is wrong, not the set: it reads child "
                    "counts out of lom_describe, and this Vector came back without one "
                    "(docs/protocol.md §5.6 promises a count). track_names "
                    "below was probed directly. 'session' was not, and is wrong here."
                ),
                COLLECTION_NOTE,
            ]
    return result


@mcp.tool(annotations=READS)
def get_track(track: TrackArg, kind: KindArg = "track") -> dict[str, Any]:
    """Read one track's mixer state, flags, device chain, sends and clip slots.

    One track in one round trip, where get_session covers the whole set. Use this to
    read a mixer before writing it with set_mix, and get_devices when the device
    chain is the only thing wanted.

    Returns:
        Dictionary with the mixer fields, the arm state and whether arming applies,
        the device names and count, the send values, the clip slot count and the
        indices of the filled ones, and a per-collection flag saying whether each
        count is exact.

    Note:
        ``volume`` and ``panning`` are normalised, not dB: 0.85 is 0 dB (measured),
        and the scale is not linear. ``display`` carries the dB reading where Live
        offered one.

        ``arm`` is read only after ``can_be_armed`` says the question applies,
        because reading it on a group track raises inside Live and would break the
        whole batch (measured). It comes back as ``null`` where it does not apply.

        Device names, send values and which clip slots are filled are measured by
        probing indices in one batch, because Live's collections report no length.
        Read ``counts_exact`` before treating any of those counts as complete.
    """
    args, err = _kind_args(kind, track)
    if err is not None:
        return err
    profile = _TRACK_KINDS[kind]
    fields, err = _read_fields([(spec_id, dict(args)) for spec_id in profile["fields"]])
    if err is not None:
        return err
    assert fields is not None

    device_base = {} if profile["device_param"] is None else {profile["device_param"]: track}
    scans: list[tuple[str, str, str, Mapping[str, Any]]] = [
        ("devices", profile["device_name"], "device", device_base)
    ]
    if profile["send"] is not None:
        scans.append(("sends", profile["send"], "send", dict(args)))
    if kind == "track":
        scans.append(("clip_slots", "clip_slot.has_clip", "slot", {"track": track}))
    measured, err = _scan(scans)
    if err is not None:
        return err

    armed: Any = None
    can_arm = _value_of(fields, "can_be_armed")
    if kind == "track" and can_arm:
        arm = _run("track.arm", track=track)
        armed = arm.get("value") if arm.get("ok") else None

    notes: list[str] = [NORMALISED_NOTE, COLLECTION_NOTE]
    if _value_of(fields, "is_foldable") or _value_of(fields, "is_grouped"):
        notes.append(GROUP_TRACK_NOTE)

    slots = measured.get("clip_slots", {})
    return {
        "ok": True,
        "kind": kind,
        "index": None if profile["param"] is None else track,
        "fields": fields,
        "arm": armed,
        "arm_applies": bool(can_arm) if kind == "track" else False,
        "devices": measured.get("devices", {}).get("values", []),
        "device_count": measured.get("devices", {}).get("count"),
        "sends": measured.get("sends", {}).get("values", []),
        "clip_slot_count": slots.get("count"),
        "filled_slots": [i for i, filled in enumerate(slots.get("values", [])) if filled],
        "counts_exact": {label: entry.get("exact") for label, entry in measured.items()},
        "notes": notes,
    }


# Measured 2026-08-30 against Live 12.4.5 on a 1.14-unit audio clip at 120 BPM:
# turning warping on doubled loop_end and end_marker while length did not follow.
# At 60 BPM the same toggle moved neither.
@mcp.tool(annotations=READS)
def get_clip(track: TrackArg, slot: SlotArg) -> dict[str, Any]:
    """Read one Session clip's properties: length, loop, warp, pitch, markers, flags.

    Checks the slot is filled first, because a clip only exists while its slot
    has one and every property read would otherwise fail one by one.

    Returns:
        Dictionary with ``has_clip``, and where that is true the clip path, whether
        it is MIDI, and the property fields. An empty slot answers ``has_clip:
        false`` and says so rather than failing.

    Note:
        Notes are not properties. Use ``read_clip_notes``. Automation is not a
        property either. Use ``read_automation``.

        ``warping``, ``warp_mode`` and ``pitch_coarse`` are reachable only through a
        generic path, because a per-command bridge has no command for them and a
        missing command is easily mistaken for a missing capability
        (docs/architecture.md, 'the restart tax'). Each was read, written, read back
        and restored on 2026-08-29 against Live 12.4.5, and all three rows are
        ``verified``. ``pitch_fine`` is read-verified only, with no write attempted
        on it, and the integer mapping behind ``warp_mode`` is still a hypothesis:
        confirm it against ``clip.available_warp_modes``, which is the authoritative
        per-clip list.

        Writing ``warping`` invalidates every time field this tool just returned. On a
        1.14-unit audio clip at 120 BPM, turning warping on took ``loop_end`` from 0.5704
        to 1.1408 and ``end_marker`` from 1.1408 to 2.2816, both doubled, while ``length``
        did not follow. At 60 BPM the same toggle moved neither. The mechanism is not
        established and is not claimed. What is established is that the numbers move and
        that the factor tracks the tempo. Call this again after any write to ``warping``
        rather than reusing what it said before.
    """
    has = _run("clip_slot.has_clip", track=track, slot=slot)
    if not has.get("ok"):
        return has
    if not has.get("value"):
        return {
            "ok": True,
            "track": track,
            "slot": slot,
            "has_clip": False,
            "note": "The slot is empty; there is no clip to read. create_clip fills it.",
        }

    fields, err = _read_fields(_clip_fields(track, slot))
    if err is not None:
        return err
    assert fields is not None
    is_midi = _value_of(fields, "is_midi_clip")
    return {
        "ok": True,
        "track": track,
        "slot": slot,
        "has_clip": True,
        "path": _clip_path(track, slot),
        "fields": fields,
        "is_midi": is_midi,
        "notes": [
            (
                "Times (length, loop_start, loop_end, markers) are in beats and clip-local: "
                "beat 0 is the clip's own start, not the arrangement's."
            ),
            "gain is normalised, not dB.",
            (
                "Live keeps notes that lie past the clip end (measured); a declared length "
                "shorter than the note content is normal, and that material never sounds."
                if is_midi
                else "Audio clip: transpose it with pitch_coarse/pitch_fine, not with notes."
            ),
        ],
    }


@mcp.tool(annotations=READS)
def get_devices(track: TrackArg, kind: KindArg = "track") -> dict[str, Any]:
    """List a track's device chain: names, class names, on/off, parameter counts.

    Reads the chain and changes nothing in it. The device indices it reports are the
    ones set_parameter, delete_device and ``song.move_device`` are addressed by.

    Returns:
        Dictionary with the device count and one entry per device carrying its index,
        name, class name, on/off state, parameter count and ``configure_needed`` flag.
        A track with an empty chain answers ``device_count: 0`` rather than an error.

    Note:
        Read the parameter count first. A third-party plug-in reporting exactly one
        parameter has not been configured, which the ``configure_needed`` flag says
        outright. That is a limit in Live, not in this server, and only Live's GUI
        lifts it. The probe behind that flag settles one question, whether the count
        is exactly one or more than one, so where it could not settle it the count
        arrives as ``parameter_count_at_least`` instead.

        For the parameters themselves, with their ranges and display units, call
        ``describe`` with ``with_parameters=True``: this tool counts them and does
        not list them. For the mixer, sends and clip slots on the same track, call
        get_track instead of adding a second call here.
    """
    _, err = _kind_args(kind, track)
    if err is not None:
        return err
    profile = _TRACK_KINDS[kind]
    device_base = {} if profile["device_param"] is None else {profile["device_param"]: track}

    listed, err = _count(profile["device_name"], "device", device_base)
    if err is not None:
        return err
    count = int(listed.get("count", 0))
    if count == 0:
        return {
            "ok": True,
            "kind": kind,
            "index": None if profile["param"] is None else track,
            "device_count": 0,
            "devices": [],
            "note": "No devices on this track.",
        }

    detail_ops = [
        {"id": profile[key], **device_base, "device": index}
        for index in range(count)
        for key in ("device_class", "device_active")
    ]
    details, err = _run_batch(detail_ops)
    if err is not None:
        return err
    assert details is not None

    params, err = _scan(
        [
            (str(index), profile["device_param_name"], "param", {**device_base, "device": index})
            for index in range(count)
        ],
    )
    if err is not None:
        return err

    devices: list[dict[str, Any]] = []
    any_unconfigured = False
    for index in range(count):
        class_row, active_row = details[index * 2 : index * 2 + 2]
        entry = params.get(str(index), {})
        exact = bool(entry.get("exact"))
        param_count = int(entry.get("count", 0))
        unconfigured = exact and param_count == 1
        any_unconfigured = any_unconfigured or unconfigured
        devices.append(
            {
                "index": index,
                "name": listed["values"][index],
                "class_name": class_row.get("value") if class_row.get("ok") else None,
                "is_active": active_row.get("value") if active_row.get("ok") else None,
                "parameter_count": param_count if exact else None,
                "parameter_count_at_least": None if exact else param_count,
                "configure_needed": unconfigured,
            }
        )

    notes = [
        (
            "parameter_count counts what the LOM exposes, which is not the same as what the "
            "device has."
        ),
        (
            "Devices can be reordered: Song.move_device(device, target_chain, position), "
            "catalog row song.move_device, through lom_call (measured 2026-08-30). Loading "
            "order still matters, because an instrument replaces rather than appends."
        ),
        COLLECTION_NOTE,
    ]
    if any_unconfigured:
        notes.append(CONFIGURE_NOTE)
    return {
        "ok": True,
        "kind": kind,
        "index": None if profile["param"] is None else track,
        "device_count": count,
        "device_count_exact": listed.get("exact"),
        "devices": devices,
        "notes": notes,
    }


@mcp.tool(annotations=READS)
def describe(
    path: Annotated[
        str,
        Field(
            description=(
                "LOM path to the object to inspect, e.g. 'song', "
                "'song.tracks[0]', 'song.tracks[0].devices[1]'. Path shapes are "
                "listed in the live://catalog resource."
            )
        ),
    ],
    depth: Annotated[
        int,
        Field(
            description=(
                "How far to descend into child collections. Deep descents over a "
                "whole set can be slow and the cost is unmeasured, so raise this "
                "deliberately rather than by default."
            )
        ),
    ] = 1,
    with_parameters: Annotated[
        bool,
        Field(
            description=(
                "For a device path, survey every parameter with its name, value, "
                "min, max, quantized steps and display unit, instead of reporting "
                "the parameters child as a bare count. This is also what diagnoses "
                "an unconfigured third-party plug-in."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Introspect any Live object: class, properties, children, and methods.

    The route to the dynamic surface: what a loaded plug-in actually exposes, which
    no catalog can know in advance because it is decided at runtime and, for
    third-party plug-ins, by what the user picked up in Configure mode.

    Returns:
        Dictionary with the object class, its properties, its child collections, the
        methods the allowlist permits, and the parameter survey where asked for.

    Note:
        Prefer this over lom_describe for a device, and prefer lom_describe for
        anything else. The difference is ``with_parameters``: lom_describe reports a
        device's parameters as a count, and a count is what a Live ``Vector`` that
        refuses ``len()`` fails to give, so a plug-in with parameters can be reported
        as having none. Measured against Live 12.4.5. When that happens this tool
        falls back to probing parameter indices, reports the names it found, and says
        which of the two answers you are looking at.

        Methods are never callable through a path. Take the name from here and invoke
        it through lom_call, which accepts only names on the Remote Script's own
        allowlist. For a device parameter, set it with set_parameter rather than
        writing the path by hand.
    """
    try:
        paths.validate(path)
    except paths.PathSyntaxError as exc:
        return {"ok": False, "error": str(exc)}

    client = _client_instance()
    if with_parameters:
        head, sep, tail = path.rpartition(".devices[")
        if not sep or not tail.endswith("]") or not tail[:-1].isdigit():
            return {
                "ok": False,
                "error": (
                    f"with_parameters needs a device path such as song.tracks[0].devices[1]; "
                    f"{path!r} is not one. Call describe without it for anything else."
                ),
            }
        device_index = int(tail[:-1])
        try:
            view = introspect.describe_device(client, head, device_index)
        except LiveError as exc:
            return _live_error(exc)
        except introspect.IntrospectionError as exc:
            return {"ok": False, "error": str(exc)}

        notes = [
            (
                "value lies on the device's own scale, bounded by min and max. display "
                "carries the physical reading, and unit is parsed out of display and "
                "belongs to it, not to value. The two scales coincide for linear "
                "controls and diverge for logarithmic ones. Measured 2026-08-30 against "
                "Live 12.4.5 on a Max device: LPF value 8794.30 of min 20 / max 21000 "
                "displayed 288 Hz, Release value 3780.74 of 1.5..20000 displayed 27.0 ms, "
                "Volume value -33.73 of -70..10 displayed -8.5 dB, while every "
                "percentage control agreed to one decimal. So aim in value units and read "
                "display back. A value is never the number display shows. To aim at the "
                "reading instead, set_parameter_display samples the taper and writes the "
                "value that produces it."
            ),
            (
                "This survey is the parameter strip. A device carries properties beside it "
                "that are not parameters and are absent from this list: the wavetable an "
                "oscillator plays (oscillator_1_wavetable_index, with oscillator_1_wavetables "
                "listing the choices), and a compressor's sidechain source and tap point "
                "(input_routing_type.display_name and input_routing_channel.display_name). "
                "Reach those with lom_get and lom_set, or ask find_path for the thing by name. "
                "A control missing here is not therefore out of reach."
            ),
        ]
        if view.advice:
            notes.append(view.advice)
        if not view.reports_units:
            notes.append(
                "This device reports no units at all, so the value stays normalised and "
                "this server will not invent hertz for it. Two causes, and they look the "
                "same from here: a VST2 never reports units however it is configured "
                "(measured), and an instance whose strip holds only Device On has nothing "
                "to report units for."
            )

        payload: dict[str, Any] = {"ok": True, "path": view.path, "device": view.to_dict()}
        if view.parameter_count == 0:
            probed, err = _device_parameter_names(head, device_index)
            if err is None and probed.get("count"):
                payload["probed_parameters"] = [
                    {"index": i, "name": name} for i, name in enumerate(probed["values"])
                ]
                payload["parameter_count"] = probed["count"] if probed["exact"] else None
                payload["parameter_count_at_least"] = (
                    None if probed["exact"] else probed["count"]
                )
                notes.insert(
                    0,
                    (
                        "The survey in 'device' reports 0 parameters and is wrong: probing "
                        f"found {probed['count']}. lom_describe emitted no count for this "
                        "Live Vector (docs/protocol.md §5.6 promises one), so the "
                        "introspection layer sees an empty list. Trust "
                        "'probed_parameters'; use lom_get on "
                        f"{head}.devices[{device_index}].parameters[i] for values."
                    ),
                )
                notes.append(COLLECTION_NOTE)
        payload["notes"] = notes
        return payload

    try:
        info = client.describe(path, depth=max(1, depth))
    except LiveError as exc:
        return _live_error(exc)
    return {
        "ok": True,
        "path": path,
        "described": info,
        "notes": [
            (
                "Properties read and write freely through a path; methods do not, they go "
                "through lom_call and only from the script's allowlist."
            ),
            (
                "A child of type 'Vector' is a collection and does not always carry a count "
                "(measured, Live 12.4.5). Probe its indices, or use get_track/get_devices, "
                "which do."
            ),
        ],
    }


# --------------------------------------------------------------------- build
# Measured 2026-08-30 against Live 12.4.5: inserting at index N shifts that track
# and every track after it up by one.
@mcp.tool(annotations=ADDS)
def create_track(
    kind: Annotated[
        Literal["midi", "audio", "return"],
        Field(
            description=(
                "What to create: 'midi' for a MIDI track, 'audio' for an audio "
                "track, 'return' for a send return. A return track is appended to "
                "song.return_tracks and cannot be named or positioned here."
            )
        ),
    ] = "midi",
    index: Annotated[
        int,
        Field(
            description=(
                "Where to insert it in song.tracks. -1 appends at the end, which is "
                "the only value that leaves existing track indices alone. Ignored "
                "for a return track."
            )
        ),
    ] = -1,
    name: Annotated[
        str,
        Field(
            description=(
                "Name for the new MIDI or audio track. Empty leaves Live to name "
                "it, which produces a default like '3-MIDI'."
            )
        ),
    ] = "",
) -> dict[str, Any]:
    """Insert a MIDI, audio, or return track and return its resolved index.

    Returns:
        Dictionary with the resolved index of the new track, its kind and name, and
        the track count before and after.

    Note:
        Inserting at index N shifts the track that was at N, and every track after it, up
        by one. Every track index held from before the call is then wrong, including ones
        in a plan not yet executed. Append with -1 where the position does not matter, and
        otherwise take the resolved index out of this answer rather than assuming it.

        Creating a track does not put an instrument on it. Select it and call
        load_device for that, then create_clip for something to play.
    """
    kinds = {
        "midi": ("song.create_midi_track", "track.name", "track"),
        "audio": ("song.create_audio_track", "track.name", "track"),
        "return": ("song.create_return_track", "return.name", "ret"),
    }
    chosen = kinds.get(kind.lower())
    if chosen is None:
        return {"ok": False, "error": f"kind must be one of {sorted(kinds)}, not {kind!r}"}
    spec_id, name_id, index_arg = chosen

    before, err = _count(name_id, index_arg, {})
    if err is not None:
        return err

    call_args: list[Any] = [] if kind.lower() == "return" else [index]
    created = _run(spec_id, call_args=call_args)
    if not created.get("ok"):
        return created

    after, err = _count(name_id, index_arg, {})
    if err is not None:
        return err
    grew = _grew(before, after)
    ok, evidence = _verdict(bool(created.get("ok")), grew)

    landed: int | None = None
    if grew and after.get("exact"):
        landed = after["count"] - 1 if index < 0 or kind.lower() == "return" else index
    named: dict[str, Any] | None = None
    names_after = after.get("values")
    if name and landed is not None:
        index_kwargs: dict[str, Any] = {index_arg: landed}
        named = _run(name_id, **index_kwargs, value=name)
        if named.get("ok"):
            refreshed, refresh_err = _count(name_id, index_arg, {})
            if refresh_err is None and refreshed.get("values") is not None:
                names_after = refreshed["values"]

    return {
        "ok": ok,
        "kind": kind,
        "requested_index": index,
        "count_before": before.get("count"),
        "count_after": after.get("count"),
        "assumed_index": landed,
        "names_after": names_after,
        "created": created,
        "named": named,
        **evidence,
        "notes": [
            (
                "assumed_index is dead-reckoned from the count and the requested position; "
                "the LOM does not report the new track."
            ),
            "Inserting anywhere but the end renumbers every later track.",
            COLLECTION_NOTE,
        ],
    }


def _selected_chain_names() -> list[str]:
    """Return device names on the currently selected track in signal order.

    The order is the one Live sends. Measured 2026-09-01 against Live 12.4.5.

    An empty result is ambiguous on purpose and must stay that way here: the caller
    reports both chains it compared, so "nothing was displaced" cannot be confused with
    "nothing was looked at".
    """
    listed = _run("view.selected_track_devices")
    if not listed.get("ok"):
        return []
    value = listed.get("value")
    if not isinstance(value, list):
        return []
    return [str(device.get("name") or "?") for device in value if isinstance(device, dict)]


# Measured 2026-08-30 against Live 12.4.5: an instrument load renames a track that
# still carries Live's default name. An effect never renames.
@mcp.tool(annotations=DESTROYS)
def load_device(
    query: Annotated[
        str,
        Field(
            description=(
                "What to look for in the browser, matched against item names, e.g. "
                "'Operator' or 'reverb'. Required for the search step; on the load "
                "step it can be dropped once uri or item_path is known."
            )
        ),
    ] = "",
    root: Annotated[
        Literal[
            "",
            "audio_effects",
            "clips",
            "current_project",
            "drums",
            "instruments",
            "legacy_libraries",
            "max_for_live",
            "midi_effects",
            "packs",
            "plugins",
            "samples",
            "sounds",
            "user_folders",
            "user_library",
        ],
        Field(
            description=(
                "Browser category to search inside. Empty searches every category, "
                "which is slower and returns more near misses."
            )
        ),
    ] = "",
    uri: Annotated[
        str,
        Field(
            description=(
                "The uri of one candidate from a previous search step, copied back "
                "verbatim to say which item to load. Resolving it walks the browser "
                "a second time, so pass root as well when the item sits deep; "
                "item_path avoids the walk altogether and is preferred."
            )
        ),
    ] = "",
    item_path: Annotated[
        str,
        Field(
            description=(
                "The browser item as a LOM path rooted at app, e.g. "
                "'app.browser.instruments.children[3]'. The reliable handle, and "
                "the one to prefer: unlike a uri it is not walked for again, so it "
                "cannot fail on the walk budget. Every search candidate carries one."
            )
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(description="How many search candidates to return."),
    ] = 12,
    confirm: Annotated[
        bool,
        Field(
            description=(
                "False searches and loads nothing, reporting the candidates and "
                "which track is selected. True loads the item named by uri or "
                "item_path."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Search Live's browser and load a device onto the currently selected track.

    Two steps, and the first one is not optional in practice: call with confirm=False
    to search and to see which track is selected, then call again with confirm=True
    and the uri or item_path of the item you chose.

    Returns:
        Dictionary of search candidates and the selected track on the search step, or
        the load result and the resulting device chain on the load step.

    Note:
        Loading an instrument is destructive. ``Browser.load_item`` takes no track
        argument: it loads onto whatever ``song.view.selected_track`` points at.
        It cannot be aimed inside a rack from here. Measured 2026-09-07 against
        Live 12.4.5: writing ``rack.view.selected_chain`` reads back as the chain
        that was asked for, and a device loaded afterwards still lands on the
        track. The chain kept its six devices and the track went from one device
        to two. Live's browser follows the selection in its own interface, which
        no write over this channel drives. An effect is appended to the end of the
        chain, but an instrument replaces the instrument already on the track and
        discards it silently, together with every parameter configured on it and
        every clip envelope pointing at it. Check the selection and the existing
        chain with get_devices before confirming.

        Loading plug-ins in rapid succession can crash Live (measured: a batch load
        during a background plug-in rescan, with a licence dialog open, took the
        whole application down). Load one at a time, check the connection between
        loads, and save the set first.

        A successful return proves the call ran, not that the track makes a sound: a
        measured Drum Rack load reported success on a silent track. Read the chain
        back with get_devices, and for a rack check that ``chains`` is not empty.

        An instrument load renames the track while the track still carries Live's default
        name, so a track name cached from an earlier get_session is stale afterwards. An
        effect never renames. To reorder what is already loaded, call ``song.move_device``
        through lom_call rather than loading again.

        Three things about the search step are worth knowing before trusting it.

        An empty ``candidates`` with ``truncated: true`` is not proof that the item is
        absent. The walk ran out of budget before it reached the item, and a search
        narrowed with ``root`` can find on the first try what an unrooted search missed
        entirely. Narrow before concluding something is not installed.

        A ``uri`` from the search step can fail to resolve on the load step for the same
        reason, with ``stopped by node_budget``: resolving one walks the browser again,
        and a uri carries no hint about where to look. The ``item_path`` in that same
        candidate addresses the item directly and cannot run out of budget, so prefer
        it. Where the uri is what you have, pass ``root`` with it and the second walk
        is narrowed to that one category the same way the search was.

        Loading onto the main track works and is reached the same way as any other
        target, by pointing the selection at it first: write ``song.view.selected_track``
        with ``{"__path__": "song.master_track"}`` through lom_set, then load. There is no
        track index for the main track, which is why the usual index does not apply.
    """
    client = _client_instance()
    if not query and not (confirm and (uri or item_path)):
        return {
            "ok": False,
            "error": "give a query to search for, or a uri (or item_path) from an "
                     "earlier search together with confirm=True to load one.",
        }
    params: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 200))}
    if root:
        params["root"] = root

    selected = _run("view.selected_track")
    selected_handle = selected.get("value") if selected.get("ok") else None

    if not confirm or not (uri or item_path):
        try:
            found = client.send("browser_walk", params)
        except LiveError as exc:
            return _live_error(exc)
        matches = found.get("matches") or []
        return {
            "ok": True,
            "loaded": False,
            "selected_track": selected_handle,
            "candidates": matches,
            "count": found.get("count"),
            "truncated": found.get("truncated"),
            "next_step": (
                "Point song.view.selected_track at the target track, then call again "
                "with item_path=<the item_path of your choice> and confirm=True. "
                "Prefer item_path over uri: it addresses the item directly, while a "
                "uri has to be walked for again and that walk can run out of budget."
            ),
            "warning": (
                "Loading an instrument replaces the instrument already on the selected track. "
                "Effects are appended; the chain can be reordered afterwards with song.move_device."
            ),
        }

    resolved: dict[str, Any] = {}
    if item_path:
        try:
            paths.validate(item_path)
        except paths.PathSyntaxError as exc:
            return {"ok": False, "error": f"invalid item_path: {exc}"}
    else:
        # The walk that resolves a uri shares the search step's budget, and an
        # unrooted one pours it into the first branch it enters. Passing the
        # root along narrows it to the category the caller already searched,
        # which is the difference between finding a User Library preset and
        # running out of nodes several folders short of it.
        lookup: dict[str, Any] = {"uri": uri}
        if root:
            lookup["root"] = root
        try:
            resolved = client.send("browser_walk", lookup)
        except LiveError as exc:
            return _live_error(exc)
    if not item_path and not resolved.get("found"):
        if resolved.get("truncated"):
            remedy = (
                "Pass the item_path from that same search candidate instead of the "
                "uri: it does not walk and cannot run out of budget."
            )
            if not root:
                remedy += (
                    " Passing root= alongside the uri also works, and narrows this "
                    "walk to one category."
                )
            return {
                "ok": False,
                "error": (
                    f"the browser walk ran out of budget before reaching {uri!r} "
                    f"(stopped by {resolved.get('truncated_by') or 'a limit'}). "
                    + remedy
                ),
                "resolved": resolved,
                "searched_root": root or None,
            }
        return {"ok": False, "error": f"no browser item with uri {uri!r}", "resolved": resolved}
    item_path = item_path or str(resolved.get("item_path") or "")

    before = _run("view.selected_track")
    chain_before = _selected_chain_names()
    loaded = _run("browser.load_item", confirm=True, call_args=[{"__path__": item_path}])
    after = _run("view.selected_track")
    chain_after = _selected_chain_names()
    displaced = [name for name in chain_before if name not in chain_after]

    notes: list[str] = []
    if displaced:
        notes.append(
            "Replaced: " + ", ".join(repr(name) for name in displaced) + " is gone from "
            "the track. An instrument load replaces the instrument that was there."
        )
    elif chain_before and chain_after:
        notes.append(
            f"Nothing was displaced: the chain went {chain_before} -> {chain_after}."
        )
    notes.extend(
        [
            "Verify with get_devices on the selected track.",
            "Pace further loads: loading plugins in rapid succession can crash Live.",
            RAN_NOT_EFFECTIVE,
        ]
    )
    return {
        "ok": bool(loaded.get("ok")),
        "loaded": bool(loaded.get("ok")),
        "uri": uri,
        "item_path": item_path,
        "item": resolved.get("item"),
        "selected_track_before": before.get("value") if before.get("ok") else None,
        "selected_track_after": after.get("value") if after.get("ok") else None,
        "chain_before": chain_before,
        "chain_after": chain_after,
        "displaced": displaced,
        "call": loaded,
        "notes": notes,
    }


@mcp.tool(annotations=ADDS)
def create_clip(
    track: TrackArg,
    slot: SlotArg,
    length_beats: Annotated[
        float,
        Field(
            description=(
                "Loop length of the new clip in beats, so 4.0 is one bar in 4/4 and "
                "16.0 is four. Must be greater than 0."
            )
        ),
    ] = 4.0,
    name: Annotated[
        str,
        Field(description="Name for the new clip. Empty leaves it unnamed."),
    ] = "",
) -> dict[str, Any]:
    """Create an empty MIDI clip of a given length in a Session slot.

    The target must be a MIDI track. An audio slot takes a sample instead, which is a
    different call: lom_call with path='song.tracks[N].clip_slots[M]',
    method='create_audio_clip' and one absolute file path (catalog row
    clip_slot.create_audio_clip).

    Returns:
        Dictionary with the creation status, the clip path, and the read-back length.

    Note:
        An occupied slot is refused rather than overwritten, so nothing is lost by
        aiming at the wrong slot. To replace a clip, delete_clip first.

        The clip arrives empty. Fill it with write_clip_notes, and copy it onto the
        Arrangement timeline with arrange once it holds something.
    """
    has = _run("clip_slot.has_clip", track=track, slot=slot)
    if not has.get("ok"):
        return has
    if has.get("value"):
        return {
            "ok": False,
            "error": (
                f"track {track} slot {slot} already has a clip. create_clip does not "
                "overwrite; delete_clip first if replacing."
            ),
            "has_clip": True,
        }

    created = _run("clip_slot.create_clip", track=track, slot=slot, call_args=[length_beats])
    if not created.get("ok"):
        return created

    named: dict[str, Any] | None = None
    if name:
        named = _run("clip.name", track=track, slot=slot, value=name)
    check, err = _read_fields(
        [
            ("clip_slot.has_clip", {"track": track, "slot": slot}),
            ("clip.length", {"track": track, "slot": slot}),
            ("clip.is_midi_clip", {"track": track, "slot": slot}),
        ]
    )
    if err is not None:
        return err
    assert check is not None
    return {
        "ok": bool(_value_of(check, "has_clip")),
        "track": track,
        "slot": slot,
        "path": _clip_path(track, slot),
        "requested_length_beats": length_beats,
        "read_back": check,
        "named": named,
        "call": created,
        "note": "Length is in beats. The clip is empty until write_clip_notes fills it.",
    }


@mcp.tool(annotations=DESTROYS)
def delete_clip(
    track: TrackArg,
    slot: SlotArg,
    confirm: ConfirmArg = False,
) -> dict[str, Any]:
    """Delete the clip in a Session slot, its notes and envelopes with it.

    Requires confirm=True. With confirm=False it reports the clip name, length and
    note count that the call would remove, and changes nothing.

    Returns:
        Dictionary reporting the deletion, or the loss report when unconfirmed.

    Note:
        This empties one Session slot. Slot indices do not shift, so nothing else
        moves and no other index goes stale.

        Emptying a clip is not the same as deleting it: write_clip_notes with an
        empty list and mode='replace' leaves the clip in place with its length,
        loop and envelopes intact. Prefer that where the slot should stay filled.
        A clip already copied into the Arrangement by arrange is a separate clip and
        survives this.
    """
    fields, err = _read_fields(
        [
            ("clip_slot.has_clip", {"track": track, "slot": slot}),
            ("clip.name", {"track": track, "slot": slot}),
            ("clip.length", {"track": track, "slot": slot}),
            ("clip.is_midi_clip", {"track": track, "slot": slot}),
            ("clip.has_envelopes", {"track": track, "slot": slot}),
        ]
    )
    if err is not None:
        return err
    assert fields is not None
    if not _value_of(fields, "has_clip"):
        return {"ok": True, "deleted": False, "track": track, "slot": slot,
                "note": "The slot is already empty; nothing to delete."}

    note_count: int | None = None
    if _value_of(fields, "is_midi_clip"):
        try:
            got = _client_instance().send("notes_get", {"path": _clip_path(track, slot)})
            note_count = got.get("count")
        except LiveError:
            note_count = None

    loss = {
        "name": _value_of(fields, "name"),
        "length_beats": _value_of(fields, "length"),
        "is_midi": _value_of(fields, "is_midi_clip"),
        "note_count": note_count,
        "has_envelopes": _value_of(fields, "has_envelopes"),
    }
    if not confirm:
        return {
            "ok": False,
            "deleted": False,
            "confirm_required": True,
            "would_lose": loss,
            "error": (
                f"delete_clip is destructive. This would delete the clip in track {track} "
                f"slot {slot} with its notes and any clip automation on it. Call again with "
                "confirm=True."
            ),
        }

    deleted = _run("clip_slot.delete_clip", confirm=True, track=track, slot=slot)
    after = _run("clip_slot.has_clip", track=track, slot=slot)
    still_there = after.get("value") if after.get("ok") else None
    return {
        "ok": bool(deleted.get("ok")) and still_there is False,
        "deleted": still_there is False,
        "track": track,
        "slot": slot,
        "lost": loss,
        "has_clip_after": still_there,
        "call": deleted,
    }


def _members_of_group(
    track: int,
    measured: Mapping[str, Any],
    tracks: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return nested tracks under a group track by index and name."""
    grouped = measured.get("is_grouped", {}).get("values", [])
    names = tracks.get("values", [])
    members: list[dict[str, Any]] = []
    index = track + 1
    while index < len(grouped) and grouped[index]:
        members.append(
            {"index": index, "name": names[index] if index < len(names) else None}
        )
        index += 1
    return members


def _group_warning(loss: Mapping[str, Any]) -> str:
    """Return the warning that a group track's counts describe only the container."""
    if not loss.get("is_group"):
        return ""
    members = loss.get("contained_tracks") or []
    if not members:
        return (
            " This is a group track. It holds no clips or devices of its own, so the "
            "counts above are about the container and not about its contents. No member "
            "tracks were found beside it, which is worth checking by eye first."
        )
    listed = ", ".join(f"{m['index']} ({m['name']!r})" for m in members)
    return (
        f" This is a group track and the counts above are about the container, not its "
        f"contents: it nests {len(members)} track(s), namely {listed}, with their own "
        "devices, clips and automation. What Live does with them when the group goes is "
        "not established by this server and was not measured. Look at them before "
        "confirming."
    )


@mcp.tool(annotations=DESTROYS)
def delete_track(track: TrackArg, confirm: ConfirmArg = False) -> dict[str, Any]:
    """Delete a track with everything on it. Requires confirm=True.

    The widest deletion here: the devices, every Session clip and every Arrangement
    clip on that track go with it. With confirm=False it reports the track name, the
    devices, the filled Session slots and the Arrangement clip count the call would
    remove, and changes nothing.

    Returns:
        Dictionary reporting the deletion, or the loss report when unconfirmed.

    Note:
        Deleting shifts every later track down one index, so any track index read
        before this call is wrong afterwards. Re-read with get_session, and where
        several tracks are going, delete from the highest index downwards so the
        earlier indices stay valid.

        For something narrower, delete_clip empties one slot and delete_device takes
        one device out of the chain, neither of which renumbers anything.
    """
    fields, err = _read_fields(
        [
            ("track.name", {"track": track}),
            ("track.is_foldable", {"track": track}),
        ]
    )
    if err is not None:
        return err
    assert fields is not None
    measured, err = _scan(
        [
            ("tracks", "track.name", "track", {}),
            ("devices", "device.name", "device", {"track": track}),
            ("clip_slots", "clip_slot.has_clip", "slot", {"track": track}),
            ("arrangement", "arrangement_clip.name", "clip", {"track": track}),
            ("is_grouped", "track.is_grouped", "track", {}),
        ]
    )
    if err is not None:
        return err

    before = measured.get("tracks", {})
    slots = measured.get("clip_slots", {})
    arrangement = measured.get("arrangement", {})
    not_applicable = arrangement.get("stopped_by") == "no_such_path"
    arrangement_names = [] if not_applicable else list(arrangement.get("values", []))
    loss = {
        "name": _value_of(fields, "name"),
        "devices": measured.get("devices", {}).get("values", []),
        "filled_slots": [i for i, filled in enumerate(slots.get("values", [])) if filled],
        "arrangement_clips": None if not_applicable else len(arrangement_names),
        "arrangement_clip_names": arrangement_names,
        "is_group": _value_of(fields, "is_foldable"),
    }
    if loss["is_group"]:
        loss["contained_tracks"] = _members_of_group(track, measured, before)
    if not confirm:
        return {
            "ok": False,
            "deleted": False,
            "confirm_required": True,
            "would_lose": loss,
            "track_count": before.get("count"),
            "error": (
                f"delete_track is destructive. This would delete track {track} "
                f"({loss['name']!r}) with its {len(loss['devices'])} device(s), "
                f"{len(loss['filled_slots'])} Session clip(s), "
                f"{'no' if not_applicable else len(arrangement_names)} Arrangement clip(s) "
                "and all of their clip automation, and shift every later track index "
                "down by one." + _group_warning(loss) + " Call again with confirm=True."
            ),
        }

    deleted = _run("song.delete_track", confirm=True, call_args=[track])
    after, err = _count("track.name", "track", {})
    if err is not None:
        return err
    ok, evidence = _verdict(bool(deleted.get("ok")), _shrank(before, after))
    return {
        "ok": ok,
        "deleted_index": track,
        "lost": loss,
        "track_count_before": before.get("count"),
        "track_count_after": after.get("count"),
        "call": deleted,
        **evidence,
        "warning": (
            "Track indices after this one have shifted down by one. Re-read the session "
            "before using any index you held from before this call."
        ),
    }


# Measured 2026-08-30 against Live 12.4.5 on a scratch track: song.move_device
# reorders a chain without deleting anything.
@mcp.tool(annotations=DESTROYS)
def delete_device(
    track: TrackArg,
    device: Annotated[
        int,
        Field(
            description=(
                "Device index in that track chain, counted from 0 left to right as "
                "Live draws it. get_devices lists the chain with its indices."
            )
        ),
    ],
    kind: KindArg = "track",
    confirm: ConfirmArg = False,
) -> dict[str, Any]:
    """Delete one device from a track chain, its settings with it.

    Requires confirm=True. With confirm=False it reports the name of the track, and
    the device's name, class and parameter count, and changes nothing.

    Returns:
        Dictionary reporting deletion status or pending loss report.

    Note:
        The track is named in the report because a device name does not identify
        one. A device class like Utility or EQ Eight sits on several tracks in a
        normal set, so a loss report naming only the device reads the same whether
        the index points at the intended chain or at a neighbour's. Check the track
        name against the one you meant before confirming.

        Deleting shifts every later device in the chain down one index, so a second
        delete aimed at an index read before the first one lands on a different
        device. Read the chain again with get_devices between deletes.

        Two neighbouring intentions are not this tool. To reorder a chain nothing
        needs deleting: call ``song.move_device`` through lom_call. To swap the
        instrument on a track, load the replacement with load_device, which replaces
        the instrument already there in one step (measured). Deleting first only
        loses the settings earlier.

        ``kind`` reaches the return and main chains, which are addressed the same way
        get_devices addresses them. A return track carries its own index and the main
        track has none, so ``track`` is not read at all when kind is master.
    """
    owner_args, err = _kind_args(kind, track)
    if err is not None:
        return err
    profile = _TRACK_KINDS[kind]
    base: dict[str, Any] = (
        {} if profile["device_param"] is None else {profile["device_param"]: track}
    )

    fields, err = _read_fields(
        [
            (profile["device_name"], {**base, "device": device}),
            (profile["device_class"], {**base, "device": device}),
        ]
    )
    if err is not None:
        return err
    assert fields is not None
    before, err = _count(profile["device_name"], "device", base)
    if err is not None:
        return err
    parameters, err = _count(
        profile["device_param_name"], "param", {**base, "device": device}
    )
    if err is not None:
        return err
    # Read separately because _read_fields keys on the segment after the dot, so
    # track.name and device.name would collide on "name". A second round trip is
    # a fair price for the one field that says which chain is about to lose a
    # device.
    owner, err = _read_fields([(profile["name"], owner_args)])
    if err is not None:
        return err
    assert owner is not None

    loss = {
        "track_name": _value_of(owner, "name"),
        "name": _value_of(fields, "name"),
        "class_name": _value_of(fields, "class_name"),
        "parameter_count": parameters.get("count"),
        "parameter_count_exact": parameters.get("exact"),
    }
    # The main track has no index, so naming one in the report would invent a
    # coordinate the caller cannot act on.
    where = {
        "track": f"track {track}",
        "return": f"return track {track}",
        "master": "the main track",
    }[kind]
    index = None if profile["param"] is None else track

    if not confirm:
        return {
            "ok": False,
            "deleted": False,
            "confirm_required": True,
            "kind": kind,
            "index": index,
            "would_lose": loss,
            "device_count": before.get("count"),
            "error": (
                f"delete_device is destructive. This would remove device {device} "
                f"({loss['name']!r}) from {where} ({loss['track_name']!r}), with "
                "every setting on it and every clip envelope aimed at it, and shift "
                "later devices down one index. A device class sits on more than one "
                "track in most sets, so check that track name against the one you "
                "meant. Call again with confirm=True."
            ),
        }

    deleted = _run(profile["device_delete"], confirm=True, call_args=[device], **base)
    after, err = _count(profile["device_name"], "device", base)
    if err is not None:
        return err
    ok, evidence = _verdict(bool(deleted.get("ok")), _shrank(before, after))
    return {
        "ok": ok,
        "kind": kind,
        "index": index,
        "track": index,
        "track_name": loss["track_name"],
        "deleted_index": device,
        "lost": loss,
        "device_count_before": before.get("count"),
        "device_count_after": after.get("count"),
        "devices_after": after.get("values"),
        "call": deleted,
        **evidence,
    }


# --------------------------------------------------------------------- music
# Measured 2026-09-01 against Live 12.4.5: a sent 0.29 read back as
# 0.29000010406260407 and a sent 0.18 as 0.17999994796869798. Re-measured
# 2026-09-02 against Live 12.4.5 at 124 BPM: both reproduced to every digit, a
# start_time of 2.29 came back as 2.290000104062604, and a duration of 0.5 came
# back exactly.
@mcp.tool(annotations=ADDS)
def write_clip_notes(
    track: TrackArg,
    slot: SlotArg,
    notes: Annotated[
        list[NoteArg],
        Field(
            description=(
                "The notes to write into the clip. Each item declares its own "
                "fields, and the item schema carries their units."
            )
        ),
    ],
    mode: Annotated[
        Literal["replace", "append"],
        Field(
            description=(
                "'replace' updates the clip content to match exactly this note list. "
                "'append' adds to what is there, which layers new notes onto existing ones."
            )
        ),
    ] = "replace",
    verify: Annotated[
        bool,
        Field(
            description=(
                "True reads the notes back and reports the difference against what "
                "was asked for, at the cost of one extra read."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Write MIDI notes into a Session clip.

    Returns:
        Dictionary containing write confirmation, validation reports, and optional diff.

    Note:
        Times are in beats and clip-local, so beat 0 is the clip's own start.

        With mode='replace' (the default), the clip is updated to match the provided note
        list. Live's native note writing appends by default, so mode='replace' ensures a
        second write updates rather than duplicates a melody (measured: 63 + 23 = 86 notes).
        Ask for 'append' by name when layering notes into an existing performance. To
        empty a clip without writing new notes, use delete_clip.

        Use read_clip_notes to inspect existing notes before rewriting them. Use
        quantize_clip to adjust timing to a grid, and transpose_clip to shift pitch
        without altering rhythm.

        A list straight from ``read_clip_notes`` can be written back. Live adds
        ``note_id`` to every note it hands out, which is identity rather than content,
        so the keys in :data:`~live_maestro.music.notes.TOLERATED_NOTE_KEYS` are
        dropped instead of refused and reported as ``input_keys_ignored``. Every other
        unrecognised key is an error.

        ``pitch``, ``start_time`` and ``duration`` are never defaulted. A reader that
        substitutes ``start_time=0.0`` and ``duration=0.25`` for missing keys turns a
        list built with ``pos``/``dur`` (the humanise spelling) into a pile of
        sixteenths stacked on beat 0, and reports success while doing it. Wrong keys are
        refused before anything is sent.

        A note does not come back bit-identical to the note sent. Times and durations
        return with a small deviation in both directions: a sent 0.29 reads back as
        0.29000010406260407 and a sent 0.18 as 0.17999994796869798, about 4e-7 relative,
        and both reproduce to every digit across runs. A ``start_time`` of 2.29 comes back
        as 2.290000104062604, and a duration of 0.5 comes back exactly. The cause is not
        established: it is not a tick grid (no grid of 96, 192, 480 or 960 per quarter
        produces those numbers) and not a single float32 conversion (float32 of 0.29 is
        0.28999999). That 0.5 survives while 0.29 does not is consistent with a
        binary-representable value passing through untouched, but the mechanism is not
        claimed here. At 124 BPM the error is around 50 nanoseconds, so musically it is
        nothing. It matters only for comparison: never test a note time for equality. The
        diff run here already uses a tolerance, which is why it reports ``0 changed`` for
        values that differ in the seventh decimal.
    """
    if mode not in ("replace", "append"):
        return {"ok": False, "error": f"mode must be 'replace' or 'append', not {mode!r}"}

    raw = toolargs.note_dicts(notes)
    report = note_ops.validate_note_dicts(raw)
    if not report.ok:
        return {
            "ok": False,
            "blocked": True,
            "error": "the note list did not validate; nothing was sent",
            "validation": report.to_dict(),
        }
    raw, dropped_on_input = note_ops.without_tolerated_keys(raw)
    try:
        parsed = note_ops.from_dicts(raw)
    except ValueError as exc:
        return {"ok": False, "blocked": True, "error": str(exc)}

    clip_path = _clip_path(track, slot)
    length = _run("clip.length", track=track, slot=slot)
    clip_length = length.get("value") if length.get("ok") else None
    second = note_ops.validate(
        parsed, clip_length=clip_length if isinstance(clip_length, (int, float)) else None
    )
    if not second.ok:
        return {
            "ok": False,
            "blocked": True,
            "error": "the note list did not validate; nothing was sent",
            "validation": second.to_dict(),
        }

    try:
        written = _client_instance().send(
            "notes_set",
            {"path": clip_path, "notes": note_ops.to_dicts(parsed), "mode": mode},
        )
    except LiveError as exc:
        return _live_error(exc)

    result: dict[str, Any] = {
        "ok": True,
        "track": track,
        "slot": slot,
        "path": clip_path,
        "mode": mode,
        "sent": len(parsed),
        "before_count": written.get("before_count"),
        "after_count": written.get("after_count"),
        "written": written.get("written"),
        "warnings": _merged_warnings(report, second),
        "notes": [NOTES_REPLACE_NOTE, STORED_NOT_AUDIBLE],
    }
    if dropped_on_input:
        result["input_keys_ignored"] = dropped_on_input
    if written.get("dropped_fields"):
        result["dropped_fields"] = written["dropped_fields"]

    if not verify:
        result["verified"] = None
        result["verified_note"] = "not checked: verification was not requested"
        return result

    try:
        back = _client_instance().send("notes_get", {"path": clip_path})
    except LiveError as exc:
        result["verified"] = None
        result["verified_note"] = f"read-back failed, so nothing is proven: {exc}"
        return result

    try:
        actual, dropped_keys = _parse_notes(back.get("notes") or [])
    except ValueError as exc:
        result["verified"] = None
        result["verified_note"] = f"read-back was not parseable, so nothing is proven: {exc}"
        return result

    expected = parsed if mode == "replace" else None
    if expected is None:
        result["verified"] = None
        result["verified_note"] = (
            "append mode: the clip now holds the old notes plus the new ones, so a diff "
            "against what was sent cannot decide anything. read_clip_notes shows the result."
        )
        result["note_count_after"] = back.get("count")
        return result

    changes = note_ops.diff(expected, actual, ignore_defaulted_extensions=True)
    if dropped_keys:
        result["read_back_keys_ignored"] = dropped_keys
    result["verified"] = changes.is_empty
    result["diff"] = {
        "summary": changes.summary(),
        "added": [n.to_dict() for n in changes.added[:20]],
        "removed": [n.to_dict() for n in changes.removed[:20]],
        "changed": [c.to_dict() for c in changes.changed[:20]],
        "unchanged": changes.unchanged,
        "truncated": max(len(changes.added), len(changes.removed), len(changes.changed)) > 20,
    }
    result["note_count_after"] = back.get("count")
    return result


# Measured 2026-08-30: surveying note density across 59 drum clips read every note
# of every clip, and one clip alone came back as 384 notes and 57k characters, past
# the tool-result cap. Measured 2026-08-30 against Live 12.4.5: four notes written
# at beats 0, 1, 2, 3 with pitches 72, 60, 67, 62 came back 60, 62, 67, 72.
@mcp.tool(annotations=READS)
def read_clip_notes(
    track: TrackArg,
    slot: SlotArg,
    from_time: Annotated[
        float | None,
        Field(
            description=(
                "First beat of the window to read, clip-local, where 0 is the clip "
                "start. Omit to read from the beginning."
            )
        ),
    ] = None,
    time_span: Annotated[
        float | None,
        Field(
            description=(
                "Length of the window in beats, not the beat it ends on: beats 24 "
                "to 32 is from_time=24, time_span=8. Omit to read to the end."
            )
        ),
    ] = None,
    count_only: Annotated[
        bool,
        Field(
            description=(
                "True counts the notes inside Live and returns the number without "
                "transferring them, which is the way to ask about a large clip."
            )
        ),
    ] = False,
    check: Annotated[
        bool,
        Field(
            description=(
                "True runs validation over the notes that came back and reports "
                "what it found. Ignored when count_only is true, since there are no "
                "notes to check."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Read MIDI notes of a Session clip, query a specific window, or count notes.

    Returns:
        Dictionary containing notes, count, window parameters, and validation report.

    Note:
        Times and positions are expressed in clip-local beats. Muted notes are included
        and carry ``mute: true``. ``time_span`` is a length in beats, not an end beat:
        beats 24 to 32 is ``from_time=24, time_span=8``.

        Windowing and counting keep a small question small. A single drum clip of 384
        notes comes back as 57k characters, past the tool-result cap, so a survey across
        59 clips cannot be a full read of each. The LOM has no ``note_count`` on ``Clip``
        at all, so a count taken out here means transferring every note to measure it.
        ``count_only=True`` counts inside Live and carries back one integer.

        With ``count_only`` there is nothing to validate, so ``check`` is ignored. With a
        window, ``check`` sees the window and not the clip.

        Returned notes are sorted by time, then pitch. Live hands its notes back ordered
        by pitch, which is the one order a musical instruction never means: "the third
        note", "every other note" and "the last note" are all about when a note sounds.
        Four notes written at beats 0, 1, 2, 3 with pitches 72, 60, 67, 62 come back 60,
        62, 67, 72, so taking every other entry off the raw list picks alternating pitches
        rather than alternating beats, silently and plausibly. For Live's own order call
        ``get_notes_extended`` through ``lom_call``.
    """
    clip_path = _clip_path(track, slot)
    params: dict[str, Any] = {"path": clip_path}
    if from_time is not None:
        params["from_time"] = from_time
    if time_span is not None:
        params["time_span"] = time_span
    if count_only:
        params["count_only"] = True
    try:
        got = _client_instance().send("notes_get", params)
    except LiveError as exc:
        return _live_error(exc)

    windowed = from_time is not None or time_span is not None
    if count_only:
        return {
            "ok": True,
            "track": track,
            "slot": slot,
            "path": clip_path,
            "count": got.get("count"),
            "window": got.get("window"),
            "note": (
                "Counted inside Live; no notes were carried back. "
                + ("The count is for the window, not the clip." if windowed else "")
            ).strip(),
        }

    raw = got.get("notes") or []
    if isinstance(raw, list):
        raw = sorted(
            raw,
            key=lambda note: (
                (note.get("start_time"), note.get("pitch"))
                if isinstance(note, dict)
                and isinstance(note.get("start_time"), (int, float))
                and isinstance(note.get("pitch"), (int, float))
                else (float("inf"), float("inf"))
            ),
        )
    payload: dict[str, Any] = {
        "ok": True,
        "track": track,
        "slot": slot,
        "path": clip_path,
        "count": got.get("count", len(raw)),
        "notes": raw,
        "api": got.get("api"),
        "note": "Times are beats, clip-local: beat 0 is the clip's own start.",
    }
    if windowed:
        payload["window"] = got.get("window")
        end_desc = (from_time or 0) + time_span if time_span is not None else "the end"
        payload["window_note"] = (
            "These are the notes of one window, not the clip. time_span is a length "
            f"in beats, so this window is {from_time or 0} to {end_desc}."
        )
    if not check:
        return payload
    try:
        parsed, dropped_keys = _parse_notes(raw)
    except ValueError as exc:
        payload["check"] = {"ok": False, "error": str(exc)}
        return payload
    if dropped_keys:
        payload["keys_ignored"] = dropped_keys
        payload["keys_ignored_note"] = (
            "Live adds these to every note it hands back; they are identity, not content, "
            "and this server does not model or write them."
        )
    length = _run("clip.length", track=track, slot=slot)
    clip_length = length.get("value") if length.get("ok") else None
    report = note_ops.validate(
        parsed, clip_length=clip_length if isinstance(clip_length, (int, float)) else None
    )
    low, high = note_ops.span(parsed) if parsed else (0.0, 0.0)
    payload["check"] = report.to_dict()
    payload["span_beats"] = [low, high]
    payload["clip_length"] = clip_length
    return payload


@mcp.tool(annotations=SETS)
def quantize_clip(
    track: TrackArg,
    slot: SlotArg,
    grid: Annotated[
        float,
        Field(
            description=(
                "Grid to snap to, in beats: 1.0 is a quarter note, 0.5 an eighth, "
                "0.25 a sixteenth, and 0.3333 a triplet eighth. Must be positive."
            )
        ),
    ] = 0.25,
    strength: Annotated[
        float,
        Field(
            description=(
                "How far to move each note towards the grid, 0.0 for not at all and "
                "1.0 for all the way. 0.5 halves the distance and keeps some of the "
                "original feel."
            )
        ),
    ] = 1.0,
    quantize_ends: Annotated[
        bool,
        Field(
            description=(
                "True snaps note ends to the grid too, which changes durations. "
                "False moves onsets and leaves every duration as it was."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Quantize MIDI clip note positions and lengths to a specified beat grid.

    Reads notes, calculates quantized positions, writes back using mode='replace',
    and verifies the result.

    Returns:
        Dictionary with the write result, the number of notes moved, and how far.

    Note:
        Parameter interactions: ``strength`` scales note displacement towards ``grid`` from
        0.0 (raw timing preserved) to 1.0 (full snap). Use partial strength (e.g. 0.5) to
        tighten timing while preserving human groove feel. Setting ``quantize_ends=True``
        snaps note ends to the grid too, adjusting note lengths. In contrast,
        ``quantize_ends=False`` shifts note onsets only and preserves articulation.

        Quantising modifies notes in place and is not undoable. Read the notes first with
        read_clip_notes if the original unquantized timing might be needed later.

        To shift pitch instead of timing, use transpose_clip. To write custom or micro-timed
        notes off-grid, use write_clip_notes.
    """
    if grid <= 0:
        return {"ok": False, "error": f"grid must be positive (beats), got {grid}"}
    if not 0.0 <= strength <= 1.0:
        return {"ok": False, "error": f"strength must be between 0.0 and 1.0, got {strength}"}

    clip_path = _clip_path(track, slot)
    try:
        got = _client_instance().send("notes_get", {"path": clip_path})
        before, _ignored = _parse_notes(got.get("notes") or [])
    except LiveError as exc:
        return _live_error(exc)
    except ValueError as exc:
        return {"ok": False, "error": f"the clip's notes did not parse: {exc}"}
    if not before:
        return {"ok": True, "track": track, "slot": slot, "moved": 0,
                "note": "The clip has no notes; nothing to quantize."}

    after = note_ops.quantize_times(
        before, grid, strength=strength, quantize_ends=quantize_ends
    )
    changes = note_ops.diff(before, after, match_on="pitch")
    written = write_clip_notes(track, slot, note_ops.to_dicts(after), mode="replace", verify=True)
    written["quantize"] = {
        "grid_beats": grid,
        "strength": strength,
        "quantize_ends": quantize_ends,
        "note_count": len(before),
        "planned_changes": changes.summary(),
    }
    written.setdefault("notes", []).append(
        "Test binary subdivisions before triplets: 0.5 is not a multiple of 1/3 but is a "
        "multiple of 1/6, so a triplet-first test reads every eighth as a sixteenth triplet."
    )
    return written


# Measured 2026-08-29 against Live 12.4.5: an audio clip is transposed by writing
# clip.pitch_coarse, and the write is relative there too.
@mcp.tool(annotations=ADDS)
def transpose_clip(
    track: TrackArg,
    slot: SlotArg,
    semitones: Annotated[
        int,
        Field(
            description=(
                "How far to move the pitch, in semitones. 12 is an octave up, -12 "
                "an octave down. Relative to where the clip is now, not absolute."
            )
        ),
    ],
    out_of_range: Annotated[
        Literal["error", "clamp", "drop"],
        Field(
            description=(
                "What to do with a MIDI note pushed outside 0..127: 'error' "
                "refuses the whole call and changes nothing, 'clamp' pins the note "
                "to 0 or 127 and keeps it, 'drop' discards it. Only 'error' leaves "
                "the clip recoverable by transposing back."
            )
        ),
    ] = "error",
) -> dict[str, Any]:
    """Transpose a MIDI clip by note pitch or an audio clip via pitch_coarse.

    Relative on both paths, so calling it twice with 2 moves the clip by 4 and there
    is no absolute target to aim at. A MIDI clip has every note pitch rewritten. An
    audio clip is repitched as a whole instead.

    Returns:
        Dictionary reporting the transposition, the notes affected, and any that hit
        the range limit.

    Note:
        When to use: Shift the overall pitch of an entire clip by a constant semitone offset.
        When NOT to use: Do not use transpose_clip to edit melody contours, re-voice chords,
        or correct scale degrees. Use write_clip_notes instead. Do not use it for timing or
        groove changes, which are handled by quantize_clip.

        An audio clip is transposed by writing clip.pitch_coarse. The write is relative
        there too, because the current value is read first and the semitones added to it,
        so repeated calls do keep climbing. Live documents the bound as -48 to 48 but that
        figure is not from a probe here. The read-back in the answer is the evidence for
        any one write.

        ``out_of_range='clamp'`` and ``'drop'`` both lose information that
        transposing back will not return: clamped notes have collapsed onto one
        pitch and dropped ones are gone. Keep the default 'error' unless the clip
        has been read out with read_clip_notes first.
    """
    if out_of_range not in ("error", "clamp", "drop"):
        return {"ok": False, "error": "out_of_range must be 'error', 'clamp' or 'drop'"}

    kind = _run("clip.is_midi_clip", track=track, slot=slot)
    if not kind.get("ok"):
        return kind
    if not kind.get("value"):
        current = _run("clip.pitch_coarse", track=track, slot=slot)
        base = current.get("value") if current.get("ok") else None
        target = semitones if base is None else int(base) + semitones
        moved = _run("clip.pitch_coarse", track=track, slot=slot, value=target)
        moved["audio_clip"] = True
        moved["pitch_coarse_before"] = base
        moved["requested_semitones"] = semitones
        moved["note"] = (
            "Audio clip: transposed with pitch_coarse, not with notes. pitch_coarse is "
            "catalogued as verified (measured 2026-08-29 on Live 12.4.5), but the -48..48 "
            "bound is Live's documentation rather than a probe; the read-back above is the "
            "evidence for this write."
        )
        return moved

    clip_path = _clip_path(track, slot)
    try:
        got = _client_instance().send("notes_get", {"path": clip_path})
        before, _ignored = _parse_notes(got.get("notes") or [])
    except LiveError as exc:
        return _live_error(exc)
    except ValueError as exc:
        return {"ok": False, "error": f"the clip's notes did not parse: {exc}"}
    if not before:
        return {"ok": True, "track": track, "slot": slot, "moved": 0,
                "note": "The clip has no notes; nothing to transpose."}

    try:
        after = note_ops.transpose(before, semitones, out_of_range=out_of_range)
    except ValueError as exc:
        return {"ok": False, "blocked": True, "error": str(exc)}

    written = write_clip_notes(track, slot, note_ops.to_dicts(after), mode="replace", verify=True)
    written["transpose"] = {
        "semitones": semitones,
        "out_of_range": out_of_range,
        "notes_in": len(before),
        "notes_out": len(after),
        "dropped": len(before) - len(after),
    }
    return written


# ----------------------------------------------------------------------- mix
@mcp.tool(annotations=SETS)
def set_mix(
    track: TrackArg,
    volume: Annotated[
        float | None,
        Field(
            description=(
                "Fader position, normalised 0.0 to 1.0, not decibels. 0.85 is 0 dB "
                "(measured) and the scale between is not linear, so 0.425 is not "
                "half the level. Omit to leave the fader alone."
            )
        ),
    ] = None,
    pan: Annotated[
        float | None,
        Field(
            description=(
                "Pan position from -1.0 hard left through 0.0 centre to 1.0 hard "
                "right. Omit to leave it alone."
            )
        ),
    ] = None,
    sends: Annotated[
        dict[str, float] | None,
        Field(
            description=(
                "Send levels to write, keyed by send letter as Live labels it "
                "('A', 'B') or by index as a string ('0', '1'), with normalised "
                "values 0.0 to 1.0. Only the sends named are written. Omit to leave "
                "every send alone."
            )
        ),
    ] = None,
    mute: Annotated[
        bool | None,
        Field(
            description=(
                "True mutes the track, False unmutes it. Not supported on the "
                "master track. Omit to leave it alone."
            )
        ),
    ] = None,
    solo: Annotated[
        bool | None,
        Field(
            description=(
                "True solos the track, which silences the others rather than "
                "changing anything on this one. Not supported on the master track. "
                "Omit to leave it alone."
            )
        ),
    ] = None,
    kind: KindArg = "track",
) -> dict[str, Any]:
    """Set volume, pan, sends, mute and solo on one track in a single round trip.

    Every argument is optional and only the ones given are written, so this is the
    cheap way to change several mixer controls at once. Read the current state with
    get_track first, since the values here are absolute and not relative.

    Returns:
        Dictionary reporting each write with its before and after value, and the dB
        display where Live offered one.

    Note:
        Volume and pan are normalised, so 0.85 is 0 dB (measured) rather than 85 per
        cent of anything. Read ``display`` in the answer for Live's own dB reading
        instead of converting the normalised number yourself.

        The master track has no mute and no solo. Both are refused there rather than
        silently ignored, and ``track`` is not read at all when kind is master.

        This is the mixer only. For a knob on a device in the chain use
        set_parameter, and for the tempo use set_tempo. To make a mixer control move
        over time rather than jump, write an envelope with write_automation.
    """
    args, err = _kind_args(kind, track)
    if err is not None:
        return err
    profile = _TRACK_KINDS[kind]

    ops: list[dict[str, Any]] = []
    labels: list[str] = []
    for field_name, value in (("volume", volume), ("panning", pan), ("mute", mute),
                              ("solo", solo)):
        if value is None:
            continue
        spec_id = profile.get(field_name if field_name != "panning" else "panning")
        if spec_id is None:
            return {"ok": False, "error": f"{kind} tracks have no {field_name}"}
        ops.append({"id": spec_id, **args, "value": value})
        labels.append(field_name)

    if sends:
        send_id = profile.get("send")
        if send_id is None:
            return {"ok": False, "error": f"{kind} tracks have no sends"}
        for key, amount in sends.items():
            index = _send_index(key)
            if index < 0:
                return {
                    "ok": False,
                    "error": (
                        f"send key {key!r} is not a letter (A, B, …) or an index ('0', '1', …)"
                    ),
                }
            ops.append({"id": send_id, **args, "send": index, "value": amount})
            labels.append(f"send[{index}]")

    if not ops:
        return {"ok": False, "error": "nothing to set: give volume, pan, sends, mute or solo"}

    results, err = _run_batch(ops, verify=True)
    if err is not None:
        return err
    assert results is not None
    changes = {label: row for label, row in zip(labels, results, strict=False)}
    return {
        "ok": all(row.get("ok") for row in results),
        "kind": kind,
        "index": None if profile["param"] is None else track,
        "round_trips": 1,
        "changes": changes,
        "notes": [NORMALISED_NOTE, BATCH_NOT_ATOMIC, STORED_NOT_AUDIBLE],
    }


def _resolve_device_parameter(
    head: str, device: int, parameter: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a parameter given as an index or a name to its index on the device.

    Three routes, in falling order of confidence: a plain integer is taken as the
    index. A name goes to ``find_parameter``. If the device does not answer that,
    the names are probed by index and ranked. A name matching several parameters is
    refused rather than guessed at, which is why the ranked list travels back with the
    answer.

    Returns:
        ``(resolution, None)`` with keys ``index``, ``matches`` and ``resolved_by``,
        or ``(None, error)`` ready to hand to the caller.
    """
    text = parameter.strip()
    if text.lstrip("-").isdigit():
        return {"index": int(text), "matches": [], "resolved_by": "index"}, None

    try:
        found = introspect.find_parameter(_client_instance(), head, device, text)
    except LiveError as exc:
        return None, _live_error(exc)
    except (introspect.IntrospectionError, ValueError) as exc:
        return None, {"ok": False, "error": str(exc)}
    if found:
        return {
            "index": found[0].index,
            "matches": [m.to_dict() for m in found],
            "resolved_by": "introspection",
        }, None

    probed, perr = _device_parameter_names(head, device)
    if perr is not None:
        return None, perr
    view = _view_from_names(head, device, probed.get("values", []))
    ranked = introspect.rank_parameters(view, text) if view.parameters else []
    if not ranked:
        names = [p.name for p in view.parameters]
        shown = names[:30]
        out: dict[str, Any] = {
            "ok": False,
            "error": f"no parameter matching {text!r} on device {device}",
            "available": shown,
            "note": COLLECTION_NOTE,
        }
        # An Operator has 195 parameters. Handing back the first 30 with no
        # sign of that reads as the whole list, and a caller who does not
        # find their name concludes it is absent.
        if len(names) > len(shown):
            out["available_truncated"] = True
            out["available_count"] = len(names)
            out["available_note"] = (
                f"showing {len(shown)} of {len(names)} parameter names. For the "
                "full list call describe with with_parameters=True on this device."
            )
        if view.parameter_count <= 1:
            out["advice"] = CONFIGURE_NOTE
        return None, out
    return {
        "index": ranked[0].index,
        "matches": [m.to_dict() for m in ranked],
        "resolved_by": "index probe",
    }, None


# Measured 2026-08-30 against Live 12.4.5: a quantized parameter takes discrete
# steps, so a written 0.5 can legitimately read back as something else.
@mcp.tool(annotations=SETS)
def set_parameter(
    track: TrackArg,
    device: Annotated[
        int,
        Field(
            description=(
                "Device index in that track chain, counted from 0. get_devices "
                "lists the chain with its indices."
            )
        ),
    ],
    parameter: Annotated[
        str,
        Field(
            description=(
                "Which parameter, either its index as a string ('1') or its name, "
                "which may be a glob ('Attack*'). A name that matches more than one "
                "parameter is refused rather than guessed at. ``describe`` with "
                "with_parameters=True lists the names."
            )
        ),
    ],
    value: Annotated[
        float,
        Field(
            description=(
                "Target value in the parameter own units, which are normalised and "
                "device-specific rather than the unit the device displays. Read "
                "``min`` and ``max`` with ``describe`` first: the range is whatever "
                "the device declares and is often not 0..1."
            )
        ),
    ],
    kind: KindArg = "track",
) -> dict[str, Any]:
    """Set one device parameter by index or name and verify the read-back.

    Returns:
        Dictionary reporting the write, the parameter index it resolved to, the
        read-back value, and the display string where the device offered one.

    Note:
        Parameter values are normalised, not the unit the device displays. Read ``min``
        and ``max`` (``describe`` with ``with_parameters=True``) rather than assuming
        a range.

        The curve from 0..1 onto the displayed unit is not linear and differs per
        device. On one third-party compressor Attack is ``v^4 * 1000 ms``, so writing
        "10 ms" linearly lands at 316 ms, a factor of 30, silently. Where the device
        reports a display, aim with the display. Where it does not (all VST2), write
        normalised and calibrate by eye once.

        A parameter that shows a display also answers ``str_for_value``: lom_call with the
        parameter path, ``method='str_for_value'`` and one value returns the string that
        value would display, and moves nothing. That maps a unit onto the scale without
        deriving the taper by trial (catalog row param.str_for_value).

        A quantized parameter takes discrete steps, so a written 0.5 can legitimately read
        back as something else, and that is reported as a clamp rather than as a failure.
    """
    _, err = _kind_args(kind, track)
    if err is not None:
        return err
    profile = _TRACK_KINDS[kind]
    device_arg = profile["device_param"]
    base: dict[str, Any] = {} if device_arg is None else {device_arg: track}
    head = _track_path_for(kind, track)

    resolved, err = _resolve_device_parameter(head, device, parameter)
    if err is not None:
        return err
    assert resolved is not None
    index = resolved["index"]
    matches = resolved["matches"]
    resolved_by = resolved["resolved_by"]

    written = _run(
        profile["parameter"], **base, device=device, param=index, value=value
    )
    written["parameter_index"] = index
    written["resolved_by"] = resolved_by
    written["matches"] = matches
    written["notes"] = [NORMALISED_NOTE, STORED_NOT_AUDIBLE]
    return written


#: How far inside the range the probes stay, as a fraction of the span. Never 0:
#: asking for a display string at param.min or param.max has taken Live down inside
#: a plugin's own formatting code, where no try/except reaches it (docs/limits.md,
#: 'Asking for a display string at an endpoint (Live)').
_DISPLAY_PROBE_INSET = 0.01

#: Probes in the first pass. One batch, one round trip, nothing written.
_DISPLAY_COARSE_SAMPLES = 48

#: Probes in the second pass, inside the bracket the first pass found.
_DISPLAY_REFINE_SAMPLES = 16


def _probe_grid(low: float, high: float, count: int) -> list[float]:
    """Return ``count`` values spread across ``low``..``high``, never touching the ends."""
    span = high - low
    inset = span * _DISPLAY_PROBE_INSET
    start, stop = low + inset, high - inset
    if count < 2 or stop <= start:
        return [(low + high) / 2.0]
    step = (stop - start) / (count - 1)
    return [start + step * i for i in range(count)]


def _sample_displays(
    row: str, base: Mapping[str, Any], device: int, index: int, values: Sequence[float]
) -> tuple[list[tuple[float, str, float | None, str | None]] | None, dict[str, Any] | None]:
    """Ask the device what each value would read as, in one round trip, writing nothing."""
    ops = [
        {"id": row, **base, "device": device, "param": index, "call_args": [v]}
        for v in values
    ]
    results, err = _run_batch(ops)
    if err is not None:
        return None, err
    assert results is not None
    out: list[tuple[float, str, float | None, str | None]] = []
    for value, result in zip(values, results, strict=False):
        if not result.get("ok"):
            continue
        shown = result.get("value")
        if not isinstance(shown, str):
            continue
        number, unit = introspect.parse_display(shown)
        out.append((value, shown, number, unit))
    return out, None


@mcp.tool(annotations=SETS)
def set_parameter_display(
    track: TrackArg,
    device: Annotated[
        int,
        Field(
            description=(
                "Device index in that track chain, counted from 0. get_devices "
                "lists the chain with its indices."
            )
        ),
    ],
    parameter: Annotated[
        str,
        Field(
            description=(
                "Which parameter, either its index as a string ('1') or its name, "
                "which may be a glob ('Attack*'). A name that matches more than one "
                "parameter is refused rather than guessed at."
            )
        ),
    ],
    target: Annotated[
        float,
        Field(
            description=(
                "The reading you want, as a bare number in the base unit: -26 for "
                "-26 dB, 8000 for 8 kHz, 150 for 150 ms, 4 for a 4:1 ratio. Not the "
                "normalised value - that is set_parameter."
            )
        ),
    ],
    unit: Annotated[
        str | None,
        Field(
            description=(
                "Which unit the target is in: 'dB', 'Hz', 'ms', '%', 'ratio', 'st', "
                "'cents' or 'x'. Give kHz as Hz and seconds as ms. Optional: when "
                "omitted the unit the device reports is used, and a device reporting "
                "a different unit than you expected is a refusal, not a guess."
            )
        ),
    ] = None,
    kind: KindArg = "track",
) -> dict[str, Any]:
    """Set a device parameter by the reading it should show, not by its raw value.

    Returns:
        Dictionary reporting the value written, the display it produced, the residual
        error against the target, the unit, and how many probes and round trips it
        took. On refusal, why - including the case where the device reports no units.

    Note:
        This is the inverse of ``set_parameter``, and it exists because there is no
        formula to invert. The curve from the raw range onto the displayed unit
        differs per parameter and is rarely linear: on one measured third-party
        compressor Attack is ``v^4 * 1000 ms``, Ratio is ``20^v`` and Threshold about
        ``40 * log10(v)``. Writing "10" at a parameter that displays milliseconds
        lands at 316 ms there, a factor of 30, and reports success.

        So the taper is sampled rather than computed: one batch of probes asks the
        device what each raw value *would* read as, the answers are bracketed around
        the target, a second batch refines inside that bracket, and only then is a
        single value written and read back. Two round trips, and nothing in the set
        moves until the last step.

        That last point is the reason not to bisect with real writes instead. A
        probing bisection that does not restore what it touched has been measured
        leaving Delay Feedback at 100 % - self-oscillation - and Saturator Drive at
        100 % across 20 parameters (catalog row return_device.str_for_value). The
        probes here write nothing at all.

        The probes stay strictly inside the range and never ask at ``min`` or
        ``max``: an endpoint has crashed Live inside a plugin's own formatting code,
        which is native code no try/except reaches (docs/limits.md, 'Asking for a
        display string at an endpoint (Live)').

        Where the device reports no unit this refuses instead of aiming. Every VST2
        reports a bare number (0 of 36 measured), which is formally a number and
        physically meaningless, so a display-driven search there would walk the
        control to its end stop and call it a result. For those, use ``set_parameter``
        with a normalised value and calibrate by ear once.

        The result is the stored value and its display, which is not audibility: a
        parameter on a device that is switched off reads back exactly the same.
    """
    _, err = _kind_args(kind, track)
    if err is not None:
        return err
    profile = _TRACK_KINDS[kind]
    row = profile["param_str_for_value"]
    device_arg = profile["device_param"]
    base: dict[str, Any] = {} if device_arg is None else {device_arg: track}
    head = _track_path_for(kind, track)

    resolved, err = _resolve_device_parameter(head, device, parameter)
    if err is not None:
        return err
    assert resolved is not None
    index = int(resolved["index"])

    try:
        view = introspect.describe_device(
            _client_instance(), head, device, with_value_items=False
        )
    except LiveError as exc:
        return _live_error(exc)
    except introspect.IntrospectionError as exc:
        return {"ok": False, "error": str(exc)}

    found = [p for p in view.parameters if p.index == index]
    if not found:
        return {
            "ok": False,
            "error": f"device {device} has no parameter at index {index}",
            "parameter_count": view.parameter_count,
            "note": COLLECTION_NOTE,
        }
    param = found[0]
    if param.minimum is None or param.maximum is None:
        return {
            "ok": False,
            "error": (
                f"{param.name!r} does not report a range, so there is nothing to search"
            ),
            "advice": "set_parameter writes a raw value without needing the range.",
        }
    low, high = param.minimum, param.maximum
    if high <= low:
        return {
            "ok": False,
            "error": f"{param.name!r} reports an empty range {low}..{high}",
        }
    if param.is_quantized:
        return {
            "ok": False,
            "error": (
                f"{param.name!r} is quantized: it takes named steps, not a continuous "
                "reading, so aiming at a display value is the wrong operation"
            ),
            "advice": (
                "describe with with_parameters=True lists the steps in 'value_items'. "
                "Write the index of the step you want with set_parameter."
            ),
        }

    wanted_unit = unit.strip() if unit else None
    if wanted_unit is not None and wanted_unit not in introspect.DISPLAY_BASE_UNITS:
        return {
            "ok": False,
            "error": f"unit {wanted_unit!r} is not one this server reads",
            "available": list(introspect.DISPLAY_BASE_UNITS),
        }

    coarse, err = _sample_displays(
        row, base, device, index, _probe_grid(low, high, _DISPLAY_COARSE_SAMPLES)
    )
    if err is not None:
        return err
    assert coarse is not None
    usable = [(v, shown, num, u) for v, shown, num, u in coarse if num is not None]
    if not usable:
        seen = [shown for _, shown, _, _ in coarse[:4]]
        return {
            "ok": False,
            "error": (
                f"{param.name!r} reports no unit, so a target reading cannot be aimed at"
            ),
            "sample_displays": seen,
            "advice": (
                "A bare number with no unit is what every VST2 answers (0 of 36 "
                "measured): it is parseable and physically meaningless. Use "
                "set_parameter with a normalised value between "
                f"{low} and {high} and calibrate once by ear."
            ),
            "notes": [NORMALISED_NOTE],
        }

    units = {u for _, _, _, u in usable if u}
    device_unit = min(units) if len(units) == 1 else None
    if wanted_unit is None:
        if device_unit is None:
            return {
                "ok": False,
                "error": (
                    f"{param.name!r} reports more than one unit across its range "
                    f"({', '.join(sorted(u for u in units if u))}), so name the one you mean"
                ),
                "advice": "Pass unit= to say which reading the target is in.",
            }
        wanted_unit = device_unit
    elif device_unit is not None and device_unit != wanted_unit:
        return {
            "ok": False,
            "error": (
                f"{param.name!r} reads in {device_unit}, and the target was given in "
                f"{wanted_unit}"
            ),
            "advice": (
                "kHz goes in as Hz and seconds as ms. Otherwise this is the wrong "
                "parameter for that target."
            ),
        }

    matching = [(v, shown, num) for v, shown, num, u in usable if u == wanted_unit]
    if not matching:
        return {
            "ok": False,
            "error": f"no probe of {param.name!r} read in {wanted_unit}",
        }

    def _closest(
        rows: Sequence[tuple[float, str, float]],
    ) -> tuple[float, str, float]:
        return min(rows, key=lambda row_: abs(row_[2] - target))

    best = _closest(matching)
    round_trips = 1
    probes = len(coarse)

    # The coarse pass locates the target between two probes; the taper inside that
    # bracket is short enough that a second, denser pass gets closer than any
    # interpolation over an unknown curve would.
    ordered = sorted(matching, key=lambda row_: row_[0])
    readings = [num for _, _, num in ordered]
    span_low = span_high = best[0]
    for (v_a, _, n_a), (v_b, _, n_b) in itertools.pairwise(ordered):
        if (n_a - target) * (n_b - target) <= 0:
            span_low, span_high = v_a, v_b
            break

    if span_high > span_low:
        refined, err = _sample_displays(
            row,
            base,
            device,
            index,
            _probe_grid(span_low, span_high, _DISPLAY_REFINE_SAMPLES),
        )
        if err is not None:
            return err
        assert refined is not None
        closer = [(v, shown, num) for v, shown, num, u in refined if num is not None
                  and u == wanted_unit]
        round_trips += 1
        probes += len(refined)
        if closer:
            best = min([best, _closest(closer)], key=lambda row_: abs(row_[2] - target))

    chosen_value, chosen_display, chosen_number = best
    written = _run(
        profile["parameter"], **base, device=device, param=index, value=chosen_value
    )
    round_trips += 1
    written["parameter"] = param.name
    written["parameter_index"] = index
    written["resolved_by"] = resolved["resolved_by"]
    written["matches"] = resolved["matches"]
    written["target"] = target
    written["unit"] = wanted_unit
    written["display_expected"] = chosen_display
    written["reading"] = chosen_number
    written["error"] = round(chosen_number - target, 6)
    written["value_range"] = [low, high]
    written["display_range"] = [min(readings), max(readings)]
    written["probes"] = probes
    written["round_trips"] = round_trips
    written["notes"] = [STORED_NOT_AUDIBLE]
    # Two different failures look alike from here, and saying the wrong one is worse
    # than saying neither: the target can lie beyond everything the device displays,
    # or the probes can simply have been too sparse to bracket it.
    if target < min(readings) or target > max(readings):
        written["out_of_range"] = True
        written["out_of_range_note"] = (
            f"the device never displayed {target} {wanted_unit} anywhere in its range: "
            f"it reads {min(readings)} to {max(readings)} {wanted_unit}. This is the "
            f"closest it gets: {chosen_display}."
        )
    elif len(matching) < 3:
        # A device can report units across part of its range only. Two readable
        # probes are not a sampled taper, and the result should not read like one.
        written["thinly_sampled"] = True
        written["thinly_sampled_note"] = (
            f"only {len(matching)} of {len(coarse)} probes read in {wanted_unit}, so the "
            "taper was barely sampled and this value is a nearest guess rather than a "
            "bracketed result. Check 'error' before relying on it."
        )
    return written


# ---------------------------------------------------------------- automation
@mcp.tool(annotations=ADDS)
def write_automation(
    track: TrackArg,
    slot: SlotArg,
    parameter: ParameterPathArg,
    points: Annotated[
        list[list[float]],
        Field(
            description=(
                "The breakpoints, as [[beat, value], ...] in clip-local beats where "
                "0 is the clip start. Values are in the parameter own normalised "
                "units, the same ones set_parameter takes, so read min and max with "
                "``describe`` rather than assuming 0..1."
            )
        ),
    ],
    interpolation: Annotated[
        Literal["linear", "hold", "exponential", "ease_in", "ease_out"],
        Field(
            description=(
                "Shape between consecutive breakpoints. 'linear' ramps straight, "
                "'hold' steps at each breakpoint and stays flat between them, and "
                "the other three bend the ramp using ``exponent``."
            )
        ),
    ] = "linear",
    exponent: Annotated[
        float,
        Field(
            description=(
                "How hard the non-linear shapes bend. 1.0 is effectively linear and "
                "higher is steeper. Ignored by 'linear' and 'hold'."
            )
        ),
    ] = 2.0,
    resolution: Annotated[
        float,
        Field(
            description=(
                "Step in beats at which the curve is written out between "
                "breakpoints, so 0.0625 lays a point every sixteenth of a beat. "
                "Finer follows the shape more closely and writes more points; "
                "ignored for 'hold', which needs no intermediate points."
            )
        ),
    ] = 0.0625,
    clear_first: Annotated[
        bool,
        Field(
            description=(
                "True resets the parameter envelope before writing, so the result "
                "matches ``points`` exactly. False overlays new breakpoints onto "
                "earlier points and mixes the two."
            )
        ),
    ] = True,
    verify: Annotated[
        bool,
        Field(
            description=(
                "True samples the stored envelope back and compares it against the "
                "curve that was generated, at the cost of one extra read."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Write an automation envelope into a Session clip and verify the read-back.

    Turns a handful of breakpoints into a written curve, then reads it back so the
    result reports the stored envelope rather than the requested one.

    Returns:
        Dictionary reporting the write, how many points were laid down, and the
        comparison between the stored curve and the generated one.

    Note:
        Automation lives in Session clips. There is no way to write an Arrangement
        envelope directly here: write it into the Session clip and then copy the clip
        onto the timeline with arrange, which carries the envelope with it.

        A verified write proves the stored curve, never that it is audible. An
        envelope on a parameter of a device that is switched off, or on a muted
        track, reads back exactly as written and changes nothing anyone can hear.

        With ``clear_first=False`` a repeated call layers points onto the ones
        already there, so a retry after an unclear result can leave a curve that
        matches neither attempt. Read it with read_automation before writing again.
        To explicitly clear an envelope without writing new points, use clear_automation.

        Playing the clip leaves the parameter at the value this curve last reached, and
        stopping does not restore it. Capture the statics you care about before the first
        playthrough: once a curve has run, nothing reports the value it covered.
    """
    try:
        paths.validate(parameter)
    except paths.PathSyntaxError as exc:
        return {"ok": False, "error": f"invalid parameter path: {exc}"}
    try:
        curve = auto.envelope(points, interpolation=interpolation, exponent=exponent)
    except ValueError as exc:
        return {"ok": False, "blocked": True, "error": str(exc)}

    clip_path = _clip_path(track, slot)
    params: dict[str, Any] = {
        "path": clip_path,
        "parameter": parameter,
        "clear_first": clear_first,
        **auto.to_write_params(curve, resolution=resolution),
    }
    try:
        written = _client_instance().send("automation_write", params)
    except LiveError as exc:
        return _live_error(exc)

    result: dict[str, Any] = {
        "ok": True,
        "track": track,
        "slot": slot,
        "path": clip_path,
        "parameter": parameter,
        "parameter_name": written.get("parameter_name"),
        "breakpoints_sent": written.get("written"),
        "steps_inserted": written.get("steps"),
        "resolution_beats": written.get("resolution"),
        "interpolation": written.get("interpolation"),
        "cleared_first": written.get("cleared"),
        "probe_time": written.get("probe_time"),
        "before_sample": written.get("before_sample"),
        "after_sample": written.get("after_sample"),
        "notes": [SESSION_ONLY_NOTE, STORED_NOT_AUDIBLE, ENVELOPE_KEEPS_ITS_LAST_VALUE],
    }
    if not verify:
        result["verified"] = None
        result["verified_note"] = "not checked: verification was not requested"
        return result

    start, end = auto.read_window(curve)
    try:
        actual = _client_instance().send(
            "automation_read",
            {"path": clip_path, "parameter": parameter, "start": start, "end": end, "points": 64},
        )
    except LiveError as exc:
        result["verified"] = None
        result["verified_note"] = f"read-back failed, so nothing is proven: {exc}"
        return result

    comparison = auto.compare(curve, actual)
    result["verified"] = comparison.ok
    result["comparison"] = comparison.as_dict()
    result["ok"] = comparison.ok
    return result


@mcp.tool(annotations=READS)
def read_automation(
    track: TrackArg,
    slot: SlotArg,
    parameter: ParameterPathArg,
    start: Annotated[
        float | None,
        Field(
            description=(
                "First beat to sample, clip-local, where 0 is the clip start. Omit "
                "to start just past beat 0, which steps over the guard that keeps a "
                "sample off the envelope edge."
            )
        ),
    ] = None,
    end: Annotated[
        float | None,
        Field(
            description=(
                "Last beat to sample, clip-local. Omit to sample to the end of the "
                "clip."
            )
        ),
    ] = None,
    points: Annotated[
        int,
        Field(
            description=(
                "How many evenly spaced samples to take across the range. Clamped "
                "to 2..512. This is the resolution of the answer, not of the stored "
                "envelope, which keeps whatever breakpoints it was written with."
            )
        ),
    ] = 64,
) -> dict[str, Any]:
    """Sample a clip envelope into a list of values and report what it found.

    Reads the curve by evaluating it at ``points`` positions, so the answer is a
    sampling of the envelope rather than the breakpoints that define it.

    Returns:
        Dictionary with the sampled values, the beat range they cover, the value
        range they span, and flags saying whether an envelope exists at all.

    Note:
        Automation lives in Session clips, so an Arrangement clip has none to read.
        als_read is the way to see breakpoints in a saved project instead.

        Use write_automation to lay a curve down and clear_automation to remove one.
        A flat result with an envelope flag of false means nothing is automated on
        that parameter, which is not the same as an envelope holding the parameter's
        current value.
    """
    try:
        paths.validate(parameter)
    except paths.PathSyntaxError as exc:
        return {"ok": False, "error": f"invalid parameter path: {exc}"}

    params: dict[str, Any] = {
        "path": _clip_path(track, slot),
        "parameter": parameter,
        "points": max(2, min(points, 512)),
    }
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    try:
        got = _client_instance().send("automation_read", params)
    except LiveError as exc:
        return _live_error(exc)

    return {
        "ok": True,
        "track": track,
        "slot": slot,
        **got,
        "notes": [
            (
                "These are samples, not breakpoints; the LOM does not enumerate support points. "
                "For explicit breakpoints, read the set file with als_read."
            ),
            (
                "Readings at or near beat 0 return the parameter default value."
            ),
            SESSION_ONLY_NOTE,
        ],
    }


#: How densely the clear_automation dry run samples the envelope it is about to
#: remove. 16 was too coarse to see what it was reporting: on a 32 beat clip that
#: is a sample every two beats, and measured 2026-09-07 against Live 12.4.5 a V
#: shaped dip written to 0.6 at beat 20 was reported as bottoming out at 0.655,
#: understating the loss by about 2 dB, because the floor fell between samples.
#: 128 puts a sample every quarter beat on that clip. It costs nothing in round
#: trips: the sampling happens inside one handler call.
CLEAR_DRY_RUN_POINTS = 128

#: The LOM offers no way to list which parameters a clip has envelopes for.
#: ``Clip.has_envelopes`` is one flag for the whole clip, and
#: ``Clip.automation_envelope`` answers for one parameter at a time, so finding
#: out means asking parameter by parameter. Saying that is better than a report
#: that looks like a list and is not one.
ENVELOPE_INVENTORY_ROUTE = (
    "This report covers one named parameter, and has_envelopes is a single flag for the "
    "clip. To see which parameters the clip automates, read "
    "clip.automation_envelopes with lom_get for the count and then "
    "automation_envelopes[i].parameter.name for each one: measured 2026-09-08 against "
    "Live 12.4.5, that names a mixer control and a device knob alike. To see a curve "
    "before losing it, call read_automation for the parameters you care about, or read "
    "the saved file with als_read, which shows the breakpoints themselves."
)


def _envelope_loss(parameter: str, existing: Mapping[str, Any] | None) -> dict[str, Any]:
    """What one envelope holds, in enough detail to decide against losing it.

    A range alone does not describe a curve. Where the low point and the high
    point sit is what says whether this is a fade, a dip, or a shape that took
    work, and it is the difference between a report and a number.
    """
    loss: dict[str, Any] = {"parameter": parameter}
    if not existing:
        loss["has_envelope"] = None
        return loss
    loss["parameter_name"] = existing.get("parameter_name")
    loss["has_envelope"] = existing.get("has_envelope")
    loss["moves"] = existing.get("moves")
    loss["range"] = [existing.get("min"), existing.get("max")]
    loss["first"] = existing.get("first")
    loss["last"] = existing.get("last")

    # The samples at or below the epsilon read the parameter default rather than
    # the curve, which is why automation_read excludes them from min and max, and
    # why a beat taken from them would point at a value that is not in the
    # envelope.
    samples = existing.get("points") or []
    skip = int(existing.get("ignored_at_zero") or 0)
    usable = [
        (float(pair[0]), float(pair[1]))
        for pair in samples[skip:]
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    ]
    if not usable:
        return loss
    loss["lowest_at_beat"] = round(min(usable, key=lambda pair: pair[1])[0], 3)
    loss["highest_at_beat"] = round(max(usable, key=lambda pair: pair[1])[0], 3)
    loss["sampled_points"] = len(usable)
    if len(usable) > 1:
        loss["sampled_every_beats"] = round(
            (usable[-1][0] - usable[0][0]) / (len(usable) - 1), 4
        )
    return loss


@mcp.tool(annotations=DESTROYS)
def clear_automation(
    track: TrackArg,
    slot: SlotArg,
    parameter: Annotated[
        str,
        Field(
            description=(
                "LOM path to the one DeviceParameter whose envelope should go, e.g. "
                "'song.tracks[0].mixer_device.volume'. Leave empty only when "
                "all_envelopes is true."
            )
        ),
    ] = "",
    all_envelopes: Annotated[
        bool,
        Field(
            description=(
                "True clears every envelope on the clip, ignoring ``parameter``. "
                "One of this or ``parameter`` has to be given; neither is refused "
                "rather than treated as clear everything."
            )
        ),
    ] = False,
    confirm: ConfirmArg = False,
) -> dict[str, Any]:
    """Delete automation envelopes on a Session clip. Requires confirm=True.

    Requires confirm=True. With confirm=False it changes nothing and reports the
    envelope it would remove: its range, where the low and the high point sit, and
    how densely it was sampled to find them.

    That report covers one named parameter. With all_envelopes it says whether the
    clip is automated at all, because has_envelopes is one flag for the whole clip.
    To see which parameters those envelopes belong to, read
    clip.automation_envelopes with lom_get and then each
    automation_envelopes[i].parameter.name.

    Returns:
        Dictionary reporting the clearance, or the envelopes that would go.

    Note:
        A cleared envelope is gone, and the parameter falls back to whatever value it
        holds outside the clip rather than to the first breakpoint. Sample it with
        read_automation first if the shape might be wanted back, since nothing here
        restores it.

        The dry run samples the curve rather than reading its breakpoints, so a
        feature narrower than the reported sampling step can still sit between two
        samples. Where the exact shape matters, read it with read_automation at a
        resolution you choose, or read the breakpoints out of the saved file with
        als_read.

        To change a curve rather than remove it, write_automation with
        ``clear_first=True`` replaces it in one call and needs no clearing first.
        This tool is for leaving the parameter unautomated.
    """
    if not parameter and not all_envelopes:
        return {
            "ok": False,
            "error": (
                "give parameter=<a DeviceParameter path> to clear one envelope, or "
                "all_envelopes=True to clear every envelope on the clip"
            ),
        }
    if parameter:
        try:
            paths.validate(parameter)
        except paths.PathSyntaxError as exc:
            return {"ok": False, "error": f"invalid parameter path: {exc}"}

    clip_path = _clip_path(track, slot)
    has = _run("clip.has_envelopes", track=track, slot=slot)
    existing: dict[str, Any] | None = None
    if parameter and not all_envelopes:
        try:
            existing = _client_instance().send(
                "automation_read",
                {
                    "path": clip_path,
                    "parameter": parameter,
                    "points": CLEAR_DRY_RUN_POINTS,
                },
            )
        except LiveError:
            existing = None

    if not confirm:
        return {
            "ok": False,
            "cleared": False,
            "confirm_required": True,
            "clip_has_envelopes": has.get("value") if has.get("ok") else None,
            "would_lose": (
                "every envelope on this clip"
                if all_envelopes
                else _envelope_loss(parameter, existing)
            ),
            **({"note": ENVELOPE_INVENTORY_ROUTE} if all_envelopes else {}),
            "error": (
                "clear_automation is destructive and cannot be undone here; "
                "pass confirm=True."
            ),
        }

    params: dict[str, Any] = {"path": clip_path}
    if all_envelopes:
        params["all"] = True
    else:
        params["parameter"] = parameter
    try:
        cleared = _client_instance().send("automation_clear", params)
    except LiveError as exc:
        return _live_error(exc)

    after = _run("clip.has_envelopes", track=track, slot=slot)
    return {
        "ok": bool(cleared.get("cleared")),
        "track": track,
        "slot": slot,
        "cleared_all": bool(cleared.get("cleared_all")),
        "parameter": cleared.get("parameter"),
        "parameter_name": cleared.get("parameter_name"),
        "clip_has_envelopes_before": has.get("value") if has.get("ok") else None,
        "clip_has_envelopes_after": after.get("value") if after.get("ok") else None,
        "note": (
            "has_envelopes is the read-back. It is a per-clip flag, so it stays true while "
            "any other envelope remains."
        ),
    }


# --------------------------------------------------------------- arrangement
@mcp.tool(annotations=ADDS)
def arrange(
    track: TrackArg,
    slot: SlotArg,
    at_beat: Annotated[
        float,
        Field(
            description=(
                "Where the copy starts on the Arrangement timeline, in beats from "
                "the start of the song. Fractional beats are accepted."
            )
        ),
    ],
    to_track: Annotated[
        int | None,
        Field(
            description=(
                "Track index to place the copy on. Omit to use the source track, "
                "which is the usual case. A MIDI clip needs a MIDI destination."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Copy a Session clip onto the Arrangement timeline at a beat position.

    A copy, not a move: the Session clip stays in its slot and the two are separate
    clips afterwards, so editing one leaves the other alone. This is how a Session
    idea becomes an arrangement, and how clip automation written by write_automation
    reaches the Arrangement timeline.

    Returns:
        Dictionary with the placement status, the destination track, and the
        Arrangement clip count before and after.

    Note:
        Call it once per placement. Repeating the same call adds another copy at the
        same beat rather than replacing the first, so a retry after an unclear result
        needs the clip count in this answer checked first.

        Use create_clip and write_clip_notes to build the source clip, and
        set_arrangement_time then play to hear where it landed.
    """
    destination = track if to_track is None else to_track
    before, err = _count("arrangement_clip.name", "clip", {"track": destination})
    if err is not None:
        return err

    placed = _run(
        "arrangement_clip.duplicate_from_session",
        track=destination,
        call_args=[{"__path__": _clip_path(track, slot)}, at_beat],
    )
    if not placed.get("ok"):
        return placed

    after, err = _count("arrangement_clip.name", "clip", {"track": destination})
    if err is not None:
        return err
    grew = _grew(before, after)
    ok, evidence = _verdict(bool(placed.get("ok")), grew)

    detail: dict[str, Any] | None = None
    if grew and after.get("exact") and after["count"]:
        index = after["count"] - 1
        fields, ferr = _read_fields(
            [
                ("arrangement_clip.name", {"track": destination, "clip": index}),
                ("arrangement_clip.start_time", {"track": destination, "clip": index}),
                ("arrangement_clip.end_time", {"track": destination, "clip": index}),
                ("arrangement_clip.length", {"track": destination, "clip": index}),
            ]
        )
        if ferr is None:
            detail = fields

    return {
        "ok": ok,
        "source": {"track": track, "slot": slot, "path": _clip_path(track, slot)},
        "destination_track": destination,
        "at_beat": at_beat,
        "arrangement_clip_count_before": before.get("count"),
        "arrangement_clip_count_after": after.get("count"),
        "last_clip": detail,
        "call": placed,
        **evidence,
        "notes": [
            SESSION_ONLY_NOTE,
            (
                "last_clip is the last entry in the destination track's arrangement list, "
                "which is the new one only if nothing else was added in between."
            ),
            COLLECTION_NOTE,
        ],
    }


@mcp.tool(annotations=ADDS)
def set_locator(
    at_beat: Annotated[
        float,
        Field(
            description=(
                "Position on the Arrangement timeline in beats from the start, "
                "where beat 0 is the beginning of the song. Fractional beats are "
                "accepted, so 33.5 is the second half of bar 9 in 4/4."
            )
        ),
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "Name for a locator this call creates. Ignored when the call "
                "deletes one, because there is nothing left to name."
            )
        ),
    ] = "",
    confirm: Annotated[
        bool,
        Field(
            description=(
                "True carries the toggle out. False changes nothing and reports the "
                "locators that already exist, which is how to tell in advance "
                "whether this call would add one or delete one."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Place or remove an Arrangement cue locator. Requires confirm=True.

    This toggles, and that is the whole difficulty with it. Live offers one call,
    ``song.set_or_delete_cue``, which adds a locator where none exists at that beat
    and deletes the one that does, so the same arguments twice leave nothing behind.
    Call with confirm=False first to see which of the two would happen.

    Returns:
        Dictionary reporting the toggle, and the cue points and their count after it.

    Note:
        A locator marks a position for a human reading the Arrangement. It does not
        move the playhead. Use set_arrangement_time to move that, and set_loop to
        mark a region rather than a point.
    """
    before, err = _count("cue.time", "cue", {})
    if err is not None:
        return err

    if not confirm:
        return {
            "ok": False,
            "confirm_required": True,
            "at_beat": at_beat,
            "locator_count": before.get("count"),
            "locator_times": before.get("values"),
            "error": (
                "set_or_delete_cue is a toggle: at a beat that already carries a locator it "
                "deletes it. Check locator_times above, then call again with confirm=True."
            ),
        }

    moved = _run("song.current_song_time", value=at_beat)
    if not moved.get("ok"):
        return moved
    toggled = _run("song.set_or_delete_cue", confirm=True)
    if not toggled.get("ok"):
        return toggled

    after, err = _count("cue.time", "cue", {})
    if err is not None:
        return err
    created = _grew(before, after)
    deleted = _shrank(before, after)
    ok, evidence = _verdict(bool(toggled.get("ok")), created)

    named: dict[str, Any] | None = None
    if created and name:
        for index, value in enumerate(after.get("values", [])):
            if isinstance(value, (int, float)) and abs(float(value) - at_beat) < 1e-6:
                named = _run("cue.name", cue=index, value=name)
                break

    return {
        "ok": ok,
        "created": created,
        "deleted": deleted,
        "at_beat": at_beat,
        "playhead": moved,
        "locator_count_before": before.get("count"),
        "locator_count_after": after.get("count"),
        "locator_times_after": after.get("values"),
        "named": named,
        **evidence,
        "note": (
            "A locator was deleted, not created: there was already one at this position."
            if deleted
            else "The locator count is the evidence; the LOM does not report the new cue point."
        ),
    }


@mcp.tool(annotations=SETS)
def set_arrangement_time(
    at_beat: Annotated[
        float,
        Field(
            description=(
                "Position on the Arrangement timeline in beats from the start, "
                "where beat 0 is the beginning of the song. Fractional beats are "
                "accepted, so 33.5 is the second half of bar 9 in 4/4."
            )
        ),
    ],
) -> dict[str, Any]:
    """Move the Arrangement playhead to a beat and read the position back.

    Writes ``song.current_song_time``. This moves where playback would start. It
    does not start it. Use play to begin, or play with ``from_beat`` to do both in
    one call.

    Returns:
        Dictionary with the write confirmation and the read-back song time.

    Note:
        Do not sample a moving transport through lom_batch. A read that follows a
        jump inside the same batch still reports the position from before the move,
        and it reports it without an error (see lom_batch). One call per position.
    """
    result = _run("song.current_song_time", value=at_beat)
    result["note"] = (
        "Compare 'after' with requested beat to verify playhead position."
    )
    return result


# ----------------------------------------------------------------- transport
@mcp.tool(annotations=SETS)
def play(
    from_beat: Annotated[
        float | None,
        Field(
            description=(
                "Cue the playhead to this beat before starting, in beats from the "
                "start of the Arrangement. Omit to start from wherever the playhead "
                "already sits."
            )
        ),
    ] = None,
    continue_playing: Annotated[
        bool,
        Field(
            description=(
                "True resumes from the current position instead of restarting. "
                "Ignored when from_beat is given, since that sets the position."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Start transport playback and read back the is_playing status.

    Returns:
        Dictionary reporting the start call and the read-back is_playing flag.

    Note:
        The read-back proves the transport is running, not that anything is audible:
        a muted track, an empty Arrangement or a track with no instrument all play
        silently. Use stop to halt, and set_arrangement_time to move the playhead
        without starting.

        Leave the transport as it was found. Nothing here stops it on its own, and a
        set left playing keeps playing for whoever is at the machine.

        A Session clip that has played leaves its track in Session view, and the
        track then ignores the Arrangement until Live's Back to Arrangement button
        is pressed. ``song.back_to_arranger`` reports that state and cannot clear
        it: measured 2026-09-07 against Live 12.4.5, writing it false reads back
        ``not_observed`` and the value stays true.
    """
    moved: dict[str, Any] | None = None
    if from_beat is not None:
        moved = _run("song.current_song_time", value=from_beat)
        if not moved.get("ok"):
            return moved
    started = _run("song.continue_playing" if continue_playing else "song.start_playing")
    state = _run("song.is_playing")
    return {
        "ok": bool(started.get("ok")) and bool(state.get("value")),
        "is_playing": state.get("value") if state.get("ok") else None,
        "from_beat": from_beat,
        "playhead": moved,
        "call": started,
        "note": "is_playing is the read-back; the call itself proves only that it did not raise.",
    }


# Measured 2026-09-08 against Live 12.4.5: firing one scene and stopping left three
# track volumes at -5.00, -1.72 and +1.50 dB rather than at 0.0 dB, and a second pass
# that stopped elsewhere in the same envelope left one of them at -0.72 dB instead.
@mcp.tool(annotations=SETS)
def stop(
    clips: Annotated[
        bool,
        Field(
            description=(
                "True also stops every playing Session clip through "
                "song.stop_all_clips. False stops the transport only: the flag "
                "exists because that alone does not stop the Session clips."
            )
        ),
    ] = False,
    quantized: Annotated[
        bool,
        Field(
            description=(
                "True lets the clip stop fall on the global launch quantisation "
                "grid, so it can lag the call by up to a bar. False stops the clips "
                "immediately. Only read when clips is true."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Stop transport playback, and optionally every playing Session clip.

    Returns:
        Dictionary reporting the transport stop and, where asked for, the clip stop.

    Note:
        With ``quantized=True`` the clips stop on the next grid point rather than at
        once, so an immediate read can still show a clip playing without anything
        having gone wrong. Use play to resume playback, or set_arrangement_time(time=0)
        to rewind the Arrangement timeline to the beginning.

        Stopping does not restore a parameter a clip envelope was driving. The envelope
        leaves the parameter at its last reached value and Live keeps no static baseline
        underneath to restore.
    """
    stopped = _run("song.stop_playing")
    clip_stop: dict[str, Any] | None = None
    if clips:
        clip_stop = _run("song.stop_all_clips", call_args=[quantized])
    state = _run("song.is_playing")
    return {
        "ok": bool(stopped.get("ok")) and state.get("value") is False,
        "is_playing": state.get("value") if state.get("ok") else None,
        "clips_stopped": clip_stop,
        "call": stopped,
        "notes": [ENVELOPE_KEEPS_ITS_LAST_VALUE],
    }


@mcp.tool(annotations=SETS)
def set_tempo(
    bpm: Annotated[
        float,
        Field(description="Tempo in beats per minute. The catalog validates 20.0 to 999.0."),
    ],
) -> dict[str, Any]:
    """Set the global song tempo in BPM and read back the stored value.

    One tempo governs the whole set, Session and Arrangement alike. There is no
    per-track tempo. The one other place a tempo can come from is a scene, below.

    Returns:
        Dictionary reporting the write and the read-back tempo.

    Note:
        A tempo set here does not survive every scene launch. Firing a scene whose
        ``tempo_enabled`` is on writes ``song.tempo`` again from ``scene.tempo``, so
        read ``song.tempo`` back after a launch rather than assuming this one held
        (catalog rows ``scene.tempo`` and ``scene.tempo_enabled``, both read verified
        2026-08-29 against Live 12.4.5).

        BPM is a real unit, which makes this tool the exception among the setters. A
        device parameter is normalised over whatever range the device declares: reach
        those with set_parameter and read ``min`` and ``max`` through ``describe``
        rather than assuming 0..1 (docs/limits.md). Volume, pan and sends are
        normalised the same way, and set_mix carries them.
    """
    result = _run("song.tempo", value=bpm)
    result["note"] = "BPM is a real unit here. Device parameters are not; those are normalised."
    return result


@mcp.tool(annotations=SETS)
def set_loop(
    enabled: Annotated[
        bool | None,
        Field(
            description=(
                "Turn the Arrangement loop on or off. Omit to leave the switch as "
                "it is and move the brace only."
            )
        ),
    ] = None,
    start: Annotated[
        float | None,
        Field(
            description=(
                "Where the loop brace begins, in beats from the start of the "
                "Arrangement. Omit to leave it where it is."
            )
        ),
    ] = None,
    length: Annotated[
        float | None,
        Field(
            description=(
                "How long the brace is, in beats, not the beat it ends on: a loop "
                "over bars 5 to 9 in 4/4 is start=16, length=16. Omit to leave it."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Move the Arrangement loop brace, or switch looping on and off.

    Every argument is optional and only the ones given are written, so the brace can
    be moved without touching the switch and the reverse.

    Returns:
        Dictionary reporting how many writes were made and the before and after
        values for each of enabled, loop_start and loop_length.

    Note:
        The brace only loops the Arrangement transport. It has no bearing on Session
        clip looping, which is a clip property. Read that with get_clip. ``length``
        is a duration and not an end position.

        Calling with no arguments at all writes nothing and reports the current
        brace, which is the cheap way to read it.
    """
    ops: list[dict[str, Any]] = []
    labels: list[str] = []
    for label, spec_id, value in (
        ("enabled", "song.loop", enabled),
        ("start", "song.loop_start", start),
        ("length", "song.loop_length", length),
    ):
        if value is None:
            continue
        ops.append({"id": spec_id, "value": value})
        labels.append(label)
    if not ops:
        return {"ok": False, "error": "nothing to set: give enabled, start or length"}

    results, err = _run_batch(ops, verify=True)
    if err is not None:
        return err
    assert results is not None
    return {
        "ok": all(row.get("ok") for row in results),
        "round_trips": 1,
        "changes": dict(zip(labels, results, strict=False)),
        "note": "start and length are beats. length is a duration, not an end position.",
    }


# ---------------------------------------------------------- file (Channel B)
@mcp.tool(annotations=READS)
def als_read(
    path: Annotated[
        str,
        Field(
            description=(
                "Filesystem path to a saved .als project or .adg rack. The file is "
                "read from disk and is not opened in Live, so it does not have to "
                "be the set currently on screen."
            )
        ),
    ],
    with_notes: Annotated[
        bool,
        Field(
            description=(
                "True parses the clip notes and reports note metrics, which is the "
                "expensive part of the read on a large project."
            )
        ),
    ] = False,
    track: Annotated[
        str,
        Field(
            description=(
                "Narrow the read to one track, given either as its name or as its "
                "index in a string. Empty reads the whole project."
            )
        ),
    ] = "",
    report: Annotated[
        bool,
        Field(
            description=(
                "True adds a formatted summary written for a person to read, "
                "alongside the structured answer rather than instead of it."
            )
        ),
    ] = False,
    locate: Annotated[
        Literal["", "tempo", "track_names"],
        Field(
            description=(
                "Answer with the arguments als_write needs to edit a field, instead "
                "of the project survey: the ElementTree expression, the attribute, "
                "the index that picks the right match, and the value currently "
                "there. 'tempo' for the project tempo, 'track_names' for every "
                "track's name. These are the fields usually edited on a file. "
                "Anything else needs an expression built by hand, which "
                "als_write's own confirm=False resolves against the file and "
                "reports on before it writes."
            )
        ),
    ] = "",
) -> dict[str, Any]:
    """Read a saved Ableton project (.als) or rack (.adg) from disk.

    Returns:
        Dictionary containing project metadata, track list, devices, and automation structure.

    Note:
        This is Channel B, a peer of the live connection rather than a fallback. It
        answers two questions the LOM never will: what is in someone else's project, and
        where an envelope's breakpoints actually sit.

        Automation lives in two places and the LOM can read only one of them. Measured
        over the 174-project corpus (docs/limits.md section 9): 52 (30 %) have clip
        envelopes but 159 (91 %) have
        track automation, and a tool that counted clip envelopes alone reported 110 of
        those projects as unautomated. The two layers are reported separately here and
        are never added together.
    """
    target = Path(path).expanduser()
    if not target.exists():
        return {"ok": False, "error": f"no such file: {target}"}

    if locate:
        # Answered off the XML rather than the parsed survey: the survey is a model
        # of the project and holds no element positions, and a position is the
        # whole point of the question.
        try:
            root = als_reader.load_xml(target)
        except (OSError, ValueError, ET.ParseError) as exc:
            return {"ok": False, "error": f"could not read {target.name}: {exc}"}
        located = _als_locate(root, locate)
        return {
            "ok": True,
            "file": target.name,
            "locate": locate,
            "locations": located,
            "count": len(located),
            "note": (
                "Hand these to als_write with operation='attribute'. index counts the "
                "matches of that expression and not tracks in the project. A track name "
                "is stored twice, in EffectiveName and in UserName: set both, or Live "
                "recomputes the shown name from the instrument and the change is lost."
            ),
        }

    try:
        project = als_reader.read_project(target, with_notes=with_notes)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not read {target.name}: {exc}"}

    if track:
        selector: int | str = int(track) if track.lstrip("-").isdigit() else track
        try:
            found = project.track(selector)
        except (KeyError, IndexError) as exc:
            return {
                "ok": False,
                "error": f"no track {track!r} in {project.file}: {exc}",
                "tracks": [t.name for t in project.tracks],
            }
        return {
            "ok": True,
            "file": project.file,
            "track": {
                "name": found.name,
                "type": found.type,
                "volume": found.volume,
                "panning": found.panning,
                "sends": found.sends,
                "devices": [
                    {
                        "name": d.name,
                        "kind": d.kind,
                        "on": d.on,
                        "slots": d.param_count,
                        "configured_parameters": len(d.configured_parameters),
                        "sidechain": None if d.sidechain is None else vars(d.sidechain),
                    }
                    for d in found.devices
                ],
                "session_clips": len(found.session_clips),
                "arrangement_clips": len(found.arrangement_clips),
                "track_automation": [
                    {
                        "target": e.target,
                        "points": e.points,
                        "from": e.time_from,
                        "to": e.time_to,
                        "min": e.value_min,
                        "max": e.value_max,
                    }
                    for e in found.track_automation
                ],
            },
            "notes": [
                SAVED_STATE_NOTE,
                (
                    "Volume here is a linear factor (1.0 = 0 dB), not the normalized 0.85 "
                    "reported by LOM faders."
                ),
            ],
        }

    automation = als_reader.automation_of(project)
    payload: dict[str, Any] = {
        "ok": True,
        "file": project.file,
        "path": project.path,
        "live_version": project.live_version,
        "creator": project.creator,
        "tempo": project.tempo,
        "is_rack": project.is_rack,
        "track_count": len(project.tracks),
        "tracks": [
            {
                "name": t.name,
                "type": t.type,
                "devices": [d.name for d in t.devices],
                "session_clips": len(t.session_clips),
                "arrangement_clips": len(t.arrangement_clips),
                "track_automation": len(t.track_automation),
                "clip_envelopes": sum(
                    len(c.envelopes) for c in t.session_clips + t.arrangement_clips
                ),
            }
            for t in project.tracks
        ],
        "automation": {
            "clip_envelopes": len(automation.clip_envelopes),
            "track_automation": len(automation.track_automation),
            "automated": automation.automated,
            "note": (
                "Clip envelopes and track automation are reported separately. "
                "Only clip envelopes can be written via Session clips; "
                "track automation is read-only from saved sets."
            ),
        },
        "notes": [SAVED_STATE_NOTE],
    }
    if report:
        payload["report"] = als_reader.format_report(project, show_devices=True)
    if with_notes:
        payload["note_clips"] = [
            {"track": name, "clip": clip.name, "notes": clip.note_count,
             "where": clip.where, "fingerprint": clip.fingerprint}
            for name, clip in als_reader.unique_note_clips(project)
        ]
    return payload


def _set_open_in_live(target: Path) -> tuple[bool | None, str]:
    """Check whether target project is currently opened in running Live session.

    Asks Live for ``song.file_path``. Measured 2026-09-01: it returned the full path of
    the open set. A running Live holding a different set is therefore no reason to
    refuse, and a running Live holding this one is a reason no flag should override: the
    edit would survive on disk only until Live's next save, and then vanish with no
    error anywhere.

    Returns:
        ``(open_here, detail)``. ``None`` means Live could not be asked, which is not
        the same as no, and the caller keeps the conservative refusal in that case.
    """
    try:
        got = _client_instance().get("song.file_path")
    except LiveError as exc:
        return None, f"Live could not be asked which set is open ({exc})"
    loaded = str(got.get("value") or "")
    if not loaded:
        return False, "Live has an unsaved set open, so it is not holding this file"
    try:
        same = Path(loaded).resolve() == target.resolve()
    except OSError:
        same = loaded.casefold() == str(target).casefold()
    if same:
        return True, f"Live has this set open: {loaded}"
    return False, f"Live is running but has a different set open: {loaded}"


# Measured 2026-09-01 against Live 12.4.5: two parameters written into an instance
# whose strip held nothing came back on reopen with their real values, writable
# through lom_set with read_back: applied.
#: How many matches a dry run describes one by one before it only counts them.
_ALS_MATCH_SAMPLE = 8


def _als_expression_for(root: Any, node: Any, parents: Mapping[Any, Any]) -> str:
    """The ElementTree expression that reaches one element, taken from the file.

    Derived rather than written down, because the tag names are not stable across
    Live versions. Measured 2026-09-07: Live 11 writes the tempo under
    ``MasterTrack`` and Live 12 writes it under ``MainTrack``, so
    ``.//MasterTrack/DeviceChain/Mixer/Tempo/Manual`` reaches it in one and
    matches nothing at all in the other. An expression built from the file in
    hand cannot be wrong about which name that file used.
    """
    chain: list[str] = []
    current = node
    while current is not None and current is not root:
        chain.append(current.tag)
        parent = parents.get(current)
        if parent is not None and parent.tag == "LiveSet":
            break
        current = parent
    return ".//" + "/".join(reversed(chain))


def _als_locate(root: Any, what: str) -> list[dict[str, Any]]:
    """Where an editable field sits, as arguments als_write can be handed.

    ``als_write``'s ``attribute`` operation needs an ElementTree expression, and
    reading the project produced none, so the expression had to come from knowing
    the .als schema. Knowing it is not enough either: the schema changed between
    Live 11 and Live 12, and an expression that is right for one file selects
    nothing in the other. Every expression here is read out of the file in hand.

    This answers for the fields people actually edit on a file. Anything else is
    still an expression built by hand, and als_write's dry run is where it gets
    checked against the file before it writes.
    """
    parents = {child: parent for parent in root.iter() for child in parent}
    found: list[dict[str, Any]] = []

    if what == "tempo":
        for node in root.iter("Tempo"):
            manual = node.find("Manual")
            if manual is None:
                continue
            expression = _als_expression_for(root, manual, parents)
            found.append(
                {
                    "what": "tempo",
                    "expression": expression,
                    "attribute": "Value",
                    "index": None if len(root.findall(expression)) == 1 else 0,
                    "current": manual.get("Value"),
                }
            )
            break
        return found

    seen: dict[str, int] = {}
    for node in root.iter():
        tag = node.tag
        if tag not in als_writer.TRACK_TAGS:
            continue
        ordinal = seen.get(tag, 0)
        seen[tag] = ordinal + 1
        name_node = node.find("Name/EffectiveName")
        if name_node is None:
            continue
        user_node = node.find("Name/UserName")
        found.append(
            {
                "what": "track name",
                "track": name_node.get("Value"),
                "kind": tag,
                "expression": _als_expression_for(root, name_node, parents),
                # Live stores the shown name and the typed name separately, and
                # leaves the typed one empty while the name comes from the
                # instrument. Setting only the shown one lets Live recompute it.
                "also_set": None if user_node is None
                else _als_expression_for(root, user_node, parents),
                "attribute": "Value",
                "index": ordinal,
                "current": name_node.get("Value"),
                "current_user_name": None if user_node is None else user_node.get("Value"),
            }
        )
    return found


def _als_expression_report(target: Path, expression: str, attribute: str) -> dict[str, Any]:
    """What an expression selects in a file, before anything is written to it.

    A dry run that reports only "call again with confirm=True" cannot catch an
    expression that points at nothing, or at six things, or at an element with no
    such attribute. Those are exactly the mistakes a hand-built expression makes.
    """
    try:
        root = als_reader.load_xml(target)
    except (OSError, ValueError, ET.ParseError) as exc:
        return {"readable": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        matches = root.findall(expression)
    except SyntaxError as exc:
        return {"readable": True, "expression_valid": False, "error": str(exc)}

    described = [
        {
            "tag": node.tag,
            "has_attribute": attribute in node.attrib,
            "current": node.get(attribute),
        }
        for node in matches[:_ALS_MATCH_SAMPLE]
    ]
    report: dict[str, Any] = {
        "readable": True,
        "expression_valid": True,
        "matches": len(matches),
        "attribute": attribute,
        "sample": described,
    }
    if len(matches) > _ALS_MATCH_SAMPLE:
        report["sample_note"] = (
            f"first {_ALS_MATCH_SAMPLE} of {len(matches)} shown, with index counting all of them"
        )
    if not matches:
        report["verdict"] = "this expression selects nothing, so the write would be refused"
    elif len(matches) > 1:
        report["verdict"] = (
            f"ambiguous: pass index=0..{len(matches) - 1} to choose one, or narrow the "
            f"expression. An ambiguous write is refused rather than aimed at the first match"
        )
    elif not described[0]["has_attribute"]:
        report["verdict"] = (
            f"the selected element carries no {attribute!r} attribute. create=True adds it, "
            f"and refusing is the default so a misspelled name does not invent a field"
        )
    else:
        report["verdict"] = "one element, and it carries the attribute"
    return report


@mcp.tool(annotations=DESTROYS)
def als_write(
    path: Annotated[
        str,
        Field(
            description=(
                "Filesystem path to the .als project to edit. A backup is written "
                "first and can be put back with the restore operation."
            )
        ),
    ],
    operation: Annotated[
        Literal["sidechain", "configure", "attribute", "restore"],
        Field(
            description=(
                "What to do. 'sidechain' wires a compressor to a trigger track, "
                "'configure' fills a plug-in parameter strip, 'attribute' sets one "
                "XML attribute anywhere in the file, 'restore' puts a backup back. "
                "Each one reads a different subset of the arguments below."
            )
        ),
    ],
    confirm: Annotated[
        bool,
        Field(
            description=(
                "True carries the edit out. False changes nothing and reports what "
                "the call requires, which is how to check the arguments first."
            )
        ),
    ] = False,
    target_track: Annotated[
        str,
        Field(
            description=(
                "Name of the track carrying the device to edit, as it appears in "
                "Live. Used by 'sidechain' and 'configure'."
            )
        ),
    ] = "",
    source_track: Annotated[
        str,
        Field(
            description=(
                "Name of the track the compressor should listen to, which is "
                "usually the kick. Used by 'sidechain' only."
            )
        ),
    ] = "",
    device: Annotated[
        int,
        Field(
            description=(
                "Device index within the target track chain, counted from 0. Used "
                "by 'sidechain' and 'configure'."
            )
        ),
    ] = 0,
    tap: Annotated[
        Literal["pre", "post"],
        Field(
            description=(
                "Where the sidechain listens on the source track: 'pre' before "
                "that track's own effects and fader, 'post' after them. Used by "
                "'sidechain' only."
            )
        ),
    ] = "post",
    expression: Annotated[
        str,
        Field(
            description=(
                "ElementTree path selecting the element to edit, e.g. "
                "'.//MasterTrack/DeviceChain/Mixer/Tempo/Manual'. Used by "
                "'attribute' only. Read the tree with als_read first."
            )
        ),
    ] = "",
    value: Annotated[
        str,
        Field(
            description=(
                "For 'attribute', the new attribute value. For 'configure', the "
                "parameter strip assignment as '<index>=<name>; ...'."
            )
        ),
    ] = "",
    attribute: Annotated[
        str,
        Field(
            description=(
                "Which attribute of the selected element to set. Live stores most "
                "numbers under 'Value'. Used by 'attribute' only."
            )
        ),
    ] = "Value",
    index: Annotated[
        int | None,
        Field(
            description=(
                "Which match to edit when the expression selects more than one "
                "element, counted from 0. An ambiguous expression is refused rather "
                "than resolved to the first match, so this is how to disambiguate."
            )
        ),
    ] = None,
    create: Annotated[
        bool,
        Field(
            description=(
                "True adds the attribute when the element does not already carry "
                "it. False refuses, which catches a misspelled attribute name "
                "instead of inventing a field Live will ignore."
            )
        ),
    ] = False,
    backup: Annotated[
        str,
        Field(
            description=(
                "Path to the backup file to put back. Required by the "
                "'restore' operation and read by no other."
            )
        ),
    ] = "",
    allow_live_running: Annotated[
        bool,
        Field(
            description=(
                "True permits the edit while Live is running at all. The check "
                "behind it looks for a running Live process and cannot tell which "
                "set that process holds, so it is not a guard against editing the "
                "open one: only you know that. What matters is the file, not the "
                "process. Live open on a different set is harmless. Live holding "
                "this file overwrites the edit from memory when it next saves."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Edit a saved .als project file on disk. Requires confirm=True.

    Supported operations: 'sidechain', 'configure', 'attribute', 'restore'.
    The target file must not be the one Live currently holds: Live writes its own
    memory over the file when it next saves, without a conflict warning, and the
    edit is gone. Live running on a different set is fine.

    Returns:
        Dictionary reporting write status, changes made, and verification report.

    Note:
        ``sidechain`` is here for the set Live is not holding, someone else's project
        wired on disk. Measured across the 174-project corpus (docs/limits.md section 9):
        74 of 82 in the house/trance BPM window use sidechain compression, median 6
        wirings each. For
        the set on screen use ``lom_set`` and keep the read-back.

        ``configure`` fills a plugin's parameter strip, which no LOM call can do whatever
        the strip currently holds. Two parameters written into an instance whose strip
        held nothing came back on reopen with their real values, writable through
        ``lom_set`` with ``read_back: applied``. Live allocates 128 slots per instance and
        leaves them all in the file, so nothing is inserted. Three fields of an existing
        element are filled.

        ``attribute`` sets one attribute anywhere in the file, addressed by an
        ElementTree expression. An expression matching more than one element is refused
        unless ``index`` picks one, and ``index`` counts the matches of that expression
        rather than tracks in the project: ``.//MidiTrack/Name/EffectiveName`` does not
        see the audio tracks at all.

        Where the expression comes from: ``als_read`` with ``locate='tempo'`` or
        ``locate='track_names'`` answers with the expression, the attribute and the
        index for those fields, which are the ones usually edited on a file. For
        anything else the expression is built by hand, and ``confirm=False`` resolves
        it against the file first and reports what it selects, so a wrong one is
        caught before it writes rather than after.

        Choosing between the four: ``sidechain`` and ``configure`` for the two things
        the LOM cannot do at all, ``attribute`` for anything else in the file, and
        ``restore`` to undo any of them. For
        the set open in Live, none of these: use lom_set, set_parameter or set_mix
        and keep the read-back.
    """
    target = Path(path).expanduser()
    if operation not in ("sidechain", "attribute", "restore", "configure"):
        return {
            "ok": False,
            "error": (
                "operation must be 'sidechain', 'attribute', 'configure' or 'restore', "
                f"not {operation!r}"
            ),
        }
    if not confirm:
        preflight: dict[str, Any] = {
            "ok": False,
            "confirm_required": True,
            "operation": operation,
            "file": str(target),
        }
        if not target.exists():
            preflight["error"] = f"no such file: {target}"
            return preflight
        # An expression built by hand is the argument most likely to be wrong, so
        # the dry run resolves it against the file rather than only announcing
        # that a confirmation is needed.
        if operation == "attribute" and expression:
            preflight["selects"] = _als_expression_report(target, expression, attribute)
        elif operation == "attribute":
            preflight["error"] = (
                "operation='attribute' needs an expression. als_read with "
                "locate='tempo' or locate='track_names' answers with the expression, "
                "the attribute and the index for those fields."
            )
            return preflight
        preflight["error"] = (
            "als_write edits a saved project file. A backup is written first and can "
            "be put back with operation='restore', but the change is real. The file "
            "must not be the one Live currently holds, because Live overwrites it "
            "from memory when it next saves. Live running on a different set is "
            "fine. Call again with confirm=True."
        )
        return preflight
    if not target.exists():
        return {"ok": False, "error": f"no such file: {target}"}

    open_here, why = _set_open_in_live(target)
    if open_here is True:
        return {
            "ok": False,
            "blocked": True,
            "error": (
                f"{why}. A write now would sit on disk until Live's next save and then be "
                "lost. Live does not have to be quit: close the set (File > New, or open "
                "another set) and retry."
            ),
        }
    if open_here is False and not allow_live_running:
        allow_live_running = True

    try:
        if operation == "restore":
            if not backup:
                return {"ok": False, "error": "restore needs backup=<path to the backup file>"}
            restored = als_writer.restore_backup(backup, target)
            return {
                "ok": True,
                "operation": "restore",
                "restored_to": str(restored),
                "from_backup": backup,
                "note": "The backup was checked for being a parseable Live set before restoring.",
            }
        if operation == "sidechain":
            if not target_track or not source_track:
                return {
                    "ok": False,
                    "error": (
                        "sidechain needs target_track (the track carrying the compressor) "
                        "and source_track"
                    ),
                }
            result = als_writer.set_sidechain_source(
                target,
                target_track=target_track,
                source_track=source_track,
                device=device,
                tap=tap,  # type: ignore[arg-type]
                allow_live_running=allow_live_running,
            )
        elif operation == "configure":
            if not target_track:
                return {"ok": False, "error": "configure needs target_track=<track name>"}
            try:
                wanted = [
                    (int(part.split("=", 1)[0].strip()), part.split("=", 1)[1].strip())
                    for part in value.split(";")
                    if part.strip()
                ]
            except (IndexError, ValueError):
                return {
                    "ok": False,
                    "error": (
                        "configure takes value='<index>=<name>; <index>=<name>' where the "
                        "index is the position in the plugin's own parameter list. Get that "
                        "list with lom_call(device, 'get_parameter_names')."
                    ),
                }
            result = als_writer.configure_plugin_parameters(
                target,
                target_track,
                wanted,
                device_index=device,
                allow_live_running=allow_live_running,
            )
        else:
            if not expression:
                return {"ok": False, "error": "attribute needs expression=<ElementTree path>"}
            result = als_writer.set_attribute(
                target,
                expression,
                value,
                attribute=attribute,
                index=index,
                create=create,
                allow_live_running=allow_live_running,
            )
    except als_writer.AlsRefused as exc:
        return {
            "ok": False,
            "refused": True,
            "written": False,
            "backup_made": False,
            "error": str(exc),
            "note": (
                "Nothing was written and no backup was made. Close the set in Live and try again."
            ),
        }
    except als_writer.AlsWriteError as exc:
        return {"ok": False, "written": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "written": False, "error": f"file error: {exc}"}

    return {
        "ok": result.verified,
        "operation": operation,
        "file": str(result.file),
        "backup": str(result.backup),
        "verified": result.verified,
        "verify_failures": list(result.verify_failures),
        "changes": [
            {"what": c.what, "attribute": c.attribute, "before": c.before, "after": c.after,
             "created": c.created}
            for c in result.changes
        ],
        "size_before": result.size_before,
        "size_after": result.size_after,
        "live_check": {
            "live_running": result.live_check.live_running,
            "method": result.live_check.method,
            "detail": result.live_check.detail,
        },
        "measured_notes": list(result.notes),
        "notes": [
            "verified indicates the file was re-read from disk and edits verified.",
            "Undo with operation='restore' and the backup path.",
        ],
    }


# ------------------------------------------------------------ generic escape
@mcp.tool(annotations=READS)
def lom_get(
    path: Annotated[
        str,
        Field(
            description=(
                "Dotted LOM path rooted at song, app or song.view, with integer "
                "subscripts for collections: 'song.tempo', 'song.tracks[0].name', "
                "'song.tracks[0].devices[1].parameters[3].value'. Any other root is "
                "refused before Live is contacted."
            )
        ),
    ],
) -> dict[str, Any]:
    """Read one property value by LOM path, with its catalogued meaning.

    The single-property read behind every getter here, for the field no getter
    exposes. Where the catalog knows the path, the answer carries a ``catalog``
    hint, and where it knows that a particular value is a sentinel it carries
    ``means`` as well: a ``scene.tempo`` of -1.0 reads back as "no tempo set for
    this scene" rather than as a tempo below the legal minimum.

    Returns:
        Dictionary with path, value, type, an optional display string for a device
        parameter, and the catalog hint and meaning where those exist.

    Note:
        A path that is not rooted at song, app or song.view is refused here without
        Live being contacted. A path Live rejects comes back as ``ok: false`` with
        Live's own ``code``, so a missing property is an error rather than a null.

        Reach for a getter first where one exists: get_session for the whole set,
        get_track, get_clip and get_devices for one of each, read_clip_notes for
        notes, read_automation for envelopes. All of them cost one round trip for
        many fields, where this costs one per field. Use lom_batch to read several
        paths in a single trip, lom_describe when the property names are not known
        yet, and lom_set to write.
    """
    try:
        paths.validate(path)
    except paths.PathSyntaxError as exc:
        return {"ok": False, "error": str(exc)}
    hint = _catalog_hint(path, writing=False)
    try:
        result: dict[str, Any] = {"ok": True, **_client_instance().get(path)}
    except LiveError as exc:
        failed = _live_error(exc)
        if hint is not None:
            failed["catalog"] = hint
        return failed
    if hint is not None:
        result["catalog"] = hint
    meaning = _means_for_path(path, result.get("value"))
    if meaning is not None:
        result["means"] = meaning
    return result


# Measured 2026-08-30 against Live 12.4.5: an out-of-range write is refused out
# loud and stores nothing, and quantised snap is what the read-back really catches.
# Measured 2026-08-29 against Live 12.4.5: handed a plain JSON value, the five
# object-valued properties answer not_settable. Measured 2026-08-30, same Live:
# RoutingType carries category and display_name read-only and an attached_object
# pointing at the Track.
@mcp.tool(annotations=SETS)
def lom_set(
    path: Annotated[
        str,
        Field(
            description=(
                "Settable dotted LOM path, e.g. 'song.tempo' or "
                "'song.tracks[0].mixer_device.volume.value'. Note the trailing "
                "``.value``: a mixer control is a DeviceParameter object and the "
                "number lives one level inside it."
            )
        ),
    ],
    value: Annotated[
        Any,
        Field(
            description=(
                "The value to store. A number, string or boolean for a scalar "
                "property; a reference dict {'__path__': 'song.tracks[2]'} for a "
                "property whose value is itself a Live object."
            )
        ),
    ],
) -> dict[str, Any]:
    """Write a property value by LOM path and return verification read-back.

    The generic write, for the property no dedicated tool covers. Prefer
    set_parameter for a device knob, set_mix for a mixer control and set_tempo for
    the tempo: each validates against the catalog before writing, which this does
    not. Use lom_call for a method, which is not reachable as a property, and
    lom_batch to send several writes in one round trip.

    Returns:
        Dictionary reporting requested, before, after, clamped, and changed status.
        For a device parameter it also carries ``display`` and ``is_quantized``.

    Note:
        Live does not accept an out-of-range value silently, however often that is given
        as the reason to read back. It refuses out loud and stores nothing, so
        ``...volume.value = 1.4`` answers
        ``live_error "Invalid value. Check the parameters range with min/max"`` and leaves
        0.85 standing. A tempo of 5000 and a panning of 5 are refused the same way.

        What the read-back really catches is quantised snap.
        ``<Compressor>.parameters[10].value = 0.4`` (Model: Peak / RMS / Expand)
        stored 0 and reported ``clamped: true, read_back: "clamped", display:
        "Peak", is_quantized: true``. A caller who asks for a value between two steps
        gets a different one and is told nowhere else.

        Three more, measured separately. A write can apply asynchronously, so the
        first read is stale and comes back ``read_back: "not_observed"``, which is
        not ``clamped`` and not a failure. A method call has no read-back at all.
        And Live ignores an unknown property name silently, reporting success for a
        write that did nothing, so check ``changed`` rather than ``ok``.

        A collection (``song.tracks``, ``device.parameters``) cannot be assigned.
        Address an element and set one of its properties.

        A Live object cannot travel as a plain value. Where a property really holds
        one (``song.view.selected_track``, ``song.view.selected_scene``,
        ``song.view.detail_clip``, ``browser.hotswap_target``, ``clip.groove``, and
        on a device ``input_routing_type`` and ``input_routing_channel``) pass
        ``value={"__path__": "song.tracks[2]"}``, resolved through the same guards as
        a lom_call argument (protocol section 5.4). Handed a plain JSON value the
        first five answer ``not_settable``.
        The two routing properties, a compressor's sidechain source and its tap
        point, take their reference out of ``<device>.available_input_routing_types``
        and ``...available_input_routing_channels``, never a name string, because
        RoutingType carries ``category`` and ``display_name`` read-only and an
        ``attached_object`` pointing at the Track.
    """
    try:
        paths.validate(path)
    except paths.PathSyntaxError as exc:
        return {"ok": False, "error": str(exc)}
    hint = _catalog_hint(path, writing=True)
    try:
        written = _client_instance().set(path, value)
    except LiveError as exc:
        failed = _live_error(exc)
        if hint is not None:
            failed["catalog"] = hint
        return failed
    result: dict[str, Any] = {"ok": True, **written, "proves": STORED_NOT_AUDIBLE}
    if hint is not None:
        result["catalog"] = hint
    meaning = _means_for_path(path, result.get("after"))
    if meaning is not None:
        result["after_means"] = meaning
    return result


@mcp.tool(annotations=ADDS)
def lom_call(
    path: Annotated[
        str,
        Field(
            description=(
                "Dotted LOM path to the object the method sits on, e.g. 'song' for "
                "song.move_device or 'song.tracks[0]' for a track method. The "
                "object that owns the method, not the object being acted on."
            )
        ),
    ],
    method: Annotated[
        str,
        Field(
            description=(
                "Method name to invoke, e.g. 'move_device'. Only names on the "
                "Remote Script's allowlist are accepted; lom_describe lists the "
                "ones permitted on a given object."
            )
        ),
    ],
    args: Annotated[
        list[Any] | None,
        Field(
            description=(
                "Positional arguments, in order. Keyword arguments are not "
                "supported. A Live object is passed as a reference dict, "
                "{'__path__': 'song.tracks[2]'}, rather than as a name or an index."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Invoke an allowlisted method on a Live object.

    The escape hatch for the calls that have no tool of their own, such as
    ``song.move_device`` for reordering a device chain. What it can reach is bounded
    by the Remote Script's allowlist, not by this signature.

    Returns:
        Dictionary reporting the call and its return value. A refused call names the
        reason, and where a dedicated tool covers the same ground it comes back with
        ``use_instead`` naming that tool.

    Note:
        A method call has no read-back, so nothing here proves the effect the way a
        write through lom_set or set_parameter does. Follow it with the matching read
        (get_devices after a move, get_session after a structural change) rather than
        treating a successful return as evidence.

        Prefer the dedicated tool wherever one exists: create_clip, delete_track,
        set_tempo, play and stop all verify what this cannot. Use lom_get and lom_set
        for properties, which are not reachable as methods, and lom_batch to send
        several calls in one round trip.
    """
    try:
        paths.validate(path)
    except paths.PathSyntaxError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        called = _client_instance().call(path, method, args or [])
    except LiveError as exc:
        failed = _live_error(exc)
        instead = _TOOL_FOR_METHOD.get(method)
        if instead is not None:
            failed["use_instead"] = instead
        return failed
    return {"ok": True, **called, "proves": RAN_NOT_EFFECTIVE}


# Measured 2026-09-01 against Live 12.4.5: four transport jumps interleaved with
# four reads returned four identical pre-jump values and no error.
@mcp.tool(annotations=ADDS)
def lom_batch(
    ops: Annotated[
        list[BatchOpArg],
        Field(
            description=(
                "The operations to run, in order. Each one declares its own op, "
                "path and payload; the item schema carries the field meanings."
            )
        ),
    ],
    atomic: Annotated[
        bool,
        Field(
            description=(
                "True stops at the first error, leaving the operations before it "
                "applied and the ones after it untried. False runs every operation "
                "and reports each result. Neither rolls anything back."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Execute multiple raw LOM operations (get, set, call) in a single round trip.

    One trip instead of many, which is what makes a wide read affordable at roughly
    450 ms per round trip.

    Returns:
        Dictionary containing ordered results for each operation.

    Note:
        A batch runs inside one handler call, so nothing Live recomputes between
        operations is visible to a later operation in the same batch. A read that follows
        a transport jump in the same batch still reports the position from before the
        move. Four jumps interleaved with four reads returned four identical pre-jump
        values and no error at all. That is the failure shape to expect here. It does not
        raise. It returns a clean set of numbers that look like a measurement of a
        parameter which never changes, and the conclusion drawn from them is wrong. Sample
        a moving transport with one call per position, never inside a batch.

        ``atomic`` is a stop switch and not a transaction: nothing is undone. After a
        partial failure, read the state back rather than assuming either all or none
        of the batch landed.

        Prefer a dedicated tool for anything it covers. get_session, get_track and
        get_devices already batch their own reads, and set_mix writes a whole mixer
        in one trip, each with verification this does not perform.
    """
    if not isinstance(ops, list) or not ops:
        return {"ok": False, "error": "ops must be a non-empty list of operations"}
    try:
        result = _client_instance().batch(toolargs.batch_ops(ops), atomic=atomic)
    except LiveError as exc:
        return _live_error(exc)
    for row in result.get("results") or []:
        if isinstance(row, dict) and row.get("status") == "success":
            meaning = _means_for_path(str(row.get("path") or ""), row.get("value"))
            if meaning is not None:
                row["means"] = meaning
    return {
        "ok": int(result.get("error_count", 0)) == 0,
        **result,
        "round_trips": 1,
        "note": BATCH_NOT_ATOMIC,
    }


@mcp.tool(annotations=READS)
def lom_describe(
    path: Annotated[
        str,
        Field(
            description=(
                "Dotted LOM path to the object to reflect on: 'song', "
                "'song.tracks[0]', 'song.tracks[0].devices[1]'."
            )
        ),
    ],
    depth: Annotated[
        int,
        Field(
            description=(
                "How many levels of child collection to walk. 1 reports the "
                "children of the addressed object only. Raising it multiplies the "
                "work and the cost is unmeasured."
            )
        ),
    ] = 1,
) -> dict[str, Any]:
    """Reflect on any LOM object: class, properties, children, callable methods.

    The raw reflection, reporting what the object itself declares. Use it to find
    the property names on an object the catalog does not cover, then read them with
    lom_get and write them with lom_set.

    Returns:
        Dictionary reporting object class, properties, child collections with their
        counts, and the methods the Remote Script allowlist will let you call.

    Note:
        For a device, prefer ``describe`` with ``with_parameters=True``. This tool
        reports a device's ``parameters`` child as a count, and a count is exactly
        what a Live ``Vector`` that refuses ``len()`` fails to give, so a plug-in
        with parameters can be reported as having none. ``describe`` falls back to
        probing indices when that happens and says which answer it is handing back.

        The methods listed here are names, not calls: invoke one through lom_call.
    """
    try:
        paths.validate(path)
    except paths.PathSyntaxError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return {"ok": True, **_client_instance().describe(path, depth=max(1, depth))}
    except LiveError as exc:
        return _live_error(exc)


@mcp.tool(annotations=READS)
def lom_enums(
    type_name: Annotated[
        str,
        Field(
            description=(
                "Dotted Live enum name, e.g. 'Song.Quantization'. Leave empty to "
                "list the enum type names instead of the members of one."
            )
        ),
    ] = "",
) -> dict[str, Any]:
    """Look up the integer behind a named Live enum member.

    Live stores an enum property as a bare integer, so a write needs the number and
    a read hands back a number that means nothing on its own. This is the lookup in
    both directions.

    Returns:
        Dictionary of member names against their integer values, or the list of enum
        type names when ``type_name`` is empty.

    Note:
        Call this before writing an enum-valued property through lom_set or
        set_parameter, rather than guessing that the members run in the order Live
        draws them. This answers what a type's members are. lom_describe answers
        what properties an object has, and neither answers the other.
    """
    params: dict[str, Any] = {}
    if type_name.strip():
        params["type"] = type_name.strip()
    try:
        found = _client_instance().send("enum_names", params)
    except LiveError as exc:
        return _live_error(exc)
    out: dict[str, Any] = {"ok": True, **found}
    if "members" in found:
        out["note"] = (
            "Read from Live itself, so these are measured names. A catalog `means` "
            "entry built from them may say so; one built from the LOM reference may not."
        )
    else:
        out["note"] = (
            "Candidates found by their signature: every public attribute is a plain int. "
            "Match a catalog row by its enum length, then ask for that type by name."
        )
    return out


# Scored against a 444-query producer-language set carrying the correct mechanism for
# each query. On the half held back from tuning, the mechanism is right 83 percent of
# the time against 17 percent for a substring search over the same catalog, and where
# the answer is a row, that row comes first 59 percent of the time against 10 percent.
@mcp.tool(annotations=READS)
def find_path(
    query: Annotated[
        str,
        Field(
            description=(
                "What you want to do, in your own words. 'turn the bass down', "
                "'sidechain the pad', 'where is the loop brace'. Producer vocabulary is "
                "expected: the lookup translates it into the catalog's own words."
            )
        ),
    ],
    limit: Annotated[
        int,
        Field(
            default=lookup.DEFAULT_LIMIT,
            ge=1,
            le=20,
            description="How many rows to return. Six is about 1.5k tokens.",
        ),
    ] = lookup.DEFAULT_LIMIT,
    area: Annotated[
        str,
        Field(
            default="",
            description=(
                "Restrict to one catalog area, e.g. 'clip' or 'track'. The areas are "
                "listed in the live://catalog resource. Empty searches all of them."
            ),
        ),
    ] = "",
    access: Annotated[
        str,
        Field(
            default="",
            description=(
                "Restrict to rows granting this verb: get, set, call, observe or "
                "automate. Use 'set' when you intend to write, which filters out the "
                "read-only parameter objects."
            ),
        ),
    ] = "",
) -> dict[str, Any]:
    """Find the mechanism that serves an intent, and the catalog rows when rows serve it.

    Returns:
        Dictionary with the chosen route, any matching rows with their paths and docs,
        and the next call to make when rows are not the answer.

    Note:
        Four routes, and which one comes back matters more than the rows do.

        ``catalog_row`` means the rows carry the answer. Each hit gives the path
        template to fill in, the access verbs, the full doc, and ``scopes``: the other
        places the same property exists, since track, return, master and chain mirror one
        another and a clip row usually has an arrangement_clip twin. ``writes`` appears
        when the best row is a read-only parameter object and names the sibling that
        actually takes a value.

        ``device_parameter`` means the answer is a knob inside a device, which this
        catalog cannot enumerate: what a device exposes depends on the device. Filter
        cutoff, resonance, attack, threshold and every other parameter behind a device
        front panel arrive this way, with the procedure to reach them.

        ``blocked`` means Live's API cannot do it at all, with what to do in Live
        instead. ``intent_tool`` is reported in the ``tool`` field alongside the rows
        rather than in place of them, because a tool that verifies its own work is the
        better route to the same end but the path is still worth seeing.
        ``unresolved`` means nothing matched. ``unmatched`` lists the words that found
        nothing, which is usually where the query went wrong.

        This exists so that finding a path costs about 1.5k tokens instead of reading the
        whole catalog. It does not touch Live: it reads the catalog only, so nothing here
        proves anything about the set that is open.
    """
    registry, err = _registry_or_error()
    if err is not None:
        return err
    assert registry is not None
    try:
        found = lookup.find(
            registry,
            query,
            limit=limit,
            area=area or None,
            access=access or None,
        )
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": f"bad lookup arguments: {exc}"}

    out: dict[str, Any] = {"ok": True, "query": found.query, "route": found.route}
    if found.hits:
        out["count"] = len(found.hits)
        out["rows"] = [
            {
                key: value
                for key, value in (
                    ("id", hit.id),
                    ("path", hit.path),
                    ("access", hit.access),
                    ("proven", hit.proven),
                    ("status", hit.status),
                    ("doc", hit.doc),
                    ("scopes", hit.scopes),
                    ("writes", hit.writes),
                )
                if value not in (None, [], "")
            }
            for hit in found.hits
        ]
    for key, value in (
        ("reason", found.reason),
        ("next_call", found.next_call),
        ("tool", found.tool),
    ):
        if value:
            out[key] = value
    if found.unmatched:
        out["unmatched"] = found.unmatched
        out["unmatched_note"] = (
            "These words matched nothing in the catalog. If one of them is the point of "
            "the question, the answer is probably a device parameter rather than a row."
        )
    if found.notes:
        out["notes"] = found.notes
    return out


def _analyzer_target_name(target: str) -> str | None:
    """Resolve a target track specifier to its track name over Channel A.

    The analyzer reports the name of the track it sits on and that track's id,
    and no index: measured 2026-09-07 against Live 12.4.5, ``live.path`` answers
    ``this_device canonical_parent`` and ``this_device`` alike with an id and with
    no canonical path. An index or "master" is therefore resolved here, where the
    Remote Script can read the set, and the name is what the match is made on.

    ``None`` where the target is already a name, or ``*``, or where Live refuses
    the path, which is what an index past the end of the set does. Costs one round
    trip, and only for a target that needs it.
    """
    if target == "*":
        return None
    if target.isdigit():
        path = f"song.tracks[{int(target)}].name"
    elif target.casefold() in ("master", "main"):
        path = "song.master_track.name"
    else:
        return None
    try:
        value = _client_instance().get(path).get("value")
    except LiveError:
        return None
    return None if value is None else str(value)


@mcp.tool(annotations=READS)
async def analyze_audio(
    track: Annotated[
        str | int,
        Field(
            default="master",
            description=(
                "Which analyzer to read: a track name, a zero-based track index, "
                "'master' for the main track, or '*' for whichever instance answers "
                "first. Every instance in the set answers every request, and the "
                "result lists them all under 'answered'. An index or 'master' costs "
                "one extra round trip, because the device reports the name of its "
                "track and the index is resolved against the set here."
            ),
        ),
    ] = "master",
    window_ms: Annotated[
        int,
        Field(
            default=400,
            ge=50,
            le=2000,
            description=(
                "How far back the averaged measurements reach, in milliseconds. "
                "This governs the averaged figures alone: RMS, both correlations "
                "and the seven bands. Loudness keeps its own EBU windows and the "
                "peaks are maxima since the last reset. "
                "This looks backwards at audio that already played and returns "
                "immediately. It does not record for this long. The device buffers "
                "a fixed number of samples, so the window this reaches depends on "
                "the sample rate: about 2180 ms at 44.1 kHz and 1000 ms at 96 kHz. "
                "A longer request is clamped without complaint, so read window_ms "
                "back off the result rather than assuming the request was honoured."
            ),
        ),
    ] = 400,
    reset: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Restart the integrated loudness and loudness range before "
                "measuring, to integrate one section rather than everything since "
                "the audio engine started. The reply that performs the reset "
                "reports no integrated figure, because it had just begun."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Measure the audio itself: loudness, true peak, stereo phase and band balance.

    Reads the LiveMaestro Analyzer Max for Live device over loopback OSC. This
    is the only tool that needs that device. Without it every other tool works and
    this one reports device_not_found with the steps to load it.

    What comes back, and what each figure is:

    - Loudness to EBU R128, from Max's loudness~: momentary (400 ms), short term
      (3 s), gated integrated since the last reset, and loudness range.
    - True peak, oversampled, so peaks between two samples count. Sample peak and
      RMS beside it, and the crest factor between peak and RMS.
    - Stereo correlation over the whole band and below 120 Hz, and whether the low
      end survives being summed to mono.
    - Energy in seven bands on the mono sum, plus their energy weighted centre.
      That centre is a centre of mass over seven band levels, not an FFT centroid.

    Where it measures matters. The device reads the signal at its own position in
    the chain, so it belongs last on the track: an effect loaded after it is not
    in what it reports. A master track's device chain sits before the master
    fader, so a master reading matches the set's output only while that fader is
    at 0.0 dB.

    The two peak figures are maxima since the meter was last reset and only rise.
    To measure a peak, call once with reset=true, let at least one pass of the
    material through, then call again. The averaged figures need no reset.

    A read-back proves the stored value and never audibility: this proves what the
    signal measured as it passed the device, which is a different and stronger
    claim than any parameter value, and still not a claim about the room.
    """
    target = str(track).strip()
    match_name = _analyzer_target_name(target)
    client = AnalyzerClient()
    replies = await client.measure(window_ms=window_ms, reset=reset)
    return analyzer_report.build(
        replies, target, reset=reset, match_name=match_name
    ).model_dump()


# ------------------------------------------------------------------ resources
def _row_payload(spec: PathSpec, *, doc_cap: int | None = None) -> dict[str, Any]:
    """Serialize a catalog PathSpec instance into a resource dictionary."""
    out: dict[str, Any] = {}
    for field in dataclass_fields(spec):
        value = getattr(spec, field.name)
        if field.name == "doc":
            continue
        default = field.default
        if default is not MISSING and default is not None and value == default:
            continue
        # A field at its empty value carries nothing and is dropped. The condition above
        # cannot do it: it skips a field only when the dataclass default is not None, so
        # range, enum, display and method would serialise as nulls on all 1166 rows, and
        # means, params and args as {} and [] because a default_factory reads as MISSING.
        # Measured: 134k tokens for the full index, 101k without them.
        if value is None or value == {} or value == []:
            continue
        if field.name in ("params", "args"):
            out[field.name] = [getattr(entry, "name", str(entry)) for entry in value]
            continue
        out[field.name] = _enum_value(value)
    out.setdefault("id", spec.id)
    out.setdefault("path", spec.path)
    out.setdefault("status", spec.status.value)

    doc = " ".join((spec.doc or "").split())
    if doc_cap is not None and len(doc) > doc_cap:
        cut = doc[:doc_cap]
        space = cut.rfind(" ")
        out["doc"] = (cut[:space] if space > 40 else cut) + " ..."
        out["doc_truncated"] = True
    else:
        out["doc"] = doc
    return out


def _catalog_defaults() -> dict[str, Any]:
    """Return dictionary of default field values omitted from individual catalog rows."""
    defaults: dict[str, Any] = {}
    for field in dataclass_fields(PathSpec):
        if field.name in ("id", "path", "doc"):
            continue
        if field.default is not MISSING and field.default is not None:
            defaults[field.name] = _enum_value(field.default)
    defaults.setdefault("access", ["get"])
    defaults.setdefault("params", [])
    defaults.setdefault("args", [])
    return defaults


@mcp.resource("live://catalog")
def catalog_resource() -> str:
    """Return a map of the catalog: counts, areas, status meanings, and how to search it."""
    try:
        registry = default_registry()
    except (CatalogError, OSError) as exc:
        return json.dumps({"error": f"the path catalog could not be loaded: {exc}"}, indent=2)

    specs = registry.all()
    areas: dict[str, int] = {}
    for spec in specs:
        areas[area_of(spec.id)] = areas.get(area_of(spec.id), 0) + 1
    payload = {
        "count": len(specs),
        "status_counts": registry.status_counts(),
        "status_meaning": {
            "verified": "run against a real Live and watched to work",
            "broken": "run and it did not work; the reason is in doc",
            "untested": "nobody has tried it: a hypothesis with a path attached",
        },
        "areas": dict(sorted(areas.items())),
        "defaults": _catalog_defaults(),
        "how_to_find_a_path": (
            "Call the find_path tool with the intent in your own words. It answers in "
            "about 1.5k tokens with the rows that apply, or with the mechanism to use "
            "when rows are not the answer: a knob on a device is discovered at runtime "
            "and is not in this catalog, and some intents Live cannot perform at all."
        ),
        "browse_an_area_at": (
            "live://catalog/{selector}, where selector is an area from the list "
            "above, a status, or a substring. That returns full rows and is large: the "
            "track area alone is about 32k tokens."
        ),
        "why_no_rows_here": (
            "All 1166 rows in one payload cost about 134,000 tokens, more than most "
            "callers can afford, and would still not answer the commonest questions: "
            "a filter cutoff is a runtime device parameter and is not a row at all. "
            "Rows come from find_path or from the selector above."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.resource("live://catalog/{selector}")
def catalog_filtered(selector: str) -> str:
    """Return filtered catalog rows matching area, status, or search query."""
    try:
        registry = default_registry()
    except (CatalogError, OSError) as exc:
        return json.dumps({"error": f"the path catalog could not be loaded: {exc}"}, indent=2)

    applied: dict[str, str] = {}
    specs = registry.by_area(selector)
    if specs:
        applied = {"area": selector}
    else:
        try:
            specs = registry.by_status(selector)
            applied = {"status": selector}
        except ValueError:
            specs = registry.search(selector)
            applied = {"search": selector}

    return json.dumps(
        {
            "filter": applied,
            "count": len(specs),
            "defaults": _catalog_defaults(),
            "rows": [_row_payload(spec) for spec in specs],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("live://session")
def session_resource() -> str:
    """Return JSON string snapshot of running Live session state."""
    client = _client_instance()
    try:
        info = client.script_info()
        snap = introspect.snapshot(client)
    except LiveError as exc:
        return json.dumps(_live_error(exc), indent=2)
    except introspect.IntrospectionError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    return json.dumps(
        {
            "script": info,
            "session": snap.to_dict(),
            "note": (
                "Live's in-memory state at taken_at. Stale as soon as anyone touches Live, "
                "and unrelated to the saved .als."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


_LIMITS_FALLBACK: list[dict[str, str]] = [
    {"limit": "Render / export / bounce", "kind": "LOM", "class": "hard",
     "why": "The Live Object Model has no export, bounce or render call.",
     "instead": "A human does the final mixdown."},
    {"limit": "Save the project", "kind": "LOM", "class": "hard",
     "why": "There is no save call, so the .als on disk is only as fresh as the last Ctrl+S.",
     "instead": "A human presses Ctrl+S; Channel B reads the saved state."},
    {"limit": "Hearing", "kind": "physical", "class": "hard",
     "why": "Every value can be read and proved; whether it sounds good cannot.",
     "instead": "The machine measures, the human hears."},
    {"limit": "Reorder devices: an assumed limit that turned out not to be one",
     "kind": "none", "class": "not a limit",
     "why": "Song.move_device(device, target_chain, position) exists and works. The call "
            "is on the Song rather than on the Device, which is why it reads as missing. "
            "Measured 2026-08-30 against Live 12.4.5; the row song.move_device is "
            "verified and the method is on the script's allowlist.",
     "instead": "Nothing to work around: call it through lom_call. Loading order is still "
                "worth planning: effects are appended, an instrument replaces."},
    {"limit": "Group tracks", "kind": "LOM", "class": "hard",
     "why": "group_track and is_grouped are read-only.",
     "instead": "Ctrl+G in the GUI, and re-read afterwards: grouping shifts every index."},
    {"limit": "Set a sidechain source: widely assumed to be a hard LOM limit, and it is not",
     "kind": "none", "class": "not a limit",
     "why": "The routing surface hangs off the device, not the track: input_routing_type is "
            "the source, input_routing_channel the tap point, and "
            "available_input_routing_types / _channels list what fits. Measured 2026-08-30 "
            "against Live 12.4.5: a reference write read back applied, and display_name then "
            "read the source track's name. S/C On arms it, as an ordinary parameter.",
     "instead": "Nothing to work around: lom_set the reference, live, no file edit and no "
                "restart. als_write(operation='sidechain') stays for a project Live has "
                "not got open."},
    {"limit": "Write track automation", "kind": "LOM", "class": "hard",
     "why": "clip.automation_envelope() returns None for Arrangement clips.",
     "instead": "Write a Session clip, then arrange it. Never the reverse."},
    {"limit": "Unconfigured VST parameters", "kind": "Live", "class": "gui",
     "why": "How much of a plugin the LOM sees is decided by the plugin. Measured "
            "2026-09-01 against Live 12.4.5, each loaded from the browser with no GUI "
            "interaction: two VST3 instruments exposed 1 parameter, a VST3 effect 16, a "
            "VST3 instrument 29 and a VST2 effect 17. Configure takes a control into the "
            "strip, 128 slots per instance, and stores the mapping in the set rather than "
            "in the plugin.",
     "instead": "Configure in Live's GUI, per parameter and per instance, or "
                "als_write(operation='configure') with the set closed in Live."},
    {"limit": "Units on VST2", "kind": "Live", "class": "gui",
     "why": "str_for_value() returns nothing usable for VST2 (measured).",
     "instead": "Write normalised; calibrate against the plug-in's own display once."},
    {"limit": "Transactions", "kind": "LOM", "class": "protocol",
     "why": "lom_batch runs in order and cannot roll back; atomic only stops at the first error.",
     "instead": "Nothing. Do not present a batch as atomic."},
    {"limit": "Retry a timed-out write", "kind": "protocol", "class": "protocol",
     "why": "A write that timed out may have landed; repeating it can double a note list.",
     "instead": "Verify with a read. The client refuses to guess."},
    {"limit": "Two requests at once", "kind": "protocol", "class": "protocol",
     "why": "No framing and no request ids: replies correlate by order alone (measured).",
     "instead": "lom_batch."},
    {"limit": "Change the Remote Script without restarting Live", "kind": "Live",
     "class": "protocol",
     "why": "Live loads Remote Scripts only at startup and prefers a stale __pycache__.",
     "instead": "A catalog row instead of a handler, wherever that is possible."},
]


@mcp.resource("live://limits")
def limits_resource() -> str:
    """Return system limits specification and documentation reference."""
    payload: dict[str, Any] = {
        "classes": {
            "LOM": "Live's Object Model does not expose it. Only Ableton can change that.",
            "Live": "Live exposes it but gates it behind the GUI. Only the user can lift it.",
            "protocol": "This server's wire format or script cannot carry it. Fixable here.",
            "physical": "Not a software problem.",
        },
        "provenance": {
            "measured": "tested against running Live or a produced artefact",
            "read from the source": "derived from code, not executed",
            "estimated": "an expectation, explicitly not a promise",
            "unverified": "nobody has tried it",
        },
        "always_true": [
            STORED_NOT_AUDIBLE,
            BATCH_NOT_ATOMIC,
            SESSION_ONLY_NOTE,
            CONFIGURE_NOTE,
            GROUP_TRACK_NOTE,
        ],
    }

    try:
        text = LIMITS_DOC.read_text(encoding="utf-8")
    except OSError:
        payload["source"] = "built-in summary (docs/limits.md is not in this installation)"
        payload["limits"] = _LIMITS_FALLBACK
        return json.dumps(payload, ensure_ascii=False, indent=2)

    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        section = re.match(r"^##\s+(\d+)\.\s+(.*)$", line)
        if section is not None:
            current = {"number": int(section.group(1)), "title": section.group(2).strip(),
                       "entries": []}
            sections.append(current)
            continue
        # docs/limits.md tags an entry with its class in parentheses after the title:
        # "### Track reordering (LOM)". The keys of payload["classes"] are LOM, Live,
        # protocol and physical, so the tag is matched case-insensitively and mapped on.
        entry = re.match(r"^###\s+(.*?)(?:\s+\((LOM|Live|Protocol|Physical)\))?\s*$", line)
        if entry is not None and current is not None:
            tag = (entry.group(2) or "").lower()
            known = [key for key in payload["classes"] if key.lower() == tag]
            current["entries"].append(
                {"title": entry.group(1).strip(), "class": known[0] if known else "unclassified"}
            )
    payload["source"] = str(LIMITS_DOC)
    payload["sections"] = sections
    payload["limits"] = _LIMITS_FALLBACK
    payload["text"] = text
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> None:
    """Serve Live Maestro MCP server over stdio transport."""
    logger.info(
        "live-maestro serving on stdio; Live bridge at %s:%d", _host(), _port()
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
