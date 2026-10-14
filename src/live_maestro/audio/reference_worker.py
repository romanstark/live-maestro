"""Isolated audio comparison and native Live capture."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

if "live_maestro" not in sys.modules:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "live_maestro", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("worker package unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_maestro"] = module
    spec.loader.exec_module(module)


def main() -> None:
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        kind = request["kind"]
        if kind == "capture":
            from live_maestro.audio.capture import capture
            report = capture(request)
        elif kind == "comparison":
            from live_maestro.audio.reference import compare
            report = compare(request)
        else:
            raise ValueError("unsupported Live audio worker kind")
        target = Path(request["output_dir"]) / "report.json"
        target.write_text(json.dumps(report, allow_nan=False, indent=2), encoding="utf-8")
        print(json.dumps({"event": "result", "ok": True, "report_path": str(target),
                          "schema": report["schema"], "review_required":
                          report.get("review_required", report.get("timing", {}).get(
                              "review_required", False)),
                          "audio_paths": report.get("audio_paths", [])},
                         allow_nan=False), flush=True)
    except Exception as error:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"event": "result", "ok": False, "error": str(error),
                          "code": "reference_audio_failed"}), flush=True)


if __name__ == "__main__":
    main()
