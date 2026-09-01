from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from harness.dashboard import load_or_create_dashboard_access_token, read_dashboard_access_token
from harness.hidden_policy import HIDDEN_INSTRUCTION_BODY
from harness.host_adapters import (
    HostIntegrationError,
    HostRegistrationCollisionError,
    HostRegistrationState,
    IntegrationChange,
    codex_skill_projection_surface,
)
from harness.runtime_paths import (
    DASHBOARD_HOST,
    RuntimePathError,
    default_runtime_paths,
    ensure_private_state_directory,
    mcp_http_listen_port,
)
from harness.skills import SkillProjectionSurface
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_CODEX_PROFILE = "codex"
_HOST_PROFILE_ENV = "HARNESS_HOST_PROFILE"
_WORKSPACE_ROOT_ENV = "HARNESS_WORKSPACE_ROOT"
_SERVER_NAME = "harness"
_ISOLATED_DEV_SERVER_NAME = "harness-dev"
_ISOLATED_DEV_COMMAND = "./scripts/dev"
_ISOLATED_DEV_ARGS = ["harness", "mcp"]
_DOGFOOD_COMMAND = "./scripts/dogfood"
_DOGFOOD_ARGS = ["mcp"]
_MCP_HTTP_PATH = "/mcp"
_MCP_HTTP_AUTHORIZATION_HEADER = "Authorization"
_MCP_HTTP_WORKSPACE_HEADER = "X-Harness-Workspace-Root"
_OWNER_MARKER = ".harness-mcp-owner.json"
_OWNER_VERSION = 1
_EXCLUDE_BEGIN = "# BEGIN HARNESS CODEX MCP"
_EXCLUDE_END = "# END HARNESS CODEX MCP"
_EXCLUDE_BODY = ("/.codex/config.toml", f"/.codex/{_OWNER_MARKER}")
_GIT_TIMEOUT_SECONDS = 5.0

CODEX_MCP_FORWARD_ENV_VARS = (
    "HOME",
    "PATH",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
    "HARNESS_SKILL_REGISTRY",
)

_LEGACY_CODEX_BOOTSTRAP_INSTRUCTION_BODY = (
    "Harness is required for repository work. Before broad repository exploration, browser "
    "inspection, or meaningful changes, locate the configured Harness MCP tools—even when they "
    "are deferred or omitted from the initial visible tool list—and call `project_status`. Do not "
    "treat omission from the initial tool list as unavailability.\n\n"
    "Use `project_search` across code, docs, Knowledge, and Task history; expand only selected refs "
    "with `project_context`, then use native host tools. Start or resume a Harness Task before "
    "meaningful changes and checkpoint meaningful progress. Targeted native search remains "
    "allowed.\n"
)

_SNAPSHOT_AWARE_CODEX_BOOTSTRAP_INSTRUCTION_BODY = (
    "Harness is required for repository work. Before broad repository exploration, browser "
    "inspection, or meaningful changes, locate the configured Harness MCP tools—even when they "
    "are deferred or omitted from the initial visible tool list—and call `project_status`. Do not "
    "treat omission from the initial tool list as unavailability. If Harness project "
    "configuration changed after this task started, stop repository work and use a fully "
    "restarted Codex client with a new task; existing instruction snapshots do not refresh.\n\n"
    "Use `project_search` across code, docs, Knowledge, and Task history; expand only selected refs "
    "with `project_context`, then use native host tools. Start or resume a Harness Task before "
    "meaningful changes and checkpoint meaningful progress. Targeted native search remains "
    "allowed.\n"
)

_TASK_BEFORE_CHANGES_CODEX_BOOTSTRAP_INSTRUCTION_BODY = (
    "Harness is required for repository work. `project_status` must be the first repository "
    "action at the start of every Codex task. Before running any shell command, reading or "
    "searching repository files, inspecting with a browser, or making changes, locate the "
    "configured Harness MCP tools—even when they are deferred or omitted from the initial visible "
    "tool list—and call `project_status`. Tool discovery needed to locate and call Harness is the "
    "only action allowed before `project_status`; do not treat initial omission as unavailability. "
    "After Harness config changes, use a fully restarted Codex client and new task; existing "
    "instruction snapshots do not refresh.\n\n"
    "After successful `project_status`, use `project_search`, expand selected refs with "
    "`project_context`, then use native tools. Start or resume a Harness Task before changes and "
    "checkpoint progress. Native search is allowed only after the initial status call.\n"
)

_TASK_BEFORE_DIAGNOSIS_CODEX_BOOTSTRAP_INSTRUCTION_BODY = (
    "Harness is required for repository work. `project_status` must be the first repository "
    "action at the start of every Codex task. Before running any shell command, reading or "
    "searching repository files, inspecting with a browser, or making changes, locate the "
    "configured Harness MCP tools—even when they are deferred or omitted from the initial visible "
    "tool list—and call `project_status`. Tool discovery needed to locate and call Harness is the "
    "only action allowed before `project_status`; do not treat initial omission as unavailability. "
    "After Harness config changes, use a fully restarted Codex client and new task; existing "
    "instruction snapshots do not refresh.\n\n"
    "After successful `project_status`, use `project_search`, expand selected refs with "
    "`project_context`, then use native tools. Start or resume a Harness Task before diagnosis or "
    "edits; on schema error retry, never skip. Checkpoint each logical stage. Native search is "
    "allowed only after the initial status call. Phase shift: complete/wait then new Task.\n"
)

CODEX_BOOTSTRAP_INSTRUCTION_BODY = (
    "Harness is required for repository work. `project_status` must be the first repository "
    "action at the start of every Codex task. Before running any shell command, reading or "
    "searching repository files, inspecting with a browser, or making changes, locate the "
    "configured Harness MCP tools—even when they are deferred or omitted from the initial visible "
    "tool list—and call `project_status`. Tool discovery needed to locate and call Harness is the "
    "only action allowed before `project_status`; do not treat initial omission as unavailability. "
    "After Harness config changes, use a fully restarted Codex client and new task; existing "
    "instruction snapshots do not refresh.\n\n"
    "After successful `project_status`, start or resume a Harness Task before diagnosis or "
    "edits; on schema error retry, never skip. Then use `project_search` before broad native "
    "exploration. `project_context` only for selected semantic refs; a code/doc path may be "
    "read natively. Checkpoint each logical stage. Phase shift: complete/wait then new Task.\n"
)

_OWNED_CODEX_BOOTSTRAP_BASES = (
    _LEGACY_CODEX_BOOTSTRAP_INSTRUCTION_BODY,
    _SNAPSHOT_AWARE_CODEX_BOOTSTRAP_INSTRUCTION_BODY,
    _TASK_BEFORE_CHANGES_CODEX_BOOTSTRAP_INSTRUCTION_BODY,
    _TASK_BEFORE_DIAGNOSIS_CODEX_BOOTSTRAP_INSTRUCTION_BODY,
    CODEX_BOOTSTRAP_INSTRUCTION_BODY,
)
_OWNED_CODEX_DEVELOPER_INSTRUCTIONS = frozenset(
    (*_OWNED_CODEX_BOOTSTRAP_BASES, HIDDEN_INSTRUCTION_BODY)
) | frozenset(f"{base}\n{HIDDEN_INSTRUCTION_BODY}" for base in _OWNED_CODEX_BOOTSTRAP_BASES)

CODEX_MCP_MISSING_WORKSPACE_ROOT_MESSAGE = (
    "Codex MCP did not receive a Workspace root. Production Harness MCP must be "
    "initialized from the trusted project .codex/config.toml with an explicit "
    "X-Harness-Workspace-Root header. Re-run `harness scan` for this Workspace and restart "
    "the Codex client."
)


def codex_mcp_forward_env_vars(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return configured bounded variables that Codex can forward without warnings."""
    values = os.environ if environment is None else environment
    return tuple(name for name in CODEX_MCP_FORWARD_ENV_VARS if values.get(name))


@dataclass(frozen=True, slots=True)
class CodexProjectRegistrationDiagnostic:
    """Read-only state for one project-scoped Codex Harness MCP registration."""

    path: Path
    state: HostRegistrationState
    expected_endpoint: str
    configured_endpoint: str | None
    configured_workspace_root: str | None
    harness_owned: bool
    preflight_error: str | None = None
    isolated_development: bool = False


@dataclass(frozen=True, slots=True)
class _OwnerMarker:
    workspace_root: str


@dataclass(frozen=True, slots=True)
class CodexAdapter:
    """Codex project-scoped MCP configuration with explicit Workspace identity.

    Harness automatically mutates only a complete ``.codex/config.toml`` container
    proven by its adjacent ownership marker. Existing user or tracked TOML is never
    rewritten; an exact desired server entry plus Harness bootstrap instructions may instead be
    adopted manually.
    """

    executable: Path
    python_executable: Path
    forward_env_vars: tuple[str, ...] = field(default_factory=codex_mcp_forward_env_vars)
    mcp_http_url: str = f"http://{DASHBOARD_HOST}:17375{_MCP_HTTP_PATH}"
    mcp_http_database: Path | None = None
    mcp_http_token: str | None = None

    def __post_init__(self) -> None:
        expected = tuple(
            name for name in CODEX_MCP_FORWARD_ENV_VARS if name in self.forward_env_vars
        )
        if self.forward_env_vars != expected:
            raise ValueError("Codex forwarded environment names must be an ordered known subset")
        if not self.mcp_http_url.startswith(
            f"http://{DASHBOARD_HOST}:"
        ) or not self.mcp_http_url.endswith(_MCP_HTTP_PATH):
            raise ValueError("Codex MCP HTTP URL must be a loopback Harness endpoint")

    @property
    def profile(self) -> str:
        return _CODEX_PROFILE

    def skill_projection_surface(self) -> SkillProjectionSurface:
        return codex_skill_projection_surface()

    def registration_state(self) -> HostRegistrationState:
        """Codex uses project-scoped registration; Harness owns no user-global entry."""
        return HostRegistrationState.ABSENT

    def register_mcp(self) -> IntegrationChange:
        """Leave user-global Codex configuration untouched."""
        return IntegrationChange.UNCHANGED

    def unregister_mcp(self) -> IntegrationChange:
        """Leave user-global Codex configuration untouched."""
        return IntegrationChange.UNCHANGED

    def workspace_hints(self, environment: Mapping[str, str]) -> tuple[WorkspaceHint, ...]:
        configured = environment.get(_WORKSPACE_ROOT_ENV)
        if not configured:
            raise HostIntegrationError(CODEX_MCP_MISSING_WORKSPACE_ROOT_MESSAGE)
        root = _workspace_root(Path(configured))
        return (
            WorkspaceHint(
                path=root,
                source="codex-project-config-root",
                match_mode=WorkspaceHintMatchMode.ROOT,
            ),
        )

    def project_registration_state(
        self, workspace_root: Path, *, hidden: bool = False
    ) -> HostRegistrationState:
        return self.project_registration_diagnostic(workspace_root, hidden=hidden).state

    def project_registration_diagnostic(
        self, workspace_root: Path, *, hidden: bool = False
    ) -> CodexProjectRegistrationDiagnostic:
        root = _workspace_root(workspace_root)
        path = _config_path(root)
        marker = _read_owner_marker(root)
        raw = _read_optional_regular_file(path, label="Codex project config")
        configured_endpoint: str | None = None
        configured_root: str | None = None
        isolated_development = False

        if raw is None:
            state = (
                HostRegistrationState.STALE_OWNED
                if marker is not None
                else HostRegistrationState.ABSENT
            )
        else:
            value = _parse_toml(raw, path)
            entry = _harness_entry(value)
            isolated_entry = _isolated_development_entry(value)
            if marker is None and not hidden and isolated_entry is not None:
                entry = isolated_entry
                isolated_development = True
            configured_endpoint, configured_root = _entry_identity(entry)
            if isolated_development:
                state = (
                    HostRegistrationState.CURRENT
                    if _isolated_development_bootstrap_is_current(value)
                    else HostRegistrationState.FOREIGN
                )
            elif entry is None:
                state = HostRegistrationState.ABSENT
            elif marker is not None:
                if not _config_is_owned_shape(value, root):
                    state = HostRegistrationState.FOREIGN
                elif _config_is_desired(
                    value,
                    root,
                    self.mcp_http_url,
                    self._http_token(),
                    hidden=hidden,
                ):
                    state = HostRegistrationState.CURRENT
                else:
                    state = HostRegistrationState.STALE_OWNED
            elif _manual_config_is_desired(
                value,
                root,
                self.mcp_http_url,
                self._http_token(),
                hidden=hidden,
            ):
                state = HostRegistrationState.CURRENT
            else:
                state = HostRegistrationState.FOREIGN

        preflight_error: str | None = None
        try:
            self.preflight_project_reconcile(root, hidden=hidden)
        except HostIntegrationError as exc:
            preflight_error = str(exc)
        return CodexProjectRegistrationDiagnostic(
            path=path,
            state=state,
            expected_endpoint=self.mcp_http_url,
            configured_endpoint=configured_endpoint,
            configured_workspace_root=configured_root,
            harness_owned=marker is not None,
            preflight_error=preflight_error,
            isolated_development=isolated_development,
        )

    def preflight_project_reconcile(self, workspace_root: Path, *, hidden: bool = False) -> None:
        root = _workspace_root(workspace_root)
        path = _config_path(root)
        marker_path = _marker_path(root)
        marker = _read_owner_marker(root)
        raw = _read_optional_regular_file(path, label="Codex project config")

        if _git_is_tracked(root, marker_path.relative_to(root)):
            raise HostIntegrationError(
                f"tracked Harness Codex ownership marker requires manual cleanup: {marker_path}"
            )
        if _git_is_tracked(root, path.relative_to(root)):
            if raw is not None:
                value = _parse_toml(raw, path)
                if not hidden and _isolated_development_entry(value) is not None:
                    if _isolated_development_bootstrap_is_current(value):
                        return
                    raise HostIntegrationError(
                        "tracked source-checkout .codex/config.toml requires the exact Harness "
                        "Codex bootstrap and local MCP placement; update the checkout and restart "
                        "Codex with a new task"
                    )
                if _manual_config_is_desired(
                    value,
                    root,
                    self.mcp_http_url,
                    self._http_token(),
                    hidden=hidden,
                ):
                    return
            requirement = (
                "an exact manual Harness MCP entry plus the exact Harness Codex bootstrap and "
                "Hidden developer instructions"
                if hidden
                else "an exact manual Harness MCP entry plus the exact Harness Codex bootstrap "
                "developer instructions"
            )
            raise HostIntegrationError(
                f"tracked .codex/config.toml requires {requirement}; "
                "Harness will not modify tracked Codex configuration"
            )
        if raw is None:
            return

        value = _parse_toml(raw, path)
        entry = _harness_entry(value)
        if marker is not None and not _config_is_owned_shape(value, root):
            raise HostIntegrationError(
                "Harness-owned Codex config contains unknown user content and cannot be "
                f"rewritten: {path}"
            )
        if _manual_config_is_desired(
            value,
            root,
            self.mcp_http_url,
            self._http_token(),
            hidden=hidden,
        ):
            return
        if marker is not None and _config_is_owned_shape(value, root):
            return
        if entry is None:
            raise HostIntegrationError(
                "existing .codex/config.toml is user-owned; add the Harness MCP table and exact "
                "Harness Codex bootstrap developer instructions manually, or move the existing "
                "project configuration before retrying"
            )
        raise HostRegistrationCollisionError(
            f"Codex project config already has a non-Harness MCP server named 'harness': {path}"
        )

    def reconcile_project(self, workspace_root: Path, *, hidden: bool = False) -> IntegrationChange:
        root = _workspace_root(workspace_root)
        self.preflight_project_reconcile(root, hidden=hidden)
        path = _config_path(root)
        marker_path = _marker_path(root)
        raw = _read_optional_regular_file(path, label="Codex project config")
        marker = _read_owner_marker(root)

        if raw is not None:
            value = _parse_toml(raw, path)
            if marker is None and not hidden and _isolated_development_entry(value) is not None:
                if _isolated_development_bootstrap_is_current(value):
                    return IntegrationChange.UNCHANGED
                raise HostIntegrationError(
                    "tracked source-checkout .codex/config.toml requires the exact Harness "
                    "Codex bootstrap and local MCP placement; update the checkout and restart "
                    "Codex with a new task"
                )
            if marker is None and _manual_config_is_desired(
                value,
                root,
                self.mcp_http_url,
                self._http_token(),
                hidden=hidden,
            ):
                return IntegrationChange.UNCHANGED
            if marker is not None and _config_is_desired(
                value,
                root,
                self.mcp_http_url,
                self._required_http_token(),
                hidden=hidden,
            ):
                if marker is not None and _ensure_codex_exclude(root):
                    return IntegrationChange.CHANGED
                return IntegrationChange.UNCHANGED
            if marker is None or not _config_is_owned_shape(value, root):
                raise HostRegistrationCollisionError(
                    f"Codex project config cannot be safely reconciled: {path}"
                )

        desired = _desired_config(
            root,
            self.mcp_http_url,
            self._required_http_token(),
            hidden=hidden,
        )
        _require_directory_safe(path.parent)
        if marker is None:
            _replace_if_unchanged(
                marker_path,
                None,
                _owner_marker_bytes(root),
                0o600,
                label="Codex ownership marker",
            )
        _ensure_codex_exclude(root)
        _replace_if_unchanged(
            path,
            raw,
            desired,
            0o600 if raw is None else stat.S_IMODE(path.lstat().st_mode),
            label="Codex project config",
        )
        return IntegrationChange.CHANGED

    def preflight_project_remove(self, workspace_root: Path) -> None:
        root = _workspace_root(workspace_root)
        path = _config_path(root)
        marker_path = _marker_path(root)
        marker = _read_owner_marker(root)
        raw = _read_optional_regular_file(path, label="Codex project config")
        if marker is None:
            if raw is None:
                return
            value = _parse_toml(raw, path)
            if _isolated_development_entry(value) is not None:
                return
            entry = _harness_entry(value)
            if entry is None or _entry_is_desired(
                entry, root, self.mcp_http_url, self._http_token()
            ):
                return
            raise HostRegistrationCollisionError(
                f"Codex project config has a non-Harness MCP server named 'harness': {path}"
            )
        if _git_is_tracked(root, path.relative_to(root)) or _git_is_tracked(
            root, marker_path.relative_to(root)
        ):
            raise HostIntegrationError(
                "tracked Harness Codex project configuration requires manual removal"
            )
        if raw is not None and not _config_is_owned_shape(_parse_toml(raw, path), root):
            raise HostIntegrationError(
                "Harness-owned Codex config contains unknown user content and cannot be removed: "
                f"{path}"
            )

    def remove_project(self, workspace_root: Path) -> IntegrationChange:
        root = _workspace_root(workspace_root)
        self.preflight_project_remove(root)
        path = _config_path(root)
        marker_path = _marker_path(root)
        marker = _read_owner_marker(root)
        raw = _read_optional_regular_file(path, label="Codex project config")
        if marker is None:
            if raw is None:
                return IntegrationChange.UNCHANGED
            value = _parse_toml(raw, path)
            if _isolated_development_entry(value) is not None:
                return IntegrationChange.UNCHANGED
            entry = _harness_entry(value)
            if entry is None or _entry_is_desired(
                entry, root, self.mcp_http_url, self._http_token()
            ):
                return IntegrationChange.UNCHANGED
            raise HostRegistrationCollisionError(
                f"Codex project config has a non-Harness MCP server named 'harness': {path}"
            )
        if _git_is_tracked(root, path.relative_to(root)) or _git_is_tracked(
            root, marker_path.relative_to(root)
        ):
            raise HostIntegrationError(
                "tracked Harness Codex project configuration requires manual removal"
            )
        if raw is not None and not _config_is_owned_shape(_parse_toml(raw, path), root):
            raise HostIntegrationError(
                "Harness-owned Codex config contains unknown user content and cannot be removed: "
                f"{path}"
            )
        if raw is not None:
            _delete_if_unchanged(path, raw, label="Codex project config")
        marker_raw = _read_optional_regular_file(marker_path, label="Codex ownership marker")
        if marker_raw is not None:
            _delete_if_unchanged(marker_path, marker_raw, label="Codex ownership marker")
        if not _another_owned_worktree(root):
            _remove_codex_exclude(root)
        _remove_empty_directory(path.parent)
        return IntegrationChange.CHANGED

    def _http_token(self) -> str | None:
        token = self.mcp_http_token
        if token is None and self.mcp_http_database is not None:
            token = read_dashboard_access_token(self.mcp_http_database)
        if token is not None and (not token.isascii() or not token or len(token) > 128):
            raise HostIntegrationError("Harness loopback MCP capability is invalid")
        return token

    def _required_http_token(self) -> str:
        token = self._http_token()
        if token is None and self.mcp_http_database is not None:
            try:
                ensure_private_state_directory(self.mcp_http_database.parent)
                created = load_or_create_dashboard_access_token(self.mcp_http_database)
            except RuntimePathError as exc:
                raise HostIntegrationError(
                    "Harness loopback MCP capability is unavailable; start the daemon and retry"
                ) from exc
            persisted = read_dashboard_access_token(self.mcp_http_database)
            if persisted is None or persisted != created:
                raise HostIntegrationError(
                    "Harness loopback MCP capability is unavailable; start the daemon and retry"
                )
            token = persisted
            if not token.isascii() or not token or len(token) > 128:
                raise HostIntegrationError("Harness loopback MCP capability is invalid")
        if token is None:
            raise HostIntegrationError(
                "Harness loopback MCP capability is unavailable; start the daemon and retry"
            )
        return token


def discover_codex_adapter(
    *,
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> CodexAdapter | None:
    """Discover the Codex CLI without reading or mutating user configuration."""
    values = os.environ if environment is None else environment
    executable = shutil.which("codex", path=values.get("PATH"))
    if executable is None:
        return None
    paths = default_runtime_paths(environment=values)
    port = mcp_http_listen_port(paths.socket, environment=values)
    if port == 0:
        raise HostIntegrationError("Codex MCP HTTP canonical port could not be resolved")
    return CodexAdapter(
        executable=_absolute_executable_path(executable),
        python_executable=_absolute_executable_path(
            Path(sys.executable) if python_executable is None else python_executable
        ),
        forward_env_vars=codex_mcp_forward_env_vars(values),
        mcp_http_url=f"http://{DASHBOARD_HOST}:{port}{_MCP_HTTP_PATH}",
        mcp_http_database=paths.database,
    )


def codex_profile_missing_workspace_root(
    environment: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if environment is None else environment
    return values.get(_HOST_PROFILE_ENV) == _CODEX_PROFILE and not values.get(_WORKSPACE_ROOT_ENV)


def codex_owned_hidden_instructions_active(workspace_root: Path) -> bool:
    """Return whether a marker-owned project config contains the exact Hidden policy."""
    root = _workspace_root(workspace_root)
    if _read_owner_marker(root) is None:
        return False
    path = _config_path(root)
    raw = _read_optional_regular_file(path, label="Codex project config")
    if raw is None:
        return False
    value = _parse_toml(raw, path)
    return _config_is_owned_shape(value, root) and value.get(
        "developer_instructions"
    ) == codex_developer_instructions(hidden=True)


def codex_developer_instructions(*, hidden: bool = False) -> str:
    """Return the exact always-loaded Codex workflow, optionally composed with Hidden policy."""
    if not hidden:
        return CODEX_BOOTSTRAP_INSTRUCTION_BODY
    return f"{CODEX_BOOTSTRAP_INSTRUCTION_BODY}\n{HIDDEN_INSTRUCTION_BODY}"


def _desired_entry(
    root: Path,
    mcp_http_url: str,
    mcp_http_token: str | None,
) -> dict[str, object]:
    if mcp_http_token is None:
        return {}
    return {
        "url": mcp_http_url,
        "required": True,
        "startup_timeout_sec": 30,
        "http_headers": {
            _MCP_HTTP_AUTHORIZATION_HEADER: f"Bearer {mcp_http_token}",
            _MCP_HTTP_WORKSPACE_HEADER: str(root),
        },
    }


def _desired_config(
    root: Path,
    mcp_http_url: str,
    mcp_http_token: str,
    *,
    hidden: bool = False,
) -> bytes:
    entry = _desired_entry(root, mcp_http_url, mcp_http_token)
    lines = [
        "# Generated and owned by Harness. Do not edit this file in place.",
        "# After this file changes, fully restart Codex and begin a new task.",
        f"developer_instructions = {_toml_string(codex_developer_instructions(hidden=hidden))}",
        "",
    ]
    lines.extend(
        [
            "[mcp_servers.harness]",
            f"url = {_toml_string(entry['url'])}",
            "required = true",
            "startup_timeout_sec = 30",
            "",
            "[mcp_servers.harness.http_headers]",
            f"{_MCP_HTTP_AUTHORIZATION_HEADER} = {_toml_string(f'Bearer {mcp_http_token}')}",
            f"{_toml_string(_MCP_HTTP_WORKSPACE_HEADER)} = {_toml_string(root)}",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _parse_toml(raw: bytes, path: Path) -> dict[str, object]:
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HostIntegrationError(f"Codex project config is not valid UTF-8 TOML: {path}") from exc
    if not isinstance(value, dict):
        raise HostIntegrationError(f"Codex project config top level must be a table: {path}")
    return value


def _harness_entry(value: dict[str, object]) -> dict[str, object] | None:
    servers = value.get("mcp_servers")
    if servers is None:
        return None
    if not isinstance(servers, dict):
        return None
    entry = servers.get(_SERVER_NAME)
    return entry if isinstance(entry, dict) else None


def _isolated_development_entry(value: dict[str, object]) -> dict[str, object] | None:
    servers = value.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    for name in (_ISOLATED_DEV_SERVER_NAME, _SERVER_NAME):
        entry = servers.get(name)
        if not isinstance(entry, dict):
            continue
        configured_type = entry.get("type")
        if configured_type is not None and configured_type != "stdio":
            continue
        env = entry.get("env")
        if not isinstance(env, dict):
            continue
        launch_isolated = (
            entry.get("command") == _ISOLATED_DEV_COMMAND
            and entry.get("args") == _ISOLATED_DEV_ARGS
        )
        launch_dogfood = (
            entry.get("command") == _DOGFOOD_COMMAND and entry.get("args") == _DOGFOOD_ARGS
        )
        if (
            (launch_isolated or launch_dogfood)
            and env.get(_WORKSPACE_ROOT_ENV) == "."
            and env.get(_HOST_PROFILE_ENV) is None
        ):
            return entry
    return None


def _isolated_development_bootstrap_is_current(value: dict[str, object]) -> bool:
    entry = _isolated_development_entry(value)
    return (
        value.get("developer_instructions") == CODEX_BOOTSTRAP_INSTRUCTION_BODY
        and entry is not None
        and entry.get("experimental_environment") == "local"
    )


def _entry_identity(entry: dict[str, object] | None) -> tuple[str | None, str | None]:
    if entry is None:
        return None, None
    command = entry.get("command")
    url = entry.get("url")
    headers = entry.get("http_headers")
    env = entry.get("env")
    root = (
        headers.get(_MCP_HTTP_WORKSPACE_HEADER)
        if isinstance(headers, dict)
        else env.get(_WORKSPACE_ROOT_ENV)
        if isinstance(env, dict)
        else None
    )
    return (
        url if isinstance(url, str) else command if isinstance(command, str) else None,
        root if isinstance(root, str) else None,
    )


def _entry_is_desired(
    entry: dict[str, object] | None,
    root: Path,
    mcp_http_url: str,
    mcp_http_token: str | None,
) -> bool:
    return entry == _desired_entry(root, mcp_http_url, mcp_http_token)


def _manual_config_is_desired(
    value: dict[str, object],
    root: Path,
    mcp_http_url: str,
    mcp_http_token: str | None,
    *,
    hidden: bool,
) -> bool:
    if not _entry_is_desired(_harness_entry(value), root, mcp_http_url, mcp_http_token):
        return False
    return value.get("developer_instructions") == codex_developer_instructions(hidden=hidden)


def _config_is_desired(
    value: dict[str, object],
    root: Path,
    mcp_http_url: str,
    mcp_http_token: str | None,
    *,
    hidden: bool,
) -> bool:
    return bool(
        mcp_http_token is not None
        and _config_is_owned_shape(value, root)
        and value
        == tomllib.loads(
            _desired_config(root, mcp_http_url, mcp_http_token, hidden=hidden).decode("utf-8")
        )
    )


def _config_is_owned_shape(value: dict[str, object], root: Path) -> bool:
    if set(value) not in ({"mcp_servers"}, {"developer_instructions", "mcp_servers"}):
        return False
    if (
        "developer_instructions" in value
        and value["developer_instructions"] not in _OWNED_CODEX_DEVELOPER_INSTRUCTIONS
    ):
        return False
    servers = value.get("mcp_servers")
    if not isinstance(servers, dict) or set(servers) != {_SERVER_NAME}:
        return False
    entry = servers.get(_SERVER_NAME)
    if not isinstance(entry, dict):
        return False
    if "url" in entry:
        headers = entry.get("http_headers")
        return (
            set(entry) == {"url", "required", "startup_timeout_sec", "http_headers"}
            and isinstance(entry.get("url"), str)
            and entry.get("required") is True
            and entry.get("startup_timeout_sec") == 30
            and isinstance(headers, dict)
            and set(headers)
            == {
                _MCP_HTTP_AUTHORIZATION_HEADER,
                _MCP_HTTP_WORKSPACE_HEADER,
            }
            and isinstance(headers.get(_MCP_HTTP_AUTHORIZATION_HEADER), str)
            and headers.get(_MCP_HTTP_WORKSPACE_HEADER) == str(root)
        )
    required_keys = {"command", "args", "cwd", "env"}
    if not required_keys <= set(entry) or not set(entry) - required_keys <= {
        "env_vars",
        "required",
        "experimental_environment",
    }:
        return False
    forwarded = entry.get("env_vars", [])
    if not isinstance(forwarded, list) or any(not isinstance(name, str) for name in forwarded):
        return False
    expected_order = tuple(name for name in CODEX_MCP_FORWARD_ENV_VARS if name in forwarded)
    env = entry.get("env")
    return (
        entry.get("args") == ["-m", "harness.mcp_process"]
        and tuple(forwarded) == expected_order
        and entry.get("required", True) is True
        and entry.get("experimental_environment") in (None, "local")
        and entry.get("cwd") == str(root)
        and isinstance(entry.get("command"), str)
        and isinstance(env, dict)
        and env
        == {
            _HOST_PROFILE_ENV: _CODEX_PROFILE,
            _WORKSPACE_ROOT_ENV: str(root),
        }
    )


def _config_path(root: Path) -> Path:
    return root / ".codex" / "config.toml"


def _marker_path(root: Path) -> Path:
    return root / ".codex" / _OWNER_MARKER


def _owner_marker_bytes(root: Path) -> bytes:
    return (
        json.dumps(
            {"version": _OWNER_VERSION, "workspace_root": str(root)},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _read_owner_marker(root: Path) -> _OwnerMarker | None:
    path = _marker_path(root)
    raw = _read_optional_regular_file(path, label="Codex ownership marker")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostIntegrationError(f"Codex ownership marker is malformed: {path}") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "workspace_root"}
        or value.get("version") != _OWNER_VERSION
        or value.get("workspace_root") != str(root)
    ):
        raise HostIntegrationError(f"Codex ownership marker does not match Workspace: {path}")
    return _OwnerMarker(workspace_root=str(root))


def _workspace_root(path: Path) -> Path:
    try:
        location = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HostIntegrationError(f"Codex Workspace path cannot be resolved: {path}") from exc
    completed = _git(location, "rev-parse", "--show-toplevel")
    if completed.returncode != 0 or not completed.stdout.strip():
        raise HostIntegrationError(f"Codex Workspace is not a Git worktree: {location}")
    try:
        root = Path(completed.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HostIntegrationError("Git returned an invalid Codex Workspace root") from exc
    if not root.is_dir():
        raise HostIntegrationError("Git returned a non-directory Codex Workspace root")
    return root


def _absolute_executable_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostIntegrationError("Git could not inspect Codex project configuration") from exc


def _git_is_tracked(root: Path, relative: Path) -> bool:
    completed = _git(root, "ls-files", "--error-unmatch", "--", relative.as_posix())
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise HostIntegrationError("Git could not determine whether Codex project config is tracked")


def _git_info_exclude(root: Path) -> Path:
    completed = _git(root, "rev-parse", "--git-path", "info/exclude")
    if completed.returncode != 0 or not completed.stdout.strip():
        raise HostIntegrationError("Git info/exclude path could not be resolved")
    path = Path(completed.stdout.strip())
    if not path.is_absolute():
        path = root / path
    return Path(os.path.abspath(path))


def _exclude_block() -> bytes:
    return ("\n".join((_EXCLUDE_BEGIN, *_EXCLUDE_BODY, _EXCLUDE_END)) + "\n").encode()


def _ensure_codex_exclude(root: Path) -> bool:
    path = _git_info_exclude(root)
    raw = _read_optional_regular_file(path, label="Git info/exclude")
    block = _exclude_block()
    content = b"" if raw is None else raw
    if block in content:
        return False
    if _EXCLUDE_BEGIN.encode() in content or _EXCLUDE_END.encode() in content:
        raise HostIntegrationError("Git info/exclude contains an ambiguous Harness Codex block")
    if content and not content.endswith(b"\n"):
        content += b"\n"
    _replace_if_unchanged(
        path,
        raw,
        content + block,
        0o644 if raw is None else stat.S_IMODE(path.lstat().st_mode),
        label="Git info/exclude",
    )
    return True


def _remove_codex_exclude(root: Path) -> None:
    path = _git_info_exclude(root)
    raw = _read_optional_regular_file(path, label="Git info/exclude")
    if raw is None:
        return
    block = _exclude_block()
    if block not in raw:
        return
    _replace_if_unchanged(
        path,
        raw,
        raw.replace(block, b"", 1),
        stat.S_IMODE(path.lstat().st_mode),
        label="Git info/exclude",
    )


def _another_owned_worktree(root: Path) -> bool:
    completed = _git(root, "worktree", "list", "--porcelain")
    if completed.returncode != 0:
        raise HostIntegrationError("Git linked worktrees could not be inspected")
    for line in completed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree "))
        try:
            other = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if other == root or not other.is_dir():
            continue
        try:
            if _read_owner_marker(other) is not None:
                return True
        except HostIntegrationError:
            continue
    return False


def _read_optional_regular_file(path: Path, *, label: str) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HostIntegrationError(f"{label} cannot be inspected: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HostIntegrationError(f"{label} is not a real regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HostIntegrationError(f"{label} cannot be read: {path}") from exc


def _require_directory_safe(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise HostIntegrationError(f"Codex config directory cannot be prepared: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise HostIntegrationError(f"Codex config directory is unsafe: {path}")


def _replace_if_unchanged(
    path: Path,
    expected: bytes | None,
    replacement: bytes,
    mode: int,
    *,
    label: str,
) -> None:
    _require_directory_safe(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=".harness-codex-", dir=path.parent)
    temporary = Path(temporary_name)
    backup: Path | None = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if expected is None:
            if not _move_if_absent(temporary, path):
                raise HostRegistrationCollisionError(f"{label} appeared before mutation: {path}")
            _fsync_directory(path.parent)
            return
        current = _read_optional_regular_file(path, label=label)
        if current != expected:
            raise HostRegistrationCollisionError(f"{label} changed before mutation: {path}")
        backup = _unused_sibling(path, ".harness-codex-backup-")
        if not _move_if_absent(path, backup):
            raise HostIntegrationError(f"{label} backup path appeared: {backup}")
        if _read_optional_regular_file(backup, label=label) != expected:
            _restore_backup(path, backup, label=label)
            backup = None
            raise HostRegistrationCollisionError(f"{label} changed during mutation: {path}")
        if not _move_if_absent(temporary, path):
            _restore_backup(path, backup, label=label, preserve_if_occupied=True)
            backup = None
            raise HostRegistrationCollisionError(
                f"{label} appeared during mutation; previous content was preserved: {path}"
            )
        backup.unlink()
        backup = None
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _delete_if_unchanged(path: Path, expected: bytes, *, label: str) -> None:
    if _read_optional_regular_file(path, label=label) != expected:
        raise HostRegistrationCollisionError(f"{label} changed before removal: {path}")
    backup = _unused_sibling(path, ".harness-codex-delete-")
    if not _move_if_absent(path, backup):
        raise HostIntegrationError(f"{label} removal backup path appeared: {backup}")
    if _read_optional_regular_file(backup, label=label) != expected:
        _restore_backup(path, backup, label=label)
        raise HostRegistrationCollisionError(f"{label} changed during removal: {path}")
    backup.unlink()
    _fsync_directory(path.parent)


def _restore_backup(
    path: Path,
    backup: Path,
    *,
    label: str,
    preserve_if_occupied: bool = False,
) -> None:
    if _path_exists(path):
        if preserve_if_occupied:
            return
        raise HostIntegrationError(
            f"{label} recovery could not restore {path}; backup preserved at {backup}"
        )
    if not _move_if_absent(backup, path):
        raise HostIntegrationError(
            f"{label} recovery could not restore {path}; backup preserved at {backup}"
        )


def _unused_sibling(path: Path, prefix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    os.close(fd)
    candidate = Path(name)
    candidate.unlink()
    return candidate


def _move_if_absent(source: Path, target: Path) -> bool:
    if os.name == "nt":
        raise HostIntegrationError("Codex project MCP configuration currently requires POSIX")
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise HostIntegrationError("atomic no-clobber rename is unavailable") from exc
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_bytes, -100, target_bytes, 1)
    else:
        renamex_np = getattr(library, "renamex_np", None)
        if renamex_np is None:
            raise HostIntegrationError("atomic no-clobber rename is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, target_bytes, 0x00000004)
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        return False
    raise OSError(error_number, os.strerror(error_number), target)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HostIntegrationError(f"Codex integration path cannot be inspected: {path}") from exc
    return True


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
