from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from queue import SimpleQueue
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit

import pytest
from test_dashboard_actions import _post
from test_dashboard_navigation_realtime import _database, _git, _read, _review_roundtrip

from harness.dashboard import DashboardServerManager, _parse_sse_view, _view_fingerprint
from harness.git_operations import read_git_snapshot
from harness.storage import connect_database


def _token(html: str) -> str:
    match = re.search(r'name="expected_token" value="([a-f0-9]{64})"', html)
    assert match is not None
    return match[1]


def test_git_page_search_actions_and_live_fingerprint(tmp_path: Path) -> None:
    root, database, _project, workspace = _database(tmp_path)
    _git(root, "branch", "feature/unique")
    invalidations: SimpleQueue[str] = SimpleQueue()
    manager = DashboardServerManager(database, workspace_invalidations=invalidations)
    try:
        base = manager.get_url()
        url = base + f"workspaces/{workspace}/git/?" + urlencode({"q": "unique"})
        status, headers, html = _read(url)
        assert status == 200
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "feature/unique" in html and "git_switch" in html
        events = re.search(r'data-events-url="([^"]+)"', html)
        assert events is not None
        view, identity, query, snapshot, page = _parse_sse_view(urlsplit(unescape(events[1])).query)
        assert (view, identity, query, page) == ("git", workspace, "unique", 1)
        assert snapshot == _view_fingerprint(database, view, identity, query, page)
        origin = base.rstrip("/")
        fields: dict[str, str | int] = {
            "action": "git_switch",
            "workspace_id": workspace,
            "expected_token": _token(html),
            "value": "refs/heads/feature/unique",
        }
        post_status, post_headers, _body = _post(url, fields, origin=origin)
        assert post_status == 303 and post_headers["Location"].endswith("?q=unique")
        assert invalidations.get_nowait() == workspace
        assert _view_fingerprint(database, view, identity, query, page) != snapshot
        with connect_database(database) as connection:
            assert read_git_snapshot(connection, workspace).branch == "feature/unique"
        stale_status, _, stale = _post(url, fields, origin=origin)
        assert stale_status == 409
        assert "Состояние Git изменилось" in stale.decode()
    finally:
        manager.close()


def test_git_posts_reject_origin_schema_and_wrong_page(tmp_path: Path) -> None:
    root, database, project, workspace = _database(tmp_path)
    _git(root, "branch", "feature")
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        url = base + f"workspaces/{workspace}/git/"
        _, _, html = _read(url)
        fields: dict[str, str | int] = {
            "action": "git_switch",
            "workspace_id": workspace,
            "expected_token": _token(html),
            "value": "refs/heads/feature",
        }
        assert _post(url, fields, origin="https://foreign.invalid")[0] == 403
        assert _post(base + f"projects/{project}/", fields, origin=base.rstrip("/"))[0] == 400
        assert _post(url, fields | {"extra": "1"}, origin=base.rstrip("/"))[0] == 400
        assert _post(url, fields | {"expected_token": "bad"}, origin=base.rstrip("/"))[0] == 400
        with pytest.raises(HTTPError) as repeated:
            _read(url + "?q=one&q=two")
        assert repeated.value.code == 400
        with pytest.raises(HTTPError) as unknown:
            _read(url + "?scope=active")
        assert unknown.value.code == 400
        with connect_database(database) as connection:
            assert read_git_snapshot(connection, workspace).branch == "main"
    finally:
        manager.close()


def test_bulk_git_buttons_use_snapshot_schema_and_preserve_files(tmp_path: Path) -> None:
    root, database, _project, workspace = _database(tmp_path)
    (root / "tracked.txt").write_text("changed\n")
    (root / "new.txt").write_text("new\n")
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        url = base + f"workspaces/{workspace}/git/"
        _, _, html = _read(url)
        assert "Подготовить все изменения" in html
        assert "Убрать всё из коммита" in html
        fields: dict[str, str | int] = {
            "action": "git_stage_all",
            "workspace_id": workspace,
            "expected_token": _token(html),
        }
        origin = base.rstrip("/")
        assert _post(url, fields, origin="https://foreign.invalid")[0] == 403
        assert _post(url, fields | {"value": "new.txt"}, origin=origin)[0] == 400
        assert _post(url, fields, origin=origin)[0] == 303
        with connect_database(database) as connection:
            assert read_git_snapshot(connection, workspace).staged_count == 2
        _, _, html = _read(url)
        assert "Код завершения: 0" in html and "git add --all" in html
        fields.update(action="git_unstage_all", expected_token=_token(html))
        assert _post(url, fields, origin=origin)[0] == 303
        with connect_database(database) as connection:
            assert read_git_snapshot(connection, workspace).staged_count == 0
        assert (root / "tracked.txt").read_text() == "changed\n"
        assert (root / "new.txt").read_text() == "new\n"
    finally:
        manager.close()


def test_task_branch_prompt_is_read_only_and_search_opens_its_exact_branch(tmp_path: Path) -> None:
    root, database, _project, workspace = _database(tmp_path)
    _git(root, "switch", "-c", "task-branch")
    task = _review_roundtrip(database, workspace)
    _git(root, "switch", "main")
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        _, _, html = _read(base + f"tasks/{task.task_id}/")
        assert "Переключиться на ветку задачи?" in html
        assert f"/workspaces/{workspace}/git/?q=task-branch" in html
        with connect_database(database) as connection:
            assert read_git_snapshot(connection, workspace).branch == "main"
        _, _, home = _read(base)
        assert f"/workspaces/{workspace}/git/" in home
        assert 'class="git-entry"' in home
    finally:
        manager.close()


def test_git_failure_keeps_message_and_returns_updated_state(tmp_path: Path) -> None:
    root, database, _project, workspace = _database(tmp_path)
    (root / "tracked.txt").write_text("changed\n")
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        url = base + f"workspaces/{workspace}/git/"
        _, _, html = _read(url)
        fields: dict[str, str | int] = {
            "action": "git_commit",
            "workspace_id": workspace,
            "expected_token": _token(html),
            "value": "My draft <commit>",
        }
        status, _, payload = _post(url, fields, origin=base.rstrip("/"))
        assert status == 409
        assert "My draft &lt;commit&gt;" in payload.decode()
        assert "Подготовить" in payload.decode()
    finally:
        manager.close()


def test_visible_git_navigation_publish_push_and_console(tmp_path: Path) -> None:
    from test_git_transport import _remote

    root, database, workspace, remote = _remote(tmp_path)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        url = base + f"workspaces/{workspace}/git/"
        _, _, home = _read(base)
        assert 'class="project-git-button"' in home
        _, _, html = _read(url)
        assert 'aria-current="page">Git</a>' in html
        assert "Опубликовать и связать" in html
        assert 'name="remote"' in html
        fields: dict[str, str | int] = {
            "action": "git_publish",
            "workspace_id": workspace,
            "expected_token": _token(html),
            "remote": "origin",
            "value": "published",
        }
        origin = base.rstrip("/")
        assert (
            _post(url, fields | {"remote": "https://foreign.invalid/repo"}, origin=origin)[0] == 409
        )
        assert _post(url, fields, origin=origin)[0] == 303
        _, _, html = _read(url)
        assert "origin/published" in html
        assert "Получить · Pull" in html and "Отправить · Push" in html
        assert "--set-upstream" in html and "Код завершения: 0" in html
        fields = {"action": "git_push", "workspace_id": workspace, "expected_token": _token(html)}
        assert _post(url, fields, origin=origin)[0] == 303
        _, _, html = _read(url)
        assert "up to date" in html
        assert "Команда выполнена" in html
        assert (remote / "refs/heads/published").exists()
        assert (root / "tracked.txt").read_text() == "baseline\n"
    finally:
        manager.close()
