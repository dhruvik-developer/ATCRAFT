"""Both public HTTP runtimes use the same minimal, pinned and scanned image."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def test_frontend_and_edge_share_the_pinned_core_nginx_runtime():
    dockerfile = (ROOT / "apps/web/Dockerfile").read_text(encoding="utf-8")
    runtime = re.search(r"^FROM (\S+) AS production$", dockerfile, re.MULTILINE)
    assert runtime is not None
    edge = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"][
        "nginx"
    ]["image"]
    assert edge == runtime.group(1)
    assert re.fullmatch(r"nginx:\d+\.\d+\.\d+-alpine\d+\.\d+-slim@sha256:[0-9a-f]{64}", edge)
    # These libraries belong to optional modules absent from our configuration;
    # adding them back would defeat the smaller runtime's security boundary.
    assert "apk add" not in dockerfile.split(" AS production", 1)[1]
    host_check = (ROOT / "scripts/check-host-nginx.sh").read_text(encoding="utf-8")
    assert edge in host_check


def test_ci_scans_the_actual_deployed_edge_and_both_public_runtimes_for_secrets():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    resolver = next(step for step in steps if step.get("id") == "edge-image")
    assert 'json.load(sys.stdin)["services"]["nginx"]["image"]' in resolver["run"]
    scans = [step for step in steps if "aquasecurity/trivy-action@" in step.get("uses", "")]
    edge_scan = next(
        step
        for step in scans
        if step["with"]["image-ref"] == "${{ steps.edge-image.outputs.image }}"
    )
    frontend_scan = next(
        step for step in scans if str(step["with"]["image-ref"]).startswith("atcraft/web:")
    )
    for scan in (edge_scan, frontend_scan):
        config = scan["with"]
        assert config["exit-code"] == "1"
        assert config["severity"] == "HIGH,CRITICAL"
        assert config["scanners"] == "vuln,secret"
        assert config.get("ignore-unfixed", False) is False
