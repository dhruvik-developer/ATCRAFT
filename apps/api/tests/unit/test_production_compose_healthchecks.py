from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_production_batch_worker_healthcheck_targets_its_node_name() -> None:
    compose_path = REPO_ROOT / "docker-compose.prod.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    worker = compose["services"]["celery-worker"]
    command = worker["command"]
    node_name = command[command.index("-n") + 1]
    healthcheck = worker["healthcheck"]

    assert node_name == "batch@%h"
    assert healthcheck["test"][0] == "CMD-SHELL"
    assert f"-d {node_name.replace('%h', '$$HOSTNAME')}" in healthcheck["test"][1]
