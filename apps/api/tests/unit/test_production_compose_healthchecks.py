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


def test_production_backend_waits_for_private_clamav() -> None:
    prod = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    dependency = prod["services"]["backend"]["depends_on"]["clamav"]

    assert dependency["condition"] == "service_healthy"
    assert prod["services"]["backend"]["environment"]["MALWARE_SCANNER"] == (
        "${MALWARE_SCANNER:-clamav}"
    )
    assert base["services"]["clamav"]["profiles"] == ["malware"]
    assert "ports" not in base["services"]["clamav"]
    assert base["services"]["clamav"]["image"].startswith("clamav/clamav:1.4@sha256:")
