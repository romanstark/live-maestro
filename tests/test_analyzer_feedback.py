"""Analyzer metadata must not turn native devices or Arrangement audio into VST advice."""

from typing import Any

import pytest

from live_maestro import server
from live_maestro.analyzer.models import TransportState
from live_maestro.lom.introspect import (
    DEVICE_ON,
    DeviceView,
    Diagnosis,
    ParameterView,
    configuration_advice,
    diagnose,
)


@pytest.mark.parametrize(
    ("device_class", "expected"),
    [("MxDeviceAudioEffect", False), ("Operator", False), ("PluginDevice", True), (None, None)],
)
def test_device_list_configure_advice_depends_on_device_type(
    monkeypatch: pytest.MonkeyPatch, device_class: str | None, expected: bool | None
) -> None:
    monkeypatch.setattr(server, "_count", lambda *_args, **_kw: (
        {"count": 1, "exact": True, "values": ["Device"]}, None
    ))
    monkeypatch.setattr(server, "_run_batch", lambda *_args, **_kw: (
        [{"ok": device_class is not None, "value": device_class}, {"ok": True, "value": True}],
        None,
    ))
    monkeypatch.setattr(server, "_scan", lambda *_args, **_kw: (
        {"0": {"count": 1, "exact": True}}, None
    ))
    result: dict[str, Any] = server.get_devices(track=0)
    assert result["devices"][0]["configure_needed"] is expected
    assert (server.CONFIGURE_NOTE in result["notes"]) is (expected is True)


@pytest.mark.parametrize("device_class", ["MaxDevice", "MxDeviceAudioEffect", "Operator"])
def test_native_device_introspection_does_not_recommend_vst_configure(device_class: str) -> None:
    view = DeviceView(
        path="song.master_track.devices[0]", name="Analyzer", class_name=device_class,
        index=0, track="song.master_track",
        parameters=(ParameterView(0, "p", DEVICE_ON),), diagnosis=Diagnosis.UNCONFIGURED,
    )
    assert configuration_advice(view) is None
    assert diagnose(view.parameters, is_plugin=False) is Diagnosis.SPARSE
    assert diagnose(view.parameters, is_plugin=True) is Diagnosis.UNCONFIGURED


def test_transport_names_session_count_and_preserves_compatibility() -> None:
    state = TransportState(
        is_playing=True, tracks_sounding=0, tracks_total=1, in_transition=False,
    )
    payload = state.model_dump()
    assert payload["session_tracks_playing"] == 0
    assert payload["tracks_sounding"] == payload["session_tracks_playing"]
    schema = TransportState.model_json_schema(mode="serialization")
    assert "Arrangement" in schema["properties"]["session_tracks_playing"]["description"]
    assert "Compatibility alias" in schema["properties"]["tracks_sounding"]["description"]
