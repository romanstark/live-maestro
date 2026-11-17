"""Wavetable's modulation matrix read and written whole, with a read-back of every cell.

Rows are targets, the names in visible_modulation_target_names; columns are sources.
The Object Model reaches single cells only, so one batch reads every cell and one
batch writes the routes, after which the touched cells are read again. A parameter
that is not yet a row is added with add_parameter_to_modulation_matrix first. The
source count is found by probing columns until every target refuses one. Source
names come from Live's own enum when it has one; otherwise columns stay numbered.
None of this has been measured against Live yet: the read-back is the evidence.
"""

from __future__ import annotations

from typing import Any

#: Columns probed when counting sources; well above any matrix Live has shown.
MAX_SOURCES = 16
#: Enum Live may expose for the source columns; absent, columns stay numbered.
SOURCE_ENUM = "WavetableDevice.ModulationSource"
#: Cells closer than this to the requested amount count as applied.
TOLERANCE = 1e-3


def _call(head: str, method: str, args: list[Any]) -> dict[str, Any]:
    return {"op": "call", "path": head, "method": method, "args": args}


def _value(result: dict[str, Any]) -> Any:
    return result.get("result", result.get("value"))


def source_names(client: Any) -> list[str] | None:
    """Return the source column names Live reports, or None when it reports none."""
    try:
        found = client.send("enum_names", {"type": SOURCE_ENUM})
    except Exception:  # noqa: BLE001 -- an absent enum is an answer, not a failure
        return None
    members = found.get("members") if isinstance(found, dict) else None
    if not isinstance(members, dict) or not members:
        return None
    return [name for name, _ in sorted(members.items(), key=lambda item: item[1])]


def read(client: Any, head: str) -> dict[str, Any]:
    """Return targets, the source count and every cell of the matrix.

    Raises:
        ValueError: when the device answers no matrix call (not a Wavetable).
    """
    names = _value(client.get(f"{head}.visible_modulation_target_names"))
    targets = [str(name) for name in names or []]
    if not targets:
        raise ValueError("the device lists no modulation targets; is it a Wavetable?")
    ops = [_call(head, "get_modulation_value", [row, column])
           for row in range(len(targets)) for column in range(MAX_SOURCES)]
    results = client.batch(ops)["results"]
    cells: dict[tuple[int, int], float] = {}
    for op, result in zip(ops, results, strict=True):
        if result.get("status") == "success":
            cells[(op["args"][0], op["args"][1])] = float(_value(result))
    if not cells:
        error = next((r for r in results if r.get("status") != "success"), {})
        raise ValueError(f"no matrix cell could be read: {error.get('error') or error}")
    columns = 1 + max(column for _, column in cells)
    labels = source_names(client)
    if labels is None or len(labels) != columns:
        labels = [f"source {index}" for index in range(columns)]
        basis = "numbered; Live reported no source names for these columns"
    else:
        basis = f"names from Live's {SOURCE_ENUM}"
    rows = [{"target_index": row, "target": name,
             "amounts": {labels[column]: cells[(row, column)] for column in range(columns)
                         if (row, column) in cells}}
            for row, name in enumerate(targets)]
    return {"targets": targets, "sources": labels, "source_basis": basis,
            "source_count": columns, "rows": rows}


def write(client: Any, head: str, routes: list[dict[str, Any]], replace: bool,
          parameter_paths: dict[str, str]) -> dict[str, Any]:
    """Add missing targets, set the routes, optionally clear others, and read back.

    Args:
        routes: dicts with target (row name), source (column name or index) and amount.
        parameter_paths: target name -> DeviceParameter path for rows not yet present.
    """
    added = []
    for name, path in parameter_paths.items():
        reference = {"__path__": path}
        check = client.call(head, "is_parameter_modulatable", [reference])
        if not _value(check):
            raise ValueError(f"{name} is not modulatable on this device")
        client.call(head, "add_parameter_to_modulation_matrix", [reference])
        added.append(name)
    before = read(client, head)
    plan: dict[tuple[int, int], float] = {}
    for route in routes:
        if route["target"] not in before["targets"]:
            raise ValueError(f"no matrix row {route['target']!r}; give its parameter to add it")
        row = before["targets"].index(route["target"])
        source = route["source"]
        column = (int(source) if str(source).isdigit()
                  else before["sources"].index(source) if source in before["sources"] else -1)
        if not 0 <= column < before["source_count"]:
            raise ValueError(f"no source column {source!r}; columns are {before['sources']}")
        plan[(row, column)] = float(route["amount"])
    if replace:
        for row in before["rows"]:
            for label, amount in row["amounts"].items():
                cell = (row["target_index"], before["sources"].index(label))
                if amount and cell not in plan:
                    plan[cell] = 0.0
    client.batch([_call(head, "set_modulation_value", [row, column, amount])
                  for (row, column), amount in plan.items()])
    checks = client.batch([_call(head, "get_modulation_value", [row, column])
                           for row, column in plan])["results"]
    cells = []
    for ((row, column), wanted), result in zip(plan.items(), checks, strict=True):
        got = float(_value(result)) if result.get("status") == "success" else None
        cells.append({"target": before["targets"][row], "source": before["sources"][column],
                      "requested": wanted, "read_back": got,
                      "state": "applied" if got is not None and abs(got - wanted) <= TOLERANCE
                      else "clamped" if got is not None else "not_observed"})
    return {"added_targets": added, "cells": cells,
            "all_applied": all(cell["state"] == "applied" for cell in cells)}
