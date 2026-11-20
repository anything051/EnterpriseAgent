"""Serve only the named, verified files in a CourseRAG run archive."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from enterprise_rag.core.documents import DocumentNotFound
from enterprise_rag.evaluation.artifacts import ALLOWED, MAX_ARCHIVE_BYTES, REQUIRED, RUN_ID, read_artifact


def list_runs(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(directory.name for directory in root.iterdir()
                  if RUN_ID.fullmatch(directory.name) and directory.is_dir() and not directory.is_symlink()
                  and (directory / "artifacts.json").is_file() and not (directory / "artifacts.json").is_symlink())


def run_files(root: Path, run_id: str) -> dict[str, bytes]:
    if RUN_ID.fullmatch(run_id) is None:
        raise DocumentNotFound()
    directory = root / run_id
    if directory.is_symlink():
        raise DocumentNotFound()
    try:
        raw = read_artifact(directory / "artifacts.json", 64 * 1024)
        manifest = json.loads(raw)
        files = manifest["files"]
        if manifest["run_id"] != run_id or not REQUIRED <= files.keys() <= ALLOWED:
            raise DocumentNotFound()
        total = len(raw)
        payload = {"artifacts.json": raw}
        for name, metadata in sorted(files.items()):
            size = metadata["bytes"]
            if type(size) is not int or not 0 <= size <= MAX_ARCHIVE_BYTES - total:
                raise DocumentNotFound()
            data = read_artifact(directory / name, size)
            if len(data) != size or sha256(data).hexdigest() != metadata["sha256"]:
                raise DocumentNotFound()
            payload[name] = data
            total += size
        return payload
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
        raise DocumentNotFound() from error


def zip_run(root: Path, run_id: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in run_files(root, run_id).items():
            archive.writestr(name, data)
    return output.getvalue()
