from __future__ import annotations

import os
import subprocess
import sys
import time
from multiprocessing.connection import Client
from multiprocessing.context import AuthenticationError
from pathlib import Path

import pytest

from harness.ipc import IpcTransportError, request_shutdown, request_status
from harness.vault.store import VaultError, VaultStore, read_private
from harness.windows_fs import (
    WindowsFileSecurityError,
    ensure_private_windows_directory,
    require_private_windows_path,
    secure_owned_windows_path,
)
from harness.windows_pipe import pipe_address

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows IPC and ACL checks")


def test_private_windows_directory_rejects_foreign_allow_ace(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32security

    secure_owned_windows_path(tmp_path, directory=True)
    private = tmp_path / "private"
    ensure_private_windows_directory(private)
    require_private_windows_path(private, directory=True)

    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, everyone)
    win32security.SetNamedSecurityInfo(
        str(private),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    with pytest.raises(WindowsFileSecurityError, match="other users"):
        require_private_windows_path(private, directory=True)


def test_vault_files_are_private_and_reject_broadened_acl(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32security

    backup = tmp_path / "backup"
    backup.mkdir()
    store = VaultStore(tmp_path / "vault")
    store.create("a-test-only-master-password", str(backup))
    snapshot = next((backup / "harness-vault-backups").glob("*.kdbx"))
    for path in (store.path, snapshot):
        require_private_windows_path(path, directory=False)

    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    descriptor = win32security.GetFileSecurity(
        str(store.path), win32security.DACL_SECURITY_INFORMATION
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, everyone)
    win32security.SetNamedSecurityInfo(
        str(store.path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    with pytest.raises(VaultError, match="unsafe_path"):
        read_private(store.path)


def test_named_pipe_daemon_status_singleton_and_authentication(tmp_path: Path) -> None:
    secure_owned_windows_path(tmp_path, directory=True)
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    ensure_private_windows_directory(state)
    ensure_private_windows_directory(runtime)
    database = state / "harness.db"
    endpoint = runtime / "harness.pipe"
    executable = Path(sys.executable).with_name("harnessd.exe")
    command = [
        str(executable),
        "serve",
        "--database",
        str(database),
        "--socket",
        str(endpoint),
    ]
    environment = dict(os.environ)
    environment["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")
    environment["XDG_RUNTIME_DIR"] = str(tmp_path / "xdg-runtime")
    process = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                status = request_status(endpoint)
                break
            except IpcTransportError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    output = (
                        process.communicate(timeout=2)[0] if process.poll() is not None else b""
                    )
                    pytest.fail(f"Windows daemon did not start: {output!r}")
                time.sleep(0.05)
        assert status.schema_version > 0

        second = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert second.returncode != 0
        assert b"already owns the IPC endpoint" in second.stdout
        assert request_status(endpoint).schema_version == status.schema_version

        with pytest.raises(AuthenticationError):
            Client(pipe_address(endpoint), family="AF_PIPE", authkey=b"wrong-key")
        assert request_status(endpoint).schema_version == status.schema_version
    finally:
        if process.poll() is None:
            try:
                request_shutdown(endpoint)
            except IpcTransportError:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
