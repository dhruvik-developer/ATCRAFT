"""Prove the deployed Compose configuration still parses.

``docker compose config`` interpolates the env file, so it catches a renamed
variable, a broken YAML anchor or a missing required value before a deploy
does. The values below are throwaway placeholders that satisfy the ``:?``
guards in docker-compose.yml; nothing is started.

Skips - with a notice, not a failure - when Docker is not installed, so the
hook does not block a commit on a machine that only edits documentation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PLACEHOLDERS = {
    "COMPOSE_PROJECT_NAME": "hook-check",
    "POSTGRES_PASSWORD": "hook-check-placeholder",
    "JWT_SECRET": "hook-check-placeholder",
    "HTTP_PORT": "18080",
}


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        sys.stderr.write("compose-config: docker not on PATH, skipping\n")
        return 0
    env = {**os.environ, **PLACEHOLDERS}
    completed = subprocess.run(  # noqa: S603 - fixed argument list
        [
            docker,
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.prod.yml",
            "config",
            "--quiet",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
