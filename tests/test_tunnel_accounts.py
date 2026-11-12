"""Real Linux useradd in a user namespace and disposable --root account database.

Opt in with HARNESS_TEST_NATIVE_ACCOUNTS=1; no host account or service mutation.
"""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
import pathlib
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

import harness.tunnel_installer as installer
import harness.tunnel_platform as native
import harness.tunnel_service as service
from harness.tunnel_platform import PlatformError
from harness.tunnel_ssh import REMOTE_BOOTSTRAP

if TYPE_CHECKING:
    import pwd
else:
    pwd = pytest.importorskip("pwd", reason="Linux account database fixture")


class AccountRoot:
    def __init__(self, root: Path) -> None:
        self.root = root
        etc = root / "etc"
        etc.mkdir(parents=True)
        (etc / "passwd").write_text("root:x:0:0:root:/root:/bin/sh\n")
        (etc / "group").write_text("root:x:0:\n")
        (etc / "shadow").write_text("root:*:1:0:99999:7:::\n")
        (etc / "gshadow").write_text("root:*::\n")
        (etc / "login.defs").write_text(
            "SYS_UID_MIN 100\nSYS_UID_MAX 999\nSYS_GID_MIN 100\nSYS_GID_MAX 999\n"
        )
        self.real_run = subprocess.run
        self.calls: list[list[str]] = []

    def lookup(self, name: str) -> pwd.struct_passwd:
        for line in (self.root / "etc/passwd").read_text().splitlines():
            fields = line.split(":")
            if fields[0] == name:
                return pwd.struct_passwd((*fields[:2], int(fields[2]), int(fields[3]), *fields[4:]))
        raise KeyError(name)

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        binary = Path(command[0]).name
        assert binary in {"useradd", "userdel"}
        assert os.geteuid() == 0 and str(self.root).startswith("/tmp/")
        actual = ["/usr/sbin/" + binary, "--root", str(self.root), *command[1:]]
        self.calls.append(actual)
        # This is the real shadow-utils program, never a success stub.
        check = kwargs.pop("check", False)
        kwargs["stderr"] = subprocess.PIPE
        result = self.real_run(actual, **kwargs)
        if result.returncode:
            print(result.stderr.decode(), file=sys.stderr)
            if check:
                result.check_returncode()
        return result

    def alter(self, name: str, field: int, value: str) -> None:
        path = self.root / "etc/passwd"
        lines = [line.split(":") for line in path.read_text().splitlines()]
        for fields in lines:
            if fields[0] == name:
                fields[field] = value
        path.write_text("".join(":".join(fields) + "\n" for fields in lines))


def local_accounts(root: Path) -> dict[str, Any]:
    accounts = AccountRoot(root / "accounts")
    install, state, units = root / "install", root / "state", root / "units"
    (install / "bundle").mkdir(parents=True)
    units.mkdir()
    original_path = Path
    fail_enable = True

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if Path(command[0]).name in {"useradd", "userdel"}:
            return accounts.run(command, **kwargs)
        assert command[0] == "/usr/bin/systemctl"
        if "enable" in command and fail_enable:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout=b"")

    def clear_owned_state() -> None:
        shutil.rmtree(state)
        shutil.rmtree(native.fakeip_cache_root(state))
        for unit in units.iterdir():
            unit.unlink()

    with (
        patch.object(service, "INSTALL", install),
        patch.object(service, "ROOT", state),
        patch.object(
            installer,
            "Path",
            lambda value: units if str(value) == "/etc/systemd/system" else original_path(value),
        ),
        patch.object(installer, "verify_bundle", return_value={}),
        patch.object(native, "require_owned"),  # explicit /tmp fixture, not a trusted install tree
        patch("os.chown"),  # root:runtime cache ACL is a separate filesystem acceptance surface
        patch.object(pwd, "getpwnam", accounts.lookup),
        patch.object(subprocess, "run", run),
    ):
        # Actual finalize creates a real account, then its ordinary failure rolls back only that account.
        with pytest.raises(subprocess.CalledProcessError):
            installer.finalize("1000")
        with pytest.raises(KeyError):
            accounts.lookup("harness-tunnel")
        assert not state.exists() and not list(units.iterdir())
        assert [Path(command[0]).name for command in accounts.calls] == ["useradd", "userdel"]
        fail_enable = False
        installer.finalize("1000")
        account = accounts.lookup("harness-tunnel")
        assert account.pw_gecos == "harness-tunnel-runtime-v1"
        assert 0 < account.pw_uid != 1000 and account.pw_shell == "/usr/sbin/nologin"
        configuration = json.loads((state / "service.json").read_text())
        assert configuration["runtime_uid"] == account.pw_uid
        # A previously owned account survives failed installation and is accepted on retry.
        clear_owned_state()
        before = (accounts.root / "etc/passwd").read_bytes()
        calls = len(accounts.calls)
        fail_enable = True
        with pytest.raises(subprocess.CalledProcessError):
            installer.finalize("1000")
        assert (accounts.root / "etc/passwd").read_bytes() == before
        assert len(accounts.calls) == calls
        fail_enable = False
        installer.finalize("1000")
        assert len(accounts.calls) == calls
        clear_owned_state()
        # Existing collision checks remain fail-closed, without account deletion or replacement.
        for field, foreign in ((4, "foreign account"), (6, "/bin/sh"), (2, "1000"), (2, "0")):
            (accounts.root / "etc/passwd").write_bytes(before)
            accounts.alter("harness-tunnel", field, foreign)
            collision = (accounts.root / "etc/passwd").read_bytes()
            with pytest.raises(PlatformError, match="посторонней"):
                installer.finalize("1000")
            assert (accounts.root / "etc/passwd").read_bytes() == collision
            assert len(accounts.calls) == calls and not state.exists() and not list(units.iterdir())
    return {
        "marker": account.pw_gecos,
        "actual_account_commands": accounts.calls,
        "new_account_rollback_and_retry": True,
        "existing_account_preserved": True,
        "foreign_marker_shell_owner_or_root_uid_refused": True,
    }


def remote_accounts(root: Path) -> dict[str, Any]:
    accounts = AccountRoot(root / "accounts")
    for name in ("etc/systemd/system", "etc/sudoers.d", "run/systemd/system", "usr/bin"):
        (root / name).mkdir(parents=True)
    (root / "etc/os-release").write_text("ID=debian\n")
    (root / "usr/bin/sudo").touch()  # selects actual maintenance-account branch; never executed
    engine = b"non-executable synthetic artifact"
    config = {
        "profile_id": "fixture",
        "engine_sha256": hashlib.sha256(engine).hexdigest(),
        "server_config": {},
        "certificate_server_name": "fixture.invalid",
        "control_script": "fixed helper",
        "maintenance_key": "ssh-ed25519 fixture",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, value in (("settings.json", json.dumps(config).encode()), ("sing-box", engine)):
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    original_path = Path

    def mapped(value: object = ".", *parts: str) -> Path:
        text = str(value)
        if text.startswith(("/etc/", "/opt/", "/run/", "/var/", "/usr/", "/root/")):
            return original_path(root, text.lstrip("/"), *parts)
        return original_path(text, *parts)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if Path(command[0]).name in {"useradd", "userdel"}:
            return accounts.run(command, **kwargs)
        if "generate" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=b"-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
            )
        assert command[0] in {"systemctl", "visudo"} or command[0] == str(
            root / "opt/harness-tunnel/fixture/sing-box"
        )
        return subprocess.CompletedProcess(command, 1 if "is-active" in command else 0, stdout=b"")

    def execute() -> None:
        callbacks: list[Any] = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(pathlib, "Path", mapped))
            stack.enter_context(patch.object(pwd, "getpwnam", accounts.lookup))
            stack.enter_context(patch.object(atexit, "register", callbacks.append))
            stack.enter_context(patch.object(signal, "signal"))
            stack.enter_context(patch.object(platform, "machine", return_value="x86_64"))
            stack.enter_context(patch.object(subprocess, "run", run))
            stack.enter_context(patch.object(socket, "socket"))
            stack.enter_context(
                patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data.getvalue())))
            )
            try:
                exec(compile(REMOTE_BOOTSTRAP, "actual bootstrap", "exec"), {})
            finally:
                for callback in reversed(callbacks):
                    callback()

    execute()
    account = accounts.lookup("htun_fixture")
    assert account.pw_gecos == "harness-tunnel-fixture"
    assert account.pw_dir == str(root / "var/lib/harness-tunnel-maint/fixture")
    assert (root / "var/lib/harness-tunnel-maint").stat().st_mode & 0o777 == 0o711
    assert (root / "var/lib/harness-tunnel-maint/fixture/.ssh/authorized_keys").is_file()
    before = (accounts.root / "etc/passwd").read_bytes()
    execute()  # repeat actual embedded program recognizes the real created account
    assert len(accounts.calls) == 1
    for field, foreign in ((4, "foreign account"), (5, "/foreign/home")):
        (accounts.root / "etc/passwd").write_bytes(before)
        accounts.alter("htun_fixture", field, foreign)
        collision = (accounts.root / "etc/passwd").read_bytes()
        with pytest.raises(RuntimeError, match="unowned account collision"):
            execute()
        assert (accounts.root / "etc/passwd").read_bytes() == collision
        assert len(accounts.calls) == 1
    return {
        "marker": account.pw_gecos,
        "actual_account_commands": accounts.calls,
        "repeat_owned_accepted": True,
        "foreign_marker_or_home_refused": True,
    }


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("HARNESS_TEST_NATIVE_ACCOUNTS") != "1",
    reason="opt-in Linux isolated shadow-utils regression",
)
@pytest.mark.parametrize("scenario", ["local", "remote"])
def test_actual_useradd_ownership_and_retry(tmp_path: Path, scenario: str) -> None:
    assert shutil.which("unshare") and Path("/usr/sbin/useradd").is_file()
    process = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            str(tmp_path),
            scenario,
            os.readlink("/proc/self/ns/user"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    proof = json.loads((tmp_path / "account-proof.json").read_text())
    assert proof["isolated_user_namespace"] and proof["host_accounts_untouched"]


if __name__ == "__main__":
    assert os.geteuid() == 0 and os.readlink("/proc/self/ns/user") != sys.argv[3]
    result = (local_accounts if sys.argv[2] == "local" else remote_accounts)(Path(sys.argv[1]))
    result.update(
        isolated_user_namespace=True,
        host_accounts_untouched=True,
        namespace=os.readlink("/proc/self/ns/user"),
    )
    (Path(sys.argv[1]) / "account-proof.json").write_text(json.dumps(result, indent=2) + "\n")
