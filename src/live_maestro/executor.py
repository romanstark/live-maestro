"""Central execution engine for catalog-driven operations.

Coordinates path resolution, validation, safety guards, dispatch, and verification
for operations defined in the LOM catalog.

Design principles:
- Single execution pipeline: Validates arguments, checks destructive confirmations,
  and verifies access permissions before dispatching commands over the transport.
- Read-back verification: Evaluates returned values against requested parameters to
  confirm whether values were applied as requested, clamped, or deferred.
- Structured results: Maps all transport outcomes into a consistent Result dataclass
  containing status, before/after values, and verification flags.
"""

from __future__ import annotations

import difflib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from live_maestro.models import Access
from live_maestro.spec import (
    ArgSpec,
    PathSpec,
    build_path,
    validate_path,
    validate_value,
)

if TYPE_CHECKING:
    from live_maestro.registry import Registry

# Reserved argument names for operation payloads (distinguished from path placeholders).
ARG_VALUE = "value"
ARG_CALL_ARGS = "call_args"
_RESERVED = frozenset({ARG_VALUE, ARG_CALL_ARGS})

# Client-side refusal codes for operations blocked prior to dispatch.
CODE_CONFIRM_REQUIRED = "confirm_required"
CODE_ACCESS_NOT_ALLOWED = "access_not_allowed"
CODE_BAD_REQUEST = "bad_request"
CODE_NO_RESULT = "no_result"

# Default verifier for 'set' operations (evaluates the read-back payload from lom_set).
DEFAULT_SET_VERIFY = "read_back"

# Catalog identifier indicating that no post-execution verification is available for a path.
VERIFY_NONE = "none"

# Float comparison tolerances for read-back verification against JSON-serialized numbers.
_REL_TOL = 1e-6
_ABS_TOL = 1e-9

# Start offset in beats for automation sampling (sampled > 0 to bypass Live's default t=0 value).
_VERIFY_START_BEATS = 0.05


@runtime_checkable
class LomClient(Protocol):
    """Transport protocol interface required by the execution engine."""

    def get(self, path: str) -> Mapping[str, Any]:
        """Read the property at the given LOM path (lom_get)."""
        ...

    def set(self, path: str, value: object) -> Mapping[str, Any]:
        """Write a value to the given LOM path and read it back (lom_set)."""
        ...

    def call(self, path: str, method: str, args: Sequence[object]) -> Mapping[str, Any]:
        """Invoke an allowlisted method on the object at the given LOM path (lom_call)."""
        ...

    def batch(
        self, ops: Sequence[Mapping[str, Any]], *, atomic: bool = False
    ) -> Mapping[str, Any]:
        """Execute multiple operations in a single round trip (lom_batch)."""
        ...


class UnknownSpecError(KeyError):
    """Raised when a requested catalog spec ID does not exist."""

    def __init__(self, message: str) -> None:
        super().__init__(message)

    def __str__(self) -> str:
        return str(self.args[0])


@dataclass
class Result:
    """Structured outcome of an operation executed through the catalog engine.

    Attributes:
        ok: True if the operation succeeded on the transport and Live side.
        id: Catalog entry identifier.
        path: Concrete LOM path that was addressed.
        code: Structured error or refusal code if the operation failed.
        message: Human-readable description of the error or status.
        value: The requested or read value.
        before: Previous value before a write operation.
        after: Resulting value read back from Live after a write.
        clamped: True if Live coerced or clamped the value during a write.
        changed: True if the value differed from its previous state.
        display: Human-readable display string formatted by Live if available.
        read_back: Raw read-back verdict from the script ('applied', 'clamped', 'not_observed').
        verified: True if post-condition verifier passed, False if failed, None if unverified.
        blocked: True if the operation was rejected locally before dispatch.
    """

    ok: bool
    id: str
    path: str
    code: str | None = None
    message: str | None = None
    value: object = None
    before: object = None
    after: object = None
    clamped: bool | None = None
    changed: bool | None = None
    display: str | None = None
    read_back: str | None = None
    verified: bool | None = None
    blocked: bool = False

    @classmethod
    def from_response(cls, resp: Mapping[str, Any], spec_id: str, path: str) -> Result:
        """Construct a Result instance from a wire protocol response dictionary."""
        ok, payload, code, message = _split_response(resp)
        echoed = payload.get("path") or resp.get("path")
        return cls(
            ok=ok,
            id=spec_id,
            path=str(echoed) if echoed else path,
            code=code,
            message=message,
            value=_first(payload, "value", "requested", "result"),
            before=payload.get("before"),
            after=payload.get("after"),
            clamped=_opt_bool(payload.get("clamped")),
            changed=_opt_bool(payload.get("changed")),
            display=_opt_str(payload.get("display")),
            read_back=_opt_str(payload.get("read_back")),
        )

    @classmethod
    def blocked_(cls, spec: PathSpec, reason: str, code: str) -> Result:
        """Construct a Result representing a client-side blocked operation."""
        return cls(ok=False, id=spec.id, path=spec.path, code=code, message=reason, blocked=True)


@dataclass
class _Prepared:
    """Pre-computed operation parameters ready for transport dispatch."""

    spec: PathSpec
    op: Access
    path: str
    value: object = None
    call_args: list[object] = field(default_factory=list)


def execute(
    client: LomClient,
    registry: Registry,
    spec_id: str,
    *,
    confirm: bool = False,
    verify: bool = True,
    **args: object,
) -> Result:
    """Execute a single catalog operation and return a structured Result.

    Args:
        client: Transport client implementing the LomClient protocol.
        registry: Spec registry containing catalog definitions.
        spec_id: Unique catalog entry ID (e.g. 'song.tempo', 'track.volume').
        confirm: Must be True to execute operations marked as destructive.
        verify: If True, executes the post-condition verifier defined in the spec.
        **args: Path parameters and payload arguments ('value' or 'call_args').

    Returns:
        A Result instance detailing execution status, returned values, and verification.

    Raises:
        UnknownSpecError: If spec_id is not found in the registry.
        ValueError: If arguments fail type or range validation.
    """
    spec = _lookup(registry, spec_id)
    op = _operation(spec, args)

    if spec.destructive and not confirm:
        return Result.blocked_(
            spec,
            f"{spec.id} is marked destructive. Pass confirm=True to run it.",
            CODE_CONFIRM_REQUIRED,
        )
    if not spec.supports(op):
        granted = ", ".join(a.value for a in spec.access) or "nothing"
        return Result.blocked_(
            spec,
            f"{spec.id} does not allow {op.value}; the catalog grants: {granted}",
            CODE_ACCESS_NOT_ALLOWED,
        )

    prepared = _prepare(spec, op, args)
    baseline = _take_baseline(client, prepared) if verify else None
    result = _dispatch(client, prepared)
    if verify:
        _apply_baseline(result, baseline)
        _run_verifier(client, prepared, args, result)
    return result


def execute_batch(
    client: LomClient,
    registry: Registry,
    ops: Sequence[Mapping[str, Any]],
    *,
    atomic: bool = False,
    verify: bool = False,
) -> list[Result]:
    """Execute multiple catalog operations in a single round-trip exchange.

    Args:
        client: Transport client implementing the LomClient protocol.
        registry: Spec registry containing catalog definitions.
        ops: Sequence of operation dictionaries, each containing 'id' and parameters.
        atomic: If True, execution halts upon the first error (note: LOM has no rollback).
        verify: If True, takes a baseline reading for every change-checking hook and runs
            the post-condition verifiers. Both cost a round trip per operation, which is
            what batching exists to avoid, so this defaults to False here.

    Returns:
        List of Result instances corresponding to the input operations.
    """
    results: list[Result | None] = [None] * len(ops)
    prepared_by_index: dict[int, _Prepared] = {}
    wire_ops: list[Mapping[str, Any]] = []
    wire_index: list[int] = []

    for i, op_spec in enumerate(ops):
        outcome = _prepare_batch_op(registry, op_spec)
        if isinstance(outcome, Result):
            results[i] = outcome
            continue
        prepared_by_index[i] = outcome
        wire_ops.append(_wire_op(outcome))
        wire_index.append(i)

    baselines: dict[int, object] = {}
    if verify:
        for index, prepared in prepared_by_index.items():
            taken = _take_baseline(client, prepared)
            if taken is not None:
                baselines[index] = taken

    if wire_ops:
        replies = _batch_replies(client, wire_ops, atomic=atomic)
        for slot, index in enumerate(wire_index):
            prepared = prepared_by_index[index]
            if slot < len(replies):
                results[index] = _result_for(prepared, replies[slot])
            else:
                results[index] = Result(
                    ok=False,
                    id=prepared.spec.id,
                    path=prepared.path,
                    code=CODE_NO_RESULT,
                    message=(
                        f"lom_batch returned {len(replies)} result(s) for {len(wire_ops)} op(s). "
                        "This op has no reply and its outcome is unknown."
                    ),
                )

    if verify:
        for index, prepared in prepared_by_index.items():
            result = results[index]
            if result is not None:
                _apply_baseline(result, baselines.get(index))
                _run_verifier(client, prepared, dict(ops[index]), result)

    return [r for r in results if r is not None]


# ------------------------------------------------------------------- internals


def _lookup(registry: Registry, spec_id: str) -> PathSpec:
    """Retrieve PathSpec for spec_id, or raise UnknownSpecError with nearest matches."""
    try:
        return registry.get(spec_id)
    except KeyError:
        known = [s.id for s in registry.all()]
        near = difflib.get_close_matches(spec_id, known, n=5, cutoff=0.5)
        hint = f"Did you mean: {', '.join(near)}?" if near else "No similar id is in the catalog."
        raise UnknownSpecError(f"unknown catalog id {spec_id!r}. {hint}") from None


def _operation(spec: PathSpec, args: Mapping[str, object]) -> Access:
    """Infer Access operation (GET, SET, or CALL) from spec and arguments."""
    if spec.method:
        if ARG_VALUE in args:
            raise ValueError(
                f"{spec.id}: row declares method {spec.method!r}, so it is a call. "
                f"Pass {ARG_CALL_ARGS}=[...] instead of {ARG_VALUE}="
            )
        return Access.CALL
    if ARG_CALL_ARGS in args:
        raise ValueError(
            f"{spec.id}: row declares no method, so {ARG_CALL_ARGS} means nothing here"
        )
    return Access.SET if ARG_VALUE in args else Access.GET


def _prepare(spec: PathSpec, op: Access, args: Mapping[str, object]) -> _Prepared:
    """Validate parameters and build concrete LOM path prior to dispatch."""
    path_args = {k: v for k, v in args.items() if k not in _RESERVED}
    path = build_path(spec, **path_args)
    value = validate_value(spec, args[ARG_VALUE]) if op is Access.SET else None
    call_args = (
        _validate_call_args(spec, _as_args(args.get(ARG_CALL_ARGS)))
        if op is Access.CALL
        else []
    )
    return _Prepared(spec=spec, op=op, path=path, value=value, call_args=call_args)


def _validate_call_args(spec: PathSpec, given: list[object]) -> list[object]:
    """Validate positional method arguments against declared ArgSpec definitions."""
    if not spec.args:
        return given

    required = sum(1 for a in spec.args if a.required)
    if len(given) > len(spec.args) or len(given) < required:
        shape = ", ".join(a.name if a.required else f"[{a.name}]" for a in spec.args)
        raise ValueError(
            f"{spec.id}: {spec.method}() takes {shape or 'no arguments'} "
            f"({required} required, {len(spec.args)} total), got {len(given)}."
        )

    out: list[object] = []
    for index, arg in enumerate(spec.args):
        if index < len(given):
            raw = given[index]
        elif arg.default is not None:
            raw = arg.default
        else:
            # A trailing optional argument the caller left out, for which the row
            # declares no default. Leaving it OFF the list is not the same as
            # sending None: the LOM is a C++ binding and each parameter has its own
            # default, which only applies to an argument that was not passed.
            # Measured 2026-09-05 against Live 12.4.5, calling
            # clip.duplicate_notes_by_id with note_ids alone: padding the two
            # optional arguments with None was refused with "Python argument types
            # in Clip.duplicate_notes_by_id(Clip, list, NoneType, NoneType) did not
            # match C++ signature: ... destination_time=None, int
            # transposition_amount=0". destination_time takes None and
            # transposition_amount does not, so a blanket None cannot be right for
            # both. Stopping here hands Live the shorter call it accepts.
            break
        if raw is None and arg.required:
            raise ValueError(f"{spec.id}: argument {arg.name!r} is required")
        out.append(_coerce_arg(spec, arg, raw))
    return out


def _coerce_arg(spec: PathSpec, arg: ArgSpec, raw: object) -> object:
    """Validate a single method argument for type, enum values, and numeric ranges."""
    if arg.lom_object:
        if isinstance(raw, Mapping) and "__path__" in raw:
            target = raw["__path__"]
        else:
            target = raw
        if not isinstance(target, str):
            raise ValueError(
                f"{spec.id}: argument {arg.name!r} is a Live object, so it travels as a "
                f"path string. Got {type(target).__name__} instead. See docs/protocol.md §5.5."
            )
        validate_path(target)
        return {"__path__": target}

    if arg.enum is not None and raw not in arg.enum:
        raise ValueError(
            f"{spec.id}: argument {arg.name!r} = {raw!r} is not one of {sorted(map(str, arg.enum))}"
        )
    if arg.range is not None and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        low, high = arg.range
        if (low is not None and raw < low) or (high is not None and raw > high):
            raise ValueError(
                f"{spec.id}: argument {arg.name!r} = {raw!r} is outside {arg.range}"
            )
    return raw


def _as_args(raw: object) -> list[object]:
    """Normalize positional call arguments to a list."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


def _dispatch(client: LomClient, prepared: _Prepared) -> Result:
    """Dispatch a prepared operation over the client transport and return a Result."""
    spec = prepared.spec
    try:
        if prepared.op is Access.GET:
            resp = client.get(prepared.path)
        elif prepared.op is Access.SET:
            resp = client.set(prepared.path, prepared.value)
        else:
            resp = client.call(prepared.path, str(spec.method), prepared.call_args)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code is None:
            raise
        return Result(
            ok=False,
            id=spec.id,
            path=prepared.path,
            code=str(code),
            message=str(getattr(exc, "message", None) or exc),
        )
    return _result_for(prepared, resp)


def _result_for(prepared: _Prepared, resp: Mapping[str, Any]) -> Result:
    """Construct a Result from transport reply, backfilling requested value."""
    result = Result.from_response(resp, prepared.spec.id, prepared.path)
    if prepared.op is Access.SET and result.value is None:
        result.value = prepared.value
    return result


def _prepare_batch_op(registry: Registry, op_spec: Mapping[str, Any]) -> _Prepared | Result:
    """Validate and prepare a single batch operation."""
    spec_id = op_spec.get("id")
    if not isinstance(spec_id, str) or not spec_id:
        raise ValueError(f"batch op is missing a string 'id': {op_spec!r}")
    spec = _lookup(registry, spec_id)
    confirm = bool(op_spec.get("confirm", False))
    args = {k: v for k, v in op_spec.items() if k not in ("id", "confirm")}

    try:
        op = _operation(spec, args)
    except ValueError as exc:
        return Result.blocked_(spec, str(exc), CODE_BAD_REQUEST)

    if spec.destructive and not confirm:
        return Result.blocked_(
            spec,
            f"{spec.id} is marked destructive. Pass confirm=True on the op to run it.",
            CODE_CONFIRM_REQUIRED,
        )
    if not spec.supports(op):
        granted = ", ".join(a.value for a in spec.access) or "nothing"
        return Result.blocked_(
            spec,
            f"{spec.id} does not allow {op.value}; the catalog grants: {granted}",
            CODE_ACCESS_NOT_ALLOWED,
        )
    try:
        return _prepare(spec, op, args)
    except (ValueError, TypeError, KeyError) as exc:
        return Result.blocked_(spec, str(exc), CODE_BAD_REQUEST)


def _wire_op(prepared: _Prepared) -> dict[str, Any]:
    """Format a prepared operation for a lom_batch request.

    See docs/protocol.md §5.7 for the wire shape.
    """
    if prepared.op is Access.GET:
        return {"op": "get", "path": prepared.path}
    if prepared.op is Access.SET:
        return {"op": "set", "path": prepared.path, "value": prepared.value}
    return {
        "op": "call",
        "path": prepared.path,
        "method": str(prepared.spec.method),
        "args": prepared.call_args,
    }


def _batch_replies(
    client: LomClient, wire_ops: Sequence[Mapping[str, Any]], *, atomic: bool
) -> list[Mapping[str, Any]]:
    """Dispatch a batch request and return individual operation response dictionaries."""
    resp = client.batch(wire_ops, atomic=atomic)
    ok, payload, code, message = _split_response(resp)
    if not ok:
        failure = {"code": code, "message": message}
        return [failure for _ in wire_ops]
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    return [
        r if isinstance(r, Mapping) else {"code": "internal", "message": repr(r)}
        for r in results
    ]


def _split_response(
    resp: Mapping[str, Any],
) -> tuple[bool, Mapping[str, Any], str | None, str | None]:
    """Parse a wire response into (ok, payload, code, message).

    Handles standard response envelopes (status: success/error), un-nested batch item payloads,
    and direct error dictionaries.
    """
    status = resp.get("status")
    if status == "error":
        return False, {}, _opt_str(resp.get("code")), _opt_str(resp.get("message"))
    if status == "success":
        result = resp.get("result")
        # Direct result envelope check: lom_batch stamps status directly onto item payloads
        if isinstance(result, Mapping) and set(resp) <= {"status", "result"}:
            return True, result, None, None
        return True, resp, None, None
    if status is None:
        if "code" in resp:
            return False, {}, _opt_str(resp.get("code")), _opt_str(resp.get("message"))
        return True, resp, None, None
    return False, {}, "internal", f"unexpected status {status!r} in reply"


def _first(payload: Mapping[str, Any], *keys: str) -> object:
    """Return the first existing key from payload."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _opt_str(raw: object) -> str | None:
    return None if raw is None else str(raw)


def _opt_bool(raw: object) -> bool | None:
    return None if raw is None else bool(raw)


def _same_value(a: object, b: object) -> bool:
    """Compare requested and read-back values with float tolerance."""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) is bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    return a == b


def _run_verifier(
    client: LomClient, prepared: _Prepared, args: Mapping[str, Any], result: Result
) -> None:
    """Execute post-condition verification hook for a completed operation."""
    if not result.ok:
        return
    name = prepared.spec.verify or (DEFAULT_SET_VERIFY if prepared.op is Access.SET else None)
    if not name or name == VERIFY_NONE:
        return
    verifier = VERIFIERS.get(name)
    if verifier is None:
        result.verified = None
        result.message = result.message or f"unknown verifier {name!r}; not verified"
        return
    result.verified = verifier(client, prepared.spec, args, result)


# -------------------------------------------------------- baselines for calls
#
# The verifiers above this line all check an absolute post-condition: a slot holds
# a clip, a clip has an envelope. A method call is often not like that. Whether
# ``duplicate_loop`` worked is not a property of the clip afterwards, it is the
# difference between afterwards and before, and nothing in a ``lom_call`` reply
# carries the before. ``lom_set`` does not have this problem because the script
# reads the property back inside Live, in the same round trip (protocol §5.4).
#
# So for the hooks that check a change, the executor takes the reading itself,
# before dispatching, and puts it in ``Result.before``. That costs one extra round
# trip (about 450 ms, docs/limits.md §4) on a deliberate, catalogued method call,
# and it makes a call row report ``before``/``after`` the way a set row does.
# ``verify=False`` skips it, and so does every hook not named here.

#: How a value is expected to move. ``_NOT_UP`` exists because a removal that finds
#: nothing to remove is a legitimate no-op, not a failure: measured 2026-09-05
#: against Live 12.4.5, ``remove_notes_extended`` over an already empty beat range
#: answered success and left the count at 6.
_SAME = "same"
_UP = "up"
_DOWN = "down"
_NOT_UP = "not_up"


def _read_number(client: LomClient, path: str) -> float | None:
    """Read a numeric property, or None if it cannot be read."""
    getter = getattr(client, "get", None)
    if not callable(getter):
        return None
    try:
        resp = getter(path)
    except Exception:  # noqa: BLE001  (an unreadable baseline is 'unknown', not fatal)
        return None
    if not isinstance(resp, Mapping):
        return None
    ok, payload, _code, _message = _split_response(resp)
    value = payload.get("value")
    if not ok or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _read_count(client: LomClient, path: str) -> int | None:
    """Read the element count of a LOM collection, or None if it cannot be read.

    Used for ``warp_markers``, which answers ``count`` alongside its list
    (protocol §5.3). Measured 2026-09-05 against Live 12.4.5: on a MIDI clip the
    read fails with ``live_error`` ("Warp markers are only available for Audio
    Clips"), which arrives here as None and leaves the operation unverified rather
    than wrongly failed.
    """
    getter = getattr(client, "get", None)
    if not callable(getter):
        return None
    try:
        resp = getter(path)
    except Exception:  # noqa: BLE001  (see _read_number)
        return None
    if not isinstance(resp, Mapping):
        return None
    ok, payload, _code, _message = _split_response(resp)
    count = payload.get("count")
    if not ok or isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _read_note_count(client: LomClient, path: str) -> int | None:
    """Count the MIDI notes in a clip via the notes_get handler (protocol §5.8).

    Duck-typed the same way ``_verify_envelope_moves`` reaches ``automation_read``:
    a client that does not offer the handler leaves the operation unverified.
    Measured 2026-09-05 against Live 12.4.5: ``count_only`` answers
    ``{'path', 'count', 'api'}`` and costs no note list.
    """
    reader = getattr(client, "notes_get", None)
    if not callable(reader):
        return None
    try:
        resp = reader(path, count_only=True)
    except Exception:  # noqa: BLE001  (an audio clip has no notes, so this is 'unknown')
        return None
    if not isinstance(resp, Mapping):
        return None
    ok, payload, _code, _message = _split_response(resp)
    count = payload.get("count")
    if not ok or isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _baseline_length(client: LomClient, path: str) -> object:
    return _read_number(client, f"{path}.length")


def _baseline_note_count(client: LomClient, path: str) -> object:
    return _read_note_count(client, path)


def _baseline_warp_markers(client: LomClient, path: str) -> object:
    return _read_count(client, f"{path}.warp_markers")


#: Hooks whose post-condition is a change, and the reading each needs beforehand.
#: A hook absent here checks an absolute state and needs no baseline.
BASELINES: dict[str, Callable[[LomClient, str], object]] = {
    "clip_length": _baseline_length,
    "notes_count": _baseline_note_count,
    "warp_state": _baseline_warp_markers,
}


def _take_baseline(client: LomClient, prepared: _Prepared) -> object:
    """Read the before-value a change-checking hook needs, or None."""
    if prepared.op is not Access.CALL:
        return None
    probe = BASELINES.get(prepared.spec.verify or "")
    if probe is None:
        return None
    return probe(client, prepared.path)


def _apply_baseline(result: Result, baseline: object) -> None:
    """Record the pre-operation reading on a successful result."""
    if baseline is not None and result.ok and result.before is None:
        result.before = baseline


def _moved(before: object, after: object, direction: str) -> bool | None:
    """Compare a before and after reading against an expected direction."""
    if before is None or after is None:
        return None
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return None
    if direction == _SAME:
        return _same_value(before, after)
    if direction == _UP:
        return float(after) > float(before)
    if direction == _DOWN:
        return float(after) < float(before)
    if direction == _NOT_UP:
        return float(after) <= float(before)
    return None


# -------------------------------------------------------------------- verifiers


def _verify_read_back(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify stored value matches requested value using read-back payload.

    Returns None if read_back is 'not_observed' (indicating asynchronous or deferred update).
    """
    if result.read_back == "not_observed":
        return None
    if result.after is None or result.value is None:
        return None
    return _same_value(result.after, result.value)


def _verify_envelope_moves(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that an automation envelope exists and is non-flat (min != max).

    Sampled strictly at t > 0 to avoid reading Live's default parameter value at t=0.
    """
    reader = getattr(client, "automation_read", None)
    if not callable(reader):
        return None
    parameter = args.get("parameter")
    if parameter is None:
        return None

    start = max(_as_float(args.get("start"), 0.0), _VERIFY_START_BEATS)
    end = _as_float(args.get("end"), 0.0)
    try:
        resp = reader(result.path, parameter, start=start, end=end if end > start else None)
    except (TypeError, ValueError):
        return None
    if not isinstance(resp, Mapping):
        return None

    ok, payload, _code, _message = _split_response(resp)
    if not ok:
        return None
    if payload.get("has_envelope") is False:
        return False
    moves = payload.get("moves")
    if isinstance(moves, bool):
        return moves
    low, high = payload.get("min"), payload.get("max")
    if low is None or high is None:
        return None
    return not _same_value(low, high)


def _as_float(raw: object, default: float) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _read_bool(client: LomClient, path: str) -> bool | None:
    """Query a boolean property value over the transport client."""
    getter = getattr(client, "get", None)
    if not callable(getter):
        return None
    try:
        resp = getter(path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(resp, Mapping):
        return None
    ok, payload, _code, _message = _split_response(resp)
    value = payload.get("value")
    return value if ok and isinstance(value, bool) else None


def _slot_of(result: Result) -> str:
    """Derive the clip slot path from an addressed clip path."""
    return result.path.removesuffix(".clip")


def _verify_slot_holds_a_clip(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that a clip slot currently contains a clip."""
    return _read_bool(client, f"{_slot_of(result)}.has_clip")


def _verify_slot_is_empty(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that a clip slot is currently empty."""
    held = _read_bool(client, f"{_slot_of(result)}.has_clip")
    return None if held is None else not held


def _verify_envelope_present(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that a clip contains at least one automation envelope."""
    return _read_bool(client, f"{result.path}.has_envelopes")


def _verify_envelope_absent(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that all envelopes on the clip have been cleared."""
    if spec.method != "clear_all_envelopes":
        return None
    held = _read_bool(client, f"{result.path}.has_envelopes")
    return None if held is None else not held


#: What each allowlisted method does to the note count of the clip it addresses.
#: Every entry measured 2026-09-05 against Live 12.4.5, on a scratch MIDI track
#: created at the end of the set and deleted afterwards: ``quantize`` moved six
#: notes and left six, ``duplicate_notes_by_id`` took six to eight,
#: ``remove_notes_by_id`` took eight back to six, and ``remove_notes_extended``
#: over an empty range left six, which is why removals expect ``_NOT_UP`` and
#: not ``_DOWN``.
#:
#: ``replace_selected_notes`` and ``set_notes`` are deliberately absent. Both are
#: legacy-API rows the script refuses outright (they open a modal dialog inside
#: Live), and neither has a note-count post-condition that holds in general.
_NOTE_COUNT_MOVE: dict[str, str] = {
    "quantize": _SAME,
    "quantize_pitch": _SAME,
    "apply_note_modifications": _SAME,
    "duplicate_region": _UP,
    "duplicate_notes_by_id": _UP,
    "add_new_notes": _UP,
    "remove_notes_extended": _NOT_UP,
    "remove_notes_by_id": _NOT_UP,
}

#: Measured 2026-09-05 against Live 12.4.5: ``duplicate_loop`` took a 4-beat clip
#: to 8 beats. ``crop`` expects ``_NOT_UP`` rather than ``_DOWN`` because
#: ``clip.length`` on a looping clip already reports the loop length, not the
#: material behind it: cropping a clip whose loop was 0..4 of 8 beats left length
#: at 4.0 either side and discarded the notes instead (8 notes to 4). Crop cannot
#: lengthen a clip, so "did not grow" is the post-condition that always holds. The
#: discarded material shows up in the note count, which is a different hook.
_CLIP_LENGTH_MOVE: dict[str, str] = {
    "duplicate_loop": _UP,
    "crop": _NOT_UP,
}

#: A move must not change how many warp markers there are. An add and a remove
#: must. All three measured 2026-09-05 against Live 12.4.5, on a scratch audio
#: track holding a copy of a warped 8-beat sample (markers at beats 0.0, 8.0 and
#: 8.03125):
#:
#: * ``remove_warp_marker(0.0)`` took the count from 3 to 2, and the hook verified
#:   it. ``remove_warp_marker`` on the last real marker answered success and removed
#:   nothing, count 2 to 2, and the hook reported that as verified: false. A status
#:   of success proves the script did not raise, never that Live acted
#:   (protocol §10).
#: * ``move_warp_marker`` left the count at 3 and verified.
#: * ``add_warp_marker`` is unreachable through lom_call, as its row says: Live
#:   answers "No registered converter was able to produce a C++ rvalue" because the
#:   method takes a WarpMarker object that JSON cannot carry. The failed call is
#:   never verified, so the ``_UP`` entry stands unexercised rather than wrong.
#:
#: A True verdict claims the declared post-condition holds, not the caller's intent.
#: For ``move_warp_marker`` it rules out an accidental add or remove and cannot prove
#: the marker moved: ``clip.warp_markers`` answers opaque WarpMarker handles carrying
#: no times (measured the same day), so the count is the only evidence in the list
#: itself. Read ``clip.warp_marker_beat_time`` per index to see where they sit.
_WARP_MARKER_MOVE: dict[str, str] = {
    "add_warp_marker": _UP,
    "remove_warp_marker": _DOWN,
    "move_warp_marker": _SAME,
}


def _verify_notes_count(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that a note-editing call moved the clip's note count as it should."""
    direction = _NOTE_COUNT_MOVE.get(spec.method or "")
    if direction is None:
        return None
    after = _read_note_count(client, result.path)
    if after is not None:
        result.after = after
    return _moved(result.before, after, direction)


def _verify_clip_length(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that a call moved the clip's length as it should."""
    direction = _CLIP_LENGTH_MOVE.get(spec.method or "")
    if direction is None:
        return None
    after = _read_number(client, f"{result.path}.length")
    if after is not None:
        result.after = after
    return _moved(result.before, after, direction)


def _verify_warp_state(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify a warp-marker edit, or that create_audio_clip filled its slot.

    ``create_audio_clip`` is addressed at the ClipSlot and had no clip to measure
    beforehand, so the post-condition it can carry is that the slot holds one now.
    Its warping is what the row's doc covers, and Live decides it asynchronously, so
    it is not a verdict this can reach one round trip later.
    """
    if spec.method == "create_audio_clip":
        return _read_bool(client, f"{result.path}.has_clip")
    direction = _WARP_MARKER_MOVE.get(spec.method or "")
    if direction is None:
        return None
    after = _read_count(client, f"{result.path}.warp_markers")
    if after is not None:
        result.after = after
    return _moved(result.before, after, direction)


def _verify_playing_state(
    client: LomClient, spec: PathSpec, args: Mapping[str, Any], result: Result
) -> bool | None:
    """Verify that a launch or stop call left the clip in the state it asked for.

    A stop that is still playing is not reported as a failure. Measured 2026-09-05
    against Live 12.4.5 with ``song.clip_trigger_quantization`` at one bar: with the
    transport already running, ``clip.stop`` left ``is_playing`` true immediately
    after the call and false 0.5 s later, because Live defers the stop to the
    quantization boundary. Returning False there would be a false alarm on a call
    that worked, so a stop still playing answers None, the same verdict
    ``read_back`` gives an asynchronous property (protocol §5.4).

    A fire accepts ``is_triggered`` as well as ``is_playing`` for the mirror-image
    reason: with the transport running, a launched clip waits at the boundary and
    is triggered before it is playing.
    """
    method = spec.method or ""
    if method == "fire":
        wants_playing = True
    elif method == "stop":
        wants_playing = False
    elif method == "set_fire_button_state":
        raw = args.get(ARG_CALL_ARGS)
        state = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
        if not state:
            # Releasing the button only stops the clip in the launch modes that
            # need it held (Gate, Repeat). In Trigger mode the release is a no-op,
            # so there is no post-condition to check without reading launch_mode.
            return None
        wants_playing = True
    else:
        return None

    playing = _read_bool(client, f"{result.path}.is_playing")
    if playing is None:
        return None
    triggered = _read_bool(client, f"{result.path}.is_triggered")
    if wants_playing:
        return bool(playing or triggered)
    return None if playing else True


#: Post-condition verification handlers mapped by catalog 'verify' identifier.
VERIFIERS: dict[str, Callable[[LomClient, PathSpec, Mapping[str, Any], Result], bool | None]] = {
    "read_back": _verify_read_back,
    "envelope_moves": _verify_envelope_moves,
    "has_clip": _verify_slot_holds_a_clip,
    "no_clip": _verify_slot_is_empty,
    "envelope_present": _verify_envelope_present,
    "envelope_absent": _verify_envelope_absent,
    "notes_count": _verify_notes_count,
    "clip_length": _verify_clip_length,
    "warp_state": _verify_warp_state,
    "playing_state": _verify_playing_state,
}
