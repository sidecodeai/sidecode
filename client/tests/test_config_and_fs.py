"""Configuration storage, origin policy, directory listing, git and diffs."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rc_client.attachments import materialise, wire_attachments
from rc_client.config import (
    Config,
    config_path,
    is_local_origin,
    load_config,
    normalise_origin,
    save_config,
)
from rc_client.diffs import from_tool_input, unified
from rc_client.errors import RcError
from rc_client.fs import list_dirs, make_dir, merge_recents
from rc_client.git import git_info, session_git, slugify
from rc_client.ids import block_uuid, is_uuid_v4


def sample_config() -> Config:
    return Config(
        gateway_origin="https://rc.example.com",
        device_id="dev-1",
        device_token="secret-token",
        name="mac-studio",
    )


def test_config_round_trips_and_is_written_0600() -> None:
    saved = save_config(sample_config())
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    loaded = load_config()
    assert loaded.gateway_origin == "https://rc.example.com"
    assert loaded.device_token == "secret-token"
    assert loaded.device_ws_url == "wss://rc.example.com/ws/device"


def test_config_dials_directly_unless_a_proxy_was_chosen() -> None:
    save_config(sample_config())
    assert load_config().proxy == ""
    chosen = sample_config()
    chosen.proxy = "http://proxy.example:3128"
    save_config(chosen)
    assert load_config().proxy == "http://proxy.example:3128"


def test_config_rejects_a_proxy_that_is_neither_env_nor_a_url() -> None:
    path = save_config(sample_config())
    path.write_text(path.read_text().replace('proxy = ""', 'proxy = "bogus"'))
    with pytest.raises(RcError) as caught:
        load_config()
    assert caught.value.code == "bad_request"


def test_config_home_follows_the_environment_override(client_home: Path) -> None:
    save_config(sample_config())
    assert config_path().parent == client_home
    assert config_path().exists()


def test_a_second_save_replaces_the_file_atomically() -> None:
    save_config(sample_config())
    updated = sample_config()
    updated.name = "renamed"
    save_config(updated)
    assert load_config().name == "renamed"
    leftovers = list(config_path().parent.glob(".*.tmp"))
    assert leftovers == []


def test_loading_without_a_config_is_a_clear_error() -> None:
    with pytest.raises(RcError) as caught:
        load_config()
    assert caught.value.code == "not_found"
    assert "rc-client enroll" in caught.value.message


def test_plain_http_is_allowed_only_for_local_addresses() -> None:
    assert normalise_origin("http://127.0.0.1:8787") == "http://127.0.0.1:8787"
    assert normalise_origin("http://192.168.1.20:8787") == "http://192.168.1.20:8787"
    assert normalise_origin("rc.example.com/") == "https://rc.example.com"
    assert normalise_origin("https://rc.example.com/path") == "https://rc.example.com"
    assert is_local_origin("http://localhost:8787")
    with pytest.raises(RcError):
        normalise_origin("http://rc.example.com")
    with pytest.raises(RcError):
        normalise_origin("ftp://rc.example.com")


def test_list_dirs_returns_directories_only_and_flags_repositories(tmp_path: Path) -> None:
    (tmp_path / "visible").mkdir()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")

    listing = list_dirs(str(tmp_path), [("/recent", 42)])
    names = [entry["name"] for entry in listing["entries"]]
    assert names == ["repo", "visible"]
    assert next(entry for entry in listing["entries"] if entry["name"] == "repo")["is_git"]
    assert listing["parent"] == str(tmp_path.parent)
    assert listing["recent"] == [{"path": "/recent", "last_used": 42}]


def test_list_dirs_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(RcError) as caught:
        list_dirs(str(tmp_path / "nope"), [])
    assert caught.value.code == "not_found"


def test_make_dir_creates_one_folder_and_returns_its_listing(tmp_path: Path) -> None:
    listing = make_dir(str(tmp_path), "new-project", [("/recent", 42)])
    made = tmp_path / "new-project"
    assert made.is_dir()
    assert listing["path"] == str(made)
    assert listing["parent"] == str(tmp_path)
    assert listing["entries"] == []
    assert listing["recent"] == [{"path": "/recent", "last_used": 42}]


@pytest.mark.parametrize(
    "name",
    ["", "a/b", "sub\\dir", ".hidden", "..", ".", "n\0ul", "x" * 256, "\u00e9" * 200],
)
def test_make_dir_refuses_a_name_that_is_not_one_component(tmp_path: Path, name: str) -> None:
    with pytest.raises(RcError) as caught:
        make_dir(str(tmp_path), name, [])
    assert caught.value.code == "bad_request"
    assert list(tmp_path.iterdir()) == []


def test_make_dir_refuses_a_path_that_is_not_absolute(tmp_path: Path) -> None:
    with pytest.raises(RcError) as caught:
        make_dir("relative/dir", "child", [])
    assert caught.value.code == "bad_request"


def test_make_dir_reports_a_missing_parent_as_not_found(tmp_path: Path) -> None:
    with pytest.raises(RcError) as caught:
        make_dir(str(tmp_path / "nope"), "child", [])
    assert caught.value.code == "not_found"


def test_make_dir_reports_an_existing_directory_as_a_conflict(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    with pytest.raises(RcError) as caught:
        make_dir(str(tmp_path), "taken", [])
    assert caught.value.code == "conflict"


def test_make_dir_reports_an_existing_file_as_a_conflict(tmp_path: Path) -> None:
    (tmp_path / "taken").write_text("x")
    with pytest.raises(RcError) as caught:
        make_dir(str(tmp_path), "taken", [])
    assert caught.value.code == "conflict"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_make_dir_reports_an_unwritable_parent_as_forbidden(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(RcError) as caught:
            make_dir(str(locked), "child", [])
        assert caught.value.code == "forbidden"
    finally:
        locked.chmod(0o700)


def test_merge_recents_keeps_the_newest_use_per_path() -> None:
    merged = merge_recents([("/a", 1), ("/b", 5)], [("/a", 9), ("", 3)])
    assert merged == [("/a", 9), ("/b", 5)]


async def test_git_info_reports_not_a_repository_outside_git(tmp_path: Path) -> None:
    assert await git_info(str(tmp_path)) == {"is_repo": False}
    assert await session_git(str(tmp_path)) is None


async def test_git_info_reads_the_branch_and_dirty_flag(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    (tmp_path / "a.txt").write_text("one")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    info = await git_info(str(tmp_path))
    assert info == {"is_repo": True, "branch": "main", "dirty": False, "ahead": 0, "behind": 0}

    (tmp_path / "a.txt").write_text("two")
    summary = await session_git(str(tmp_path), worktree=True)
    assert summary == {"branch": "main", "dirty": True, "ahead": 0, "behind": 0, "worktree": True}


def test_slugify_produces_a_safe_worktree_name() -> None:
    assert slugify("Fix the flaky auth test!") == "fix-the-flaky-auth-test"
    assert slugify("***") == "session"


def test_unified_diff_counts_additions_and_deletions() -> None:
    diff = unified("a.py", "one\n", "one\ntwo\n")
    assert diff["additions"] == 1
    assert diff["deletions"] == 0
    assert diff["path"] == "a.py"

    write = from_tool_input("Write", {"file_path": "/x/new.py", "content": "a\nb\n"})
    assert write is not None
    assert write["additions"] == 2
    assert from_tool_input("Bash", {"command": "ls"}) is None


def test_multiedit_diffs_are_merged() -> None:
    merged = from_tool_input(
        "MultiEdit",
        {
            "file_path": "/x/a.py",
            "edits": [
                {"old_string": "one\n", "new_string": "ONE\n"},
                {"old_string": "two\n", "new_string": "TWO\n"},
            ],
        },
    )
    assert merged is not None
    assert merged["additions"] == 2
    assert merged["deletions"] == 2


def test_attachments_are_written_0600_with_their_decoded_size() -> None:
    written = materialise(
        "sess-1",
        [{"name": "shot.png", "mime": "image/png", "data_base64": "aGVsbG8="}],
    )
    assert len(written) == 1
    assert written[0].size == 5
    assert Path(written[0].path).read_bytes() == b"hello"
    assert stat.S_IMODE(Path(written[0].path).stat().st_mode) == 0o600
    assert wire_attachments(written) == [{"name": "shot.png", "mime": "image/png", "size": 5}]


def test_attachments_reject_bad_base64() -> None:
    with pytest.raises(RcError) as caught:
        materialise("sess-1", [{"name": "x", "mime": "text/plain", "data_base64": "!!!"}])
    assert caught.value.code == "bad_request"


def test_block_ids_are_stable_uuid_v4_values() -> None:
    first = block_uuid("msg_01:0")
    assert first == block_uuid("msg_01:0")
    assert first != block_uuid("msg_01:1")
    assert is_uuid_v4(first)
    existing = "3db744e1-a6c3-4f21-bc51-a8ca32c6cd9a"
    assert block_uuid(existing) == existing
