from __future__ import annotations

from hashlib import sha256

# ruff: noqa: RUF001 -- Russian operator-facing UI.
from html import escape as _escape
from urllib.parse import urlencode

from harness.git_operations import FILE_LIMIT, GitSnapshot


def escape(value: str, quote: bool = True) -> str:
    """Render unusual POSIX filename bytes without turning them into executable form values."""
    return _escape(value.encode("utf-8", "backslashreplace").decode("utf-8"), quote=quote)


def _browser_name(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def git_fields(state: GitSnapshot, action: str, value: str | None = None) -> str:
    fields = {
        "action": action,
        "workspace_id": state.workspace.workspace_id,
        "expected_token": state.token,
    }
    if value is not None:
        fields["value"] = value
    return "".join(
        f'<input type="hidden" name="{name}" value="{escape(text, quote=True)}">'
        for name, text in fields.items()
    )


def render_git_panel(
    state: GitSnapshot,
    url: str,
    *,
    error: str | None = None,
    draft_action: str = "",
    draft_value: str = "",
) -> str:
    branch = state.branch or "Отдельный коммит (detached HEAD)"
    post_url = url + (
        "?" + urlencode({"q": state.query, "page": state.page})
        if state.query or state.page != 1
        else ""
    )
    blocked = bool(state.file_count or state.operation)
    disabled = " disabled" if blocked else ""
    branches = []
    for item in state.branches:
        current = not item.remote and item.name == state.branch
        busy = item.occupied and not current
        label = (
            "Текущая"
            if current
            else "Открыта в другой папке"
            if busy
            else "Удалённая"
            if item.remote
            else "Локальная"
        )
        actions = ""
        if not _browser_name(item.ref):
            label = "Для этого имени используйте терминал"
        elif not current:
            actions = (
                f'<form method="post" action="{escape(post_url, quote=True)}">'
                + git_fields(state, "git_switch", item.ref)
                + f'<button type="submit"{disabled}>Переключиться</button></form>'
                if not busy
                else ""
            )
            actions += (
                f'<details class="git-merge-choice" id="merge-{sha256(item.ref.encode()).hexdigest()[:16]}"><summary>Слить…</summary><div>'
                + f'<p class="git-direction"><strong>{escape(item.name)}</strong> → <strong>{escape(branch)}</strong></p>'
                + '<p class="section-note">Изменения выбранной ветки попадут в текущую. Исходная ветка сохранится.</p>'
                + f'<form method="post" action="{escape(post_url, quote=True)}">'
                + git_fields(state, "git_merge", item.ref)
                + f'<button type="submit"{disabled if state.branch and state.head else " disabled"}>Влить в {escape(branch)}</button></form></div></details>'
            )
        branches.append(
            '<li class="git-branch-row"><div class="git-branch-name">'
            + f'<strong class="mono" title="{escape(item.name, quote=True)}">{escape(item.name)}</strong><span>{label}</span></div>'
            + f'<div class="git-branch-actions">{actions}</div></li>'
        )
    files = []
    for file in state.files:
        label = "Конфликт" if file.conflict else "Подготовлен" if file.staged else "Не подготовлен"
        if file.staged and file.worktree_status != " ":
            label += " · есть новые изменения"
        stage = ""
        operable = _browser_name(file.path)
        if not operable:
            label += " · для этого имени используйте терминал"
        if operable and (not file.staged or file.worktree_status != " "):
            stage = (
                f'<form method="post" action="{escape(post_url, quote=True)}">'
                + git_fields(state, "git_stage", file.path)
                + f'<button type="submit">{"Подготовить новые изменения" if file.staged else "Подготовить"}</button></form>'
            )
        if operable and file.staged and not state.operation:
            stage += (
                f'<form method="post" action="{escape(post_url, quote=True)}">'
                + git_fields(state, "git_unstage", file.path)
                + '<button type="submit">Убрать из коммита</button></form>'
            )
        files.append(
            f'<li class="git-file-row"><div><strong class="mono">{escape(file.path)}</strong>'
            + f'<span>{label}</span></div><div class="git-file-actions">{stage}</div></li>'
        )
    operation = ""
    if state.operation:
        operation = (
            '<section class="git-warning" role="status"><h2>'
            + ("Слияние требует внимания" if state.operation == "merge" else "Идёт операция Git")
            + "</h2><p>Исправьте конфликты в редакторе, подготовьте исправленные файлы и сделайте коммит."
            + " Отмена слияния уберёт изменения этого слияния и правки, сделанные при разрешении конфликтов.</p>"
            + (
                f'<form method="post" action="{escape(post_url, quote=True)}">'
                + git_fields(state, "git_abort_merge")
                + '<button type="submit">Отменить слияние</button></form>'
                if state.operation == "merge"
                else "<p>Завершите операцию в терминале и обновите страницу.</p>"
            )
            + "</section>"
        )
    notice = (
        f'<div class="git-warning" role="alert"><strong>Операция остановлена</strong><p>{escape(error)}</p></div>'
        if error
        else ""
    )
    create_value = draft_value if draft_action == "git_create" else ""
    commit_value = draft_value if draft_action == "git_commit" else ""
    create_form = (
        f'<details class="git-create"{" open" if draft_action == "git_create" else ""}><summary>+ Новая ветка</summary>'
        + f'<form method="post" action="{escape(post_url, quote=True)}">'
        + git_fields(state, "git_create")
        + '<label for="git-new-name">Название ветки</label>'
        + f'<input id="git-new-name" name="value" required maxlength="256" placeholder="feature/название" value="{escape(create_value, quote=True)}">'
        + f'<p class="section-note">Создать от {escape(branch)} и сразу переключиться.</p>'
        + f'<button type="submit"{" disabled" if state.operation else ""}>Создать и переключиться</button></form></details>'
    )
    pagination = ""
    if state.file_count > FILE_LIMIT:
        total = (state.file_count + FILE_LIMIT - 1) // FILE_LIMIT
        pagination = '<nav class="history-pagination" aria-label="Страницы изменённых файлов">'
        for target, text in ((state.page - 1, "← Назад"), (state.page + 1, "Далее →")):
            if 1 <= target <= total:
                pagination += f'<a href="{escape(url + "?" + urlencode({"q": state.query, "page": target}), quote=True)}">{text}</a>'
        pagination += f"<span>{state.page} / {total}</span></nav>"
    return (
        f'<div class="git-workbench{" has-changes" if state.file_count else ""}" id="git-workbench">'
        + notice
        + operation
        + '<section class="git-status-bar"><div><span>Текущая ветка</span>'
        + f'<strong class="mono">{escape(branch)}</strong><small>{escape((state.head or "Нет коммитов")[:12])}</small></div>'
        + f"<p>{state.file_count} изменённых файлов · {state.staged_count} подготовлено</p></section>"
        + (
            '<p class="git-switch-hint">Перед переключением или слиянием сделайте коммит. Новую ветку можно создать вместе с текущими изменениями.</p>'
            if blocked and not state.operation
            else ""
        )
        + '<div class="git-columns"><section class="panel git-branches"><div class="panel-head"><h2>Ветки</h2>'
        + f'<span class="section-note">Всего {state.branch_count}</span></div><div class="panel-body">'
        + f'<form class="git-search" method="get" action="{escape(url, quote=True)}">'
        + f'<label for="git-query">Найти ветку</label><div><input id="git-query" name="q" type="search" maxlength="256" value="{escape(state.query, quote=True)}" placeholder="Название или часть названия…">'
        + '<button type="submit">Найти</button></div></form>'
        + f'<p class="section-note">{"Результаты поиска" if state.query else "Текущая и с недавними коммитами"} · {len(state.branches)} из {state.matching_branch_count}. Удалённые ветки показаны по локальным данным Git.</p>'
        + create_form
        + '<ul class="git-branch-list">'
        + "".join(branches)
        + "</ul>"
        + (
            '<p class="section-note">Уточните название, чтобы найти нужную ветку среди остальных.</p>'
            if state.matching_branch_count > len(state.branches)
            else ""
        )
        + (
            '<p class="empty-state">Ветки не найдены. Измените запрос или создайте новую.</p>'
            if not branches
            else ""
        )
        + "</div></section>"
        + '<section class="panel git-changes"><div class="panel-head"><h2>Изменения и коммит</h2>'
        + f'<span class="section-note">{state.file_count} файлов</span></div><div class="panel-body">'
        + '<p class="section-note">Подготовьте все изменения одной кнопкой или выберите отдельные файлы. В коммит попадут только подготовленные изменения.</p>'
        + '<div class="git-file-actions">'
        + f'<form method="post" action="{escape(post_url, quote=True)}">'
        + git_fields(state, "git_stage_all")
        + f'<button type="submit"{" disabled" if not state.file_count else ""}>Подготовить все изменения</button></form>'
        + f'<form method="post" action="{escape(post_url, quote=True)}">'
        + git_fields(state, "git_unstage_all")
        + f'<button type="submit"{" disabled" if not state.staged_count or state.operation else ""}>Убрать всё из коммита</button></form></div>'
        + '<p class="section-note">«Все» включает файлы на всех страницах этого проекта. Убрать из коммита — сохранить правки в файлах.</p>'
        + '<ul class="git-file-list">'
        + "".join(files)
        + "</ul>"
        + pagination
        + ('<p class="empty-state">Всё сохранено. Рабочая папка чистая.</p>' if not files else "")
        + f'<form class="git-commit" method="post" action="{escape(post_url, quote=True)}">'
        + git_fields(state, "git_commit")
        + '<label for="git-message">Сообщение коммита</label>'
        + f'<textarea id="git-message" name="value" required maxlength="2048" rows="3" placeholder="Что изменилось и зачем">{escape(commit_value)}</textarea>'
        + f'<button class="primary" type="submit"{" disabled" if state.conflict_count or (not state.staged_count and state.operation != "merge") or state.branch is None or state.operation not in {None, "merge"} else ""}>Сделать коммит · {state.staged_count} файлов</button>'
        + "</form></div></section></div></div>"
    )
