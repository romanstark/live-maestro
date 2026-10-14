"""Live discovery offers control/capture, with no model setup or inference tools."""

import asyncio
import importlib.util

from live_maestro import server


def test_live_tool_surface_owns_control_and_not_transcription():
    tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert {"capture_audio", "compare_audio", "write_arrangement_notes", "write_drum_clip"} <= tools
    assert (
        not {"transcribe_stem", "analyze_drums", "analyze_reference_audio", "describe_audio"}
        & tools
    )
    assert importlib.util.find_spec("live_maestro.audio.semantic") is None
    assert importlib.util.find_spec("live_maestro.audio.model_runtime") is None


def test_removed_analysis_job_kinds_are_rejected(tmp_path):
    import pytest

    from live_maestro.audio.jobs import AudioJobs

    manager = AudioJobs(tmp_path / "state")
    try:
        for kind in ("semantic", "reference", "drums"):
            with pytest.raises(ValueError, match="unsupported"):
                manager.submit({"kind": kind}, str(tmp_path))
        assert not (tmp_path / "state").exists()
    finally:
        manager.close()
