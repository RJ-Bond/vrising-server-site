"""Regression tests for _discard_stray_version_diff() — the safety net in both
site_update() (this file) and install.sh for a real incident: an old, since-removed
bug used to stamp a raw git hash directly into the git-tracked VERSION file, which
left a server's working tree "dirty" in a way `git pull --ff-only` only ever
complained about when a merge would actually need to touch VERSION. If the repo
already happened to be caught up (nothing new to pull), the stray content just sat
there forever, silently, since nothing forced git to reconcile it — which is exactly
what happened in production. This exercises the fix against a real git repo (not
mocked), since the function shells out to real `git` subprocess calls."""
import subprocess

import pytest

from backend.routers.admin_system import _discard_stray_version_diff

pytestmark = pytest.mark.asyncio


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "VERSION").write_text("v0.1.1")
    _git(tmp_path, "add", "VERSION")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


async def test_discards_stray_local_modification(repo):
    (repo / "VERSION").write_text("6612135")  # stray hash, exactly the production incident
    await _discard_stray_version_diff(str(repo))
    assert (repo / "VERSION").read_text() == "v0.1.1"


async def test_leaves_clean_version_untouched(repo):
    await _discard_stray_version_diff(str(repo))
    assert (repo / "VERSION").read_text() == "v0.1.1"


async def test_does_not_touch_other_locally_modified_files(repo):
    (repo / "other.txt").write_text("untracked, irrelevant")
    (repo / "VERSION").write_text("6612135")
    await _discard_stray_version_diff(str(repo))
    assert (repo / "VERSION").read_text() == "v0.1.1"
    assert (repo / "other.txt").read_text() == "untracked, irrelevant"


async def test_never_raises_on_a_non_git_directory(tmp_path):
    # Defensive: main.py's lifespan/admin flows shouldn't crash the whole update
    # stream over this helper if /opt/vrising-site somehow isn't a git repo.
    await _discard_stray_version_diff(str(tmp_path / "not-a-repo"))
