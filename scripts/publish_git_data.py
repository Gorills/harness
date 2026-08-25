from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_API_VERSION = "2026-03-10"
_ZERO_SHA = "0" * 40
_ALLOWED_BLOB_MODES = {"100644", "100755", "120000"}
_ALLOWED_GITLINK_MODE = "160000"
_WRITE_PROBE = b"harness exact-tree publication preflight\n"


class PublicationError(RuntimeError):
    """Raised when exact publication evidence cannot be established."""


@dataclass(frozen=True)
class RemoteCommit:
    sha: str
    tree_sha: str
    parent_shas: tuple[str, ...]


@dataclass(frozen=True)
class Change:
    path: str
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str | None
    object_type: str
    size: int | None


@dataclass(frozen=True)
class Candidate:
    base_commit_sha: str
    base_tree_sha: str
    expected_tree_sha: str
    branch: str
    changes: tuple[Change, ...]
    published_commit_sha: str | None = None


@dataclass(frozen=True)
class PublicationResult:
    commit_sha: str
    tree_sha: str
    branch: str
    base_commit_sha: str


class GitDataApi(Protocol):
    def get_branch_commit_sha(self, branch: str) -> str | None: ...

    def get_commit(self, commit_sha: str) -> RemoteCommit: ...

    def create_blob(self, content: bytes) -> str: ...

    def create_tree(self, base_tree_sha: str, changes: tuple[Change, ...]) -> str: ...

    def create_commit(self, message: str, tree_sha: str, parent_sha: str) -> str: ...

    def create_branch(self, branch: str, sha: str) -> None: ...

    def update_branch(self, branch: str, sha: str) -> None: ...


class GitHubRestGitDataApi:
    def __init__(
        self, repository: str, token: str, api_url: str = "https://api.github.com"
    ) -> None:
        if repository.count("/") != 1:
            raise PublicationError("repository must use owner/name form")
        if not token:
            raise PublicationError("GitHub token is empty")
        self._repository = repository
        self._token = token
        self._api_url = api_url.rstrip("/")

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        url = f"{self._api_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "harness-exact-tree-publisher",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
        except HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise PublicationError(
                f"GitHub API {method} {path} failed: HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise PublicationError(f"GitHub API {method} {path} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise PublicationError(f"GitHub API {method} {path} timed out") from exc
        if not body:
            return {}
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationError(f"GitHub API {method} {path} returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise PublicationError(f"GitHub API {method} {path} returned a non-object response")
        return decoded

    def get_branch_commit_sha(self, branch: str) -> str | None:
        encoded_branch = quote(branch, safe="/")
        response = self._request_json(
            "GET",
            f"/repos/{self._repository}/git/ref/heads/{encoded_branch}",
            allow_not_found=True,
        )
        if response is None:
            return None
        object_data = response.get("object")
        if not isinstance(object_data, dict):
            raise PublicationError(f"GitHub ref response for {branch!r} lacks object")
        sha = object_data.get("sha")
        if not isinstance(sha, str):
            raise PublicationError(f"GitHub ref response for {branch!r} lacks object.sha")
        return sha

    def get_commit(self, commit_sha: str) -> RemoteCommit:
        response = self._request_json("GET", f"/repos/{self._repository}/git/commits/{commit_sha}")
        assert response is not None
        tree = response.get("tree")
        parents = response.get("parents")
        returned_sha = response.get("sha")
        if not isinstance(returned_sha, str):
            raise PublicationError("GitHub commit response lacks sha")
        if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
            raise PublicationError("GitHub commit response lacks tree.sha")
        if not isinstance(parents, list):
            raise PublicationError("GitHub commit response lacks parents")
        parent_shas: list[str] = []
        for parent in parents:
            if not isinstance(parent, dict) or not isinstance(parent.get("sha"), str):
                raise PublicationError("GitHub commit response contains malformed parent")
            parent_shas.append(parent["sha"])
        return RemoteCommit(returned_sha, tree["sha"], tuple(parent_shas))

    def create_blob(self, content: bytes) -> str:
        # Encoding happens here, directly from the staged Git object bytes. Do not transport
        # hand-built base64 across an agent/tool boundary.
        payload = {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        }
        response = self._request_json("POST", f"/repos/{self._repository}/git/blobs", payload)
        assert response is not None
        sha = response.get("sha")
        if not isinstance(sha, str):
            raise PublicationError("GitHub create-blob response lacks sha")
        return sha

    def create_tree(self, base_tree_sha: str, changes: tuple[Change, ...]) -> str:
        entries: list[dict[str, str | None]] = []
        for change in changes:
            mode = change.new_mode if change.new_sha is not None else change.old_mode
            entries.append(
                {
                    "path": change.path,
                    "mode": mode,
                    "type": change.object_type,
                    "sha": change.new_sha,
                }
            )
        payload = {"base_tree": base_tree_sha, "tree": entries}
        response = self._request_json("POST", f"/repos/{self._repository}/git/trees", payload)
        assert response is not None
        sha = response.get("sha")
        if not isinstance(sha, str):
            raise PublicationError("GitHub create-tree response lacks sha")
        return sha

    def create_commit(self, message: str, tree_sha: str, parent_sha: str) -> str:
        payload = {"message": message, "tree": tree_sha, "parents": [parent_sha]}
        response = self._request_json("POST", f"/repos/{self._repository}/git/commits", payload)
        assert response is not None
        sha = response.get("sha")
        if not isinstance(sha, str):
            raise PublicationError("GitHub create-commit response lacks sha")
        return sha

    def create_branch(self, branch: str, sha: str) -> None:
        payload = {"ref": f"refs/heads/{branch}", "sha": sha}
        self._request_json("POST", f"/repos/{self._repository}/git/refs", payload)

    def update_branch(self, branch: str, sha: str) -> None:
        encoded_branch = quote(branch, safe="/")
        payload = {"sha": sha, "force": False}
        self._request_json(
            "PATCH",
            f"/repos/{self._repository}/git/refs/heads/{encoded_branch}",
            payload,
        )


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], capture_output=True)
    if check and result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PublicationError(f"git {' '.join(args)} failed: {stderr}")
    return result


def _git_text(args: list[str]) -> str:
    return _git(args).stdout.decode("utf-8").strip()


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _assert_clean_worktree() -> None:
    if _git(["diff", "--quiet"], check=False).returncode != 0:
        raise PublicationError(
            "working tree has unstaged tracked changes; stage the exact candidate first"
        )
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"]).stdout
    if untracked:
        paths = [item.decode("utf-8", errors="replace") for item in untracked.split(b"\0") if item]
        raise PublicationError(
            f"working tree has untracked files; stage or remove them first: {paths[:5]}"
        )


def _object_type_for_mode(mode: str) -> str:
    if mode in _ALLOWED_BLOB_MODES:
        return "blob"
    if mode == _ALLOWED_GITLINK_MODE:
        return "commit"
    raise PublicationError(f"unsupported Git mode in publication candidate: {mode}")


def _parse_raw_changes(raw: bytes) -> tuple[Change, ...]:
    if not raw:
        return ()
    fields = raw.split(b"\0")
    changes: list[Change] = []
    index = 0
    while index < len(fields) - 1:
        header = fields[index]
        if not header:
            break
        index += 1
        if index >= len(fields) or not fields[index]:
            raise PublicationError("malformed git diff-tree output: missing path")
        path_bytes = fields[index]
        index += 1
        parts = header.decode("ascii").split()
        if len(parts) != 5 or not parts[0].startswith(":"):
            raise PublicationError(f"malformed git diff-tree header: {header!r}")
        old_mode = parts[0][1:]
        new_mode = parts[1]
        old_sha = parts[2]
        new_sha_raw = parts[3]
        status = parts[4]
        if status not in {"A", "M", "D", "T"}:
            raise PublicationError(
                f"unsupported staged diff status {status!r}; use --no-renames-compatible changes"
            )
        path = path_bytes.decode("utf-8")
        if new_sha_raw == _ZERO_SHA:
            object_type = _object_type_for_mode(old_mode)
            changes.append(Change(path, old_mode, new_mode, old_sha, None, object_type, None))
            continue
        object_type = _object_type_for_mode(new_mode)
        size = None
        if object_type == "blob":
            size = int(_git_text(["cat-file", "-s", new_sha_raw]))
        changes.append(Change(path, old_mode, new_mode, old_sha, new_sha_raw, object_type, size))
    return tuple(changes)


def build_candidate(*, base_commit_sha: str, base_tree_sha: str, branch: str) -> Candidate:
    _assert_clean_worktree()
    _git(["cat-file", "-e", f"{base_tree_sha}^{{tree}}"])
    expected_tree_sha = _git_text(["write-tree"])
    check_result = _git(["diff-tree", "--check", base_tree_sha, expected_tree_sha], check=False)
    if check_result.returncode != 0:
        details = check_result.stdout.decode("utf-8", errors="replace").strip()
        raise PublicationError(f"candidate fails git diff --check semantics: {details}")
    raw = _git(
        ["diff-tree", "--raw", "-r", "-z", "--no-renames", base_tree_sha, expected_tree_sha]
    ).stdout
    changes = _parse_raw_changes(raw)
    if not changes:
        raise PublicationError(
            "candidate has no staged changes relative to the canonical base tree"
        )
    return Candidate(base_commit_sha, base_tree_sha, expected_tree_sha, branch, changes)


def _read_staged_blob(change: Change) -> bytes:
    if change.new_sha is None or change.object_type != "blob":
        raise PublicationError(f"{change.path}: not a staged blob")
    content = _git(["cat-file", "blob", change.new_sha]).stdout
    actual_sha = _git_blob_sha(content)
    if actual_sha != change.new_sha:
        raise PublicationError(
            f"{change.path}: git cat-file bytes hash to {actual_sha}, expected staged {change.new_sha}"
        )
    return content


def preflight(api: GitDataApi, *, base_branch: str, task_branch: str) -> Candidate:
    base_commit_sha = api.get_branch_commit_sha(base_branch)
    if base_commit_sha is None:
        raise PublicationError(f"remote base branch {base_branch!r} does not exist")
    base_commit = api.get_commit(base_commit_sha)
    if base_commit.sha != base_commit_sha:
        raise PublicationError(
            f"remote base commit lookup returned {base_commit.sha}, expected {base_commit_sha}"
        )
    candidate = build_candidate(
        base_commit_sha=base_commit_sha,
        base_tree_sha=base_commit.tree_sha,
        branch=task_branch,
    )
    current_task_sha = api.get_branch_commit_sha(task_branch)
    if current_task_sha not in {None, base_commit_sha}:
        current_task = api.get_commit(current_task_sha)
        if (
            current_task.sha == current_task_sha
            and current_task.tree_sha == candidate.expected_tree_sha
            and current_task.parent_shas == (base_commit_sha,)
        ):
            return Candidate(
                candidate.base_commit_sha,
                candidate.base_tree_sha,
                candidate.expected_tree_sha,
                candidate.branch,
                candidate.changes,
                current_task_sha,
            )
        raise PublicationError(
            f"task branch {task_branch!r} already points to {current_task_sha}, expected absent, base {base_commit_sha}, or the exact candidate tree"
        )

    # Prove Git Data write permission and byte-preserving transport before feature bytes are sent.
    # The probe is content-addressed, unreferenced, and identical on every run.
    expected_probe_sha = _git_blob_sha(_WRITE_PROBE)
    remote_probe_sha = api.create_blob(_WRITE_PROBE)
    if remote_probe_sha != expected_probe_sha:
        raise PublicationError(
            f"Git Data write probe returned {remote_probe_sha}, expected {expected_probe_sha}"
        )
    return candidate


def _reconcile_branch_mutation(
    api: GitDataApi,
    *,
    branch: str,
    expected_sha: str,
    description: str,
    mutate: Callable[[], None],
) -> None:
    try:
        mutate()
    except PublicationError as exc:
        try:
            actual_sha = api.get_branch_commit_sha(branch)
        except PublicationError as reconcile_exc:
            raise PublicationError(
                f"{description} failed and remote ref reconciliation also failed: {reconcile_exc}"
            ) from exc
        if actual_sha == expected_sha:
            return
        raise PublicationError(
            f"{description} failed; remote branch is {actual_sha}, expected {expected_sha}: {exc}"
        ) from exc


def publish(
    api: GitDataApi,
    *,
    base_branch: str,
    task_branch: str,
    message: str,
) -> PublicationResult:
    candidate = preflight(api, base_branch=base_branch, task_branch=task_branch)
    if candidate.published_commit_sha is not None:
        return PublicationResult(
            candidate.published_commit_sha,
            candidate.expected_tree_sha,
            task_branch,
            candidate.base_commit_sha,
        )

    for change in candidate.changes:
        if change.new_sha is None or change.object_type != "blob":
            continue
        content = _read_staged_blob(change)
        remote_sha = api.create_blob(content)
        if remote_sha != change.new_sha:
            raise PublicationError(
                f"{change.path}: remote blob {remote_sha} != staged blob {change.new_sha}; refusing to build tree"
            )

    remote_tree_sha = api.create_tree(candidate.base_tree_sha, candidate.changes)
    if remote_tree_sha != candidate.expected_tree_sha:
        raise PublicationError(
            f"remote tree {remote_tree_sha} != expected staged tree {candidate.expected_tree_sha}; refusing to create commit"
        )

    # Re-check canonical base after all unreferenced object writes and before any commit/ref becomes durable.
    latest_base_sha = api.get_branch_commit_sha(base_branch)
    if latest_base_sha != candidate.base_commit_sha:
        raise PublicationError(
            f"remote base moved from {candidate.base_commit_sha} to {latest_base_sha}; refusing to publish stale candidate"
        )
    current_task_sha = api.get_branch_commit_sha(task_branch)
    if current_task_sha not in {None, candidate.base_commit_sha}:
        raise PublicationError(
            f"task branch moved to {current_task_sha}; expected absent or base {candidate.base_commit_sha}"
        )

    commit_sha = api.create_commit(message, remote_tree_sha, candidate.base_commit_sha)
    commit = api.get_commit(commit_sha)
    if commit.sha != commit_sha:
        raise PublicationError(
            f"created commit lookup returned {commit.sha}, expected {commit_sha}"
        )
    if commit.tree_sha != candidate.expected_tree_sha or commit.parent_shas != (
        candidate.base_commit_sha,
    ):
        raise PublicationError(
            "created commit does not point to the verified tree with the exact canonical base parent"
        )

    latest_base_sha = api.get_branch_commit_sha(base_branch)
    if latest_base_sha != candidate.base_commit_sha:
        raise PublicationError(
            f"remote base moved from {candidate.base_commit_sha} to {latest_base_sha} after commit creation; refusing ref update"
        )

    if current_task_sha is None:
        _reconcile_branch_mutation(
            api,
            branch=task_branch,
            expected_sha=candidate.base_commit_sha,
            description=f"creating task branch {task_branch!r} at the canonical base",
            mutate=lambda: api.create_branch(task_branch, candidate.base_commit_sha),
        )
        current_task_sha = api.get_branch_commit_sha(task_branch)
        if current_task_sha != candidate.base_commit_sha:
            raise PublicationError("new task branch did not resolve to the exact canonical base")

    _reconcile_branch_mutation(
        api,
        branch=task_branch,
        expected_sha=commit_sha,
        description=f"updating task branch {task_branch!r} to the verified commit",
        mutate=lambda: api.update_branch(task_branch, commit_sha),
    )
    final_sha = api.get_branch_commit_sha(task_branch)
    if final_sha != commit_sha:
        raise PublicationError(
            f"task branch resolved to {final_sha}, expected created commit {commit_sha}"
        )
    final_commit = api.get_commit(final_sha)
    if final_commit.sha != final_sha:
        raise PublicationError(
            f"published branch commit lookup returned {final_commit.sha}, expected {final_sha}"
        )
    if final_commit.tree_sha != candidate.expected_tree_sha:
        raise PublicationError("published branch tree differs from the verified expected tree")
    if final_commit.parent_shas != (candidate.base_commit_sha,):
        raise PublicationError(
            "published branch commit parent differs from the verified canonical base"
        )

    return PublicationResult(
        commit_sha, candidate.expected_tree_sha, task_branch, candidate.base_commit_sha
    )


def _token_from_environment() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    raise PublicationError("set GH_TOKEN or GITHUB_TOKEN for GitHub Git Data API publication")


def _candidate_json(candidate: Candidate) -> dict[str, Any]:
    return {
        "base_commit_sha": candidate.base_commit_sha,
        "base_tree_sha": candidate.base_tree_sha,
        "expected_tree_sha": candidate.expected_tree_sha,
        "branch": candidate.branch,
        "published_commit_sha": candidate.published_commit_sha,
        "changes": [
            {
                "path": change.path,
                "old_mode": change.old_mode,
                "new_mode": change.new_mode,
                "sha": change.new_sha,
                "type": change.object_type,
                "size": change.size,
            }
            for change in candidate.changes
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight or publish one exact staged Git tree through GitHub's Git Data API."
    )
    parser.add_argument("action", choices=("preflight", "publish"))
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--branch", required=True, help="task branch to create/update")
    parser.add_argument(
        "--base", default="main", help="canonical remote base branch (default: main)"
    )
    parser.add_argument(
        "--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    parser.add_argument("--message", help="commit message; required for publish")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.action == "publish" and not args.message:
        raise PublicationError("--message is required for publish")
    token = _token_from_environment()
    api = GitHubRestGitDataApi(args.repo, token, args.api_url)
    if args.action == "preflight":
        candidate = preflight(api, base_branch=args.base, task_branch=args.branch)
        print(json.dumps(_candidate_json(candidate), indent=2, sort_keys=True))
        return 0
    result = publish(
        api,
        base_branch=args.base,
        task_branch=args.branch,
        message=args.message,
    )
    print(
        json.dumps(
            {
                "base_commit_sha": result.base_commit_sha,
                "branch": result.branch,
                "commit_sha": result.commit_sha,
                "tree_sha": result.tree_sha,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicationError as exc:
        print(f"publication error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
