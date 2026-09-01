from __future__ import annotations

from harness.registry import VisibilityMode
from harness.search import SearchMatchKind
from harness.task_checkpoints import TaskEventType
from harness.tasks import TaskOperatorStatus, TaskState, TaskWaitReason

SKIP_TO_CONTENT = "К содержимому"
BRAND = "Harness"
WORKSPACE_HOME = "Дашборд"
PROJECTS_NAV = "Проекты"
OPEN_NAVIGATION = "Открыть навигацию"
ALL_PROJECTS = "Все проекты"
CURRENT_TASK = "Текущая задача"
PROJECT_OVERVIEW = "Обзор проекта"
WORKSPACE_OVERVIEW = "Папка"
TASK_OVERVIEW = "Карточка задачи"
LIVE_CONNECTING = "Подключаемся"
LIVE_REFRESH = "Обновить"
NAVIGATION = "Навигация"
BREADCRUMB_PROJECTS = "Все проекты"
PAGE_PROJECTS = "Проекты"
PAGE_PROJECTS_LEAD = "Поиск по всем задачам и последние обновления."
HOME_SEARCH_LABEL = "Поиск по всем задачам"
HOME_SEARCH_PLACEHOLDER = "Задача, ветка, Jira или комментарий"
RECENT_TASKS_HOME = "Последние задачи"
METRIC_PROJECTS = "Проекты"
METRIC_ACTIVE = "Активные задачи"
METRIC_REVIEW = "На ревью"
METRIC_INDEX = "Проиндексировано"
METRICS_LABEL = "Сводка"
SECTION_WORKSPACES = "Папки"
EMPTY_WORKSPACES_TITLE = "Пока нет проектов"
EMPTY_WORKSPACES_HINT = "Откройте Git-репозиторий и выполните harness scan."
EMPTY_PROJECT_WORKSPACES_TITLE = "Пока нет папок"
EMPTY_PROJECT_WORKSPACES_HINT = "У этого проекта ещё нет зарегистрированной папки."
PROJECT_MANAGEMENT = "Управление проектом"
DELETE_PROJECT = "Удалить проект"
DELETE_PROJECT_SUMMARY = "Удаление проекта"
DELETE_PROJECT_HINT = (
    "Harness удалит регистрацию, задачи, знания и индекс проекта. Файлы на диске останутся."
)
DELETE_PROJECT_CONFIRM_LABEL = "Для подтверждения введите УДАЛИТЬ"
DELETE_PROJECT_CONFIRM_VALUE = "УДАЛИТЬ"
WORKSPACE_RELOCATION = "Перенос папки"
WORKSPACE_RELOCATION_SUMMARY = "Проект перенесён в другую папку"
WORKSPACE_RELOCATION_HINT = "Укажите новый абсолютный путь к Git-репозиторию. Задачи и знания сохранятся, индекс будет пересобран. После переноса выполните harness scan в новой папке, чтобы обновить настройки интеграций."
WORKSPACE_RELOCATION_LABEL = "Новый путь"
WORKSPACE_RELOCATION_PLACEHOLDER = "/новый/путь/к/проекту"
WORKSPACE_RELOCATION_SUBMIT = "Обновить путь"
PROJECT_PREFIX = "Проект"
TASK_FOCUS = "Задача"
NEXT_STEP = "Следующий шаг"
OPEN_TASK = "Открыть задачу"
OPEN_WORKSPACE = "Открыть папку"
OPEN_PROJECT = "Открыть проект"
NO_TASK = "Пока нет задачи"
BRANCH = "Ветка"
DETACHED_HEAD = "(detached)"
DIRTY = "Изменения"
INDEX = "Индекс"
MODE = "Режим"
STATE_IDLE = "нет задачи"
STATE_REVIEW = "ревью"
STATE_WORKING = "в работе"
STATE_WAITING = "ожидание"
STATE_COMPLETED = "завершена"
STATE_CANCELLED = "отменена"
VISIBILITY_NORMAL = "обычный"
VISIBILITY_HIDDEN = "скрытый"
VISIBILITY_SET_HIDDEN = "Скрытый"
VISIBILITY_SET_NORMAL = "Обычный"
VISIBILITY_HINT_HIDDEN = "Правила и git-ignore включены. Cursor не блокирует git-команды агента."
VISIBILITY_HINT_NORMAL = "Обычный режим. Публикация в git — по правилам хоста."
ACCEPT = "Принять"
ACTION_REJECTED = "Действие не принято"
CANCEL = "Отменить"
CANCEL_TASK = "Отменить задачу"
REOPEN_TASK = "Открыть заново"
FEEDBACK_SUMMARY = "Замечание"
FEEDBACK_LABEL = "Что изменить"
FEEDBACK_PLACEHOLDER = "Что должен сделать агент дальше"
FEEDBACK_SUBMIT = "Отправить и продолжить"
COMMENT_SUMMARY = "Комментарий"
COMMENT_LABEL = "Комментарий оператора"
COMMENT_PLACEHOLDER = "Контекст, решение или заметка по задаче"
COMMENT_SUBMIT = "Добавить комментарий"
JIRA = "Jira"
JIRA_LABEL = "Ссылка на задачу Jira"
JIRA_PLACEHOLDER = "https://jira.example/browse/PROJECT-123"
JIRA_SAVE = "Сохранить ссылку"
JIRA_CLEAR = "Удалить ссылку"
OPERATOR_STATUS = "Операторский статус"
OPERATOR_STATUS_NONE = "Не задан"
OPERATOR_STATUS_DEPLOY_TEST = "Деплой на тест"
OPERATOR_STATUS_DEPLOY_PROD = "Деплой на прод"
OPERATOR_STATUS_SAVE = "Сохранить статус"
GIT_UNAVAILABLE = "Git недоступен"
WORKSPACE_FALLBACK = "папка"
WORKSPACE_STATE = "Состояние"
PROJECT = "Проект"
DIRTY_PATHS = "Изменения"
INDEXED_PATHS = "Индекс"
VISIBILITY = "Режим"
TASK = "Задача"
ACTIONS = "Действия"
NO_ACTIONS = "Сейчас действий нет"
SEARCH_SECTION = "Поиск"
SEARCH_PLACEHOLDER = "Задача, ветка, Jira, комментарий или путь"
SEARCH_LABEL = "Поиск по задачам и индексу"
SEARCH = "Найти"
NO_SEARCH_HITS_TITLE = "Ничего не нашлось"
RECENT_TASKS = "Задачи"
NO_TASKS_TITLE = "Пока нет задач"
REVISION = "рев."
TASK_FACTS = "Данные"
WORKSPACE = "Папка"
STATE = "Статус"
WAIT_REASON = "Причина ожидания"
STACK_HINTS = "Стек"
CREATED = "Создана"
UPDATED = "Обновлена"
TIMELINE = "История"
NEXT = "Дальше"
EVENT_CREATED = "Создана"
EVENT_RESUMED = "Возобновлена"
EVENT_REOPENED = "Открыта заново"
EVENT_CHECKPOINT = "Контрольная точка"
EVENT_ACCEPTED = "Принята"
EVENT_FEEDBACK = "Замечание"
EVENT_COMMENT = "Комментарий"
EVENT_JIRA_UPDATED = "Ссылка Jira изменена"
EVENT_OPERATOR_STATUS_UPDATED = "Операторский статус изменён"
EVENT_CANCELLED = "Отменена"
UNAVAILABLE_TITLE = "Дашборд недоступен"
UNAVAILABLE_HEADING = "Дашборд недоступен"
WAIT_OPERATOR_REVIEW = "ревью"
WAIT_OPERATOR_INPUT = "ввод оператора"
WAIT_EXTERNAL = "внешнее"
MATCH_EXACT_PATH = "точный путь"
MATCH_EXACT_FILENAME = "имя файла"
MATCH_IDENTIFIER = "идентификатор"
MATCH_SUBSTRING = "подстрока пути"
EM_DASH = "—"

_TASK_STATE_LABELS = {
    TaskState.WORKING.value: STATE_WORKING,
    TaskState.WAITING.value: STATE_WAITING,
    TaskState.COMPLETED.value: STATE_COMPLETED,
    TaskState.CANCELLED.value: STATE_CANCELLED,
}
_VISIBILITY_LABELS = {
    VisibilityMode.NORMAL.value: VISIBILITY_NORMAL,
    VisibilityMode.HIDDEN.value: VISIBILITY_HIDDEN,
}
_WAIT_REASON_LABELS = {
    TaskWaitReason.OPERATOR_REVIEW.value: WAIT_OPERATOR_REVIEW,
    TaskWaitReason.OPERATOR_INPUT.value: WAIT_OPERATOR_INPUT,
    TaskWaitReason.EXTERNAL.value: WAIT_EXTERNAL,
}
_MATCH_KIND_LABELS = {
    SearchMatchKind.EXACT_PATH.value: MATCH_EXACT_PATH,
    SearchMatchKind.EXACT_FILENAME.value: MATCH_EXACT_FILENAME,
    SearchMatchKind.IDENTIFIER_TOKENS.value: MATCH_IDENTIFIER,
    SearchMatchKind.PATH_SUBSTRING.value: MATCH_SUBSTRING,
}
_EVENT_LABELS = {
    TaskEventType.CREATED: EVENT_CREATED,
    TaskEventType.RESUMED: EVENT_RESUMED,
    TaskEventType.REOPENED: EVENT_REOPENED,
    TaskEventType.CHECKPOINT: EVENT_CHECKPOINT,
    TaskEventType.ACCEPTED: EVENT_ACCEPTED,
    TaskEventType.OPERATOR_FEEDBACK: EVENT_FEEDBACK,
    TaskEventType.OPERATOR_COMMENT: EVENT_COMMENT,
    TaskEventType.JIRA_LINK_UPDATED: EVENT_JIRA_UPDATED,
    TaskEventType.OPERATOR_STATUS_UPDATED: EVENT_OPERATOR_STATUS_UPDATED,
    TaskEventType.CANCELLED: EVENT_CANCELLED,
}
_OPERATOR_STATUS_LABELS = {
    TaskOperatorStatus.DEPLOY_TEST.value: OPERATOR_STATUS_DEPLOY_TEST,
    TaskOperatorStatus.DEPLOY_PROD.value: OPERATOR_STATUS_DEPLOY_PROD,
}


def ru_plural(count: int, one: str, few: str, many: str) -> str:
    """Return the Russian plural form for a non-negative count."""
    n = abs(count) % 100
    n1 = n % 10
    if 11 <= n <= 14:
        return many
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def task_state_label(state: str | None, wait_reason: str | None = None) -> str:
    if state is None:
        return STATE_IDLE
    if state == TaskState.WAITING.value and wait_reason == TaskWaitReason.OPERATOR_REVIEW.value:
        return STATE_REVIEW
    return _TASK_STATE_LABELS.get(state, state)


def visibility_label(mode: str) -> str:
    return _VISIBILITY_LABELS.get(mode, mode)


def wait_reason_label(reason: str | None) -> str:
    if reason is None:
        return EM_DASH
    return _WAIT_REASON_LABELS.get(reason, reason)


def match_kind_label(kind: str) -> str:
    return _MATCH_KIND_LABELS.get(kind, kind)


def event_label(event_type: TaskEventType) -> str:
    return _EVENT_LABELS[event_type]


def operator_status_label(status: str | None) -> str:
    if status is None:
        return OPERATOR_STATUS_NONE
    return _OPERATOR_STATUS_LABELS.get(status, status)


def workspace_count_label(count: int) -> str:
    noun = ru_plural(count, "папка", "папки", "папок")
    return f"{count} {noun}"


def event_count_label(count: int) -> str:
    noun = ru_plural(count, "событие", "события", "событий")
    return f"{count} {noun}"


def omitted_events_label(count: int) -> str:
    noun = ru_plural(count, "событие скрыто", "события скрыты", "событий скрыто")
    return f"Ещё {count} {noun}"


def more_paths_label(count: int) -> str:
    return f"+{count} ещё"


def project_crumb(project_id: str) -> str:
    return f"{PROJECT} {project_id[:8]}"


def task_crumb(task_id: str) -> str:
    del task_id
    return TASK


def document_title(label: str) -> str:
    return f"{label} · {BRAND}"
