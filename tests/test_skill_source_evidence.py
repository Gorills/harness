from __future__ import annotations

import ast
import json
import sqlite3
import warnings
from pathlib import Path

import pytest

import harness.skills as skills_module
from harness.builtin_skills import sync_builtin_skills
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.skills import detect_workspace_stack, load_skill_registry, resolve_workspace_skills
from harness.storage import connect_database, initialize_database


def _workspace(tmp_path: Path, files: dict[str, str]) -> tuple[Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir()
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, connection, workspace.workspace_id


def _embedded_assets() -> str:
    css = ":root { color: white; }\nbody { background: black; }\nmain { display: grid; }\n" * 12
    js = (
        "document.querySelector('main')?.addEventListener('click', () => window.scrollTo(0, 0));\n"
        * 8
    )
    return f"DASHBOARD_CSS = {css!r}\nDASHBOARD_JS = {js!r}\n"


def test_bounded_python_runtime_evidence_selects_relevant_builtin_skills(tmp_path: Path) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname="stdlib-service"\nversion="0.1"\n',
            "src/service/storage.py": (
                "import sqlite3 as db\ndef open_database():\n    return db.connect(':memory:')\n"
            ),
            "src/service/http.py": (
                "from http.server import ThreadingHTTPServer as Server\n"
                "class ApplicationServer(Server):\n    pass\n"
            ),
            "src/service/telemetry.py": (
                "from logging.config import dictConfig as configure\n"
                "def setup_logging():\n    configure({'version': 1})\n"
            ),
            "src/service/assets.py": _embedded_assets(),
        },
    )
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    try:
        stack = detect_workspace_stack(connection, workspace_id)
        selected = {
            item.definition.skill_id
            for item in resolve_workspace_skills(
                connection, workspace_id, load_skill_registry(registry)
            )
        }
    finally:
        connection.close()
    assert {"database-backed", "backend-service", "observability", "web-frontend"} <= stack.facets
    assert {
        "data-integrity",
        "server-application",
        "observability",
        "frontend-design",
        "public-frontend",
    } <= selected


def test_python_imports_comments_docs_and_test_files_do_not_activate_surfaces(
    tmp_path: Path,
) -> None:
    example = (
        "import sqlite3\nimport logging\nfrom http.server import HTTPServer\n"
        "logger = logging.getLogger(__name__)\n"
        "# sqlite3.connect(':memory:'); HTTPServer(('', 0), object)\n"
        "EXAMPLE = \"sqlite3.connect(':memory:'); HTTPServer(('', 0), object)\"\n"
        "if TYPE_CHECKING:\n    sqlite3.connect(':memory:')\n"
        "CSS_SAMPLE = 'body { color: red; }'\n"
    )
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "src/service.py": example,
            "tests/test_service.py": "import sqlite3\nsqlite3.connect(':memory:')\n",
            "docs/example.py": "from http.server import HTTPServer\nHTTPServer(('', 0), object)\n",
            "examples/logging.py": "import logging\nlogging.basicConfig()\n",
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()
    assert stack.facets == frozenset({"software-project"})


def test_changed_python_file_is_not_used_until_index_refresh(tmp_path: Path) -> None:
    root, connection, workspace_id = _workspace(
        tmp_path,
        {"src/storage.py": "import sqlite3\nsqlite3.connect(':memory:')\n"},
    )
    try:
        assert "database-backed" in detect_workspace_stack(connection, workspace_id).facets
        (root / "src" / "storage.py").write_text("import sqlite3\n", encoding="utf-8")
        assert "database-backed" not in detect_workspace_stack(connection, workspace_id).facets
        scan_workspace(connection, workspace_id)
        assert "database-backed" not in detect_workspace_stack(connection, workspace_id).facets
    finally:
        connection.close()


def test_python_source_evidence_obeys_per_file_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {"src/storage.py": "import sqlite3\nsqlite3.connect(':memory:')\n"},
    )
    monkeypatch.setattr(skills_module, "_MAX_PYTHON_EVIDENCE_FILE_BYTES", 20)
    try:
        assert "database-backed" not in detect_workspace_stack(connection, workspace_id).facets
    finally:
        connection.close()


def test_embedded_assets_in_expo_package_do_not_infer_public_web(tmp_path: Path) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "apps/mobile/package.json": json.dumps(
                {"dependencies": {"expo": "55", "react-native": "0.83", "react-dom": "19"}}
            ),
            "apps/mobile/assets.py": _embedded_assets(),
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()
    assert "mobile-app" in stack.facets
    assert "web-frontend" not in stack.facets


def test_python_source_evidence_caps_files_and_bytes_in_index_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_source = "import sqlite3\nsqlite3.connect(':memory:')\n"
    http_source = "from http.server import HTTPServer\nHTTPServer(('', 0), object)\n"
    _, connection, workspace_id = _workspace(
        tmp_path,
        {"src/a_database.py": database_source, "src/b_http.py": http_source},
    )
    try:
        monkeypatch.setattr(skills_module, "_MAX_PYTHON_EVIDENCE_FILES", 1)
        first = detect_workspace_stack(connection, workspace_id)
        assert "database-backed" in first.facets
        assert "backend-service" not in first.facets
        assert detect_workspace_stack(connection, workspace_id).facets == first.facets

        monkeypatch.setattr(skills_module, "_MAX_PYTHON_EVIDENCE_FILES", 128)
        monkeypatch.setattr(
            skills_module, "_MAX_PYTHON_EVIDENCE_TOTAL_BYTES", len(database_source.encode())
        )
        capped = detect_workspace_stack(connection, workspace_id)
        assert "database-backed" in capped.facets
        assert "backend-service" not in capped.facets
    finally:
        connection.close()


def test_python_source_evidence_rejects_grown_file_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, connection, workspace_id = _workspace(
        tmp_path,
        {"src/storage.py": "import sqlite3\nsqlite3.connect(':memory:')\n"},
    )
    monkeypatch.setattr(skills_module, "_MAX_PYTHON_EVIDENCE_FILE_BYTES", 128)
    try:
        (root / "src" / "storage.py").write_text("x" * 129, encoding="utf-8")
        assert "database-backed" not in detect_workspace_stack(connection, workspace_id).facets
    finally:
        connection.close()


def test_python_import_evidence_respects_parameters_and_local_shadowing(tmp_path: Path) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "src/service.py": (
                "import sqlite3 as db\n"
                "from http.server import HTTPServer\n"
                "from logging.config import dictConfig\n"
                "def storage(db):\n    return db.connect(':memory:')\n"
                "def server(HTTPServer):\n    return HTTPServer(('', 0), object)\n"
                "def telemetry(dictConfig):\n    dictConfig({'version': 1})\n"
                "def shadowed():\n    db = object()\n    return db.connect(':memory:')\n"
            )
        },
    )
    try:
        assert detect_workspace_stack(connection, workspace_id).facets == frozenset(
            {"software-project"}
        )
    finally:
        connection.close()


def test_python_imports_do_not_leak_between_functions_or_class_methods(tmp_path: Path) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "src/service.py": (
                "def setup():\n"
                "    import sqlite3 as db\n"
                "    from http.server import HTTPServer\n"
                "    from logging.config import dictConfig\n"
                "def unrelated():\n"
                "    db.connect(':memory:')\n"
                "    HTTPServer(('', 0), object)\n"
                "    dictConfig({'version': 1})\n"
                "class Handler:\n"
                "    from http.server import HTTPServer\n"
                "    def create(self):\n        return HTTPServer(('', 0), object)\n"
            )
        },
    )
    try:
        assert detect_workspace_stack(connection, workspace_id).facets == frozenset(
            {"software-project"}
        )
    finally:
        connection.close()


def test_python_imports_inside_same_function_remain_evidence(tmp_path: Path) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "src/service.py": (
                "def storage():\n"
                "    import sqlite3 as db\n"
                "    return db.connect(':memory:')\n"
                "def server():\n"
                "    from http.server import HTTPServer\n"
                "    return HTTPServer(('', 0), object)\n"
                "def telemetry():\n"
                "    from logging.config import dictConfig\n"
                "    dictConfig({'version': 1})\n"
            )
        },
    )
    try:
        facets = detect_workspace_stack(connection, workspace_id).facets
        assert {"database-backed", "backend-service", "observability"} <= facets
    finally:
        connection.close()


def test_embedded_python_web_assets_in_sibling_app_survive_mobile_suppression(
    tmp_path: Path,
) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "apps/mobile/package.json": json.dumps(
                {"dependencies": {"expo": "55", "react-native": "0.83", "react-dom": "19"}}
            ),
            "apps/mobile/assets.py": _embedded_assets(),
            "apps/server/dashboard_assets.py": _embedded_assets(),
        },
    )
    try:
        facets = detect_workspace_stack(connection, workspace_id).facets
        assert {"mobile-app", "web-frontend"} <= facets
    finally:
        connection.close()


def test_android_manifest_suppresses_embedded_assets_in_same_mobile_project(
    tmp_path: Path,
) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "apps/mobile/android/app/src/main/AndroidManifest.xml": "<manifest />",
            "apps/mobile/assets.py": _embedded_assets(),
            "apps/server/assets.py": _embedded_assets(),
        },
    )
    try:
        assert {"mobile-app", "web-frontend"} <= detect_workspace_stack(
            connection, workspace_id
        ).facets
    finally:
        connection.close()


def test_android_manifest_does_not_turn_its_embedded_mobile_assets_into_web(
    tmp_path: Path,
) -> None:
    _, connection, workspace_id = _workspace(
        tmp_path,
        {
            "apps/mobile/android/app/src/main/AndroidManifest.xml": "<manifest />",
            "apps/mobile/assets.py": _embedded_assets(),
        },
    )
    try:
        facets = detect_workspace_stack(connection, workspace_id).facets
        assert "mobile-app" in facets
        assert "web-frontend" not in facets
    finally:
        connection.close()


def test_invalid_escape_warning_from_foreign_python_source_is_suppressed(tmp_path: Path) -> None:
    source = f"import sqlite3\nPATTERN = '{chr(92)}d'\nsqlite3.connect(':memory:')\n"
    with warnings.catch_warnings(record=True) as baseline:
        warnings.simplefilter("always", SyntaxWarning)
        ast.parse(source)
    assert any(issubclass(item.category, SyntaxWarning) for item in baseline)
    _, connection, workspace_id = _workspace(tmp_path, {"src/storage.py": source})
    try:
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always", SyntaxWarning)
            facets = detect_workspace_stack(connection, workspace_id).facets
        assert "database-backed" in facets
        assert not any(issubclass(item.category, SyntaxWarning) for item in recorded)
    finally:
        connection.close()
