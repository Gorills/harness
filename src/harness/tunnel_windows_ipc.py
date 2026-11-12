# ruff: noqa: RUF001
"""System-owned Windows service files and owner-scoped authenticated named pipe."""

from __future__ import annotations

import re
import subprocess
from multiprocessing.connection import Client, answer_challenge, deliver_challenge
from pathlib import Path
from typing import Any, cast

from harness.tunnel_platform import PlatformError

PIPE_NAME = r"\\.\pipe\HarnessTunnel-control-v1"


class PipePeer:
    def __init__(self, connection: Any, key: bytes) -> None:
        self.connection, self.key = connection, key
        self.timeout = 10.0
        self.buffer = b""

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def send_bytes(self, data: bytes) -> None:
        self.connection.send_bytes(data)

    def recv_bytes(self, maximum: int) -> bytes:
        if not self.connection.poll(self.timeout):
            raise TimeoutError("named pipe timeout")
        return bytes(self.connection.recv_bytes(maximum))

    def authenticate(self, *, server: bool) -> None:
        if server:
            deliver_challenge(cast(Any, self), self.key)
            answer_challenge(cast(Any, self), self.key)
        else:
            answer_challenge(cast(Any, self), self.key)
            deliver_challenge(cast(Any, self), self.key)

    def sendall(self, payload: bytes) -> None:
        self.send_bytes(payload)

    def recv(self, count: int) -> bytes:
        if not self.buffer:
            self.buffer = self.recv_bytes(65540)
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> PipePeer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def require_admin_path(path: Path, *, directory: bool, reader_sid: str | None = None) -> None:
    import win32security

    if path.is_symlink() or path.is_junction() or path.is_dir() != directory:
        raise PlatformError("Путь службы Windows заменён ссылкой или имеет неверный тип")
    descriptor = win32security.GetFileSecurity(
        str(path),
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    privileged = {"S-1-5-18", "S-1-5-32-544"}
    if owner not in privileged:
        raise PlatformError("Файл сетевой службы должен принадлежать SYSTEM или администраторам")
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        raise PlatformError("У сетевой службы отсутствует защищённый DACL")
    write_mask = (
        0x00000002
        | 0x00000004
        | 0x00000010
        | 0x00000100
        | 0x00010000
        | 0x00040000
        | 0x00080000
        | 0x40000000
        | 0x10000000
    )
    if directory:
        write_mask |= 0x00000040
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if ace[0][0] == win32security.ACCESS_DENIED_ACE_TYPE:
            continue
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise PlatformError("Неподдерживаемый DACL сетевой службы")
        sid = win32security.ConvertSidToStringSid(ace[2])
        if sid not in privileged and (
            ace[1] & write_mask or (reader_sid is not None and sid != reader_sid)
        ):
            raise PlatformError("Сетевой компонент доступен посторонним для изменения")


def secure_admin_path(path: Path, *, directory: bool, reader_sid: str | None = None) -> None:
    import win32security

    if reader_sid is not None and not re.fullmatch(r"S-1-5(?:-\d+)+", reader_sid):
        raise PlatformError("Некорректный владелец Windows-службы")
    flags = "OICI" if directory else ""
    sddl = f"O:BAG:BAD:P(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"
    if reader_sid:
        sddl += f"(A;{flags};FRFX;;;{reader_sid})"
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    win32security.SetFileSecurity(
        str(path),
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )
    require_admin_path(path, directory=directory, reader_sid=reader_sid)


def connect(root: Path, timeout: float) -> Any:
    key = root / "control.key"
    require_admin_path(key, directory=False)
    secret = key.read_bytes()
    if len(secret) != 32:
        raise PlatformError("Некорректный ключ управления Windows-службой")
    connection = Client(PIPE_NAME, family="AF_PIPE", authkey=None)
    peer = PipePeer(connection, secret)
    peer.settimeout(timeout)
    try:
        _require_system_server(connection.fileno())
        peer.authenticate(server=False)
    except BaseException:
        peer.close()
        raise
    return peer


def _require_system_server(handle: int) -> None:
    """A shared user-readable key alone cannot authenticate a SYSTEM server."""
    import ctypes

    import win32api
    import win32con
    import win32security

    function = ctypes.WinDLL("kernel32.dll").GetNamedPipeServerProcessId  # type: ignore[attr-defined]
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    function.restype = ctypes.c_int32
    pid = ctypes.c_uint32()
    if not function(handle, ctypes.byref(pid)):
        raise PlatformError("Не подтверждён процесс сетевой службы Windows")
    process = win32api.OpenProcess(0x1000, False, pid.value)
    try:
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        try:
            sid = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            )
            if sid != "S-1-5-18":
                raise PlatformError("Сетевая служба Windows должна работать от SYSTEM")
        finally:
            token.Close()
    finally:
        process.Close()


class Listener:
    def __init__(self, root: Path, owner_sid: str) -> None:
        import pywintypes  # type: ignore[import-untyped]
        import win32pipe  # type: ignore[import-untyped]
        import win32security

        if not re.fullmatch(r"S-1-5(?:-\d+)+", owner_sid):
            raise PlatformError("Некорректный владелец Windows-службы")
        self.root, self.owner_sid = root, owner_sid
        self.pipe = win32pipe
        self.security = win32security
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = (
            win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                f"D:P(A;;GA;;;SY)(A;;GRGW;;;{owner_sid})", 1
            )
        )
        self.attributes = attributes
        require_admin_path(root / "control.key", directory=False, reader_sid=owner_sid)
        self.secret = (root / "control.key").read_bytes()
        if len(self.secret) != 32:
            raise PlatformError("Некорректный ключ управления Windows-службой")

    def accept(self) -> Any:
        from multiprocessing.connection import PipeConnection  # type: ignore[attr-defined]

        import win32api
        import win32con

        handle = self.pipe.CreateNamedPipe(
            PIPE_NAME,
            self.pipe.PIPE_ACCESS_DUPLEX | 0x00080000,
            self.pipe.PIPE_TYPE_MESSAGE | self.pipe.PIPE_READMODE_MESSAGE | self.pipe.PIPE_WAIT,
            1,
            65540,
            65540,
            10000,
            self.attributes,
        )
        peer = None
        try:
            self.pipe.ConnectNamedPipe(handle, None)
            self.pipe.ImpersonateNamedPipeClient(handle)
            try:
                token = self.security.OpenThreadToken(
                    win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True
                )
                sid = self.security.ConvertSidToStringSid(
                    self.security.GetTokenInformation(token, self.security.TokenUser)[0]
                )
                token.Close()
                if sid not in {self.owner_sid, "S-1-5-18"}:
                    raise PlatformError("Другой пользователь не может управлять туннелем")
            finally:
                self.security.RevertToSelf()
            connection = PipeConnection(int(handle.Detach()))
            peer = PipePeer(connection, self.secret)
            peer.settimeout(10)
            peer.authenticate(server=True)
            return peer
        except BaseException:
            if peer is not None:
                peer.close()
            else:
                handle.Close()
            raise


class WindowsChild:
    """A native process created suspended and assigned before its first thread."""

    def __init__(self, handle: Any, pid: int) -> None:
        self.handle, self.pid = handle, pid

    def poll(self) -> int | None:
        import win32process  # type: ignore[import-untyped]

        result = int(win32process.GetExitCodeProcess(self.handle))
        return None if result == 259 else result

    def terminate(self) -> None:
        import win32api

        win32api.TerminateProcess(self.handle, 1)

    kill = terminate

    def wait(self, timeout: float) -> int:
        import win32event  # type: ignore[import-untyped]

        if win32event.WaitForSingleObject(self.handle, int(timeout * 1000)) != 0:
            raise subprocess.TimeoutExpired("owned tunnel engine", timeout)
        result = self.poll()
        return 1 if result is None else result


class EngineJob:
    def __init__(self) -> None:
        import win32job  # type: ignore[import-untyped]

        self.job = win32job.CreateJobObject(None, None)
        information = win32job.QueryInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation
        )
        information["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            self.job, win32job.JobObjectExtendedLimitInformation, information
        )

    def spawn(self, command: list[str]) -> WindowsChild:
        import win32api
        import win32con
        import win32job
        import win32process

        startup = win32process.STARTUPINFO()
        process, thread, pid, _ = win32process.CreateProcess(
            command[0],
            subprocess.list2cmdline(command),
            None,
            None,
            False,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
            None,
            None,
            startup,
        )
        try:
            win32job.AssignProcessToJobObject(self.job, process)
            win32process.ResumeThread(thread)
        except BaseException:
            win32api.TerminateProcess(process, 1)
            process.Close()
            raise
        finally:
            thread.Close()
        return WindowsChild(process, pid)


def interface_luid(alias: str) -> int | None:
    import ctypes

    library = ctypes.WinDLL("iphlpapi.dll")  # type: ignore[attr-defined]
    function = library.ConvertInterfaceAliasToLuid
    function.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint64)]
    function.restype = ctypes.c_uint32
    value = ctypes.c_uint64()
    return value.value if function(alias, ctypes.byref(value)) == 0 else None


def service_dispatch(runtime: Any) -> None:
    import servicemanager  # type: ignore[import-untyped]
    import win32event
    import win32service  # type: ignore[import-untyped]
    import win32serviceutil  # type: ignore[import-untyped]

    class TunnelService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        _svc_name_ = "HarnessTunnel"
        _svc_display_name_ = "Harness Tunnel"

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self.stopped = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            runtime.stop.set()
            # Wake blocking ConnectNamedPipe without changing any network rule.
            try:
                peer = connect(runtime.root, 2)
                peer.close()
            except OSError:
                pass
            win32event.SetEvent(self.stopped)

        def SvcDoRun(self) -> None:
            runtime.serve_windows()

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(TunnelService)
    servicemanager.StartServiceCtrlDispatcher()
