from __future__ import annotations

# ruff: noqa: RUF001 -- Russian operator-facing diagnostics.
from collections.abc import Callable

from harness.git_operations import GitOperationError, GitOperationRequest, GitSnapshot


def _fetch(remote: str, spec: str, execute: Callable[[list[str]], bytes]) -> bytes:
    return execute(
        [
            "fetch",
            "--refmap=",
            "--no-tags",
            "--no-prune",
            "--no-prune-tags",
            "--no-recurse-submodules",
            "--",
            remote,
            spec,
        ]
    )


def run_transport(
    state: GitSnapshot, request: GitOperationRequest, execute: Callable[[list[str]], bytes]
) -> None:
    """Operate on one explicit configured remote and one branch; never use push defaults."""
    remote: str | None
    if request.action == "git_fetch":
        remote = request.value
    else:
        remote = request.remote if request.action == "git_publish" else state.upstream_remote
        if state.branch is None or state.head is None:
            raise GitOperationError("Сначала создайте ветку и сделайте первый коммит.")
    if not remote or remote not in state.remotes or remote.startswith("-"):
        raise GitOperationError(
            "Выберите удалённый репозиторий. Для новой ветки используйте «Опубликовать ветку»."
        )
    if request.action == "git_fetch":
        execute(["check-ref-format", f"refs/remotes/{remote}/branch"])
        _fetch(remote, f"+refs/heads/*:refs/remotes/{remote}/*", execute)
        return
    target = request.value.strip() if request.action == "git_publish" else state.upstream_branch
    if not target or target.startswith("-") or "@{" in target:
        raise GitOperationError("Укажите название удалённой ветки.")
    execute(["check-ref-format", "refs/heads/" + target])
    if request.action == "git_pull":
        if state.file_count:
            raise GitOperationError(
                "Перед Pull сделайте коммит или сохраните изменения в терминале."
            )
        execute(["check-ref-format", f"refs/remotes/{remote}/{target}"])
        try:
            _fetch(remote, f"+refs/heads/{target}:refs/remotes/{remote}/{target}", execute)
        except GitOperationError as exc:
            raise GitOperationError(
                "Не удалось получить ветку с сервера. Проверьте вывод Git ниже.\n" + str(exc)
            ) from exc
        # Explicit refmaps keep configured mirror fetches away from local branches.
        fetched = execute(["rev-parse", "--verify", "FETCH_HEAD"]).decode("ascii").strip()
        head = execute(["rev-parse", "--verify", "HEAD"]).decode("ascii").strip()
        branch = (
            execute(["symbolic-ref", "--quiet", "HEAD"]).decode("utf-8", "surrogateescape").strip()
        )
        dirty = execute(["status", "--porcelain=v1", "--untracked-files=all"])
        if head != state.head or branch != "refs/heads/" + str(state.branch) or dirty:
            raise GitOperationError(
                "Папка или ветка изменилась во время Fetch. Данные получены; проверьте состояние перед Pull."
            )
        arguments = ["merge", "--ff-only", "--no-autostash", "--no-edit", "--", fetched]
    else:
        # Override mirror/follow-tags config; publish only the current branch.
        arguments = [
            "-c",
            f"remote.{remote}.mirror=false",
            "push",
            "--porcelain",
            "--no-force",
            "--no-follow-tags",
            "--recurse-submodules=no",
        ]
        if request.action == "git_publish":
            arguments.append("--set-upstream")
        arguments.extend(["--", remote, f"refs/heads/{state.branch}:refs/heads/{target}"])
    try:
        execute(arguments)
    except GitOperationError as exc:
        detail = str(exc)
        if "fast-forward" in detail or "fetch first" in detail or "[rejected]" in detail:
            hint = "Ветки разошлись или на сервере есть новые коммиты. Обновите ветки и выполните слияние перед Push."
        elif any(
            word in detail.lower()
            for word in (
                "authentication",
                "publickey",
                "could not read username",
                "terminal prompts disabled",
            )
        ):
            hint = "Не удалось войти на сервер. Проверьте SSH-ключи или учётные данные Git в терминале."
        else:
            hint = "Проверьте вывод Git ниже. После ошибки или таймаута обновите состояние перед повтором."
        raise GitOperationError(hint + "\n" + detail) from exc
