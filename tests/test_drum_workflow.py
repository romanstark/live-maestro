"""DAW previews accept external drum analysis without starting inference."""
import json
from pathlib import Path


def test_daw_previews_never_contact_live(tmp_path: Path, monkeypatch):
    from live_maestro import server

    data = {
        "schema": "maestro.drum-events",
        "schema_version": 1,
        "tempo_map": {"anchors": [{"seconds": 0, "beat": 0}, {"seconds": 2, "beat": 4}]},
        "events": [{"source_seconds": 1, "midi_pitch": 36, "velocity": 100}],
    }
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(
        server, "_client_instance", lambda: (_ for _ in ()).throw(AssertionError("Live contacted"))
    )
    assert server.write_drum_clip(str(path), 0, 0)["blocked"]
    assert server.apply_tempo_map(str(path))["blocked"]
    data["tempo_map"]["anchors"].append({"seconds": 4, "beat": 6})
    path.write_text(json.dumps(data))
    assert server.apply_tempo_map(str(path), True)["code"] == "arrangement_import_required"
