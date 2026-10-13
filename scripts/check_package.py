"""Install release artifacts and probe the independent file workflow outside the checkout."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


def run(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    print("CHECK", Path(command[0]).name, " ".join(command[1:3]), flush=True)
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def validate_artifacts(wheel: Path, sdist: Path) -> None:
    """Refuse local runtimes and other unlisted roots before installing artifacts."""
    with zipfile.ZipFile(wheel) as archive:
        roots = {name.split("/")[0] for name in archive.namelist()}
        unexpected = {root for root in roots if root != "live_maestro"
                      and not root.endswith(".dist-info")}
        if unexpected:
            raise ValueError(f"unlisted wheel roots: {sorted(unexpected)}")
    with tarfile.open(sdist) as archive:
        names = ["/".join(member.name.split("/")[1:]) for member in archive.getmembers()]
        roots = {name.split("/")[0] for name in names if name}
        allowed = {
            "src", "live-remote-script", "scripts", "docs", "devices", "tests",
            "measurements", "README.md", "LICENSE", "THIRD-PARTY.md", "CLA.md",
            "CONTRIBUTING.md", ".gitignore", "pyproject.toml", "PKG-INFO",
        }
        if roots - allowed:
            raise ValueError(f"unlisted sdist roots: {sorted(roots - allowed)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    args = parser.parse_args()
    wheel, sdist = args.wheel.resolve(), args.sdist.resolve()
    validate_artifacts(wheel, sdist)
    with tempfile.TemporaryDirectory(prefix="live-maestro-package-") as directory:
        root = Path(directory)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("LIVE_MAESTRO_AUDIO_PYTHON", None)
        venv.create(root / "venv", with_pip=True)
        binary = root / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
        suffix = ".exe" if sys.platform == "win32" else ""
        python = binary / ("python" + suffix)
        environment["VIRTUAL_ENV"] = str(root / "venv")
        environment["PATH"] = str(binary) + os.pathsep + environment.get("PATH", "")
        run([str(python), "-m", "pip", "install", "-q", str(wheel)], root, environment)
        run(
            [str(python), "-c", (
                "import asyncio; from pathlib import Path; from live_maestro.server import mcp; "
                "from live_maestro.installer import DEFAULT_SOURCE,DEVICE_SOURCE; "
                "assert DEFAULT_SOURCE.is_file() and DEVICE_SOURCE.is_file(); "
                "assert len(asyncio.run(mcp.list_tools()))==59; "
                "import live_maestro.audio.jobs as jobs; "
                "assert (Path(jobs.__file__).parent/'drum_worker.py').is_file(); "
                "assert 'numpy' not in __import__('sys').modules"
            )], root, environment,
        )
        run([str(python), "-m", "pip", "install", "-q", str(wheel) + "[dev,audio]"],
            root, environment)
        run([str(python), str(Path(__file__).parent / "probe_drum_workflow.py"),
             "--python", str(python), "--output", str(root / "wheel-probe")], root, environment)
        source = root / "source"
        source.mkdir()
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError("sdist links are not accepted")
                if not (source / member.name).resolve().is_relative_to(source.resolve()):
                    raise ValueError("sdist member escapes its root")
            archive.extractall(source, filter="data" if sys.version_info >= (3, 12) else None)
        project = next(source.iterdir())
        run([str(python), "-m", "pip", "install", "-q", "-e", str(project) + "[dev,audio]"],
            root, environment)
        run([str(python), "-m", "pytest", "-q"], project, environment)
        run([str(binary / ("ruff" + suffix)), "check", "src", "scripts", "tests"],
            project, environment)
        run([str(binary / ("pyright" + suffix)), "--pythonpath", str(python)], project, environment)
        run([str(binary / ("pyrefly" + suffix)), "check", "--python-interpreter-path", str(python)],
            project, environment)
        run([str(python), str(project / "scripts/probe_drum_workflow.py"),
             "--python", str(python), "--output", str(root / "sdist-probe")], root, environment)
        print(f"PACKAGE PASSED: {sys.platform}, Python {sys.version.split()[0]}", flush=True)


if __name__ == "__main__":
    main()
