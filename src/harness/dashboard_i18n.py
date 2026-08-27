from __future__ import annotations

from harness.registry import VisibilityMode
from harness.search import SearchMatchKind
from harness.task_checkpoints import TaskEventType
from harness.tasks import TaskState, TaskWaitReason

SKIP_TO_CONTENT = "К содержимому"
BRAND = "Harness"
LIVE_CONNECTING = "Подключаемся"
LIVE_REFRESH = "Обновить"
NAVIGATION = "Навигация"
BREADCRUMB_PROJECTS = "Проекты"
PAGE_PROJECTS = "Проекты"
PAGE_PROJECTS_LEAD = (
    "Что сейчас в работе: проекты, Git-состояние, индекс, задачи и операторское ревью "
    "в одном локальном control plane."
)
METRIC_PROJECTS = "Проекты"
METRIC_ACTIVE = "Активные задачи"
METRIC_REVIEW = "На ревью"
METRIC_INDEX = "Проиндексировано"
METRICS_LABEL = "Сводка"
SECTION_WORKSPACES = "Рабочие копии"
EMPTY_WORKSPACES_TITLE = "Пока нет рабочих копий"
EMPTY_WORKSPACES_HINT = "Откройте Git-репозиторий и выполните harness scan."
EMPTY_PROJECT_WORKSPACES_TITLE = "Пока нет рабочих копий"
EMPTY_PROJECT_WORKSPACES_HINT = "У этого проекта ещё нет зарегистрированных копий."
PROJECT_PREFIX = "Проект"
TASK_FOCUS = "Задача"
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
FEEDBACK_SUMMARY = "Замечание"
FEEDBACK_LABEL = "Что изменить"
FEEDBACK_PLACEHOLDER = "Что должен сделать агент дальше"
FEEDBACK_SUBMIT = "Отправить и продолжить"
GIT_UNAVAILABLE = "Git недоступен"
WORKSPACE_FALLBACK = "копия"
WORKSPACE_STATE = "Состояние"
PROJECT = "Проект"
DIRTY_PATHS = "Изменения"
INDEXED_PATHS = "Индекс"
VISIBILITY = "Режим"
TASK = "Задача"
ACTIONS = "Действия"
NO_ACTIONS = "Сейчас действий нет"
SEARCH_SECTION = "Поиск"
SEARCH_PLACEHOLDER = "Путь, имя файла, идентификатор"
SEARCH_LABEL = "Поиск по индексу"
SEARCH = "Найти"
NO_SEARCH_HITS_TITLE = "Ничего не нашлось"
RECENT_TASKS = "Задачи"
NO_TASKS_TITLE = "Пока нет задач"
REVISION = "рев."
TASK_FACTS = "Данные"
WORKSPACE = "Копия"
STATE = "Статус"
WAIT_REASON = "Причина ожидания"
STACK_HINTS = "Стек"
CREATED = "Создана"
UPDATED = "Обновлена"
TIMELINE = "История"
NEXT = "Дальше"
EVENT_CREATED = "Создана"
EVENT_RESUMED = "Возобновлена"
EVENT_CHECKPOINT = "Контрольная точка"
EVENT_ACCEPTED = "Принята"
EVENT_FEEDBACK = "Замечание"
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
    TaskEventType.CHECKPOINT: EVENT_CHECKPOINT,
    TaskEventType.ACCEPTED: EVENT_ACCEPTED,
    TaskEventType.OPERATOR_FEEDBACK: EVENT_FEEDBACK,
    TaskEventType.CANCELLED: EVENT_CANCELLED,
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


def workspace_count_label(count: int) -> str:
    noun = ru_plural(count, "рабочая копия", "рабочие копии", "рабочих копий")
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


def document_title(label: str) -> str:
    return f"{label} · {BRAND}"
