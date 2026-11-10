"""Where configuration thinks it is on disk.

``src/core/config.py`` locates an optional ``.env`` by counting directories up
from its own file. That count is correct in a checkout and wrong in the
container image, where ``src/`` is copied straight to ``/app`` and there are
simply fewer parents to count. Indexing blindly raised ``IndexError`` while the
module was still being imported - so the application could not start at all,
and gunicorn reported "Worker failed to boot" above a pathlib traceback that
mentioned neither configuration nor a file.

It survived 3 600 tests, a clean lint and a green build because every one of
them runs from a checkout. The layout is the input, and only one layout was
ever supplied.

So the roots are computed by small total functions and tested against every
layout as data. Asserting on the real ``REPO_ROOT`` constant would only ever
re-check the checkout - the case that already worked.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from src.core.config import API_ROOT, REPO_ROOT, AppEnv, Settings, _api_root, _repo_root

# (description, path to config.py, expected api root, expected repo root)
LAYOUTS = [
    (
        "the container image: src/ copied to /app",
        PurePosixPath("/app/src/core/config.py"),
        "/app",
        # There is no repository above /app. Clamped to the filesystem root,
        # which makes env_file name a "/.env" that does not exist - and a
        # deployment passes real environment variables, so no file is wanted.
        "/",
    ),
    (
        "a posix checkout",
        PurePosixPath("/srv/atcraft/apps/api/src/core/config.py"),
        "/srv/atcraft/apps/api",
        "/srv/atcraft",
    ),
    (
        "a windows checkout",
        PureWindowsPath(r"C:\Project\Other\ATCRAFT\apps\api\src\core\config.py"),
        r"C:\Project\Other\ATCRAFT\apps\api",
        r"C:\Project\Other\ATCRAFT",
    ),
    (
        "installed directly under the filesystem root",
        PurePosixPath("/src/core/config.py"),
        "/",
        "/",
    ),
]


@pytest.mark.parametrize(
    ("description", "module_path", "expected_api", "expected_repo"),
    LAYOUTS,
    ids=[case[0] for case in LAYOUTS],
)
def test_both_roots_resolve_for_every_layout_the_code_is_installed_in(
    description: str,
    module_path: PurePosixPath | PureWindowsPath,
    expected_api: str,
    expected_repo: str,
) -> None:
    assert str(_api_root(module_path)) == expected_api, description
    assert str(_repo_root(module_path)) == expected_repo, description


@pytest.mark.parametrize("depth", range(0, 7))
def test_no_depth_raises_indexerror(depth: int) -> None:
    """The actual failure, as its own case at every depth.

    The bug was not a wrong path - it was an exception during import. String
    comparisons alone would still pass against code that raises for a layout
    nobody thought to list, which is exactly how this shipped: the deployed
    layout was the one nobody listed.
    """
    path = PurePosixPath("/".join(["", *["d"] * depth, "config.py"]))
    assert _api_root(path).is_absolute()
    assert _repo_root(path).is_absolute()


def test_the_roots_never_point_below_the_file_they_are_derived_from() -> None:
    # A clamp that went the other way - returning a *descendant* - would place
    # env_file inside src/, where a stray .env would be picked up in production.
    path = PurePosixPath("/app/src/core/config.py")
    for root in (_api_root(path), _repo_root(path)):
        assert path.is_relative_to(root)


def test_the_real_constants_still_point_at_this_checkout() -> None:
    # The checkout case, which every other test in the suite relies on:
    # API_ROOT is where alembic.ini and pyproject.toml live, REPO_ROOT is where
    # the compose files and the shared .env live.
    assert (API_ROOT / "pyproject.toml").is_file()
    assert (API_ROOT / "alembic.ini").is_file()
    assert (REPO_ROOT / "docker-compose.yml").is_file()


# --- Where files are stored ------------------------------------------------------
#
# The template's STORAGE_PATH=./storage used to resolve against the working
# directory. The hybrid API, worker and seed start from apps/api, the Docker
# development stack mounts the repository root's storage/, and both use one
# database - so every seeded PDF and upload was recorded in it and written to a
# directory the other mode never reads.


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, app_env=AppEnv.test, jwt_secret="test", **overrides)


@pytest.mark.parametrize("start_in", ["apps/api", "the repository root", "elsewhere"])
def test_a_relative_storage_path_means_the_repository_root_from_any_working_directory(
    start_in: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    working_directory = {"apps/api": API_ROOT, "the repository root": REPO_ROOT}.get(
        start_in, tmp_path
    )
    monkeypatch.chdir(working_directory)
    monkeypatch.delenv("STORAGE_PATH", raising=False)
    repository_storage = (REPO_ROOT / "storage").resolve()

    assert _settings(storage_path=Path("./storage")).storage_path == repository_storage
    assert (
        _settings(storage_path=Path("data/files")).storage_path
        == (REPO_ROOT / "data" / "files").resolve()
    )
    # The template's value and the default agree, so dropping the line changes nothing.
    assert _settings().storage_path == repository_storage


def test_an_absolute_or_home_storage_path_is_kept(tmp_path: Path) -> None:
    assert _settings(storage_path=tmp_path / "files").storage_path == (tmp_path / "files").resolve()
    assert (
        _settings(storage_path=Path("~/atcraft-files")).storage_path
        == (Path.home() / "atcraft-files").resolve()
    )
