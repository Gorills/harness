"""Authenticated Windows named-pipe transport for Harness IPC."""

from __future__ import annotations

import hashlib
import os
import secrets
from multiprocessing.connection import (
    Client,
    Connection,
    Listener,
    answer_challenge,
    deliver_challenge,
)
from pathlib import Path
from types import TracebackType

from harness.windows_fs import (
    ensure_private_windows_directory,
    require_private_windows_path,
    secure_owned_windows_path,
)

_KEY_BYTES = 32


def pipe_address(endpoint: Path) -> str:
    """Derive a stable local name without Windows path-length constraints."""
    normalized = str(endpoint.resolve(strict=False)).casefold().encode("utf-8")
    digest = hashlib.sha256(normalized).hexdigest()
    return rf"\\.\pipe\harness-{digest}"


def _key_path(endpoint: Path) -> Path:
    return endpoint.with_name(f"{endpoint.name}.key")


def read_pipe_key(endpoint: Path) -> bytes:
    require_private_windows_path(endpoint.parent, directory=True)
    key_path = _key_path(endpoint)
    require_private_windows_path(key_path, directory=False)
    key = key_path.read_bytes()
    if len(key) != _KEY_BYTES:
        raise OSError("Harness named-pipe key has an invalid length")
    return key


def ensure_pipe_key(endpoint: Path) -> bytes:
    ensure_private_windows_directory(endpoint.parent)
    key_path = _key_path(endpoint)
    try:
        return read_pipe_key(endpoint)
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(_KEY_BYTES)
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_pipe_key(endpoint)
    with os.fdopen(descriptor, "wb") as handle:
        secure_owned_windows_path(key_path, directory=False)
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return key


class WindowsPipeSocket:
    """Small socket-shaped adapter over one message-oriented pipe connection."""

    def __init__(self, connection: Connection, key: bytes) -> None:
        self._connection = connection
        self._key = key
        self._buffer = b""
        self._timeout: float | None = None

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        return self._timeout

    def authenticate_server(self) -> None:
        peer = _TimedAuthenticationConnection(self._connection, self._timeout)
        deliver_challenge(peer, self._key)  # type: ignore[arg-type]
        answer_challenge(peer, self._key)  # type: ignore[arg-type]

    def authenticate_client(self) -> None:
        peer = _TimedAuthenticationConnection(self._connection, self._timeout)
        answer_challenge(peer, self._key)  # type: ignore[arg-type]
        deliver_challenge(peer, self._key)  # type: ignore[arg-type]

    def sendall(self, payload: bytes) -> None:
        self._connection.send_bytes(payload)

    def recv(self, maximum: int) -> bytes:
        if maximum <= 0:
            return b""
        if not self._buffer:
            if not self._connection.poll(self._timeout):
                raise TimeoutError("named-pipe receive timed out")
            from harness.ipc import MAX_MESSAGE_BYTES, IpcMessageTooLargeError

            try:
                self._buffer = self._connection.recv_bytes(MAX_MESSAGE_BYTES + 1)
            except OSError as exc:
                raise IpcMessageTooLargeError("IPC message exceeds the byte limit") from exc
        chunk, self._buffer = self._buffer[:maximum], self._buffer[maximum:]
        return chunk

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> WindowsPipeSocket:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


class WindowsPipeListener:
    def __init__(self, endpoint: Path) -> None:
        self._key = ensure_pipe_key(endpoint)
        self._listener = Listener(pipe_address(endpoint), family="AF_PIPE", authkey=None)

    def accept(self) -> WindowsPipeSocket:
        return WindowsPipeSocket(self._listener.accept(), self._key)

    def close(self) -> None:
        self._listener.close()


class _TimedAuthenticationConnection:
    def __init__(self, connection: Connection, timeout: float | None) -> None:
        self._connection = connection
        self._timeout = timeout

    def send_bytes(self, payload: bytes) -> None:
        self._connection.send_bytes(payload)

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        if not self._connection.poll(self._timeout):
            raise TimeoutError("named-pipe authentication timed out")
        return self._connection.recv_bytes(maxlength)


def connect_pipe(endpoint: Path, *, timeout: float) -> WindowsPipeSocket:
    key = read_pipe_key(endpoint)
    peer = WindowsPipeSocket(Client(pipe_address(endpoint), family="AF_PIPE", authkey=None), key)
    peer.settimeout(timeout)
    try:
        peer.authenticate_client()
    except BaseException:
        peer.close()
        raise
    return peer
