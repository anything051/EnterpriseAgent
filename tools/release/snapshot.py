"""Back up release databases and restore only into new databases in another Compose project."""

import argparse
from datetime import datetime, timezone
from hashlib import file_digest
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

SERVICES = {
    "course-db": ("course-api", "COURSE_RAG_DATABASE_URL", ("rag_app",)),
    "enterprise-db": ("enterprise-api", "ENTERPRISE_RAG_DATABASE_URL", ("rag_app", "enterprise_reader")),
}
RECOVERED = "enterprise_rag_recovered"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def database_names(compose: list[str]) -> dict[str, str]:
    result = subprocess.run([*compose, "config", "--format", "json"], text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Cannot resolve Compose settings. Run docker compose config --quiet.")
    configured = json.loads(result.stdout)["services"]
    names = {}
    for service, (api, env, _) in SERVICES.items():
        name = urlsplit(configured[api]["environment"][env]).path.removeprefix("/")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
            raise ValueError(f"Invalid database name for {service}")
        names[service] = name
    return names


def database(compose, service, command, *args, **kwargs):
    return subprocess.run([*compose, "exec", "-T", service, command, "-U", "rag_owner", *args],
                          check=True, **kwargs)


def backup(compose: list[str], project: str, archive: Path) -> None:
    names = database_names(compose)
    archive.mkdir(mode=0o700)
    files = {}
    for service, db in names.items():
        path = archive / f"{service}.dump"
        with path.open("xb") as stream:
            database(compose, service, "pg_dump", "--no-password", "-Fc", "-d", db, stdout=stream)
        files[path.name] = {"database": db, "bytes": path.stat().st_size, "sha256": digest(path)}
    manifest = {"version": 1, "source_project": project, "created_at": datetime.now(timezone.utc).isoformat(),
                "files": files}
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Both database snapshots and their manifest were written")


def restore(compose: list[str], project: str, archive: Path) -> None:
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["version"] != 1 or manifest["source_project"] == project
            or manifest["files"].keys() != {f"{service}.dump" for service in SERVICES}):
        raise ValueError("Restore requires a complete archive and a different Compose project")
    if set(database_names(compose).values()) != {RECOVERED}:
        raise ValueError(f"Set both destination database variables to {RECOVERED}")
    for service in SERVICES:
        path = archive / f"{service}.dump"
        check = manifest["files"][path.name]
        if path.stat().st_size != check["bytes"] or digest(path) != check["sha256"]:
            raise ValueError(f"Snapshot checksum does not match for {service}")
        existing = database(compose, service, "psql", "--no-password", "-tA", "-d", "enterprise_rag", "-c",
                            f"SELECT 1 FROM pg_database WHERE datname='{RECOVERED}'",
                            stdout=subprocess.PIPE, text=True).stdout.strip()
        if existing:
            raise ValueError(f"Restore database already exists in {service}; pick a fresh project")
    for service, (_, _, roles) in SERVICES.items():
        database(compose, service, "createdb", "--no-password", "--template=template0", RECOVERED)
        acl = f"REVOKE CONNECT ON DATABASE {RECOVERED} FROM PUBLIC; GRANT CONNECT ON DATABASE {RECOVERED} TO {', '.join(roles)}"
        database(compose, service, "psql", "--no-password", "-v", "ON_ERROR_STOP=1", "-d", "enterprise_rag", "-c", acl)
        with (archive / f"{service}.dump").open("rb") as stream:
            database(compose, service, "pg_restore", "--no-password", "--single-transaction", "--exit-on-error",
                     "-d", RECOVERED, stdin=stream)
    print("Both databases restored in the destination project")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    compose = ["docker", "compose", "--env-file", str(args.env_file), "-f", "compose.release.yaml", "-p", args.project]
    try:
        operation = backup if args.command == "backup" else restore
        operation(compose, args.project, args.archive)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Snapshot operation failed: {error}\n")


if __name__ == "__main__":
    main()
