"""Prepare a manifest for a completed CourseRAG evaluation run."""

from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,70}")
REQUIRED = {"config.json", "samples.jsonl", "metrics.json", "report.md", "failures.md"}
ALLOWED = REQUIRED | {"ragas.json"}
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


def read_artifact(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular artifact: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Artifact size limit exceeded: {path.name}")
    return data


def write_manifest(directory: Path) -> Path:
    if RUN_ID.fullmatch(directory.name) is None or not directory.is_dir() or directory.is_symlink():
        raise ValueError("Expected a run directory named with its run ID")
    names = REQUIRED | {name for name in ALLOWED - REQUIRED if (directory / name).exists()}
    remaining = MAX_ARCHIVE_BYTES
    files = {}
    for name in sorted(names):
        data = read_artifact(directory / name, remaining)
        if name == "config.json":
            config = json.loads(data)
            if not isinstance(config, dict) or config.get("run_id") != directory.name:
                raise ValueError("Run directory and config.json run ID must match")
        files[name] = {"bytes": len(data), "sha256": sha256(data).hexdigest()}
        remaining -= len(data)
    manifest = json.dumps({"run_id": directory.name, "files": files}, indent=2) + "\n"
    if len(manifest.encode()) > remaining:
        raise ValueError("Archive size limit exceeded")

    destination = directory / "artifacts.json"
    stream = NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(manifest)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
