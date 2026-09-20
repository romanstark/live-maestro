"""Validate FastMCP tool input schemas and argument models.

Tests that tool schemas expose explicit field names and types without unresolved
$ref references, and verifies coercion and validation rules in toolargs.py.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import re
from typing import Any, get_args

import pytest
from mcp.types import TextContent
from pydantic import ValidationError

from live_maestro import server
from live_maestro.music.notes import KNOWN_NOTE_KEYS, validate_note_dicts
from live_maestro.toolargs import (
    BatchOp,
    NoteIn,
    batch_ops,
    inline_schema_refs,
    note_dicts,
)

REMOTE_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "live-remote-script" / "__init__.py"

#: Arguments exempt from container shape checking.
#: lom_call forwards positional arguments to arbitrary allowlisted LOM methods
#: which accept diverse types and object reference dictionaries.
TYPELESS_BY_DESIGN = {("lom_call", "args")}


def _text_payload(result: Any) -> str:
    """Return the text a call_tool answer carries, whichever shape it arrived in.

    ``call_tool`` answers either a sequence of content blocks or a
    ``(blocks, structured)`` pair depending on the version in use, and only the
    text block holds the JSON this file reads.
    """
    blocks = result[0] if isinstance(result, tuple) else result
    first = blocks[0]
    assert isinstance(first, TextContent), (
        f"expected a text content block, got {type(first).__name__}"
    )
    return first.text


def _tools() -> list[Any]:
    """Return all registered MCP tools with input schemas."""
    return asyncio.run(server.mcp.list_tools())


def _says_nothing_about_its_contents(schema: dict[str, Any]) -> str | None:
    """Return description if schema represents a container without typed items."""
    for branch in schema.get("anyOf") or [schema]:
        if "$ref" in branch:
            return "a reference the client is left to resolve on its own"
        if branch.get("type") == "object":
            typed_values = isinstance(branch.get("additionalProperties"), dict)
            if not branch.get("properties") and not typed_values:
                return "an object with neither declared properties nor a typed value schema"
        if branch.get("type") == "array":
            items = branch.get("items") or {}
            if "$ref" in items:
                return "an array of references the client is left to resolve on its own"
            if not items:
                return "an array whose items declare no type at all"
            if items.get("type") == "object" and not items.get("properties"):
                return "an array of objects with no declared properties"
    return None


def test_no_tool_argument_declares_a_container_without_a_shape() -> None:
    """Verify that all nested container arguments declare field names and types."""
    offenders: list[str] = []
    for tool in _tools():
        for name, prop in (tool.inputSchema.get("properties") or {}).items():
            if (tool.name, name) in TYPELESS_BY_DESIGN:
                continue
            defect = _says_nothing_about_its_contents(prop)
            if defect is not None:
                offenders.append(f"{tool.name}.{name} is {defect}")
    assert not offenders, (
        "these arguments tell a client nothing about their contents:\n  "
        + "\n  ".join(offenders)
        + "\nDeclare a model in live_maestro.toolargs, or add the pair to "
          "TYPELESS_BY_DESIGN with the reason it cannot be typed."
    )


def test_the_exemption_list_stays_a_list_of_real_arguments() -> None:
    """Verify that TYPELESS_BY_DESIGN only contains active tool arguments."""
    known = {(tool.name, name)
             for tool in _tools()
             for name in (tool.inputSchema.get("properties") or {})}
    stale = sorted(TYPELESS_BY_DESIGN - known)
    assert not stale, f"TYPELESS_BY_DESIGN names arguments that are gone: {stale}"


def test_every_published_argument_says_what_it_means() -> None:
    """A declared type without a description still leaves the units unstated.

    A client reads ``inputSchema`` before it reads any prose, and
    ``{"type": "integer", "title": "Track"}`` does not say which collection the
    number indexes or where the counting starts. Shared meanings are declared once
    in ``live_maestro.toolargs``, and a bespoke argument carries its own.
    """
    offenders: list[str] = []
    for tool in _tools():
        for name, prop in (tool.inputSchema.get("properties") or {}).items():
            if not prop.get("description"):
                offenders.append(f"{tool.name}.{name}")
    assert not offenders, (
        "these arguments reach a client with a type but no meaning:\n  "
        + "\n  ".join(offenders)
        + "\nAnnotate the parameter with Field(description=...), or reuse one of "
          "the shared annotations in live_maestro.toolargs."
    )


def test_every_tool_declares_what_it_does_to_the_set() -> None:
    """Annotations are the half of the disclosure a client can act on mechanically.

    Prose can say a call is destructive. Only ``destructiveHint`` lets a client hold
    it back without reading. A tool that declares nothing is indistinguishable from
    a read.
    """
    missing = [tool.name for tool in _tools() if tool.annotations is None]
    assert not missing, (
        "these tools publish no annotations:\n  "
        + "\n  ".join(missing)
        + "\nPass one of READS, SETS, ADDS or DESTROYS to @mcp.tool()."
    )


def test_no_tool_invites_a_retry_it_cannot_survive() -> None:
    """``idempotentHint`` is a promise that a failed call is safe to send again.

    A write that timed out may already have landed in Live, which is why this
    project never retries one automatically (docs/limits.md, 'Write timeouts and
    retry policy'). A tool that accumulates, toggles or removes must not claim
    otherwise: the hint would license exactly the retry that doubles the notes or
    deletes the wrong device. A read may always claim it.
    """
    offenders: list[str] = []
    for tool in _tools():
        hints = tool.annotations
        if hints is None:
            continue  # covered by its own test
        if hints.destructiveHint and hints.idempotentHint:
            offenders.append(f"{tool.name} is destructive and claims to be idempotent")
        if hints.readOnlyHint and hints.destructiveHint:
            offenders.append(f"{tool.name} claims to read only and to destroy at once")
        if hints.readOnlyHint and not hints.idempotentHint:
            offenders.append(f"{tool.name} reads but declines to be called twice")
    assert not offenders, (
        "annotations that contradict themselves:\n  " + "\n  ".join(offenders)
    )


def test_no_tool_schema_leaves_a_reference_for_the_client_to_follow() -> None:
    """Verify that tool input schemas do not contain unresolved $ref or $defs."""
    offenders: list[str] = []
    for tool in _tools():
        if _carries(tool.inputSchema, "$ref"):
            offenders.append(f"{tool.name} still references a definition")
        if "$defs" in tool.inputSchema:
            offenders.append(f"{tool.name} still ships a $defs block")
    assert not offenders, "\n  ".join(offenders)


def _carries(node: Any, key: str) -> bool:
    """Return True if key appears anywhere in the schema tree."""
    if isinstance(node, list):
        return any(_carries(item, key) for item in node)
    if not isinstance(node, dict):
        return False
    return key in node or any(_carries(value, key) for value in node.values())


def test_the_note_schema_reaches_the_client_with_its_field_types() -> None:
    """Verify that write_clip_notes notes items expose explicit field types inline."""
    tool = next(t for t in _tools() if t.name == "write_clip_notes")
    note = tool.inputSchema["properties"]["notes"]["items"]
    assert note.get("properties"), "the note fields are not present inline"
    assert note["properties"]["pitch"]["type"] == "integer"
    assert note["properties"]["start_time"]["type"] == "number"
    assert note["properties"]["duration"]["type"] == "number"
    assert sorted(note["required"]) == ["duration", "pitch", "start_time"]
    assert note["properties"]["pitch"]["description"], (
        "a declared type without a description still leaves the units unstated"
    )


def test_the_batch_op_schema_reaches_the_client_with_its_verbs() -> None:
    """Verify that lom_batch ops items declare allowed op verbs and properties."""
    tool = next(t for t in _tools() if t.name == "lom_batch")
    op = tool.inputSchema["properties"]["ops"]["items"]
    assert op.get("properties"), "the op fields are not present inline"
    assert sorted(op["properties"]["op"]["enum"]) == ["call", "get", "set"]
    assert op["additionalProperties"] is False, (
        "the schema has to tell a client that an unknown op key is refused"
    )


def test_the_model_docstrings_stay_out_of_the_shipped_schema() -> None:
    """Verify that tool schema descriptions are concise single-line strings."""
    for tool_name, argument in (("write_clip_notes", "notes"), ("lom_batch", "ops")):
        tool = next(t for t in _tools() if t.name == tool_name)
        described = tool.inputSchema["properties"][argument]["items"]["description"]
        assert "\n" not in described, f"{tool_name}.{argument} carries a multi-line docstring"
        assert len(described) < 200, (
            f"{tool_name}.{argument} description is {len(described)} chars. "
            "The long form belongs in the docstring."
        )


# ------------------------------------------------------- reference flattening


def test_inlining_replaces_a_reference_with_its_definition() -> None:
    """Verify that inline_schema_refs replaces $ref pointers with their definitions."""
    flat = inline_schema_refs({
        "type": "object",
        "properties": {"notes": {"type": "array", "items": {"$ref": "#/$defs/N"}}},
        "$defs": {"N": {"type": "object", "properties": {"pitch": {"type": "integer"}}}},
    })
    assert "$defs" not in flat
    assert flat["properties"]["notes"]["items"]["properties"]["pitch"]["type"] == "integer"


def test_inlining_keeps_what_sat_beside_the_reference() -> None:
    """Verify inline_schema_refs preserves sibling attributes (default, title)."""
    flat = inline_schema_refs({
        "properties": {"op": {"$ref": "#/$defs/O", "title": "Op", "default": None}},
        "$defs": {"O": {"type": "string", "enum": ["get", "set"]}},
    })
    op = flat["properties"]["op"]
    assert op["type"] == "string"
    assert op["enum"] == ["get", "set"]
    assert op["title"] == "Op"
    assert op["default"] is None


def test_inlining_leaves_a_recursive_model_resolvable() -> None:
    """Verify recursive schemas retain unresolved definitions to avoid recursion."""
    recursive = {
        "properties": {"node": {"$ref": "#/$defs/Node"}},
        "$defs": {"Node": {"type": "object",
                           "properties": {"child": {"$ref": "#/$defs/Node"}}}},
    }
    flat = inline_schema_refs(recursive)
    assert "$defs" in flat, "a surviving reference needs its definitions kept"
    assert flat["$defs"]["Node"]["properties"]["child"]["$ref"] == "#/$defs/Node"


def test_inlining_leaves_a_schema_without_definitions_alone() -> None:
    """Verify that schemas without $defs are returned unchanged."""
    plain = {"type": "object", "properties": {"track": {"type": "integer"}}}
    assert inline_schema_refs(plain) == plain


def test_the_note_schema_declares_every_key_the_validator_knows() -> None:
    """Verify that NoteIn fields match KNOWN_NOTE_KEYS."""
    declared = set(NoteIn.model_fields)
    assert declared == set(KNOWN_NOTE_KEYS), (
        "NoteIn and KNOWN_NOTE_KEYS disagree about what a note carries. "
        f"only in NoteIn: {sorted(declared - KNOWN_NOTE_KEYS)}, "
        f"only in KNOWN_NOTE_KEYS: {sorted(KNOWN_NOTE_KEYS - declared)}"
    )


def test_a_stringified_number_is_coerced_rather_than_refused() -> None:
    """Verify that stringified numbers in note inputs are coerced to numeric types."""
    note = NoteIn.model_validate(
        {"pitch": "36", "start_time": "0.0", "duration": "0.25", "velocity": "100"}
    )
    assert note.model_dump(exclude_unset=True) == {
        "pitch": 36, "start_time": 0.0, "duration": 0.25, "velocity": 100.0,
    }


def test_a_fractional_pitch_is_refused_rather_than_truncated() -> None:
    """Verify that fractional pitch values raise a validation error."""
    with pytest.raises(ValidationError):
        NoteIn.model_validate({"pitch": 36.5, "start_time": 0.0, "duration": 1.0})


def test_an_omitted_optional_stays_omitted_instead_of_becoming_null() -> None:
    """Verify that omitted optional note fields remain absent rather than None."""
    carried = note_dicts([NoteIn.model_validate({"pitch": 60, "start_time": 0.0,
                                                 "duration": 1.0})])
    assert carried == [{"pitch": 60, "start_time": 0.0, "duration": 1.0}]

    report = validate_note_dicts(carried)
    assert report.ok, [i.to_dict() for i in report.issues if i.severity == "error"]
    assert [i.code for i in report.warnings] == ["missing_velocity"]


def test_an_explicit_null_is_read_as_not_given() -> None:
    """Verify explicit null values for optional note fields are treated as omitted."""
    carried = note_dicts([NoteIn.model_validate({
        "pitch": 60, "start_time": 0.0, "duration": 1.0, "velocity": None,
    })])
    assert carried == [{"pitch": 60, "start_time": 0.0, "duration": 1.0}]
    report = validate_note_dicts(carried)
    assert report.ok
    assert [i.code for i in report.warnings] == ["missing_velocity"]

    with pytest.raises(ValidationError) as caught:
        NoteIn.model_validate({"pitch": None, "start_time": 0.0, "duration": 1.0})
    codes = [error["type"] for error in caught.value.errors()]
    assert "missing" not in codes, (
        f"a null pitch was reported as an absent one: {codes}"
    )
    assert codes == ["int_type"], codes


def test_a_boolean_is_refused_where_a_number_belongs() -> None:
    """Verify that boolean values are rejected for numeric note fields."""
    for field in ("pitch", "start_time", "duration", "velocity",
                  "probability", "velocity_deviation", "release_velocity"):
        note = {"pitch": 60, "start_time": 0.0, "duration": 1.0, "velocity": 100}
        note[field] = True
        with pytest.raises(ValidationError, match="boolean is not a number"):
            NoteIn.model_validate(note)

    fine = NoteIn.model_validate({"pitch": 60, "start_time": 0.0, "duration": 1.0,
                                  "velocity": 100, "mute": True})
    assert fine.mute is True


def test_a_note_built_from_the_schemas_own_declaration_is_never_refused() -> None:
    """Verify that a note constructed from schema defaults passes validation."""
    tool = next(t for t in _tools() if t.name == "write_clip_notes")
    note_schema = tool.inputSchema["properties"]["notes"]["items"]

    payload: dict[str, Any] = {}
    for name, spec in note_schema["properties"].items():
        if "default" in spec:
            payload[name] = spec["default"]
        elif spec.get("type") == "integer":
            payload[name] = 60
        else:
            payload[name] = 1.0

    carried = note_dicts([NoteIn.model_validate(payload)])
    report = validate_note_dicts(carried)
    assert report.ok, (
        "a note assembled from the schema's own declaration was refused: "
        f"{[i.to_dict() for i in report.issues if i.severity == 'error']}"
    )

    assert note_schema["additionalProperties"] is False, (
        "the schema says extra keys are welcome while validate_note_dicts refuses them"
    )


def test_an_unknown_note_key_still_reaches_the_validator_with_its_own_message() -> None:
    """Verify that unrecognized note keys pass through to validate_note_dicts."""
    note = NoteIn.model_validate({"pitch": 60, "start_time": 0.0, "duration": 1.0,
                                  "pos": 0, "dur": 1})
    passed_through = note.model_dump(exclude_unset=True)
    assert passed_through["pos"] == 0
    assert passed_through["dur"] == 1


def test_plain_dicts_survive_the_seam_untouched() -> None:
    """Verify that raw dictionary inputs pass through note_dicts unaltered."""
    assert note_dicts([{"pitch": 60, "start_time": 0.0, "duration": 1.0}]) == [
        {"pitch": 60, "start_time": 0.0, "duration": 1.0}
    ]
    assert note_dicts(["not a note"]) == ["not a note"], (
        "a non-mapping entry must reach validate_note_dicts so it can be named "
        "as not_a_mapping with its index"
    )


# --------------------------------------------------------------- batch operations


def _op_keys_the_handler_reads() -> set[str]:
    """Return keys read by Remote Script batch handlers."""
    tree = ast.parse(REMOTE_SCRIPT.read_text(encoding="utf-8"))
    wanted = {"_handle_lom_get", "_handle_lom_set", "_handle_lom_call"}
    keys: set[str] = set()
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in wanted):
            continue
        seen.add(node.name)
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "params"
                    and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)):
                keys.add(sub.args[0].value)
            if (isinstance(sub, ast.Compare)
                    and isinstance(sub.left, ast.Constant)
                    and isinstance(sub.left.value, str)):
                for op, comparator in zip(sub.ops, sub.comparators, strict=False):
                    if (isinstance(op, (ast.In, ast.NotIn))
                            and isinstance(comparator, ast.Name)
                            and comparator.id == "params"):
                        keys.add(sub.left.value)
    assert seen == wanted, f"batch handlers missing from the script: {sorted(wanted - seen)}"
    return keys


def _verbs_the_batch_dispatches() -> set[str]:
    """Return operation verbs dispatched by Remote Script batch handler."""
    tree = ast.parse(REMOTE_SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_handle_lom_batch"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Dict)):
                continue
            named_handlers = any(
                isinstance(target, ast.Name) and target.id == "handlers"
                for target in sub.targets
            )
            if named_handlers:
                return {
                    key.value
                    for key in sub.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
    raise AssertionError("no 'handlers' dispatch dict found in _handle_lom_batch")


def test_the_batch_op_model_offers_exactly_the_verbs_the_script_dispatches() -> None:
    """Verify BatchOp op literals match Remote Script batch handler verbs."""
    declared = set(get_args(BatchOp.model_fields["op"].annotation))
    dispatched = _verbs_the_batch_dispatches()
    assert declared == dispatched, (
        "BatchOp and the Remote Script disagree about the op verbs. "
        f"only in BatchOp: {sorted(declared - dispatched)}, "
        f"only in the script: {sorted(dispatched - declared)}"
    )


def test_the_batch_op_model_declares_exactly_what_the_handler_reads() -> None:
    """Verify BatchOp fields match parameters read by Remote Script batch handlers."""
    declared = set(BatchOp.model_fields)
    expected = _op_keys_the_handler_reads() | {"op"}
    assert declared == expected, (
        "BatchOp and the Remote Script disagree about an op's keys. "
        f"only in BatchOp: {sorted(declared - expected)}, "
        f"only in the handler: {sorted(expected - declared)}"
    )


def test_a_misspelled_op_key_is_refused_instead_of_ignored() -> None:
    """Verify that unrecognized keys in BatchOp inputs raise a validation error."""
    with pytest.raises(ValidationError) as caught:
        BatchOp.model_validate({"op": "get", "path": "song.tempo", "depth": 2})
    assert "depth" in str(caught.value)


def test_a_batch_set_tells_an_absent_value_from_a_null_one() -> None:
    """Verify that batch_ops distinguishes between omitted values and explicit null."""
    absent = batch_ops([BatchOp.model_validate({"op": "set", "path": "song.tempo"})])
    assert absent == [{"op": "set", "path": "song.tempo"}]

    null = batch_ops([BatchOp.model_validate({"op": "set", "path": "song.tempo",
                                              "value": None})])
    assert null[0]["value"] is None

    zero = batch_ops([BatchOp.model_validate({"op": "set", "path": "song.tempo",
                                              "value": 0})])
    assert zero[0]["value"] == 0


def test_an_unknown_op_verb_is_refused_before_the_socket() -> None:
    """Verify that invalid op verbs in BatchOp inputs raise a validation error."""
    with pytest.raises(ValidationError):
        BatchOp.model_validate({"op": "delete", "path": "song.tracks[0]"})


def test_plain_op_dicts_survive_the_seam_untouched() -> None:
    """Verify that raw dictionary inputs pass through batch_ops unaltered."""
    assert batch_ops([{"op": "get", "path": "song.tempo"}]) == [
        {"op": "get", "path": "song.tempo"}
    ]


# ------------------------------------------------------------------- end to end


class _FakeNoteClient:
    """Mock transport client recording notes_set payload and simulating read-back."""

    def __init__(self) -> None:
        self.sent: dict[str, Any] = {}

    def send(self, handler: str, params: dict[str, Any]) -> dict[str, Any]:
        if handler == "notes_set":
            self.sent = params
            return {"before_count": 0, "after_count": len(params["notes"]),
                    "written": len(params["notes"])}
        if handler == "notes_get":
            back = self.sent.get("notes") or []
            return {"notes": back, "count": len(back)}
        raise AssertionError(f"unexpected handler {handler!r}")


def test_a_stringified_note_reaches_live_as_numbers_through_the_real_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify stringified note fields are coerced to numbers during execution."""
    fake = _FakeNoteClient()
    monkeypatch.setattr(server, "_client_instance", lambda: fake)
    monkeypatch.setattr(
        server, "_run", lambda spec_id, **kw: {"ok": True, "value": 4.0}
    )

    result = asyncio.run(server.mcp.call_tool("write_clip_notes", {
        "track": 0,
        "slot": 0,
        "notes": [
            {"pitch": "36", "start_time": "0.0", "duration": "0.25", "velocity": "100"},
            {"pitch": "36", "start_time": "1.0", "duration": "0.25"},
        ],
    }))

    payload = json.loads(_text_payload(result))
    assert payload["ok"] is True, payload
    assert payload["sent"] == 2

    written = fake.sent["notes"][0]
    assert isinstance(written["pitch"], int)
    assert isinstance(written["start_time"], float)
    assert written["pitch"] == 36
    assert written["velocity"] == 100.0


#: Tools whose ``root`` argument names a browser root the Remote Script has to walk.
_BROWSER_ROOT_TOOLS = ("load_device", "find_sound")


def _script_browser_roots() -> tuple[str, ...]:
    """The roots the Remote Script actually walks, read out of the script itself."""
    block = re.search(
        r"_BROWSER_ROOTS = \((.*?)\)", REMOTE_SCRIPT.read_text(encoding="utf-8"), re.DOTALL
    )
    assert block is not None, "the Remote Script no longer declares _BROWSER_ROOTS"
    return tuple(re.findall(r'"([a-z_]+)"', block.group(1)))


def test_no_tool_offers_a_browser_root_the_script_cannot_walk() -> None:
    """A root in a published enum is a promise, and the walk is what has to keep it.

    Measured 2026-09-17 against Live 12.4.6, ``root="user_folders"`` answers
    ``no_such_path``, "unknown
    browser root 'user_folders'", while the catalog carries verified rows for
    ``browser.user_folders`` and ``browser.legacy_libraries``. The two lists genuinely
    differ, so the schema has to follow the walk rather than the catalog.
    """
    walkable = set(_script_browser_roots())
    offenders: list[str] = []
    for tool in _tools():
        if tool.name not in _BROWSER_ROOT_TOOLS:
            continue
        schema = tool.inputSchema.get("properties", {}).get("root", {})
        offered = {value for value in schema.get("enum", []) if value}
        assert offered, f"{tool.name} no longer publishes a root enum"
        for root in sorted(offered - walkable):
            offenders.append(f"{tool.name} offers root {root!r}, which the walk refuses")
    assert not offenders, (
        "browser roots promised to clients that the Remote Script does not walk:\n  "
        + "\n  ".join(offenders)
    )


def test_the_finder_and_the_loader_agree_on_which_roots_exist() -> None:
    """A candidate found under one tool's root has to be loadable through the other's.

    ``find_sound`` puts the root it searched into every candidate's ``load_with`` block,
    so a root one tool offers and the other refuses breaks the chain from finding to
    loading at the point the caller copies the block across.
    """
    enums = {}
    for tool in _tools():
        if tool.name in _BROWSER_ROOT_TOOLS:
            enums[tool.name] = {
                value
                for value in tool.inputSchema["properties"]["root"].get("enum", [])
                if value
            }
    assert set(enums) == set(_BROWSER_ROOT_TOOLS), enums
    assert enums["find_sound"] <= enums["load_device"], (
        "find_sound can search roots load_device cannot be pointed at: "
        f"{sorted(enums['find_sound'] - enums['load_device'])}"
    )
