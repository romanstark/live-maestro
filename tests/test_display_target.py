"""Aiming a device parameter at the reading it should show.

Covers the display-string parser, the probe grid that must never touch the ends of a
range, the macro numbering that differs between this project's two channels, and the
``set_parameter_display`` tool end to end against a synthetic non-linear taper.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.types import TextContent

from live_maestro import server
from live_maestro.als.read import Macro
from live_maestro.lom.introspect import DeviceView, ParameterView, parse_display


def _text_payload(result: Any) -> str:
    """Return the text a call_tool answer carries, whichever shape it arrived in."""
    blocks = result[0] if isinstance(result, tuple) else result
    first = blocks[0]
    assert isinstance(first, TextContent), (
        f"expected a text content block, got {type(first).__name__}"
    )
    return first.text


# ------------------------------------------------------------------- the parser


def test_two_units_are_folded_so_readings_can_be_compared() -> None:
    """1.00 s is larger than 60.0 ms, and only a shared base unit shows that."""
    assert parse_display("8.00 kHz") == (8000.0, "Hz")
    assert parse_display("1.00 s") == (1000.0, "ms")
    assert parse_display("60.0 ms") == (60.0, "ms")

    seconds, _ = parse_display("1.00 s")
    millis, _ = parse_display("60.0 ms")
    assert seconds is not None and millis is not None
    assert seconds > millis


def test_a_ratio_reads_as_its_strength_whichever_way_live_prints_it() -> None:
    assert parse_display("4.00 : 1") == (4.0, "ratio")
    assert parse_display("1 : 4.00") == (4.0, "ratio")


def test_a_reading_without_a_unit_is_refused_rather_than_believed() -> None:
    """A bare number is what every VST2 answers: parseable and meaningless."""
    assert parse_display("0.66") == (None, None)
    assert parse_display("On") == (None, None)
    assert parse_display("-inf dB") == (None, None)
    assert parse_display("Bell") == (None, None)
    assert parse_display("") == (None, None)
    assert parse_display(None) == (None, None)


def test_a_thousands_comma_does_not_turn_twelve_thousand_into_twelve() -> None:
    assert parse_display("12,000 Hz") == (12000.0, "Hz")


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        ("-12.0 dB", (-12.0, "dB")),
        ("50%", (50.0, "%")),
        ("+7 st", (7.0, "st")),
        ("-25 cents", (-25.0, "cents")),
    ],
)
def test_the_units_this_server_reads(shown: str, expected: tuple[float, str]) -> None:
    assert parse_display(shown) == expected


# --------------------------------------------------------------------- the grid


def test_the_probe_grid_never_asks_at_either_end_of_the_range() -> None:
    """An endpoint has taken Live down inside a plugin's own formatting code."""
    grid = server._probe_grid(0.0, 1.0, server._DISPLAY_COARSE_SAMPLES)
    assert len(grid) == server._DISPLAY_COARSE_SAMPLES
    assert min(grid) > 0.0
    assert max(grid) < 1.0
    assert grid == sorted(grid)


def test_the_grid_stays_inside_a_range_that_does_not_start_at_zero() -> None:
    grid = server._probe_grid(-40.0, 0.0, 8)
    assert min(grid) > -40.0
    assert max(grid) < 0.0


# -------------------------------------------------------------------- the macro


def test_a_macro_carries_the_lom_index_so_nobody_does_the_arithmetic() -> None:
    """The file counts slots from 0, the LOM counts parameters from 1."""
    macro = Macro(index=6, name="Drive", value="0.5", default="0.0")
    assert macro.index == 6
    assert macro.lom_parameter_index == 7


# --------------------------------------------------------------- the tool, e2e


def _one_parameter(**kw: Any) -> DeviceView:
    """A device with a single parameter, for the tool to aim at."""
    fields: dict[str, Any] = {
        "index": 1,
        "path": "song.tracks[0].devices[0].parameters[1]",
        "name": "Attack",
        "value": 0.5,
        "minimum": 0.0,
        "maximum": 1.0,
    }
    fields.update(kw)
    return DeviceView(
        path="song.tracks[0].devices[0]",
        name="Compressor",
        class_name="Compressor2",
        index=0,
        track="song.tracks[0]",
        parameters=(ParameterView(**fields),),
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, view: DeviceView, taper: Any
) -> dict[str, Any]:
    """Wire the tool to a synthetic device whose display follows ``taper``."""
    written: dict[str, Any] = {}
    monkeypatch.setattr(server, "_client_instance", lambda: object())
    monkeypatch.setattr(server.introspect, "describe_device", lambda *a, **k: view)

    def fake_batch(ops: Any, **_: Any) -> tuple[list[dict[str, Any]], None]:
        return [{"ok": True, "value": taper(op["call_args"][0])} for op in ops], None

    def fake_run(spec_id: str, **kw: Any) -> dict[str, Any]:
        written.update(kw)
        return {"ok": True, "value": kw.get("value"), "read_back": "applied"}

    monkeypatch.setattr(server, "_run_batch", fake_batch)
    monkeypatch.setattr(server, "_run", fake_run)
    return written


def _call(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"track": 0, "device": 0, "parameter": "1"}
    args.update(kw)
    return json.loads(
        _text_payload(asyncio.run(server.mcp.call_tool("set_parameter_display", args)))
    )


def test_it_lands_on_the_reading_through_a_fourth_power_taper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attack as v^4 * 1000 ms: writing 150 linearly would be thirty times out."""
    written = _install(
        monkeypatch, _one_parameter(), lambda v: f"{(v ** 4) * 1000:.2f} ms"
    )

    payload = _call(target=150, unit="ms")

    assert payload["ok"] is True, payload
    assert payload["unit"] == "ms"
    assert abs(payload["reading"] - 150) < 5, payload
    # v^4 * 1000 reads 150 ms at v = 0.6223. A linear write of 150 would clamp to
    # max and display 1000 ms.
    assert abs(written["value"] - 0.6223) < 0.01, written
    assert payload["round_trips"] == 3
    assert "out_of_range" not in payload


def test_it_finds_the_value_on_a_logarithmic_frequency_taper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _install(
        monkeypatch,
        _one_parameter(name="Frequency"),
        lambda v: f"{20 * (1000 ** v):.1f} Hz",
    )

    payload = _call(target=8000, unit="Hz")

    assert payload["ok"] is True, payload
    assert abs(payload["reading"] - 8000) < 200, payload
    assert 0.0 < written["value"] < 1.0


def test_it_takes_the_unit_from_the_device_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _one_parameter(), lambda v: f"{-40 + 40 * v:.1f} dB")

    payload = _call(target=-26)

    assert payload["ok"] is True, payload
    assert payload["unit"] == "dB"


def test_a_target_in_the_wrong_unit_is_refused_not_converted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _one_parameter(), lambda v: f"{-40 + 40 * v:.1f} dB")

    payload = _call(target=8000, unit="Hz")

    assert payload["ok"] is False
    assert "dB" in payload["error"]


def test_a_device_that_reports_no_unit_is_refused_rather_than_aimed_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every VST2 answers a bare number, and a search there walks to the end stop."""
    _install(monkeypatch, _one_parameter(), lambda v: f"{v:.2f}")

    payload = _call(target=150, unit="ms")

    assert payload["ok"] is False
    assert "no unit" in payload["error"]
    assert "set_parameter" in payload["advice"]


def test_a_quantized_parameter_is_the_wrong_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _one_parameter(name="Filter Type", is_quantized=True),
        lambda v: "Bell",
    )

    payload = _call(target=1, unit="dB")

    assert payload["ok"] is False
    assert "quantized" in payload["error"]
    assert "value_items" in payload["advice"]


def test_a_target_the_device_cannot_display_says_so_instead_of_pretending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _one_parameter(), lambda v: f"{-40 + 40 * v:.1f} dB")

    payload = _call(target=12, unit="dB")

    assert payload["ok"] is True, payload
    assert payload["out_of_range"] is True
    assert "never displayed" in payload["out_of_range_note"]


def test_a_barely_sampled_taper_is_flagged_instead_of_reading_as_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device can report units across part of its range only."""

    def patchy(value: float) -> str:
        return f"{-40 + 40 * value:.1f} dB" if value > 0.95 else f"{value:.2f}"

    _install(monkeypatch, _one_parameter(), patchy)

    payload = _call(target=-1, unit="dB")

    assert payload["ok"] is True, payload
    assert payload["thinly_sampled"] is True
    assert "barely sampled" in payload["thinly_sampled_note"]


def test_an_unknown_unit_is_named_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _one_parameter(), lambda v: f"{v:.1f} dB")

    payload = _call(target=1, unit="furlongs")

    assert payload["ok"] is False
    assert "furlongs" in payload["error"]
    assert "dB" in payload["available"]


def test_nothing_is_written_while_the_probes_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probing bisection with real writes has left parameters at 100 percent."""
    seen: list[str] = []
    monkeypatch.setattr(server, "_client_instance", lambda: object())
    monkeypatch.setattr(
        server.introspect, "describe_device", lambda *a, **k: _one_parameter()
    )

    def fake_batch(ops: Any, **_: Any) -> tuple[list[dict[str, Any]], None]:
        rows: list[dict[str, Any]] = []
        for op in ops:
            seen.append(str(op["id"]))
            shown = -40 + 40 * float(op["call_args"][0])
            rows.append({"ok": True, "value": f"{shown:.1f} dB"})
        return rows, None

    monkeypatch.setattr(server, "_run_batch", fake_batch)
    monkeypatch.setattr(
        server, "_run", lambda spec_id, **kw: {"ok": True, "value": kw.get("value")}
    )

    payload = _call(target=-26, unit="dB")

    assert payload["ok"] is True, payload
    assert seen, "the probes did not run"
    assert set(seen) == {"param.str_for_value"}, seen
