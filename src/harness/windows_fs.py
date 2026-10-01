"""Windows owner and DACL checks for private Harness runtime artifacts.

The module has no import-time Win32 dependency on POSIX hosts. Windows callers
must use the ACL checks instead of interpreting POSIX mode bits from ``stat``.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any


class WindowsFileSecurityError(RuntimeError):
    """A private Windows runtime path has unexpected ownership or access."""


def _sid() -> Any:
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    return win32security.GetTokenInformation(token, win32security.TokenUser)[0]


def _security(path: Path) -> Any:
    import win32security

    try:
        return win32security.GetFileSecurity(
            str(path),
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
    except OSError as exc:
        raise WindowsFileSecurityError(f"Windows path security could not be read: {path}") from exc


def _require_real_path(path: Path, *, directory: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise WindowsFileSecurityError(f"Windows path could not be inspected: {path}") from exc
    if path.is_symlink() or path.is_junction():
        raise WindowsFileSecurityError(f"Windows path must not be a link: {path}")
    if directory and not stat.S_ISDIR(metadata.st_mode):
        raise WindowsFileSecurityError(f"Windows path must be a directory: {path}")
    if not directory and not stat.S_ISREG(metadata.st_mode):
        raise WindowsFileSecurityError(f"Windows path must be a regular file: {path}")


def require_private_windows_path(path: Path, *, directory: bool) -> None:
    """Require current-user ownership and no non-privileged foreign allow ACE."""
    import win32security

    _require_real_path(path, directory=directory)
    descriptor = _security(path)
    user = _sid()
    owner = descriptor.GetSecurityDescriptorOwner()
    if owner != user:
        raise WindowsFileSecurityError(f"Windows path must be owned by the current user: {path}")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        raise WindowsFileSecurityError(f"Windows path must have a private DACL: {path}")
    allowed = {
        win32security.ConvertSidToStringSid(user),
        "S-1-5-18",  # Local System
        "S-1-5-32-544",  # Local Administrators
    }
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        ace_type = ace[0][0]
        if ace_type == win32security.ACCESS_DENIED_ACE_TYPE:
            continue
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise WindowsFileSecurityError(f"Windows path has an unsupported access ACE: {path}")
        if win32security.ConvertSidToStringSid(ace[2]) not in allowed:
            raise WindowsFileSecurityError(f"Windows path grants access to other users: {path}")


def secure_owned_windows_path(path: Path, *, directory: bool) -> None:
    """Replace inherited permissions on an owned new path with a user-only DACL."""
    import ntsecuritycon
    import win32con
    import win32security

    _require_real_path(path, directory=directory)
    user = _sid()
    if _security(path).GetSecurityDescriptorOwner() != user:
        raise WindowsFileSecurityError(f"Windows path must be owned by the current user: {path}")
    dacl = win32security.ACL()
    inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE if directory else 0
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION, inheritance, ntsecuritycon.FILE_ALL_ACCESS, user
    )
    try:
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    except OSError as exc:
        raise WindowsFileSecurityError(f"Windows path DACL could not be secured: {path}") from exc
    require_private_windows_path(path, directory=directory)


def ensure_private_windows_directory(path: Path) -> None:
    """Create a private directory, or reject an existing untrusted directory."""
    try:
        path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=False)
        secure_owned_windows_path(path, directory=True)
    require_private_windows_path(path, directory=True)
