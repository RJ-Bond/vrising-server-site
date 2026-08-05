"""copy_backup_offsite() (backend/helpers.py) — best-effort mirror of a just-created
local backup file to BACKUP_REMOTE_PATH, called from _auto_backup_task (backend/main.py)
after each nightly local backup. Must never raise: an offsite failure (unmounted path,
full disk, permissions) must not take down the local backup that already succeeded or
the retention cleanup that runs after it in the caller."""
import shutil

from backend import helpers


def test_copy_backup_offsite_noop_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers, "BACKUP_REMOTE_PATH", "")
    src = tmp_path / "vrising_20260101_000000.db"
    src.write_bytes(b"fake db content")
    assert helpers.copy_backup_offsite(src) is False


def test_copy_backup_offsite_copies_file_via_shutil_fallback(tmp_path, monkeypatch):
    remote_dir = tmp_path / "remote"
    monkeypatch.setattr(helpers, "BACKUP_REMOTE_PATH", str(remote_dir))
    # Force the shutil.copy2 fallback path regardless of whether rsync happens to be
    # installed on the machine actually running this test.
    monkeypatch.setattr(shutil, "which", lambda name: None)

    src = tmp_path / "vrising_20260101_000000.db"
    src.write_bytes(b"fake db content")

    assert helpers.copy_backup_offsite(src) is True
    dest = remote_dir / src.name
    assert dest.exists()
    assert dest.read_bytes() == b"fake db content"


def test_copy_backup_offsite_uses_rsync_when_available(tmp_path, monkeypatch):
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    monkeypatch.setattr(helpers, "BACKUP_REMOTE_PATH", str(remote_dir))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rsync" if name == "rsync" else None)

    calls = []

    def fake_run(cmd, check, timeout):
        calls.append(cmd)
        # Emulate what rsync would actually do so the destination exists afterward.
        shutil.copy2(cmd[-2], cmd[-1])

        class _Result:
            returncode = 0
        return _Result()

    monkeypatch.setattr("backend.helpers.subprocess.run", fake_run)

    src = tmp_path / "vrising_20260101_000000.db"
    src.write_bytes(b"fake db content")

    assert helpers.copy_backup_offsite(src) is True
    assert calls and calls[0][0] == "rsync"
    assert (remote_dir / src.name).read_bytes() == b"fake db content"


def test_copy_backup_offsite_never_raises_on_failure(tmp_path, monkeypatch):
    # BACKUP_REMOTE_PATH points under a path component that's a plain file, not a
    # directory — Path.mkdir(parents=True) raises NotADirectoryError internally, which
    # copy_backup_offsite must swallow and turn into a plain `False` return.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setattr(helpers, "BACKUP_REMOTE_PATH", str(blocker / "subdir"))

    src = tmp_path / "vrising_20260101_000000.db"
    src.write_bytes(b"fake db content")

    assert helpers.copy_backup_offsite(src) is False
