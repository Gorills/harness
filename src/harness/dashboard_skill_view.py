# ruff: noqa: RUF001
"""HTML presentation of skill policy, exact previews, and delivery facts."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape

from harness import dashboard_i18n as labels
from harness.dashboard_skills import DashboardSkillCatalogItem, DashboardSkillsSnapshot
from harness.skill_policy import (
    MANAGED_PROJECT_SKILL_FACETS,
    ProjectSkillFacetMode,
    ProjectSkillPolicy,
)

_FACETS = {
    "backend-service": labels.SKILL_SCOPE_BACKEND,
    "web-frontend": labels.SKILL_SCOPE_FRONTEND,
    "mobile-app": labels.SKILL_SCOPE_MOBILE,
    "database-backed": labels.SKILL_SCOPE_DATABASE,
    "godot-project": labels.SKILL_SCOPE_GODOT,
    "containerized": labels.SKILL_SCOPE_CONTAINERS,
    "observability": labels.SKILL_SCOPE_OBSERVABILITY,
    "ci-pipeline": labels.SKILL_SCOPE_CI,
    "deployment-ops": labels.SKILL_SCOPE_DEPLOYMENT,
}
_MODES = {
    ProjectSkillFacetMode.AUTO: (labels.SKILL_SCOPE_AUTO, labels.SKILL_SCOPE_AUTO_HINT),
    ProjectSkillFacetMode.INCLUDED: (labels.SKILL_SCOPE_INCLUDED, labels.SKILL_SCOPE_INCLUDED_HINT),
    ProjectSkillFacetMode.EXCLUDED: (labels.SKILL_SCOPE_EXCLUDED, labels.SKILL_SCOPE_EXCLUDED_HINT),
}
_STATUS = {
    "current": ("Файлы скиллов актуальны", "Доставка проверена по содержимому файлов."),
    "pending": ("Требуется обновление файлов", "Настройка сохранена, доставка ещё не завершена."),
    "conflict": (
        "Конфликт файлов",
        "Пользовательские файлы сохранены. Устраните конфликт и повторите применение.",
    ),
    "source_overlay": (
        "Скиллы этого checkout обновляются локально",
        "Глобальная установка сохраняет изоляцию исходного checkout. Обновите его через scripts/dev harness scan.",
    ),
    "no_host": (
        "Нет подключённого хоста",
        "Настройка сохранена. Для доставки подключите Codex или Cursor.",
    ),
    "error": ("Не удалось проверить доставку", "Проверьте причину ниже и обновите состояние."),
}


def _skill_list(ids: tuple[str, ...], catalog: Mapping[str, DashboardSkillCatalogItem]) -> str:
    if not ids:
        return '<p class="skill-empty">Нет</p>'
    rows = []
    for skill_id in ids:
        item = catalog.get(skill_id)
        description = "" if item is None else item.description
        summary = labels.SKILL_SUMMARIES.get(skill_id, description)
        rows.append(
            "<li><code>"
            + escape(skill_id)
            + "</code><span>"
            + escape(summary)
            + "</span>"
            + (
                '<details class="skill-description"><summary>Условия применения</summary><p>'
                + escape(description)
                + "</p></details>"
                if description
                else ""
            )
            + "</li>"
        )
    return '<ul class="skill-catalog">' + "".join(rows) + "</ul>"


def _reason(reason: str) -> str:
    kind, _, value = reason.partition(":")
    if kind == "facet":
        return "Область: " + _FACETS.get(
            value, "Базовый набор" if value == "software-project" else value
        )
    return {"dependency": "Зависимость: ", "language": "Язык: ", "manifest": "Файл: "}.get(
        kind, ""
    ) + (value or reason)


def _delivery(snapshot: DashboardSkillsSnapshot, action: str) -> str:
    rows = []
    catalog = {item.skill_id: item for item in snapshot.catalog}
    for workspace in snapshot.workspaces:
        title, hint = _STATUS[workspace.projection_status]
        counts = ""
        if workspace.projection_status in {"current", "pending"}:
            counts = (
                f"<p>Выбрано скиллов: {len(workspace.selected)} · "
                f"Совпадающих проекций: {workspace.matching} · "
                f"Требуют обновления: {workspace.missing_or_changed} · "
                f"Ожидают удаления: {workspace.stale_owned}</p>"
            )
        reasons = (
            '<ul class="skill-reasons">'
            + "".join(
                "<li><code>"
                + escape(item.skill_id)
                + "</code>: "
                + escape("; ".join(_reason(reason) for reason in item.reasons))
                + "</li>"
                for item in workspace.selected
            )
            + "</ul>"
        )
        rows.append(
            '<section class="skill-delivery-row" data-skill-status="'
            + escape(workspace.projection_status, quote=True)
            + '"><h4>'
            + escape(str(workspace.workspace_root))
            + "</h4><strong>"
            + escape(title)
            + "</strong><p>"
            + escape(hint)
            + "</p>"
            + counts
            + (
                '<p class="skill-diagnostic">' + escape(workspace.detail) + "</p>"
                if workspace.detail
                else ""
            )
            + '<p class="skill-empty">Хосты: '
            + escape(", ".join(workspace.profiles) or "не подключены")
            + "</p>"
            + "<details><summary>Выбранный набор и причины ("
            + str(len(workspace.selected))
            + ")</summary>"
            + _skill_list(tuple(item.skill_id for item in workspace.selected), catalog)
            + reasons
            + "</details></section>"
        )
    return (
        '<section class="skill-delivery" aria-labelledby="skill-delivery-title">'
        '<h3 id="skill-delivery-title">Фактическая доставка</h3>'
        + "".join(rows)
        + ("<p>У проекта пока нет папок.</p>" if not rows and not snapshot.error else "")
        + '<p class="management-hint">Наличие файлов не подтверждает чтение инструкций текущей беседой. '
        "Если хост не обнаружил изменения, откройте новую сессию; после изменения конфигурации перезапустите хост.</p>"
        + '<a class="btn" href="'
        + escape(action + "#skill-scope", quote=True)
        + '">Обновить состояние</a></section>'
    )


def render_skill_policy(
    project_id: str,
    policy: ProjectSkillPolicy,
    *,
    action: str,
    snapshot: DashboardSkillsSnapshot | None,
) -> str:
    catalog = {} if snapshot is None else {item.skill_id: item for item in snapshot.catalog}
    rows = []
    for facet in MANAGED_PROJECT_SKILL_FACETS:
        current = (
            ProjectSkillFacetMode.INCLUDED
            if facet in policy.included_facets
            else ProjectSkillFacetMode.EXCLUDED
            if facet in policy.excluded_facets
            else ProjectSkillFacetMode.AUTO
        )
        label, hint = _MODES[current]
        members = tuple(item.skill_id for item in catalog.values() if facet in item.facets)
        actions = []
        for mode, (mode_label, _) in _MODES.items():
            if mode is current:
                actions.append(
                    '<span class="skill-scope-current" aria-current="true">'
                    + escape(mode_label)
                    + "</span>"
                )
            previews = []
            if snapshot is not None:
                for workspace in snapshot.workspaces:
                    found = next((item for item in workspace.facets if item.facet == facet), None)
                    candidate = (
                        None
                        if found is None
                        else next(item for item in found.options if item.mode is mode)
                    )
                    previews.append(
                        '<section class="skill-preview-workspace"><h5>'
                        + escape(str(workspace.workspace_root))
                        + "</h5>"
                    )
                    if candidate is None:
                        previews.append(
                            "<p>Предпросмотр недоступен: " + escape(workspace.detail) + "</p>"
                        )
                    else:
                        previews.extend(
                            [
                                "<strong>Добавится</strong>",
                                _skill_list(candidate.add, catalog),
                                "<strong>Уберётся</strong>",
                                _skill_list(candidate.remove, catalog),
                                "<details><summary>Останется: "
                                + str(len(candidate.keep))
                                + "</summary>",
                                _skill_list(candidate.keep, catalog),
                                "</details>",
                            ]
                        )
                    if workspace.projection_status in {
                        "source_overlay",
                        "no_host",
                        "error",
                        "conflict",
                    }:
                        previews.append(
                            '<p class="skill-warning">'
                            + escape(_STATUS[workspace.projection_status][1])
                            + "</p>"
                        )
                    previews.append("</section>")
            fields = {
                "action": "set_skill_scope",
                "project_id": project_id,
                "facet": facet,
                "mode": mode.value,
            }
            form = (
                '<form method="post" action="'
                + escape(action, quote=True)
                + '">'
                + "".join(
                    '<input type="hidden" name="'
                    + escape(name, quote=True)
                    + '" value="'
                    + escape(value, quote=True)
                    + '">'
                    for name, value in fields.items()
                )
            )
            can_preview = (
                snapshot is not None
                and not snapshot.error
                and all(
                    any(item.facet == facet for item in workspace.facets)
                    for workspace in snapshot.workspaces
                )
            )
            disabled = "" if can_preview else " disabled"
            count = len(snapshot.workspaces) if snapshot is not None else 0
            form += (
                '<button class="btn" type="submit" aria-label="'
                + escape("Применить: " + _FACETS[facet] + " — " + mode_label, quote=True)
                + '"'
                + disabled
                + ">Применить к папкам: "
                + str(count)
                + "</button></form>"
            )
            actions.append(
                '<details class="skill-mode-preview" id="skill-preview-'
                + facet
                + "-"
                + mode.value
                + '"><summary>'
                + escape("Повторить применение" if mode is current else mode_label)
                + '</summary><div class="skill-preview-body">'
                + "".join(previews)
                + ("<p>Сначала восстановите предпросмотр состава.</p>" if not can_preview else "")
                + form
                + "</div></details>"
            )
        detected = (
            ""
            if snapshot is None
            else '<p class="skill-empty">'
            + escape(
                "; ".join(
                    workspace.workspace_root.name
                    + ": "
                    + ("обнаружено" if facet in workspace.detected_facets else "не обнаружено")
                    for workspace in snapshot.workspaces
                )
            )
            + "</p>"
        )
        rows.append(
            '<section class="skill-scope-row" data-mode="'
            + current.value
            + '"><div class="skill-scope-copy"><h3>'
            + escape(_FACETS[facet])
            + "</h3><span>"
            + escape(label + " · " + hint)
            + "</span>"
            + detected
            + "<details><summary>Скиллы области ("
            + str(len(members))
            + ")</summary>"
            + _skill_list(members, catalog)
            + '</details></div><div class="skill-scope-actions">'
            + "".join(actions)
            + "</div></section>"
        )
    error = (
        ""
        if snapshot is not None and not snapshot.error
        else '<p class="skill-warning" role="status">'
        + escape(
            "Состав скиллов недоступен. "
            + (snapshot.error if snapshot is not None else "Обновите страницу настроек.")
        )
        + "</p>"
    )
    core = tuple(item.skill_id for item in catalog.values() if "software-project" in item.facets)
    return (
        '<section class="panel skill-scope-panel" id="skill-scope"><div class="panel-head"><h2>'
        + escape(labels.SKILL_SCOPE)
        + '</h2></div><div class="panel-body"><p class="management-hint skill-scope-hint">'
        + escape(labels.SKILL_SCOPE_HINT)
        + "</p>"
        + error
        + (_delivery(snapshot, action) if snapshot is not None else "")
        + '<details class="skill-baseline"><summary>Базовые скиллы ('
        + str(len(core))
        + ")</summary>"
        + "<p>Входят в отбор для каждой зарегистрированной папки. Фактическая доставка показана выше.</p>"
        + _skill_list(core, catalog)
        + '</details><div class="skill-scope-list">'
        + "".join(rows)
        + "</div></div></section>"
    )
