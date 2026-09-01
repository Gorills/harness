from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

from harness.dashboard import DashboardServerManager
from harness.storage import initialize_database


def _assert_hardened(error: HTTPError) -> None:
    assert error.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in error.headers["Content-Security-Policy"]
    assert error.headers["X-Content-Type-Options"] == "nosniff"
    assert error.headers["Referrer-Policy"] == "no-referrer"
    assert "form-action 'self'" in error.headers["Content-Security-Policy"]
    assert error.headers.get("Server") is None
    assert error.headers.get("Date") is None


def test_dashboard_hardens_unscoped_and_unsupported_http_responses(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        root = f"http://127.0.0.1:{parsed.port}/"
        with urlopen(root, timeout=2) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"

        with pytest.raises(HTTPError) as unknown_error:
            urlopen(f"{root}not-a-dashboard/", timeout=2)
        assert unknown_error.value.code == 404
        _assert_hardened(unknown_error.value)

        post = Request(
            url,
            data=b"action=cancel",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with pytest.raises(HTTPError) as post_error:
            urlopen(post, timeout=2)
        assert post_error.value.code == 403
        _assert_hardened(post_error.value)
        assert "Действие не принято" in post_error.value.read().decode("utf-8")

        for method in ("HEAD", "PUT"):
            request = Request(url, method=method)
            with pytest.raises(HTTPError) as method_error:
                urlopen(request, timeout=2)
            assert method_error.value.code == 501
            _assert_hardened(method_error.value)
    finally:
        manager.close()
