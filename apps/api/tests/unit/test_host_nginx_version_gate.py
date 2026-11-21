"""``check-host-nginx.sh --installed`` refuses a host nginx too old to load the config.

The host configuration uses ``http2 on;``, which nginx 1.25.1 introduced, and
Ubuntu's own package is 1.18 on 22.04 and 1.24 on 24.04. The check used to load
the installed files only inside a container running a newer nginx, so it passed
a configuration the box's own nginx rejects at ``sudo nginx -t`` - or, after a
release's reinstall, at the next restart. It now reads the host's ``nginx -v``.

The gate's two functions are loaded from the script and run against a stub
``nginx`` on PATH; Docker is not needed.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit

SCRIPT = REPO_ROOT / "scripts" / "check-host-nginx.sh"


@pytest.fixture(scope="module")
def bash() -> str:
    spec = importlib.util.spec_from_file_location(
        "shell_syntax", REPO_ROOT / "scripts/hooks/check_shell.py"
    )
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    try:
        return str(checker.find_bash())
    except RuntimeError as missing:
        pytest.skip(str(missing))


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _gate() -> str:
    source = _script()
    minimum = re.search(r'^MIN_HOST_NGINX="[^"]+"$', source, re.M)
    assert minimum, "check-host-nginx.sh no longer names MIN_HOST_NGINX"
    definitions = [minimum.group()]
    for name in ("version_at_least", "require_host_nginx_version"):
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", source, re.M | re.S)
        assert match, f"check-host-nginx.sh no longer defines {name}"
        definitions.append(match.group())
    return "\n".join(definitions)


def _run_gate(bash: str, tmp_path: Path, reported: str | None) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    if reported is not None:
        nginx = stubs / "nginx"
        # nginx prints its version on stderr.
        nginx.write_bytes(f"#!/bin/sh\necho '{reported}' >&2\n".encode())
        nginx.chmod(0o755)
    # The stub directory goes first, so it answers before any real nginx on the
    # machine running the test. Git Bash needs the Windows path in POSIX form.
    script = f"""
set -euo pipefail
stubs=$GATE_STUBS
if command -v cygpath >/dev/null 2>&1; then stubs=$(cygpath -u "$stubs"); fi
export PATH="$stubs:$PATH"
{_gate()}
require_host_nginx_version
"""
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-s"],
        input=script,
        text=True,
        capture_output=True,
        env={**os.environ, "GATE_STUBS": str(stubs), "LC_ALL": "C"},
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("version", ["1.24.0", "1.18.0", "1.25.0", "0.99.9"])
def test_a_host_nginx_older_than_the_minimum_is_refused(
    bash: str, tmp_path: Path, version: str
) -> None:
    result = _run_gate(bash, tmp_path, f"nginx version: nginx/{version} (Ubuntu)")
    assert result.returncode != 0, result.stdout
    assert f"the host runs nginx {version}" in result.stderr
    assert "1.25.1 or later" in result.stderr
    assert "ONE-BOX-RUNBOOK.md, step 8" in result.stderr


@pytest.mark.parametrize("version", ["1.25.1", "1.26.3", "1.30.7", "1.100.0", "2.0.0"])
def test_a_host_nginx_at_or_above_the_minimum_passes(
    bash: str, tmp_path: Path, version: str
) -> None:
    result = _run_gate(bash, tmp_path, f"nginx version: nginx/{version}")
    assert result.returncode == 0, result.stderr
    assert f"host nginx {version}" in result.stdout


def test_a_host_without_nginx_is_refused(bash: str, tmp_path: Path) -> None:
    if shutil.which("nginx") or any(
        Path(directory, "nginx").exists() for directory in ("/usr/sbin", "/usr/local/sbin")
    ):
        pytest.skip("this machine has a real nginx, which the gate would find")
    result = _run_gate(bash, tmp_path, None)
    assert result.returncode != 0, result.stdout
    assert "this host has no nginx" in result.stderr


def test_an_unreadable_version_is_refused(bash: str, tmp_path: Path) -> None:
    result = _run_gate(bash, tmp_path, "something else entirely")
    assert result.returncode != 0, result.stdout
    assert "could not read the host nginx version" in result.stderr


def test_installed_mode_runs_the_gate_and_the_minimum_image_matches_it() -> None:
    source = _script()
    installed = re.search(r"^  installed\)\n(.*?)^    ;;", source, re.M | re.S)
    assert installed, "check-host-nginx.sh no longer has an --installed branch"
    assert "require_host_nginx_version || exit 1" in installed.group(1)
    minimum = re.search(r'^MIN_HOST_NGINX="([^"]+)"$', source, re.M)
    assert minimum
    images = re.search(r"^NGINX_IMAGES=\(\n(.*?)^\)", source, re.M | re.S)
    assert images
    versions = re.findall(r"nginx:(\d+\.\d+\.\d+)-", images.group(1))
    # The deployment runtime, then the oldest nginx a host may run: the
    # templates must load on the minimum the gate admits, or the gate is a
    # claim nothing tests.
    assert len(versions) == 2
    assert versions[1] == minimum.group(1)


def test_the_runbook_installs_a_host_nginx_the_gate_accepts() -> None:
    runbook = (REPO_ROOT / "ONE-BOX-RUNBOOK.md").read_text(encoding="utf-8")
    minimum = re.search(r'^MIN_HOST_NGINX="([^"]+)"$', _script(), re.M)
    assert minimum
    assert f"nginx {minimum.group(1)} or later" in runbook
    # Ubuntu's archive is 1.18 or 1.24: nginx is installed only once the
    # nginx.org repository is in apt's sources.
    repository = runbook.find("https://nginx.org/packages/ubuntu")
    install = runbook.find("apt-get install -y nginx")
    assert 0 <= repository < install
