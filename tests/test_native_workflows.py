"""Native workflows fail before unsafe writes and preserve ambiguous batch receipts."""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_browser_walk_coverage import SCRIPT_MODULE as remote
from test_tempo_workflows import saved_set
from test_tool_schema import _text_payload

from live_maestro import native_workflows as workflows
from live_maestro import server
from live_maestro.client import LiveConnectionError
from live_maestro.toolargs import NoteIn


def audio(path: Path) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        handle.writeframes(b"\0\0" * 44100)
    return path


class NotesClient:
    def __init__(self, fail: int | None = None) -> None:
        self.fail = fail
        self.requests: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []

    def get(self, path: str) -> dict[str, Any]:
        return {"value": True if path.endswith("is_midi_clip") else 4096}

    def send(self, handler: str, params: dict[str, Any]) -> dict[str, Any]:
        if handler == "notes_get":
            return {"notes": self.notes}
        self.requests.append(params)
        if len(self.requests) == self.fail:
            raise LiveConnectionError("connection dropped after send")
        if params["mode"] == "replace":
            self.notes = []
        self.notes.extend(params["notes"])
        return {"written": len(params["notes"])}


def notes(count: int) -> list[NoteIn]:
    return [NoteIn(pitch=36, start_time=i * 0.25, duration=0.1, velocity=100) for i in range(count)]


def test_arrangement_chunks_compare_the_complete_native_readback(monkeypatch) -> None:
    client = NotesClient()
    monkeypatch.setattr(server, "_client_instance", lambda: client)
    result = server.write_arrangement_notes(
        "song.tracks[0].arrangement_clips[0]", notes(257), "replace", True
    )
    assert result["ok"] and result["verified"]
    assert [len(row["notes"]) for row in client.requests] == [128, 128, 1]
    assert [row["mode"] for row in client.requests] == ["replace", "append", "append"]


def test_disconnect_reports_only_acknowledged_chunks_and_never_retries(monkeypatch) -> None:
    client = NotesClient(fail=2)
    monkeypatch.setattr(server, "_client_instance", lambda: client)
    result = server.write_arrangement_notes(
        "song.tracks[0].arrangement_clips[0]", notes(257), "replace", True
    )
    assert not result["ok"] and len(client.requests) == 2
    assert len(result["confirmed_chunks"]) == 1
    assert result["in_flight"] == {"offset": 128, "count": 128, "delivery": "unknown"}


def test_actual_mcp_typed_tempo_and_warp_previews_are_offline(tmp_path, monkeypatch) -> None:
    def unavailable():
        raise AssertionError("a preview must not contact Live")

    monkeypatch.setattr(server, "_client_instance", unavailable)
    source = saved_set(tmp_path / "source.als")
    for tool, args in (
        (
            "write_tempo_map",
            {
                "source_path": str(source),
                "output_path": str(tmp_path / "copy.als"),
                "events": [
                    {"beat": 0, "bpm": 120, "shape": "ramp"},
                    {"beat": 8, "bpm": 180, "shape": "step"},
                ],
            },
        ),
        (
            "set_warp_markers",
            {
                "path": "song.tracks[0].arrangement_clips[0]",
                "markers": [{"seconds": 0, "beat": 0}, {"seconds": 10, "beat": 20}],
            },
        ),
    ):
        response = asyncio.run(server.mcp.call_tool(tool, args))
        assert json.loads(_text_payload(response))["blocked"]
    assert not (tmp_path / "copy.als").exists()


def test_invalid_attack_cannot_create_a_pad_or_replace_a_sample(tmp_path) -> None:
    source = audio(tmp_path / "one-shot.wav")
    unavailable: Any = None
    with pytest.raises(ValueError, match="exceeds source"):
        workflows.load_pad(unavailable, str(source), "song.tracks[0].devices[0]", 36, 44100, True)


class Clip:
    is_audio_clip = True
    sample_length = 44100 * 20
    sample_rate = 44100
    warping = False

    def __init__(self) -> None:
        self.warp_markers = [
            SimpleNamespace(sample_time=0, beat_time=0),
            SimpleNamespace(sample_time=10, beat_time=20),
            SimpleNamespace(sample_time=20.02, beat_time=40.04),
        ]
        self.removed: list[float] = []

    def remove_warp_marker(self, beat: float) -> None:
        self.removed.append(beat)
        self.warp_markers = [m for m in self.warp_markers if m.beat_time != beat]

    def add_warp_marker(self, marker: Any) -> None:
        self.warp_markers = [m for m in self.warp_markers if m.beat_time != marker.beat_time]
        self.warp_markers.append(marker)
        self.warp_markers.sort(key=lambda m: m.beat_time)


def handler(clip: Clip, monkeypatch: pytest.MonkeyPatch, constructor: Any):
    monkeypatch.setattr(
        remote, "Live", SimpleNamespace(Clip=SimpleNamespace(WarpMarker=constructor))
    )
    script = remote.LiveMaestro.__new__(remote.LiveMaestro)
    script.resolve_clip = lambda path: (clip, path)
    return script


def test_warp_constructor_failure_precedes_enable_and_remove(monkeypatch) -> None:
    clip = Clip()

    def unavailable(*args, **kwargs):
        raise TypeError("not constructible")

    script = handler(clip, monkeypatch, unavailable)
    with pytest.raises(remote.LomError):
        script._handle_warp_markers_set(
            {"path": "clip", "markers": [{"seconds": 0, "beat": 0}, {"seconds": 20, "beat": 50}]}
        )
    assert not clip.warping and not clip.removed and len(clip.warp_markers) == 3


def test_warp_replacement_removes_auto_markers_and_retains_shadow(monkeypatch) -> None:
    clip = Clip()
    script = handler(
        clip,
        monkeypatch,
        lambda sample_time, beat_time: SimpleNamespace(
            sample_time=sample_time, beat_time=beat_time
        ),
    )
    result = script._handle_warp_markers_set(
        {"path": "clip", "markers": [{"seconds": 0, "beat": 0}, {"seconds": 20, "beat": 40}]}
    )
    assert result["verified"] and clip.warping
    assert clip.removed == [20]
    assert result["shadow_marker"] == {"seconds": 20.02, "beat": 40.04}


def test_warp_point_outside_source_is_refused_before_enable(monkeypatch) -> None:
    clip = Clip()
    script = handler(clip, monkeypatch, object)
    with pytest.raises(remote.LomError, match="source audio duration"):
        script._handle_warp_markers_set(
            {"path": "clip", "markers": [{"seconds": 0, "beat": 0}, {"seconds": 21, "beat": 40}]}
        )
    assert not clip.warping and not clip.removed


def test_browser_clear_is_an_explicit_nullable_reference(monkeypatch) -> None:
    browser = SimpleNamespace(hotswap_target=SimpleNamespace(name="pad"))
    script = remote.LiveMaestro.__new__(remote.LiveMaestro)
    result = script._set_object_reference(
        "app.browser.hotswap_target",
        browser,
        "hotswap_target",
        browser.hotswap_target,
        {"__path__": None},
    )
    assert browser.hotswap_target is None
    assert result["reference_stored"] and result["after"] is None
