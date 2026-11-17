"""Preset vocabulary through the Vital adapter, and plugin states read from a saved set."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from live_maestro import presets, server
from live_maestro.toolargs import ModulationRouteIn


def base_preset() -> dict:
    settings = {"env_1_attack": 0.0, "env_1_release": 0.0, "env_1_sustain": 1.0,
                "filter_1_cutoff": 60.0, "filter_1_on": 0.0, "lfo_1_tempo": 8.0,
                "lfo_1_sync": 1.0, "reverb_decay_time": 0.0,
                "modulations": [{"source": "env_2", "destination": "osc_1_level"}]
                + [{"source": "", "destination": ""} for _ in range(3)],
                "modulation_1_amount": 0.5, "modulation_2_amount": 0.0,
                "modulation_3_amount": 0.0, "modulation_4_amount": 0.0}
    return {"author": "", "settings": settings}


@pytest.mark.parametrize(("name", "value"), [
    ("env1.attack_s", 0.004), ("env2.release_s", 0.35), ("env1.sustain", 0.6),
    ("filter1.cutoff_hz", 880.0), ("filter2.resonance", 0.82), ("filter2.type", "bandpass"),
    ("lfo1.rate", "1/8T"), ("lfo2.rate", "1/4"), ("lfo1.retrigger", "bar"),
    ("osc2.level", 0.5), ("osc1.destination", "filter1+2"), ("reverb.decay_s", 1.4),
    ("reverb.mix", 0.3)])
def test_vocabulary_round_trips_through_vital(name: str, value) -> None:
    encoded, remark = presets.vital_encode(name, value)
    assert remark is None
    decoded = presets.vital_decode(encoded)
    assert decoded[name] == (pytest.approx(value, rel=1e-4) if isinstance(value, float)
                             else value)


def test_measured_encodings_hold() -> None:
    assert presets.vital_encode("env1.attack_s", 16.0)[0] == {"env_1_attack": 2.0}
    assert presets.vital_encode("filter1.cutoff_hz", 440.0)[0] == {"filter_1_cutoff": 69.0}
    assert presets.vital_encode("lfo1.rate", "1/8T")[0] == {"lfo_1_tempo": 9.0,
                                                           "lfo_1_sync": 3.0}
    assert presets.vital_encode("reverb.decay_s", 2.0)[0] == {"reverb_decay_time": 1.0}


def test_unmeasured_values_are_refused_and_cutoff_clamps() -> None:
    with pytest.raises(ValueError, match="measured"):
        presets.vital_encode("lfo1.rate", "1/16")
    with pytest.raises(ValueError, match="unknown"):
        presets.vital_encode("chorus.depth", 0.5)
    encoded, remark = presets.vital_encode("filter1.cutoff_hz", 30000.0)
    assert encoded == {"filter_1_cutoff": 136.0} and remark is not None


def test_write_keeps_routes_and_refuses_existing_files(tmp_path: Path) -> None:
    base = tmp_path / "base.vital"
    base.write_text(json.dumps(base_preset()), encoding="utf-8")
    target = tmp_path / "out" / "bass.vital"
    route = ModulationRouteIn(source="lfo1", destination="filter2.cutoff", amount=0.25)
    arguments = {"action": "write", "path": str(target), "base_path": str(base),
                 "parameters": {"filter2.type": "bandpass", "lfo1.rate": "1/8T"},
                 "modulations": [route], "name": "Bass A"}
    plan = server.instrument_preset(**arguments)
    assert plan["executed"] is False and not target.exists()
    assert server.instrument_preset(**arguments, confirm=True)["ok"] is True
    written = json.loads(target.read_text(encoding="utf-8"))["settings"]
    assert written["modulations"][0] == {"source": "env_2", "destination": "osc_1_level"}
    assert written["modulations"][1] == {"source": "lfo_1", "destination": "filter_2_cutoff"}
    assert written["modulation_2_amount"] == 0.25 and written["filter_2_blend"] == 1.0
    again = server.instrument_preset(**arguments, confirm=True)
    assert again["ok"] is False and "exists" in again["error"]


def test_read_als_finds_the_plugin_state(tmp_path: Path) -> None:
    state = b"\x00" * 176 + json.dumps(base_preset()).encode()
    xml = (f"<Ableton><LiveSet><Tracks><MidiTrack><Name><EffectiveName Value='Bass'/></Name>"
           f"<DeviceChain><DeviceChain><Devices><PluginDevice><PluginDesc><Vst3PluginInfo>"
           f"<Name Value='Vital'/><Preset><Vst3Preset><ProcessorState>{state.hex()}"
           f"</ProcessorState></Vst3Preset></Preset></Vst3PluginInfo></PluginDesc>"
           f"</PluginDevice></Devices></DeviceChain></DeviceChain></MidiTrack></Tracks>"
           f"</LiveSet></Ableton>")
    path = tmp_path / "set.als"
    path.write_bytes(gzip.compress(xml.encode()))
    result = server.instrument_preset(action="read_als", path=str(path), track="Bass")
    device = result["devices"][0]
    assert device["adapter"] == "vital"
    assert device["parameters"]["filter1.cutoff_hz"] == pytest.approx(261.63, abs=0.01)
    assert device["parameters"]["lfo1.rate"] == "1/4"


def test_gzip_wrapped_state_is_read() -> None:
    blob = b"\x01\x00\x00\x00" * 5 + gzip.compress(b'{"a": 1}')
    assert presets.state_json(blob) == {"a": 1}
