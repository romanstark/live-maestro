"""Instrument presets in physical units, through one adapter per instrument format.

The vocabulary belongs to synthesis, not to a product: envelope times in seconds,
filter cutoff in Hz and filter type, LFO rate as a tempo division and its retrigger,
reverb decay in seconds, oscillator level and routing, and modulation routes. An
adapter turns those into one instrument's stored values and back. Vital is the first
adapter because its presets are open JSON. Live cannot load a plugin preset, so the
user loads a written file in the plugin. read_als_states() pulls each VST3 plugin's
saved state out of an .als, so the values the plugin really holds can be checked.
"""

from __future__ import annotations

import gzip
import json
import math
import re
from pathlib import Path
from typing import Any

from live_maestro.als.read import TRACK_TAGS, _devices_of, _first, load_xml

#: Vital's tempo-synced LFO rates by stored index, measured on saved presets.
VITAL_TEMPO = {"1/2": 7.0, "1/4": 8.0, "1/8": 9.0}
#: Vital's LFO sync modes, measured on saved presets: straight or triplet tempo.
VITAL_SYNC = {"seconds": 0.0, "tempo": 1.0, "triplet": 3.0}
VITAL_FILTER_TYPES = {"lowpass": 0.0, "bandpass": 1.0, "highpass": 2.0}
VITAL_DESTINATIONS = {"filter1": 0.0, "filter1+2": 2.0, "effects": 3.0}
VITAL_RETRIGGER = {"note": 0.0, "bar": 1.0}
#: Vital's stored cutoff is a MIDI note number within this range.
VITAL_CUTOFF_NOTES = (8.0, 136.0)
ENVELOPE_PARTS = ("delay", "attack", "hold", "decay", "release")
KEY = re.compile(r"^(?P<group>env|filter|lfo|osc)(?P<index>[1-9])\.(?P<field>[a-z_+]+)$"
                 r"|^reverb\.(?P<reverb>decay_s|mix|on)$")


def _invert(table: dict[str, float]) -> dict[float, str]:
    return {value: name for name, value in table.items()}


def vital_encode(name: str, value: Any) -> tuple[dict[str, Any], str | None]:
    """Return Vital settings for one vocabulary entry, and a note when it was clamped.

    Raises:
        ValueError: for an unknown entry or a value this adapter has not measured.
    """
    match = KEY.match(name)
    if match is None:
        raise ValueError(f"unknown parameter {name!r}")
    if match["reverb"]:
        field = match["reverb"]
        if field == "decay_s":
            return {"reverb_decay_time": math.log2(_positive(name, value))}, None
        if field == "mix":
            return {"reverb_dry_wet": _unit(name, value)}, None
        return {"reverb_on": 1.0 if value else 0.0}, None
    group, index, field = match["group"], match["index"], match["field"]
    if group == "env":
        if field == "sustain":
            return {f"env_{index}_sustain": _unit(name, value)}, None
        part = field.removesuffix("_s")
        if field.endswith("_s") and part in ENVELOPE_PARTS:
            # Vital stores envelope times as seconds to the power 1/4.
            return {f"env_{index}_{part}": _nonnegative(name, value) ** 0.25}, None
    if group == "filter":
        if field == "cutoff_hz":
            note = 69 + 12 * math.log2(_positive(name, value) / 440)
            low, high = VITAL_CUTOFF_NOTES
            clamped = min(high, max(low, note))
            remark = (f"{name} clamped to {440 * 2 ** ((clamped - 69) / 12):.1f} Hz"
                      if clamped != note else None)
            return {f"filter_{index}_cutoff": clamped}, remark
        if field == "resonance":
            return {f"filter_{index}_resonance": _unit(name, value)}, None
        if field == "type":
            return {f"filter_{index}_blend": _choice(name, value, VITAL_FILTER_TYPES)}, None
        if field == "on":
            return {f"filter_{index}_on": 1.0 if value else 0.0}, None
    if group == "lfo":
        if field == "rate":
            text = str(value)
            triplet = text.endswith("T")
            division = text.removesuffix("T")
            if division not in VITAL_TEMPO:
                raise ValueError(f"{name}: Vital rates measured so far are "
                                 f"{sorted(VITAL_TEMPO)} with optional T; use native for others")
            return {f"lfo_{index}_tempo": VITAL_TEMPO[division],
                    f"lfo_{index}_sync": VITAL_SYNC["triplet" if triplet else "tempo"]}, None
        if field == "retrigger":
            return {f"lfo_{index}_sync_type": _choice(name, value, VITAL_RETRIGGER)}, None
    if group == "osc":
        if field == "level":
            return {f"osc_{index}_level": _unit(name, value)}, None
        if field == "destination":
            return {f"osc_{index}_destination": _choice(name, value, VITAL_DESTINATIONS)}, None
        if field == "on":
            return {f"osc_{index}_on": 1.0 if value else 0.0}, None
    raise ValueError(f"unknown parameter {name!r}")


def vital_decode(settings: dict[str, Any]) -> dict[str, Any]:
    """Return the vocabulary entries a Vital settings dict holds, in physical units."""
    out: dict[str, Any] = {}
    tempo, sync = _invert(VITAL_TEMPO), _invert(VITAL_SYNC)
    for key, value in settings.items():
        if not isinstance(value, (int, float)):
            continue
        parts = key.split("_")
        if len(parts) == 3 and parts[0] == "env" and parts[2] in ENVELOPE_PARTS:
            out[f"env{parts[1]}.{parts[2]}_s"] = round(float(value) ** 4, 6)
        elif len(parts) == 3 and parts[0] == "env" and parts[2] == "sustain":
            out[f"env{parts[1]}.sustain"] = value
        elif len(parts) == 3 and parts[0] == "filter" and parts[2] == "cutoff":
            out[f"filter{parts[1]}.cutoff_hz"] = round(440 * 2 ** ((value - 69) / 12), 2)
        elif len(parts) == 3 and parts[0] == "filter" and parts[2] == "resonance":
            out[f"filter{parts[1]}.resonance"] = value
        elif len(parts) == 3 and parts[0] == "filter" and parts[2] == "blend":
            out[f"filter{parts[1]}.type"] = _invert(VITAL_FILTER_TYPES).get(float(value), value)
        elif len(parts) == 3 and parts[0] == "lfo" and parts[2] == "tempo":
            mode = sync.get(float(settings.get(f"lfo_{parts[1]}_sync", -1)))
            name = tempo.get(float(value))
            if mode in {"tempo", "triplet"} and name is not None:
                out[f"lfo{parts[1]}.rate"] = name + ("T" if mode == "triplet" else "")
        elif key.startswith("lfo_") and key.endswith("_sync_type"):
            out[f"lfo{parts[1]}.retrigger"] = _invert(VITAL_RETRIGGER).get(float(value), value)
        elif len(parts) == 3 and parts[0] == "osc" and parts[2] == "level":
            out[f"osc{parts[1]}.level"] = value
        elif len(parts) == 3 and parts[0] == "osc" and parts[2] == "destination":
            out[f"osc{parts[1]}.destination"] = _invert(VITAL_DESTINATIONS).get(float(value),
                                                                               value)
        elif key == "reverb_decay_time":
            out["reverb.decay_s"] = round(2 ** float(value), 4)
        elif key == "reverb_dry_wet":
            out["reverb.mix"] = value
    return dict(sorted(out.items()))


def vital_route(name: str) -> str:
    """Return Vital's name for a modulation source or target such as lfo1 or filter1.cutoff."""
    return re.sub(r"^([a-z]+)(\d)", r"\1_\2", name).replace(".", "_")


def write_vital(base: dict[str, Any], parameters: dict[str, Any], native: dict[str, Any],
                modulations: list[dict[str, Any]], replace_modulations: bool,
                name: str | None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Return the new preset, the settings it changed, and clamp notes.

    Raises:
        ValueError: when the base is not a Vital preset or no modulation slot is free.
    """
    import copy

    preset = copy.deepcopy(base)
    settings = preset.get("settings")
    if not isinstance(settings, dict) or "modulations" not in settings:
        raise ValueError("base is not a Vital preset: no settings with modulations")
    changed: dict[str, Any] = {}
    notes = []
    for key, value in parameters.items():
        encoded, remark = vital_encode(key, value)
        changed.update(encoded)
        if remark:
            notes.append(remark)
    changed.update(native)
    slots = settings["modulations"]
    if replace_modulations:
        slots = [{"source": "", "destination": ""} for _ in slots]
        for number in range(1, len(slots) + 1):
            if f"modulation_{number}_amount" in settings:
                changed[f"modulation_{number}_amount"] = 0.0
    for route in modulations:
        free = next((i for i, slot in enumerate(slots) if not slot.get("source")), None)
        if free is None:
            raise ValueError("no free modulation slot; pass replace_modulations")
        slots[free] = {"source": vital_route(route["source"]),
                       "destination": vital_route(route["destination"])}
        changed[f"modulation_{free + 1}_amount"] = float(route["amount"])
    settings.update(changed)
    settings["modulations"] = slots
    if name:
        preset["preset_name"] = name
    return preset, changed, notes


def state_json(blob: bytes) -> dict[str, Any] | None:
    """Return the JSON object inside a plugin state, gzip-compressed or plain, else None."""
    start = blob.find(b"\x1f\x8b")
    if start >= 0:
        try:
            blob = gzip.decompress(blob[start:])
        except (OSError, EOFError):
            pass
    text = blob.decode("utf-8", errors="ignore")
    brace = text.find("{")
    while brace >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(text, brace)
        except json.JSONDecodeError:
            brace = text.find("{", brace + 1)
            continue
        return value if isinstance(value, dict) else None
    return None


def read_als_states(path: str, track: int | str) -> list[dict[str, Any]]:
    """Return every plugin device on one track with its decoded state, if JSON.

    Raises:
        ValueError: when the track is not in the set.
    """
    root = load_xml(path)
    tracks = [element for element in root.iter() if element.tag in TRACK_TAGS]
    chosen = None
    for position, element in enumerate(tracks):
        label = _first(element, "./Name/EffectiveName", "./Name/UserName")
        if (isinstance(track, int) and position == track) or label == track:
            chosen = element
            break
    if chosen is None:
        raise ValueError(f"no track {track!r} in {Path(path).name}")
    found = []
    for position, (element, device) in enumerate(_devices_of(chosen)):
        if element.tag != "PluginDevice":
            continue
        state = element.find(".//ProcessorState")
        blob = (bytes.fromhex("".join((state.text or "").split()))
                if state is not None else b"")
        data = state_json(blob) if blob else None
        settings = data.get("settings") if isinstance(data, dict) else None
        adapter = ("vital" if isinstance(settings, dict) and "env_1_attack" in settings
                   and "filter_1_cutoff" in settings else None)
        found.append({"device_index": position, "name": device.name,
                      "state_bytes": len(blob), "state_json": data is not None,
                      "adapter": adapter,
                      "parameters": vital_decode(settings) if adapter == "vital" and
                      isinstance(settings, dict) else None})
    return found


def _positive(name: str, value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _nonnegative(name: str, value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be zero or positive")
    return number


def _unit(name: str, value: Any) -> float:
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"{name} must be within 0..1")
    return number


def _choice(name: str, value: Any, table: dict[str, float]) -> float:
    if value not in table:
        raise ValueError(f"{name} must be one of {sorted(table)}")
    return table[value]
