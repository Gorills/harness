from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.retrieval import search_tasks
from harness.search_text import contains_russian_case_phrase
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_accept, task_start

_RELEVANCE_CASES = (
    ("задачей", "task:tasks"),
    ("задачами", "task:tasks"),
    ("проверкой", "task:checks"),
    ("проверки", "task:checks"),
    ("мониторингом", "task:monitoring"),
    ("мониторинга", "task:monitoring"),
)

_NEGATIVE_QUERIES = ("мониторингом", "проверкой")


def _completed_task(connection: sqlite3.Connection, workspace_id: str, title: str) -> str:
    task = task_start(connection, workspace_id, title)
    task_accept(connection, workspace_id, task.task_id, expected_revision=task.revision)
    return f"task:{task.task_id}"


def _corpus(
    root: Path, *, unrelated: bool = False
) -> tuple[sqlite3.Connection, str, dict[str, str]]:
    root.mkdir()
    workspace_root = root / "repo"
    workspace_root.mkdir()
    files = (
        {
            "docs/unrelated.md": "Монитор показывает состояние. Провернуть ключ.\n",
            "src/unrelated.py": "# Монитор показывает состояние. Провернуть ключ.\nVALUE = 1\n",
        }
        if unrelated
        else {
            "docs/управление-задачами.md": "Управление задачами проекта.\n",
            "docs/проверка.md": "Проверка резервного копирования.\n",
            "docs/мониторинг.md": "Мониторинг серверов.\n",
            "docs/задачник.md": "Задачник для учебного курса.\n",
            "src/check_tasks.py": (
                'def check_tasks():\n    """Проверка задач. Task validation."""\n    return []\n'
            ),
            "src/healthcheck.py": (
                'def healthcheck():\n    """Мониторинг серверов. Server monitoring."""\n'
                "    return True\n"
            ),
        }
    )
    for relative_path, source in files.items():
        path = workspace_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    database = root / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(
            connection, project_id=project.project_id, path=workspace_root
        )
        scan_workspace(connection, workspace.workspace_id)
        task_titles = (
            (("unrelated", "Монитор и поворот ключа"), ("turn", "Провернуть ключ"))
            if unrelated
            else (
                ("tasks", "Управление задачами"),
                ("checks", "Проверка резервного копирования"),
                ("monitoring", "Мониторинг серверов"),
                ("taskbook", "Задачник для учебного курса"),
            )
        )
        refs = {}
        for name, title in task_titles:
            refs[f"task:{name}"] = _completed_task(connection, workspace.workspace_id, title)
        return connection, workspace.workspace_id, refs
    except Exception:
        connection.close()
        raise


@pytest.fixture(scope="module")
def corpus(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[sqlite3.Connection, str, dict[str, str]]]:
    result = _corpus(tmp_path_factory.mktemp("russian-relevance") / "corpus")
    try:
        yield result
    finally:
        result[0].close()


@pytest.fixture(scope="module")
def unrelated_corpus(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[sqlite3.Connection, str, dict[str, str]]]:
    result = _corpus(tmp_path_factory.mktemp("russian-negative") / "corpus", unrelated=True)
    try:
        yield result
    finally:
        result[0].close()


@pytest.mark.parametrize(("query", "expected"), _RELEVANCE_CASES)
def test_russian_english_and_mixed_queries_find_the_expected_top_result(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
    query: str,
    expected: str,
) -> None:
    connection, _workspace_id, refs = corpus
    expected_ref = refs.get(expected, expected)
    hits = search_tasks(connection, query, limit=5)
    assert hits, f"No Task result for {query!r}"
    assert hits[0].ref == expected_ref


@pytest.mark.parametrize("query", _NEGATIVE_QUERIES)
def test_inflection_does_not_match_a_different_root(
    unrelated_corpus: tuple[sqlite3.Connection, str, dict[str, str]],
    query: str,
) -> None:
    connection, _workspace_id, _refs = unrelated_corpus
    assert search_tasks(connection, query, limit=5) == ()


def test_task_identifier_lookup_is_unchanged_after_natural_queries(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
) -> None:
    connection, _workspace_id, refs = corpus
    reference = refs["task:tasks"]
    hits = search_tasks(connection, reference, limit=5)
    assert hits[0].ref == reference


def test_exact_task_title_precedes_case_forms_and_derivations(tmp_path: Path) -> None:
    connection, workspace_id, refs = _corpus(tmp_path / "corpus")
    try:
        exact = _completed_task(connection, workspace_id, "Задачей")
        hits = search_tasks(connection, "задачей", limit=5)
        assert [hit.ref for hit in hits] == [exact, refs["task:tasks"], refs["task:taskbook"]]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("first", "second"),
    [("операция", "опера"), ("приложение", "приложить"), ("подписание", "подписать")],
)
def test_derivations_are_not_treated_as_case_equivalent_phrases(first: str, second: str) -> None:
    assert contains_russian_case_phrase((first,), second) is False
    assert contains_russian_case_phrase((second,), first) is False


@pytest.mark.parametrize(
    ("first", "second", "reverse_supported"),
    [
        ("хранение", "хранения", True),
        ("значение", "значения", True),
        ("решение", "решения", True),
        ("функция", "функции", True),
        # The established suffix fallback finds данных for данные, not the reverse.
        ("данные", "данных", False),
        ("компания", "компании", True),
        ("настоящего", "настоящий", True),
        ("операцией", "операция", True),
    ],
)
def test_russian_word_form_recall_preserves_supported_directions(
    tmp_path: Path, first: str, second: str, reverse_supported: bool
) -> None:
    connection, workspace_id, _refs = _corpus(tmp_path / "corpus")
    try:
        second_ref = _completed_task(connection, workspace_id, second)
        first_hits = search_tasks(connection, first, limit=5)
        assert first_hits[0].ref == second_ref
        first_ref = _completed_task(connection, workspace_id, first)
        second_hits = search_tasks(connection, second, limit=5)
        assert second_hits[0].ref == second_ref
        if reverse_supported:
            assert first_ref in {hit.ref for hit in second_hits}
    finally:
        connection.close()
