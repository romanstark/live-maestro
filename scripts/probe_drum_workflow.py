"""Probe the installed MCP server with generated drum fixtures, without opening Live."""

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
    """Read the actual SDK response."""
    return result.structuredContent or json.loads(result.content[0].text)


async def probe(root: Path, python: Path) -> None:
    """Exercise typed tool dispatch, native audio processing and read-only DAW previews."""
    root.mkdir(parents=True, exist_ok=False)
    rate = 48000
    stems = []
    for instrument, times in (("kick", [0.1, 0.6, 1.1, 1.6]), ("snare", [0.35, 1.35])):
        samples = np.zeros((rate * 2, 2))
        for seconds in times:
            t = np.arange(round(rate * 0.06)) / rate
            signal = (
                0.6
                * np.sin(2 * np.pi * (70 if instrument == "kick" else 700) * t)
                * np.exp(-t * 70)
            )
            start = round(seconds * rate)
            samples[start : start + len(signal)] += signal[:, None]
        path = root / f"{instrument}.wav"
        sf.write(path, samples, rate, subtype="PCM_24")
        stems.append(
            {
                "path": str(path),
                "instrument": instrument,
                "source_offset_seconds": 3,
                "source_sample_rate": 44100,
            }
        )
    environment = dict(os.environ)
    environment["LIVE_MAESTRO_AUDIO_DATA_DIR"] = str(root / "data")
    environment.pop("PYTHONPATH", None)
    command = StdioServerParameters(
        command=str(python), args=["-m", "live_maestro.server"], env=environment, cwd=str(root)
    )
    async with stdio_client(command) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert len(tools.tools) == 59
        await session.read_resource(AnyUrl("live://skill/live-rebuild-drums"))
        started = payload(
            await session.call_tool(
                "analyze_drums",
                {
                    "stems": stems,
                    "output_dir": str(root / "outputs"),
                    "tempo_bpm": 120,
                    "cut_one_shots": True,
                },
            )
        )
        assert started["ok"], started
        job = started
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            job = payload(await session.call_tool("get_audio_job", {"job_id": started["id"]}))
            if job["state"] in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.2)
        assert job["state"] == "succeeded", job
        assert job["result"]["events"] == 6, job
        analysis_path = job["result"]["analysis_path"]
        data = json.loads(Path(analysis_path).read_text(encoding="utf-8"))
        assert all(event["source_sample_rate"] == 44100 for event in data["events"])
        preview = payload(
            await session.call_tool(
                "write_drum_clip",
                {
                    "analysis_path": analysis_path,
                    "track": 0,
                    "slot": 0,
                },
            )
        )
        assert preview["blocked"] and preview["plan"]["count"] == 6
        tempo = payload(
            await session.call_tool("apply_tempo_map", {"analysis_path": analysis_path})
        )
        assert tempo["blocked"] and abs(tempo["plan"]["bpms"][0] - 120) < 0.001
        (root / "protocol-result.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "tools": 59,
                    "state": job["state"],
                    "events": 6,
                    "one_shots": len(job["result"]["one_shots"]),
                    "live_writes": 0,
                    "output": str(root),
                }
            ),
            flush=True,
        )


def main() -> None:
    """Choose a unique evidence directory and installed interpreter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    root = args.output.resolve() / ("drum-probe-" + uuid.uuid4().hex)
    # Preserve a venv launcher symlink: resolving it loses that environment's packages.
    asyncio.run(probe(root, args.python.expanduser().absolute()))


if __name__ == "__main__":
    main()
