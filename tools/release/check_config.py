"""Check effective Compose release settings without printing secrets."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def validate(services: dict) -> None:
    course_db = services["course-db"]["environment"]
    business_db = services["enterprise-db"]["environment"]
    course_api = services["course-api"]["environment"]
    business_api = services["enterprise-api"]["environment"]
    credentials = {
        "COURSE_DB_OWNER_PASSWORD": course_db.get("POSTGRES_PASSWORD"),
        "COURSE_DB_APP_PASSWORD": course_db.get("RAG_DB_APP_PASSWORD"),
        "ENTERPRISE_DB_OWNER_PASSWORD": business_db.get("POSTGRES_PASSWORD"),
        "ENTERPRISE_DB_APP_PASSWORD": business_db.get("RAG_DB_APP_PASSWORD"),
        "ENTERPRISE_DB_READER_PASSWORD": business_db.get("ENTERPRISE_DB_READER_PASSWORD"),
        "COURSE_RAG_TOKEN": course_api.get("COURSE_RAG_TOKEN"),
        "ENTERPRISE_ANALYST_TOKEN": business_api.get("ENTERPRISE_ANALYST_TOKEN"),
        "ENTERPRISE_VIEWER_TOKEN": business_api.get("ENTERPRISE_VIEWER_TOKEN"),
    }
    for name, value in credentials.items():
        pattern = r"[A-Za-z0-9._~-]{24,128}" if name.endswith("_PASSWORD") else r"[A-Za-z0-9._~+-]{24,256}"
        if not value or not re.fullmatch(pattern, value) or value.startswith("REPLACE_WITH_"):
            raise ValueError(f"Invalid credential setting: {name}")
    if len(set(credentials.values())) != len(credentials):
        raise ValueError("Use a different value for each of the eight release credentials")
    for name in ("course-db", "enterprise-db", "inventory"):
        if services[name].get("ports"):
            raise ValueError(f"{name} must have no host ports")
    for name in ("course-api", "course-ui", "enterprise-api", "enterprise-ui"):
        ports = services[name].get("ports", [])
        if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1":
            raise ValueError(f"{name} must publish one loopback-only port")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(args.env_file), "-f", "compose.release.yaml", "config", "--format", "json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        parser.exit(1, "Invalid Compose settings. Run docker compose config --quiet for details.\n")
    try:
        validate(json.loads(result.stdout)["services"])
    except (KeyError, ValueError) as error:
        parser.exit(1, f"Release preflight failed: {error}\n")
    print("Release credentials and loopback port bindings checked")


if __name__ == "__main__":
    main()
