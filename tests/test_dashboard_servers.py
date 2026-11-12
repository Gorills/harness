from __future__ import annotations

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest
from test_dashboard_actions import _post
from test_dashboard_draft_javascript import _BROWSER_PROBE, _NODE

from harness.dashboard import DashboardServerManager
from harness.dashboard_assets import DASHBOARD_JS
from harness.dashboard_servers import render_servers_content
from harness.servers import ServerError, archive_server, get_server, save_server
from harness.storage import connect_database, initialize_database


class _ServerControls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.kind_options: list[dict[str, str | bool]] = []
        self.fields: dict[str, dict[str, str | None]] = {}
        self.in_kind = False
        self.form_action = ""
        self.page_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("data-server-page-url"):
            self.page_url = str(values["data-server-page-url"])
        if tag == "form":
            self.form_action = ""
        if tag == "input" and values.get("name") == "action":
            self.form_action = str(values.get("value") or "")
        if (
            tag in {"input", "textarea", "select"}
            and values.get("name")
            and self.form_action
            not in {
                "server_site_save",
                "server_site_delete",
                "server_profile_save",
                "server_profile_delete",
            }
            and (values.get("name") != "expected_revision" or self.form_action == "server_save")
        ):
            self.fields[str(values["name"])] = values
        if tag == "select":
            self.in_kind = values.get("name") == "kind"
        elif tag == "option" and self.in_kind:
            self.kind_options.append(
                {
                    "value": values.get("value") or "",
                    "selected": "selected" in values,
                    "defaultSelected": "selected" in values,
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.in_kind = False


@pytest.mark.parametrize("archived", [False, True])
def test_clean_server_kind_defaults_allow_navigation(tmp_path: Path, archived: bool) -> None:
    if _NODE is None:
        pytest.skip("Node is required to execute dashboard JS")
    database = tmp_path / "state.db"
    initialize_database(database)
    connection = connect_database(database)
    save_server(connection, "sample", 0, {"name": "Sample"})
    if archived:
        archive_server(connection, "sample", 1)
    connection.close()
    controls = _ServerControls()
    controls.feed(render_servers_content(database, "sample", {}, saved=archived))
    assert [option["value"] for option in controls.kind_options if option["selected"]] == ["audit"]
    result = subprocess.run(
        [_NODE, "-e", _BROWSER_PROBE],
        input=json.dumps(
            {
                "source": DASHBOARD_JS,
                "scenario": "mutation-server-archive-navigation"
                if archived
                else "navigation-server-detail",
                "renderedOptions": controls.kind_options,
            }
        ),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_server_conflict_renderer_marks_editable_draft_and_refresh_uses_current_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    connection = connect_database(database)
    save_server(connection, "sample", 0, {"name": "Original", "role": "Stored role"})
    save_server(connection, "sample", 1, {"name": "Concurrent name", "role": "Stored role"})
    connection.close()
    controls = _ServerControls()
    controls.feed(
        render_servers_content(
            database,
            "sample",
            {},
            error="Revision changed",
            draft={
                "action": "server_save",
                "expected_revision": "1",
                "name": "Original",
                "role": "Draft role",
            },
        )
    )
    assert controls.fields["role"]["data-recovered-draft"] == "true"
    assert controls.fields["expected_revision"]["value"] == "1"
    assert "data-recovered-draft" not in controls.fields["source"]
    refreshed = _ServerControls()
    refreshed.feed(render_servers_content(database, "sample", {}))
    assert refreshed.fields["expected_revision"]["value"] == "2"
    assert refreshed.fields["name"]["value"] == "Concurrent name"


def test_dashboard_server_crud_persistence_origin_and_stale_feedback(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        parsed = urlsplit(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        assert "Серверов по выбранным условиям нет" in urlopen(base + "servers/").read().decode()
        fields: dict[str, str | int] = {
            "action": "server_save",
            "server_id": "sample",
            "expected_revision": "0",
            "name": "Sample",
            "addresses": "192.0.2.10",
            "domains": "sample.example.invalid",
            "services": "nginx",
            "ssh_alias": "sample-ssh",
            "user": "ops",
            "port": "2222",
        }
        assert _post(base + "servers/", fields, origin=None)[0] == 403
        assert _post(base + "servers/", fields, origin="http://foreign.invalid")[0] == 403
        status, _, html = _post(base + "servers/", fields, origin=origin)
        assert status == 200 and b"Sample" in html
        detail = base + "servers/sample/"
        assert "192.0.2.10" in urlopen(detail).read().decode()
        fields.update(expected_revision="1", name="Edited")
        assert _post(detail, fields, origin=origin)[0] == 200
        status, _, html = _post(detail, {**fields, "name": "Stale draft"}, origin=origin)
        assert status == 409 and b"Stale draft" in html
        connection = connect_database(database)
        assert get_server(connection, "sample")["metadata"]["name"] == "Edited"
        connection.close()
        assert (
            _post(
                detail,
                {
                    "action": "server_record",
                    "server_id": "sample",
                    "expected_revision": "2",
                    "kind": "audit",
                    "summary": "Checked read-only",
                    "checked_at": "2026-10-05T00:00:00Z",
                    "source": "manual",
                },
                origin=origin,
            )[0]
            == 200
        )
        assert (
            _post(
                detail,
                {
                    "action": "server_archive",
                    "server_id": "sample",
                    "expected_revision": "3",
                    "archived": "1",
                },
                origin=origin,
            )[0]
            == 200
        )
        assert b'href="/servers/sample/"' not in urlopen(base + "servers/?q=Edited").read()
        assert b"Edited" in urlopen(base + "servers/?archived=1").read()
    finally:
        manager.close()
    manager = DashboardServerManager(database)
    try:
        html = urlopen(manager.get_url() + "servers/sample/").read().decode()
        assert "Checked read-only" in html and "В архиве" in html
    finally:
        manager.close()


def test_site_and_profile_http_forms_preserve_other_apps_conflicts_and_references(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    connection = connect_database(database)
    save_server(
        connection,
        "shared",
        0,
        {
            "name": "Shared",
            "facts": [{"text": "Keep this fact", "source": "operator", "checked_at": None}],
        },
    )
    connection.close()
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        parsed = urlsplit(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        detail = base + "servers/shared/"
        profile_fields: dict[str, str | int] = {
            "action": "server_profile_save",
            "server_id": "shared",
            "expected_revision": "1",
            "profile_key": "@new",
            "profile_id": "nuxt-test",
            "name": "Nuxt test",
            "environment": "test",
            "stack": "Nuxt | 4.5.2\nNode.js | 24",
            "domain_pattern": "{site}.example.invalid",
            "path_pattern": "/srv/{site}",
            "access_policy": "BasicAuth before publication",
            "steps": "Prepare path\nVerify access",
            "source": "operator",
        }
        assert _post(detail, profile_fields, origin=origin)[0] == 200
        app: dict[str, str | int] = {
            "action": "server_site_save",
            "server_id": "shared",
            "expected_revision": "2",
            "site_key": "@new",
            "site_id": "new",
            "profile_id": "nuxt-test",
            "name": "<script>app-canary</script>",
            "domains": "site.example.invalid",
            "stack": "Nuxt | 4.5.2\nNode.js |",
            "ports": "3000",
            "paths": "/srv/site",
            "runtime": "systemd frontend",
            "access_policy": "BasicAuth; credential reference",
            "source": "operator",
        }
        status, _, html = _post(detail, app, origin=origin)
        assert status == 200
        assert (
            b"<script>app-canary</script>" not in html
            and b"&lt;script&gt;app-canary&lt;/script&gt;" in html
        )
        assert (
            b'name="site_key" value="new"' in html
        )  # explicit ID "new" never aliases the create sentinel
        other: dict[str, str | int] = {
            **app,
            "expected_revision": "3",
            "site_id": "other",
            "name": "Other app",
            "domains": "other.example.invalid",
        }
        assert _post(detail, other, origin=origin)[0] == 200
        edit: dict[str, str | int] = {
            **app,
            "expected_revision": "3",
            "site_key": "new",
            "name": "Unsaved app draft",
        }
        status, _, html = _post(detail, edit, origin=origin)
        assert (
            status == 409
            and b"Unsaved app draft" in html
            and b'data-recovered-draft="true"' in html
        )
        connection = connect_database(database)
        current = get_server(connection, "shared")
        assert len(current["metadata"]["sites"]) == 2 and current["revision"] == 4
        assert current["metadata"]["facts"][0]["text"] == "Keep this fact"
        connection.close()
        assert _post(detail, {**edit, "expected_revision": "4"}, origin=origin)[0] == 200
        profile_delete: dict[str, str | int] = {
            "action": "server_profile_delete",
            "server_id": "shared",
            "expected_revision": "5",
            "profile_key": "nuxt-test",
        }
        assert (
            _post(detail, profile_delete, origin=origin)[0] == 400
        )  # referenced profile cannot disappear
        site_delete: dict[str, str | int] = {
            "action": "server_site_delete",
            "server_id": "shared",
            "expected_revision": "5",
            "site_key": "new",
        }
        assert _post(detail, site_delete, origin=origin)[0] == 200
        connection = connect_database(database)
        current = get_server(connection, "shared")
        assert [site["site_id"] for site in current["metadata"]["sites"]] == ["other"]
        assert current["metadata"]["platform_profiles"][0]["path_pattern"] == "/srv/{site}"
        connection.close()
    finally:
        manager.close()


@pytest.mark.parametrize(
    "action,key_name,key",
    [
        ("server_site_save", "site_key", "front"),
        ("server_profile_save", "profile_key", "nuxt-test"),
    ],
)
def test_native_nested_conflict_does_not_echo_draft_into_other_forms_or_advance_cas(
    tmp_path: Path, action: str, key_name: str, key: str
) -> None:
    from test_server_sites import profile, site

    class Forms(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.forms: list[dict[str, str]] = []
            self.current: dict[str, str] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "form":
                self.current = {}
                self.forms.append(self.current)
            if tag == "input" and self.current is not None and values.get("name"):
                self.current[str(values["name"])] = str(values.get("value") or "")

        def handle_endtag(self, tag: str) -> None:
            if tag == "form":
                self.current = None

    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as c:
        save_server(
            c,
            "shared",
            0,
            {
                "name": "Actual server title",
                "domains": ["whole.example.invalid"],
                "sites": [site()],
                "platform_profiles": [profile()],
            },
        )
        card = get_server(c, "shared")
        save_server(c, "shared", 1, {**card["metadata"], "role": "Concurrent role"})
    forms = Forms()
    forms.feed(
        render_servers_content(
            database,
            "shared",
            {},
            error="Revision changed",
            draft={
                "action": action,
                key_name: key,
                "server_id": "shared",
                "expected_revision": "1",
                "name": "Unsaved nested title",
                "domains": "nested.example.invalid",
                "source": "Nested source",
            },
        )
    )
    main = next(f for f in forms.forms if f.get("action") == "server_save")
    nested = next(f for f in forms.forms if f.get("action") == action and f.get(key_name) == key)
    assert main["name"] == "Actual server title" and main["role"] == "Concurrent role"
    assert main["expected_revision"] == "2"
    assert nested["name"] == "Unsaved nested title" and nested["expected_revision"] == "1"
    refreshed = Forms()
    refreshed.feed(
        render_servers_content(
            database, "shared", {"site_id": "front"} if action == "server_site_save" else {}
        )
    )
    assert (
        next(f for f in refreshed.forms if f.get("action") == action and f.get(key_name) == key)[
            "expected_revision"
        ]
        == "2"
    )


def test_server_ui_clamps_removed_last_child_pages_and_preserves_neighbor_filters(
    tmp_path: Path, isolated_harness_http_ports: None
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        save_server(
            connection,
            "many",
            0,
            {
                "name": "Synthetic page fixture",
                "sites": [
                    {"site_id": f"app-{n:03}", "name": f"Application {n}", "source": "fixture"}
                    for n in range(96)
                ],
                "platform_profiles": [
                    {"profile_id": f"profile-{n:02}", "name": f"Profile {n}", "source": "fixture"}
                    for n in range(13)
                ],
            },
        )
    manager = DashboardServerManager(database)
    try:
        detail = manager.get_url() + "servers/many/"
        origin = "http://" + urlsplit(detail).netloc
        status, _, html = _post(
            detail + "?sites_q=app&sites_offset=95&profiles_offset=8",
            {
                "action": "server_site_delete",
                "server_id": "many",
                "expected_revision": "1",
                "site_key": "app-095",
            },
            origin=origin,
        )
        assert status == 200
        assert b"sites_q=app&amp;sites_offset=90&amp;profiles_offset=8" in html
        assert b'id="site-app-090"' in html and b'id="profile-profile-08"' in html
        with urlopen(detail + "?sites_q=app&sites_offset=95&profiles_offset=8") as response:
            assert "sites_offset=90" in response.url and "profiles_offset=8" in response.url
            assert b'id="site-app-094"' in response.read()
        status, _, html = _post(
            detail + "?sites_q=app&sites_offset=90&profiles_offset=12",
            {
                "action": "server_profile_delete",
                "server_id": "many",
                "expected_revision": "2",
                "profile_key": "profile-12",
            },
            origin=origin,
        )
        assert status == 200 and b"profiles_offset=8" in html
        assert b'id="profile-profile-11"' in html and b'id="site-app-090"' in html
        # Public MCP/domain offsets retain their contract; only the dashboard reconciles them.
        with connect_database(database) as connection:
            card = get_server(connection, "many", sites_offset=95, profiles_offset=12)
            assert card["metadata"]["sites"] == [] and card["sites_page"]["offset"] == 95
            assert card["metadata"]["platform_profiles"] == []
        filters = {"sites_q": "missing", "sites_offset": "95", "profiles_offset": "8"}
        content = render_servers_content(database, "many", filters)
        assert filters == {"sites_q": "missing", "sites_offset": "0", "profiles_offset": "8"}
        assert "По выбранным условиям приложений нет" in content
        assert "Сайты ещё не описаны" not in content
        # A filtered result shrinking from6 to5 must similarly recover its last valid page.
        filters = {"sites_q": "Application 9", "sites_offset": "50", "profiles_offset": "8"}
        content = render_servers_content(database, "many", filters)
        assert filters["sites_offset"] == "5" and 'id="site-app-094"' in content
    finally:
        manager.close()


@pytest.mark.parametrize("kind", ["site", "profile"])
@pytest.mark.parametrize("delete_selected", [True, False])
def test_server_http_delete_reconciles_only_its_confirmed_exact_selection(
    tmp_path: Path, isolated_harness_http_ports: None, kind: str, delete_selected: bool
) -> None:
    from harness.servers import edit_server

    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        save_server(
            connection,
            "selected",
            0,
            {
                "name": "Synthetic selection fixture",
                "sites": [
                    {"site_id": key, "name": f"{key} app", "source": "fixture"}
                    for key in ("selected", "other")
                ],
                "platform_profiles": [
                    {"profile_id": key, "name": key, "source": "fixture"}
                    for key in ("selected", "other")
                ],
            },
        )
        edit_server(connection, "selected", 1, metadata={"role": "Concurrent role"})
    manager = DashboardServerManager(database)
    try:
        detail = manager.get_url() + "servers/selected/"
        origin = "http://" + urlsplit(detail).netloc
        filters = {
            "q": "fixture",
            "archived": "0",
            "environment": "test",
            "offset": "9",
            "before": "999",
            "sites_q": "app",
            "sites_offset": "0",
            "site_id": "selected" if kind == "site" else "other",
            "profiles_offset": "0",
            "profile_id": "selected" if kind == "profile" else "other",
        }
        selected_url = detail + "?" + urlencode(filters)
        with urlopen(selected_url) as response:
            assert response.status == 200
        key = "selected" if delete_selected else "other"
        fields: dict[str, str | int] = {
            "action": f"server_{kind}_delete",
            "server_id": "selected",
            "expected_revision": "1",
            f"{kind}_key": key,
        }
        # Rejected deletion preserves both exact selections and the stale form revision.
        status, _, html = _post(selected_url, fields, origin=origin)
        controls = _ServerControls()
        controls.feed(html.decode())
        assert status == 409
        assert parse_qs(urlsplit(controls.page_url).query) == {
            name: [value] for name, value in filters.items()
        }
        if delete_selected:
            assert b'expected_revision" value="1"' in html
        status, _, html = _post(selected_url, {**fields, "expected_revision": "2"}, origin=origin)
        controls = _ServerControls()
        controls.feed(html.decode())
        assert status == 200 and "Сохранено" in html.decode()
        expected = filters.copy()
        if delete_selected:
            del expected[f"{kind}_id"]
        assert parse_qs(urlsplit(controls.page_url).query) == {
            name: [value] for name, value in expected.items()
        }
        assert f'id="{kind}-{key}"'.encode() not in html
        with urlopen(origin + controls.page_url) as response:
            assert response.status == 200
            assert f'id="{kind}-{key}"'.encode() not in response.read()
        with connect_database(database) as connection:
            current = get_server(connection, "selected")
            assert current["revision"] == 3
            collection = "sites" if kind == "site" else "platform_profiles"
            assert [child[f"{kind}_id"] for child in current["metadata"][collection]] == [
                "other" if delete_selected else "selected"
            ]
            # Missing exact selections keep their existing domain/MCP contract.
            with pytest.raises(ServerError):
                get_server(
                    connection,
                    "selected",
                    site_id=key if kind == "site" else None,
                    profile_id=key if kind == "profile" else None,
                )
        if delete_selected:
            with pytest.raises(HTTPError) as error:
                urlopen(selected_url)
            assert error.value.code == 404
            error.value.close()
    finally:
        manager.close()


def test_neighbor_draft_revision_from_production_js_prevents_real_sqlite_lost_update(
    tmp_path: Path,
) -> None:
    if _NODE is None:
        pytest.skip("Node is required to execute dashboard JS")
    from harness.servers import ServerConflictError, edit_server

    result = subprocess.run(
        [_NODE, "-e", _BROWSER_PROBE],
        input=json.dumps({"source": DASHBOARD_JS, "scenario": "neighbor-cas-site-success"}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    stale_revision = int(json.loads(result.stdout)["neighbor_revisions"][0])
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        save_server(connection, "shared", 0, {"name": "Fixture"})
        for revision in range(1, 7):
            edit_server(connection, "shared", revision, metadata={"role": f"fixture {revision}"})
        edit_server(
            connection,
            "shared",
            7,
            site={
                "site_id": "neighbor",
                "name": "Concurrent authoritative name",
                "source": "fixture",
            },
        )
        for _ in range(2):
            with pytest.raises(ServerConflictError):
                edit_server(
                    connection,
                    "shared",
                    stale_revision,
                    site={"site_id": "neighbor", "name": "Stale UI draft", "source": "fixture"},
                )
        current = get_server(connection, "shared", site_id="neighbor", history_limit=0)
        assert current["revision"] == 8
        assert current["metadata"]["sites"][0]["name"] == "Concurrent authoritative name"
