"""Durable, cancellable isolated audio jobs, independent of the Live bridge."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def data_root() -> Path:
    """Use an independent per-user local directory, with an explicit override."""
    if value := os.environ.get("LIVE_MAESTRO_AUDIO_DATA_DIR"):
        return Path(value).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / "live-maestro/audio"


def atomic(path: Path, value: dict[str, Any]) -> None:
    """Write one complete JSON record, cleaning only our own temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".audio-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class AudioJobs:
    """Serialize local analysis workers; no inference happens in the MCP event loop."""

    def __init__(self, root: Path | None = None):
        self.root = root or data_root()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-audio")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.processes: dict[str, subprocess.Popen[str]] = {}

    def _save(self, job: dict[str, Any]) -> None:
        atomic(self.root / "jobs" / f"{job['id']}.json", job)

    def get(self, identifier: str) -> dict[str, Any]:
        """Read live/persisted evidence, treating old unfinished records as interrupted."""
        if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            raise ValueError("invalid audio job ID")
        with self.lock:
            if identifier in self.jobs:
                return copy.deepcopy(self.jobs[identifier])
            job = json.loads(
                (self.root / "jobs" / f"{identifier}.json").read_text(encoding="utf-8")
            )
            if job["state"] not in TERMINAL:
                job["state"] = "interrupted"
            return job

    def submit(self, request: dict[str, Any], output_root: str) -> dict[str, Any]:
        """Validate input existence, then enqueue into a unique run directory."""
        kind = request.get("kind", "comparison")
        if kind not in {"comparison", "capture"}:
            raise ValueError("unsupported audio job kind")
        for key in ("reference_path", "candidate_path", "audio_path"):
            if request.get(key):
                path = Path(request[key]).expanduser().resolve()
                if not path.is_file():
                    raise ValueError(f"input does not exist: {path}")
                request[key] = str(path)
        for item in request.get("stems", []):
            for key in ("path", "reference_path"):
                if item.get(key):
                    path = Path(item[key]).expanduser().resolve()
                    if not path.is_file():
                        raise ValueError(f"input does not exist: {path}")
                    item[key] = str(path)
        identifier = uuid.uuid4().hex
        directory = Path(output_root).expanduser().resolve() / f"live-audio-{identifier}"
        job = {
            "id": identifier,
            "state": "queued",
            "progress": 0.0,
            "created_at": time.time(),
            "kind": kind,
            "output_dir": str(directory),
        }
        with self.lock:
            self.jobs[identifier] = job
            self._save(job)
        self.pool.submit(self._run, identifier, copy.deepcopy(request))
        return {"ok": True, **self.get(identifier)}

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        """Stop only this job's owned process tree."""
        if process.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.poll() is None:
                process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def cancel(self, identifier: str) -> dict[str, Any]:
        """Cancel queued/running work while preserving partial output evidence."""
        with self.lock:
            job = self.jobs.get(identifier)
            if job is None:
                return {"ok": True, **self.get(identifier)}
            if job["state"] not in TERMINAL:
                job.update(state="cancelled", finished_at=time.time())
                if process := self.processes.get(identifier):
                    if job.get("kind") == "capture":
                        Path(job["output_dir"], "cancel.request").touch()
                        job["cleanup_pending"] = True
                    else:
                        self._stop(process)
                self._save(job)
            return {"ok": True, **copy.deepcopy(job)}

    def _run(self, identifier: str, request: dict[str, Any]) -> None:
        job = self.jobs[identifier]
        output = Path(job["output_dir"])
        result = None
        process = None
        try:
            with self.lock:
                if job["state"] == "cancelled":
                    return
                job.update(state="running", started_at=time.time())
                self._save(job)
            output.mkdir(parents=True, exist_ok=False)
            request["output_dir"] = str(output)
            atomic(output / "request.json", request)
            python = request.get("worker_python") or os.environ.get(
                "LIVE_MAESTRO_AUDIO_PYTHON", sys.executable
            )
            with (output / "worker.log").open("w", encoding="utf-8") as errors:
                process = subprocess.Popen(
                    [
                        python,
                        "-I",
                        str(Path(__file__).with_name(
                            "reference_worker.py"
                        )),
                        str(output / "request.json"),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                    encoding="utf-8",
                    env=dict(os.environ),
                    start_new_session=sys.platform != "win32",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                with self.lock:
                    self.processes[identifier] = process
                    if job["state"] == "cancelled":
                        if job.get("kind") == "capture":
                            (output / "cancel.request").touch()
                        else:
                            self._stop(process)
                if process.stdout is None:
                    raise RuntimeError("worker channel unavailable")
                for line in process.stdout:
                    event = json.loads(line)
                    with self.lock:
                        if event["event"] == "progress":
                            job["progress"] = min(0.99, event["progress"])
                            if "stage" in event:
                                job["stage"] = event["stage"]
                            self._save(job)
                        elif event["event"] == "result":
                            result = event
                process.wait()
            if result is None or not result.get("ok") or process.returncode:
                raise RuntimeError(
                    result.get("error", "worker exited without results")
                    if result
                    else "worker crashed"
                )
            self._verify_file_result(output, result, request)
            with self.lock:
                if job["state"] != "cancelled":
                    job.update(state="succeeded", progress=1.0, result=result,
                               manifest=str(output / "manifest.json"), finished_at=time.time())
                    self._save(job)
        except Exception as error:  # noqa: BLE001
            with self.lock:
                if job["state"] != "cancelled":
                    job.update(
                        state="failed",
                        error=str(error),
                        code=result.get("code", "audio_job_failed")
                        if result
                        else "audio_job_failed",
                        finished_at=time.time(),
                    )
                self._save(job)
        finally:
            if process:
                if process.stdout:
                    process.stdout.close()
                self._stop(process)
                process.wait()
            with self.lock:
                self.processes.pop(identifier, None)
                if job.get("cleanup_pending"):
                    job["cleanup_pending"] = False
                    self._save(job)

    def close(self) -> None:
        """Ensure no worker or queue thread survives MCP shutdown."""
        for identifier in list(self.jobs):
            self.cancel(identifier)
        self.pool.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def _verify_file_result(
        output: Path, result: dict[str, Any], request: dict[str, Any]
    ) -> None:
        paths = [Path(result["report_path"]), *map(Path, result.get("audio_paths", []))]
        fingerprints = {}
        for path in paths:
            if not path.resolve().is_relative_to(output.resolve()) or not path.is_file():
                raise ValueError("audio worker result is absent or outside its run directory")
            if path.suffix == ".wav":
                from live_maestro.audio.sample_header import read_sample_header
                header = read_sample_header(path)
                if (header.frames <= 0 or header.channels not in (1, 2)
                        or header.expected_pcm_bytes != header.payload_bytes):
                    raise ValueError("capture contains no complete mono/stereo audio")
            fingerprints[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        report = json.loads(paths[0].read_text(encoding="utf-8"))
        if report.get("schema") != f"maestro.{request['kind']}":
            raise ValueError("audio worker report schema does not match the job")
        atomic(output / "manifest.json", {
            "request": request, "result": result, "output_sha256": fingerprints,
        })


_manager: AudioJobs | None = None
_manager_lock = threading.Lock()


def manager() -> AudioJobs:
    """Create the manager lazily; tool discovery never writes directories."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = AudioJobs()
        return _manager


def close() -> None:
    """Close an existing manager without creating one during shutdown."""
    if _manager:
        _manager.close()
