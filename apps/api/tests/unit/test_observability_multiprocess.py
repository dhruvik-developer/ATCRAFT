"""The exporter combines metrics written by separate API/worker processes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_rendered_metrics_sum_values_from_distinct_processes(tmp_path: Path) -> None:
    api_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    increment = (
        "import sys; "
        "from src.core.observability import http_requests_total; "
        "http_requests_total.labels(method='GET', route='/probe', status='200')"
        ".inc(int(sys.argv[1]))"
    )
    for amount in ("2", "3"):
        subprocess.run(
            [sys.executable, "-c", increment, amount],
            cwd=api_root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )

    render = (
        "from src.core.observability import render_metrics; print(render_metrics()[0].decode())"
    )
    result = subprocess.run(
        [sys.executable, "-c", render],
        cwd=api_root,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )

    assert (
        'atcraft_http_requests_total{method="GET",route="/probe",status="200"} 5.0' in result.stdout
    )
