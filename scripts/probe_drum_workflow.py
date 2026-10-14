"""Probe portable drum import and recorded comparisons without opening Live."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl


def payload(result: Any) -> dict[str, Any]:
    return result.structuredContent or json.loads(result.content[0].text)


async def probe(root: Path, python: Path) -> None:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "source.wav"
    rate = 16000
    sf.write(source, 0.1 * np.sin(2 * np.pi * 220 * np.arange(rate * 2) / rate), rate)
    analysis = root / "analysis.json"
    analysis.write_text(
        json.dumps(
            {
                "schema": "maestro.drum-events",
                "schema_version": 1,
                "tempo_map": {
                    "anchors": [{"seconds": 0, "beat": 0}, {"seconds": 2, "beat": 4}],
                    "review_required": False,
                },
                "events": [{"source_seconds": 0.5, "midi_pitch": 36, "velocity": 100}],
            }
        ),
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=str(python),
        args=["-m", "live_maestro.server"],
        env={**os.environ, "LIVE_MAESTRO_AUDIO_DATA_DIR": str(root / "state")},
        cwd=str(root),
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        assert (
            not {"analyze_drums", "transcribe_stem", "describe_audio", "analyze_reference_audio"}
            & tools
        )
        for name in ("live-rebuild-drums", "live-rebuild-reference"):
            await session.read_resource(AnyUrl("live://skill/" + name))
        preview = payload(
            await session.call_tool(
                "write_drum_clip", {"analysis_path": str(analysis), "track": 0, "slot": 0}
            )
        )
        assert preview["blocked"] and preview["plan"]["count"] == 1
        tempo = payload(
            await session.call_tool("apply_tempo_map", {"analysis_path": str(analysis)})
        )
        assert tempo["blocked"] and abs(tempo["plan"]["bpms"][0] - 120) < 0.001
        started = payload(
            await session.call_tool(
                "compare_audio",
                {
                    "reference_path": str(source),
                    "candidate_path": str(source),
                    "output_dir": str(root / "outputs"),
                    "end_seconds": 2,
                    "confirm": True,
                },
            )
        )
        assert started["ok"], started
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline:
            job = payload(await session.call_tool("get_audio_job", {"job_id": started["id"]}))
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                assert job["state"] == "succeeded", job
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Comparison did not complete.")
        report = payload(await session.call_tool("get_audio_report", {"job_id": started["id"]}))
        assert report["report"]["quality_score"] is None
        assert report["report"]["gain_aligned_waveform_error"] < 1e-8
        captured = payload(
            await session.call_tool(
                "capture_audio", {"output_dir": str(root / "outputs"), "duration_seconds": 1}
            )
        )
        assert captured["ok"] and not captured["executed"]
        result = {
            "tools": len(tools),
            "comparison": "succeeded",
            "drum_import": "previewed",
            "live_writes": 0,
            "model_tools": 0,
        }
        (root / "protocol-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("scratch"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    asyncio.run(probe(args.output_dir / ("protocol-" + uuid.uuid4().hex), args.python))


if __name__ == "__main__":
    main()
