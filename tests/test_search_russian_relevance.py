from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.retrieval import ProjectSearchScope, search_exact_source_inspection, search_project
from harness.search_text import contains_russian_case_phrase, matching_term_count
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_accept, task_start

_RELEVANCE_CASES = (
    ("задачей", ProjectSearchScope.TASKS, "task:tasks"),
    ("задачами", ProjectSearchScope.TASKS, "task:tasks"),
    ("проверкой", ProjectSearchScope.TASKS, "task:checks"),
    ("проверки", ProjectSearchScope.TASKS, "task:checks"),
    ("мониторингом", ProjectSearchScope.TASKS, "task:monitoring"),
    ("мониторинга", ProjectSearchScope.TASKS, "task:monitoring"),
    ("задачей", ProjectSearchScope.DOCS, "doc:docs/управление-задачами.md"),
    ("проверкой", ProjectSearchScope.DOCS, "doc:docs/проверка.md"),
    ("мониторингом", ProjectSearchScope.DOCS, "doc:docs/мониторинг.md"),
    ("задачей", ProjectSearchScope.CODE, "code:src/check_tasks.py"),
    ("мониторингом", ProjectSearchScope.CODE, "code:src/healthcheck.py"),
    ("мониторингом healthcheck", ProjectSearchScope.CODE, "code:src/healthcheck.py"),
    ("where task validation happens", ProjectSearchScope.CODE, "code:src/check_tasks.py"),
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


@pytest.mark.parametrize(("query", "scope", "expected"), _RELEVANCE_CASES)
def test_russian_english_and_mixed_queries_find_the_expected_top_result(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
    query: str,
    scope: ProjectSearchScope,
    expected: str,
) -> None:
    connection, workspace_id, refs = corpus
    expected_ref = refs.get(expected, expected)
    hits = search_project(connection, workspace_id, query, scope=scope, limit=5)
    assert hits, f"No result for {query!r} in {scope}"
    assert hits[0].ref == expected_ref


@pytest.mark.parametrize(
    "scope", [ProjectSearchScope.TASKS, ProjectSearchScope.DOCS, ProjectSearchScope.CODE]
)
@pytest.mark.parametrize("query", _NEGATIVE_QUERIES)
def test_inflection_does_not_match_a_different_root(
    unrelated_corpus: tuple[sqlite3.Connection, str, dict[str, str]],
    query: str,
    scope: ProjectSearchScope,
) -> None:
    connection, workspace_id, _refs = unrelated_corpus
    assert search_project(connection, workspace_id, query, scope=scope, limit=5) == ()


def test_exact_filename_still_identifies_the_requested_file(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
) -> None:
    connection, workspace_id, _refs = corpus
    hits = search_project(
        connection, workspace_id, "check_tasks.py", scope=ProjectSearchScope.CODE, limit=5
    )
    assert hits[0].ref == "code:src/check_tasks.py"


@pytest.mark.parametrize(("query", "occurrences"), [('"задачами"', 1), ('"задачей"', 0)])
def test_quoted_exact_coverage_does_not_expand_russian_inflections(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
    query: str,
    occurrences: int,
) -> None:
    connection, workspace_id, _refs = corpus
    inspected = search_exact_source_inspection(
        connection, workspace_id, query, scope=ProjectSearchScope.DOCS
    )
    assert inspected.coverage is not None
    assert inspected.coverage.complete is True
    assert inspected.coverage.needle_kind == "quoted_literal"
    assert inspected.coverage.matched_occurrences == occurrences


def test_identifier_exact_coverage_remains_case_sensitive(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
) -> None:
    connection, workspace_id, _refs = corpus
    for query, occurrences in (("check_tasks", 1), ("CHECK_TASKS", 0)):
        inspected = search_exact_source_inspection(
            connection, workspace_id, query, scope=ProjectSearchScope.CODE
        )
        assert inspected.coverage is not None
        assert inspected.coverage.complete is True
        assert inspected.coverage.matched_occurrences == occurrences


def test_task_identifier_lookup_is_unchanged_after_natural_queries(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
) -> None:
    connection, workspace_id, refs = corpus
    reference = refs["task:tasks"]
    hits = search_project(
        connection, workspace_id, reference, scope=ProjectSearchScope.TASKS, limit=5
    )
    assert hits[0].ref == reference


def test_exact_task_title_precedes_case_forms_and_derivations(tmp_path: Path) -> None:
    connection, workspace_id, refs = _corpus(tmp_path / "corpus")
    try:
        exact = _completed_task(connection, workspace_id, "Задачей")
        hits = search_project(
            connection, workspace_id, "задачей", scope=ProjectSearchScope.TASKS, limit=5
        )
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
        first_hits = search_project(
            connection, workspace_id, first, scope=ProjectSearchScope.TASKS, limit=5
        )
        assert first_hits[0].ref == second_ref
        first_ref = _completed_task(connection, workspace_id, first)
        second_hits = search_project(
            connection, workspace_id, second, scope=ProjectSearchScope.TASKS, limit=5
        )
        assert second_hits[0].ref == second_ref
        if reverse_supported:
            assert first_ref in {hit.ref for hit in second_hits}
    finally:
        connection.close()


def test_mixed_query_evidence_contains_the_original_russian_source_words(
    corpus: tuple[sqlite3.Connection, str, dict[str, str]],
) -> None:
    connection, workspace_id, _refs = corpus
    hits = search_project(
        connection,
        workspace_id,
        "мониторингом healthcheck",
        scope=ProjectSearchScope.CODE,
        limit=5,
    )
    assert hits[0].ref == "code:src/healthcheck.py"
    evidence = hits[0].evidence
    assert evidence is not None
    assert "def healthcheck" in evidence.snippet
    assert "Мониторинг серверов" in evidence.snippet
    assert "мониторингом" not in evidence.snippet.casefold()


@pytest.mark.parametrize(
    ("query_form", "source_form"), [("настоящего", "настоящий"), ("операцией", "операция")]
)
def test_case_phrase_fts_and_live_evidence_agree_on_new_endings(
    tmp_path: Path, query_form: str, source_form: str
) -> None:
    root = tmp_path / "corpus"
    connection, workspace_id, _refs = _corpus(root)
    try:
        (root / "repo" / "src" / "wordforms.py").write_text(
            f'def caseprobe():\n    """{source_form}"""\n    return 1\n', encoding="utf-8"
        )
        scan_workspace(connection, workspace_id)
        assert contains_russian_case_phrase((query_form,), source_form) is True
        assert matching_term_count((query_form,), source_form) == 1
        hits = search_project(
            connection,
            workspace_id,
            f"{query_form} caseprobe",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )
        assert hits[0].ref == "code:src/wordforms.py"
        assert hits[0].evidence is not None
        assert source_form in hits[0].evidence.snippet
    finally:
        connection.close()
