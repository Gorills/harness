from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from publish_git_data import (
    GitHubRestGitDataApi,
    PublicationError,
    RemoteCommit,
    build_candidate,
    preflight,
    publish,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return result.stdout.decode("utf-8").strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / "remove.txt").write_text("remove\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(
        repo,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "base",
    )
    return repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def _blob_sha(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode("ascii") + content).hexdigest()


class FakeApi:
    def __init__(self, *, base_sha: str, base_tree: str, task_sha: str | None = None) -> None:
        self.base_sha = base_sha
        self.base_tree = base_tree
        self.task_sha = task_sha
        self.created_blobs: list[bytes] = []
        self.expected_tree_sha: str | None = None
        self.created_commit_sha = "c" * 40
        self.created_commit: RemoteCommit | None = None
        self.branch_created_at: str | None = None
        self.updated_to: str | None = None
        self.move_base_on_read: int | None = None
        self._base_reads = 0

    def get_branch_commit_sha(self, branch: str) -> str | None:
        if branch == "main":
            self._base_reads += 1
            if self.move_base_on_read is not None and self._base_reads >= self.move_base_on_read:
                return "f" * 40
            return self.base_sha
        return self.task_sha

    def get_commit(self, commit_sha: str) -> RemoteCommit:
        if commit_sha == self.base_sha:
            return RemoteCommit(self.base_sha, self.base_tree, ())
        if self.created_commit is not None and commit_sha == self.created_commit.sha:
            return self.created_commit
        raise AssertionError(f"unexpected commit lookup {commit_sha}")

    def create_blob(self, content: bytes) -> str:
        self.created_blobs.append(content)
        return _blob_sha(content)

    def create_tree(self, base_tree_sha: str, changes: tuple[Any, ...]) -> str:
        assert base_tree_sha == self.base_tree
        assert changes
        assert self.expected_tree_sha is not None
        return self.expected_tree_sha

    def create_commit(self, message: str, tree_sha: str, parent_sha: str) -> str:
        assert message
        self.created_commit = RemoteCommit(self.created_commit_sha, tree_sha, (parent_sha,))
        return self.created_commit_sha

    def create_branch(self, branch: str, sha: str) -> None:
        assert self.task_sha is None
        self.branch_created_at = sha
        self.task_sha = sha

    def update_branch(self, branch: str, sha: str) -> None:
        self.updated_to = sha
        self.task_sha = sha


def test_build_candidate_uses_exact_staged_tree_and_blob_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha, base_tree = _make_repo(tmp_path)
    large = ("αβγ dashboard\n" * 9000).encode("utf-8")
    (repo / "keep.txt").write_bytes(large)
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    (repo / "remove.txt").unlink()
    _git(repo, "add", "--all")
    monkeypatch.chdir(repo)

    candidate = build_candidate(
        base_commit_sha=base_sha, base_tree_sha=base_tree, branch="feat/test"
    )

    assert candidate.expected_tree_sha == _git(repo, "write-tree")
    by_path = {change.path: change for change in candidate.changes}
    assert set(by_path) == {"keep.txt", "new.txt", "remove.txt"}
    assert by_path["keep.txt"].new_sha == _blob_sha(large)
    assert by_path["keep.txt"].size == len(large)
    assert by_path["remove.txt"].new_sha is None


def test_preflight_refuses_dirty_or_previously_moved_task_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha, base_tree = _make_repo(tmp_path)
    (repo / "keep.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    (repo / "keep.txt").write_text("unstaged\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    api = FakeApi(base_sha=base_sha, base_tree=base_tree)
    with pytest.raises(PublicationError, match="unstaged"):
        preflight(api, base_branch="main", task_branch="feat/test")

    (repo / "keep.txt").write_text("staged\n", encoding="utf-8")
    api.task_sha = "e" * 40
    with pytest.raises(PublicationError, match="already points"):
        preflight(api, base_branch="main", task_branch="feat/test")


def test_rest_client_machine_encodes_blob_bytes_without_manual_base64() -> None:
    api = GitHubRestGitDataApi("owner/repo", "token")
    captured: dict[str, Any] = {}
    payload_bytes = ("utf8-π\n" * 10000).encode("utf-8")

    def fake_request(
        method: str, path: str, payload: dict[str, Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        captured.update({"method": method, "path": path, "payload": payload})
        assert payload is not None
        decoded = base64.b64decode(payload["content"], validate=True)
        return {"sha": _blob_sha(decoded)}

    api._request_json = fake_request  # type: ignore[method-assign]
    returned = api.create_blob(payload_bytes)

    assert returned == _blob_sha(payload_bytes)
    assert captured["payload"]["encoding"] == "base64"
    assert base64.b64decode(captured["payload"]["content"], validate=True) == payload_bytes


def test_rest_client_ref_paths_preserve_branch_slashes() -> None:
    api = GitHubRestGitDataApi("owner/repo", "token")
    seen: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str, path: str, payload: dict[str, Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        seen.append((method, path, payload))
        if method == "GET":
            return {"object": {"sha": "a" * 40}}
        return {}

    api._request_json = fake_request  # type: ignore[method-assign]
    assert api.get_branch_commit_sha("feat/exact tree") == "a" * 40
    api.update_branch("feat/exact tree", "b" * 40)

    assert seen[0][1] == "/repos/owner/repo/git/ref/heads/feat/exact%20tree"
    assert seen[1][1] == "/repos/owner/repo/git/refs/heads/feat/exact%20tree"
    assert seen[1][2] == {"sha": "b" * 40, "force": False}


def test_publish_verifies_every_layer_before_non_force_ref_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha, base_tree = _make_repo(tmp_path)
    content = ("large exact bytes π\n" * 8000).encode("utf-8")
    (repo / "keep.txt").write_bytes(content)
    _git(repo, "add", "--all")
    monkeypatch.chdir(repo)
    expected_tree = _git(repo, "write-tree")
    api = FakeApi(base_sha=base_sha, base_tree=base_tree)
    api.expected_tree_sha = expected_tree

    result = publish(api, base_branch="main", task_branch="feat/test", message="feat: test")

    assert api.created_blobs[-1] == content
    assert len(api.created_blobs) == 2  # deterministic preflight probe + feature blob
    assert api.branch_created_at == base_sha
    assert api.updated_to == result.commit_sha
    assert result.tree_sha == expected_tree
    assert result.base_commit_sha == base_sha


def test_publish_fails_closed_before_ref_mutation_on_blob_or_base_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha, base_tree = _make_repo(tmp_path)
    (repo / "keep.txt").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "--all")
    monkeypatch.chdir(repo)
    expected_tree = _git(repo, "write-tree")

    class BadFeatureBlobApi(FakeApi):
        def create_blob(self, content: bytes) -> str:
            self.created_blobs.append(content)
            if len(self.created_blobs) == 1:
                return _blob_sha(content)  # preflight probe succeeds
            return "0" * 40

    bad_blob = BadFeatureBlobApi(base_sha=base_sha, base_tree=base_tree)
    bad_blob.expected_tree_sha = expected_tree
    with pytest.raises(PublicationError, match="remote blob"):
        publish(bad_blob, base_branch="main", task_branch="feat/test", message="feat: test")
    assert bad_blob.branch_created_at is None
    assert bad_blob.updated_to is None

    moved_base = FakeApi(base_sha=base_sha, base_tree=base_tree)
    moved_base.expected_tree_sha = expected_tree
    moved_base.move_base_on_read = 3  # after unreferenced commit creation
    with pytest.raises(PublicationError, match="after commit creation"):
        publish(moved_base, base_branch="main", task_branch="feat/test", message="feat: test")
    assert moved_base.created_commit is not None
    assert moved_base.branch_created_at is None
    assert moved_base.updated_to is None
