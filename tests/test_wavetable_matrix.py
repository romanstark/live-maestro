"""Wavetable matrix reads, writes and read-backs against an in-memory device."""

from __future__ import annotations

from typing import Any

import pytest

from live_maestro import server
from live_maestro.toolargs import MatrixRouteIn
from live_maestro.wavetable import read

HEAD = "song.tracks[2].devices[0]"
SOURCES = 4


class FakeWavetable:
    """Answers the matrix calls the way the Remote Script relays them."""

    def __init__(self, names: list[str] | None = None) -> None:
        self.targets = ["Osc 1 Pos", "Filter 1 Freq"]
        self.cells: dict[tuple[int, int], float] = {(1, 2): 0.4}
        self.names = names
        self.params = {"5": "Amp Volume"}

    def get(self, path: str) -> dict[str, Any]:
        assert path == f"{HEAD}.visible_modulation_target_names"
        return {"value": list(self.targets)}

    def send(self, handler: str, params: dict[str, Any]) -> dict[str, Any]:
        assert handler == "enum_names"
        if self.names is None:
            raise RuntimeError("no such enum")
        return {"members": {name: index for index, name in enumerate(self.names)}}

    def _one(self, op: dict[str, Any]) -> dict[str, Any]:
        row, column = op["args"][0], op["args"][1]
        if not (0 <= row < len(self.targets) and 0 <= column < SOURCES):
            return {"status": "error", "error": "index out of range"}
        if op["method"] == "set_modulation_value":
            self.cells[(row, column)] = max(-1.0, min(1.0, op["args"][2] * 0.5 + 0.0))
            return {"status": "success", "result": None}
        return {"status": "success", "result": self.cells.get((row, column), 0.0)}

    def batch(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        return {"results": [self._one(op) for op in ops]}

    def call(self, path: str, method: str, args: list[Any]) -> dict[str, Any]:
        parameter = args[0]["__path__"].rsplit("[", 1)[1].rstrip("]")
        if method == "is_parameter_modulatable":
            return {"result": parameter in self.params}
        self.targets.append(self.params[parameter])
        return {"result": None}


def test_read_counts_sources_and_keeps_numbered_columns() -> None:
    matrix = read(FakeWavetable(), HEAD)
    assert matrix["source_count"] == SOURCES
    assert matrix["sources"] == ["source 0", "source 1", "source 2", "source 3"]
    assert matrix["rows"][1]["amounts"]["source 2"] == 0.4


def test_read_uses_live_names_only_when_they_fit() -> None:
    named = read(FakeWavetable(["Env 2", "Env 3", "LFO 1", "LFO 2"]), HEAD)
    assert named["sources"][2] == "LFO 1" and "names from" in named["source_basis"]
    wrong = read(FakeWavetable(["Env 2", "Env 3"]), HEAD)
    assert wrong["sources"][0] == "source 0"


def test_write_adds_a_row_reports_clamps_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWavetable(["Env 2", "Env 3", "LFO 1", "LFO 2"])
    monkeypatch.setattr(server, "_client_instance", lambda: fake)
    routes = [MatrixRouteIn(target="Amp Volume", source="LFO 1", amount=0.0, parameter="5"),
              MatrixRouteIn(target="Osc 1 Pos", source="1", amount=0.6)]
    plan = server.wavetable_matrix(2, 0, routes=routes, replace=True)
    assert plan["executed"] is False and plan["add_rows"] == {"Amp Volume": f"{HEAD}.parameters[5]"}
    assert fake.targets == ["Osc 1 Pos", "Filter 1 Freq"]
    done = server.wavetable_matrix(2, 0, routes=routes, replace=True, confirm=True)
    assert done["added_targets"] == ["Amp Volume"]
    states = {(c["target"], c["source"]): c["state"] for c in done["cells"]}
    assert states[("Amp Volume", "LFO 1")] == "applied"
    assert states[("Osc 1 Pos", "Env 3")] == "clamped"
    assert states[("Filter 1 Freq", "LFO 1")] == "applied"
    assert fake.cells[(1, 2)] == 0.0 and done["all_applied"] is False


def test_unknown_row_without_parameter_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_client_instance", FakeWavetable)
    route = MatrixRouteIn(target="Pitch", source="0", amount=0.2)
    answer = server.wavetable_matrix(2, 0, routes=[route], confirm=True)
    assert answer["ok"] is False and "no matrix row" in answer["error"]
