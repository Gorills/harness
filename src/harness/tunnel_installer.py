# ruff: noqa: RUF001
"""Prepare verified artifacts before one OS elevation and install an owned runtime.

No checkout/user-virtualenv Python is used by the long-running privileged service.
The catalog is product-owned; neither browser input nor an arbitrary manifest can
approve a network binary. An empty audit channel fails before elevation or SSH.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.name == "nt":
    site = Path(sys.base_prefix) / "Lib/site-packages"
    sys.path.extend(str(path) for path in (site, site / "win32", site / "win32/lib"))
    if (site / "pywin32_system32").is_dir():
        _dll_directory = os.add_dll_directory(str(site / "pywin32_system32"))  # type: ignore[attr-defined]

from harness.tunnel_artifacts import ArtifactError, verify_bundle
from harness.tunnel_platform import (
    PlatformError,
    create_fakeip_cache,
    fakeip_cache_root,
    systemd_units,
    write_private,
)

_MODULES = (
    "__init__.py",
    "tunnels.py",
    "tunnel_artifacts.py",
    "tunnel_builder.py",
    "tunnel_config.py",
    "tunnel_health.py",
    "tunnel_platform.py",
    "tunnel_service.py",
    "tunnel_phone.py",
    "tunnel_ssh.py",
    "tunnel_windows.py",
    "tunnel_windows_ipc.py",
    "tunnel_launcher.py",
    "tunnel_installer.py",
    "tunnel_bundle_catalog.json",
)


def catalog_entry() -> dict[str, Any]:
    path = Path(__file__).with_name("tunnel_bundle_catalog.json")
    catalog = json.loads(path.read_bytes())
    architecture = {"AMD64": "amd64", "x86_64": "amd64", "aarch64": "arm64", "ARM64": "arm64"}.get(
        platform.machine()
    )
    if architecture is None:
        raise ArtifactError("Эта архитектура сетевого компонента ещё не поддерживается")
    key = f"{sys.platform}-{architecture}"
    entry = catalog.get("bundles", {}).get(key)
    if not isinstance(entry, dict) or entry.get("audit", {}).get("status") != "approved":
        raise ArtifactError(
            "Для этой платформы ещё нет сетевой сборки с завершённым аудитом зависимостей"
        )
    return entry


def download_component(specification: dict[str, Any], destination: Path) -> None:
    url = specification["archive_url"]
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise ArtifactError("Недопустимый источник сетевого компонента")
    with tempfile.NamedTemporaryFile(dir=destination.parent) as archive:
        digest = hashlib.sha256()
        size = 0
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                if not response.url.startswith(
                    (
                        "https://github.com/",
                        "https://release-assets.githubusercontent.com/",
                        "https://objects.githubusercontent.com/",
                    )
                ):
                    raise ArtifactError("Недопустимое перенаправление сетевого пакета")
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise ArtifactError("Архив сетевого пакета слишком велик")
                    digest.update(chunk)
                    archive.write(chunk)
        except OSError as exc:
            raise ArtifactError("Не удалось получить проверенный сетевой пакет") from exc
        archive.flush()
        if digest.hexdigest() != specification["archive_sha256"]:
            raise ArtifactError("Контрольная сумма загруженного архива не совпала")
        member = specification["member"]
        if specification["format"] == "zip":
            with zipfile.ZipFile(archive.name) as bundle:
                info = bundle.getinfo(member)
                if info.file_size > 256 * 1024 * 1024 or info.is_dir():
                    raise ArtifactError("Недопустимый компонент архива")
                with bundle.open(info) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
        else:
            with tarfile.open(archive.name) as bundle:
                tar_info = bundle.getmember(member)
                if not tar_info.isfile() or tar_info.size > 256 * 1024 * 1024:
                    raise ArtifactError("Недопустимый компонент архива")
                tar_source = bundle.extractfile(tar_info)
                if tar_source is None:
                    raise ArtifactError("В архиве отсутствует компонент")
                with tar_source, destination.open("xb") as output:
                    shutil.copyfileobj(tar_source, output)
        with destination.open("rb") as output:
            if hashlib.file_digest(output, "sha256").hexdigest() != specification["sha256"]:
                destination.unlink(missing_ok=True)
                raise ArtifactError("Контрольная сумма сетевого компонента не совпала")
        destination.chmod(0o755)


def prepare_install(directory: Path) -> dict[str, Any]:
    entry = catalog_entry()  # mandatory gate before downloading or asking for elevation
    package = directory / "packages" / "harness"
    package.mkdir(parents=True, mode=0o700)
    for name in _MODULES:
        shutil.copyfile(Path(__file__).with_name(name), package / name)
    shutil.copytree(Path(__file__).with_name("tunnel_recipe"), package / "tunnel_recipe")
    bundle = directory / "bundle"
    bundle.mkdir(mode=0o700)
    components = {}
    for name, specification in entry["components"].items():
        if specification.get("audit_status") != "approved":
            continue
        filename = specification["file"]
        if Path(filename).name != filename or filename in {".", ".."}:
            raise ArtifactError("Недопустимое имя сетевого компонента")
        if specification.get("method") == "source-build":
            from harness.tunnel_builder import build_engine

            build_engine(specification, bundle / filename)
        else:
            download_component(specification, bundle / filename)
        components[name] = {
            key: specification[key]
            for key in ("file", "sha256", "version", "source", "audit_status")
        }
    manifest = {key: entry[key] for key in ("schema", "version", "platform", "audit")}
    manifest.update(components=components, system_packages=entry.get("system_packages", {}))
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    # Dereference into an owned snapshot; never retain a symlink back to the
    # original user's Python distribution or virtual environment.
    shutil.copytree(
        Path(sys.base_prefix),
        directory / "runtime",
        symlinks=False,
        ignore=shutil.ignore_patterns("site-packages", "__pycache__"),
    )
    if os.name == "nt":
        site = directory / "runtime" / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        for module in (
            "win32",
            "win32com",
            "pywin32_system32",
            "pywin32.pth",
            "servicemanager.pyd",
        ):
            for source_root in {Path(value) for value in sys.path if "site-packages" in value}:
                source = source_root / module
                if source.is_dir():
                    shutil.copytree(source, site / module, dirs_exist_ok=True)
                elif source.is_file():
                    shutil.copyfile(source, site / module)
        spec = importlib.util.find_spec("pywintypes")
        if spec is not None and spec.origin:
            shutil.copyfile(spec.origin, site / "pywintypes.py")
    return entry


def request_install() -> None:
    """Called by the operator UI/CLI; all preparation precedes OS permission."""
    if os.environ.get("HARNESS_DEV_ROOT") or (
        (Path(__file__).parents[2] / "pyproject.toml").is_file()
        and (Path(__file__).parents[2] / "scripts/dev").is_file()
    ):
        raise PlatformError(
            "Системная установка недоступна из исходного checkout; используйте установленный Harness"
        )
    with tempfile.TemporaryDirectory(prefix="harness-network-install-") as temporary:
        source = Path(temporary)
        prepare_install(source)
        hashes = {
            path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source.rglob("*")
            if path.is_file()
        }
        if any("\n" in name or "\r" in name or "\\" in name for name in hashes):
            raise PlatformError("Недопустимое имя файла в снимке установщика")
        if os.name == "nt":
            snapshot = source / "snapshot-checks.json"
            snapshot.write_text(json.dumps(hashes))
            _elevate_windows(source, hashlib.sha256(snapshot.read_bytes()).hexdigest())
        else:
            owner = os.getuid()
            target = Path("/opt/harness-tunnel")
            snapshot = source / "snapshot.sha256"
            snapshot.write_text(
                "\n".join(f"{digest}  {target / name}" for name, digest in hashes.items()) + "\n"
            )
            snapshot_sha256 = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            python = (
                source
                / "runtime"
                / "bin"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
            )
            relative_python = python.relative_to(source)
            shell = "\n".join(
                [
                    "set -eu",
                    "umask 077",
                    "harness_created=0",
                    f'trap \'harness_status=$?; trap - EXIT; if [ "$harness_status" -ne 0 ] && [ "$harness_created" -eq 1 ]; then /usr/bin/rm -rf -- {shlex.quote(str(target))}; fi; exit "$harness_status"\' EXIT',
                    "trap 'exit 1' HUP INT TERM",
                    f"/usr/bin/mkdir -m 0700 {shlex.quote(str(target))}",
                    "harness_created=1",
                    f"/usr/bin/cp -RL --no-preserve=ownership {shlex.quote(str(source))}/. {shlex.quote(str(target))}/",
                    f"/usr/bin/chown -R root:root {shlex.quote(str(target))}",
                    f"/usr/bin/chmod -R go-w {shlex.quote(str(target))}",
                    f'test "$(/usr/bin/find {shlex.quote(str(target))} -type f -printf . | /usr/bin/wc -c)" -eq {len(hashes) + 1}',
                    "/usr/bin/sha256sum --check --strict <<'HARNESS_VERIFIED_SNAPSHOT'",
                    f"{snapshot_sha256}  {target / 'snapshot.sha256'}",
                    "HARNESS_VERIFIED_SNAPSHOT",
                    f"/usr/bin/sha256sum --check --strict {shlex.quote(str(target / 'snapshot.sha256'))}",
                    f"{shlex.quote(str(target / relative_python))} -I -S {shlex.quote(str(target / 'packages/harness/tunnel_installer.py'))} finalize {owner}",
                ]
            )
            result = subprocess.run(
                ["/usr/bin/pkexec", "/bin/sh", "-c", shell],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
                check=False,
            )
            if result.returncode:
                raise PlatformError(
                    "Установка сетевой службы не завершена или системный запрос отменён"
                )


def request_emergency_off() -> None:
    """Execute only the fixed owned recovery entrypoint under OS elevation."""
    from harness.tunnel_platform import require_owned
    from harness.tunnel_service import INSTALL

    require_owned(INSTALL, directory=True)
    launcher = INSTALL / "packages/harness/tunnel_launcher.py"
    require_owned(launcher)
    if os.name == "nt":
        import win32event  # type: ignore[import-untyped]
        from win32com.shell import shell, shellcon  # type: ignore[import-untyped]

        python = INSTALL / "runtime/python.exe"
        require_owned(python)
        process = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb="runas",
            lpFile=str(python),
            lpParameters=subprocess.list2cmdline(["-I", "-S", str(launcher), "emergency-off"]),
            nShow=0,
        )
        handle = process["hProcess"]
        try:
            if win32event.WaitForSingleObject(handle, 60000) != 0 or win32process_exit_code(handle):
                raise PlatformError("Аварийное отключение не подтверждено")
        finally:
            handle.Close()
    else:
        python = (
            INSTALL / "runtime/bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        )
        require_owned(python)
        result = subprocess.run(
            ["/usr/bin/pkexec", str(python), "-I", "-S", str(launcher), "emergency-off"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise PlatformError("Аварийное отключение не подтверждено")


def _elevate_windows(source: Path, snapshot_sha256: str) -> None:
    import ctypes

    import win32api
    import win32con
    import win32event
    import win32security
    from win32com.shell import shell, shellcon

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    owner_sid = win32security.ConvertSidToStringSid(
        win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    )
    token.Close()
    target = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "HarnessTunnel"

    # PowerShell is an OS-owned bootstrap copier. It never imports source code
    # before it has moved into the admin-owned immutable install directory.
    def quote(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    python = target / "runtime/python.exe"
    script = "; ".join(
        [
            "$ErrorActionPreference='Stop'",
            "$harnessCreated=$false; try {",
            f"if(Test-Path {quote(target)}){{throw 'owned installation already exists'}}",
            f"New-Item -ItemType Directory -Path {quote(target)}|Out-Null",
            "$harnessCreated=$true",
            f"Copy-Item -Path ({quote(source)}+'\\*') -Destination {quote(target)} -Recurse",
            f"& icacls.exe {quote(target)} /reset /T /Q|Out-Null",
            "if($LASTEXITCODE -ne 0){throw 'snapshot ACL reset failed'}",
            f"& icacls.exe {quote(target)} /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T /Q|Out-Null",
            "if($LASTEXITCODE -ne 0){throw 'snapshot ACL failed'}",
            f"& icacls.exe {quote(target)} /setowner '*S-1-5-32-544' /T /Q|Out-Null",
            "if($LASTEXITCODE -ne 0){throw 'snapshot owner failed'}",
            f"if((Get-FileHash -LiteralPath {quote(target / 'snapshot-checks.json')} -Algorithm SHA256).Hash -ne {quote(snapshot_sha256)}){{throw 'snapshot manifest mismatch'}}",
            f"$checks=Get-Content -Raw -LiteralPath {quote(target / 'snapshot-checks.json')}|ConvertFrom-Json",
            f"$files=@(Get-ChildItem -LiteralPath {quote(target)} -Recurse -Force)",
            "if(@($files|Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint}).Count -ne 0){throw 'snapshot reparse point'}",
            "if(@($files|Where-Object {-not $_.PSIsContainer}).Count -ne @($checks.PSObject.Properties).Count+1){throw 'unexpected snapshot file'}",
            f"foreach($item in $checks.PSObject.Properties){{if((Get-FileHash -LiteralPath (Join-Path {quote(target)} $item.Name) -Algorithm SHA256).Hash -ne $item.Value){{throw 'snapshot digest mismatch'}}}}",
            f"& {quote(python)} -I -S {quote(target / 'packages/harness/tunnel_installer.py')} finalize {quote(owner_sid)}",
            "if($LASTEXITCODE -ne 0){throw 'finalize failed'}",
            "} catch { if($harnessCreated){Remove-Item -LiteralPath "
            + quote(target)
            + " -Recurse -Force}; exit 1 }; exit 0",
        ]
    )
    import base64

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    executable = (
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    process = shell.ShellExecuteEx(
        fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
        lpVerb="runas",
        lpFile=str(executable),
        lpParameters=f"-NoProfile -NonInteractive -EncodedCommand {encoded}",
        nShow=0,
    )
    handle = process["hProcess"]
    try:
        if (
            win32event.WaitForSingleObject(handle, 300000) != win32con.WAIT_OBJECT_0
            or win32process_exit_code(handle) != 0
        ):
            raise PlatformError("Установка Windows-службы не завершена")
    finally:
        handle.Close()
    del ctypes


def win32process_exit_code(handle: Any) -> int:
    import win32process  # type: ignore[import-untyped]

    return int(win32process.GetExitCodeProcess(handle))


def finalize(owner: str) -> None:
    """Preflight all resources; roll back only this first-install transaction."""
    from harness.tunnel_platform import require_owned
    from harness.tunnel_service import INSTALL, ROOT

    root_existed = ROOT.exists()
    if ROOT.is_symlink() or (root_existed and (not ROOT.is_dir() or any(ROOT.iterdir()))):
        raise PlatformError("Каталог состояния уже занят; существующая служба сохранена")
    if root_existed:
        require_owned(ROOT, directory=True)
    cache_root = fakeip_cache_root(ROOT)
    if cache_root.exists() or cache_root.is_symlink():
        raise PlatformError("Каталог кеша уже занят; существующие данные сохранены")
    units: dict[str, str] = {}
    if os.name != "nt":
        python = (
            INSTALL / "runtime/bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        )
        units = systemd_units(python, INSTALL / "packages/harness/tunnel_launcher.py", ROOT)
        if any(
            (Path("/etc/systemd/system") / name).exists()
            or (Path("/etc/systemd/system") / name).is_symlink()
            for name in units
        ):
            raise PlatformError("Имя службы уже занято; посторонний unit сохранён")
    else:
        import pywintypes  # type: ignore[import-untyped]
        import win32service  # type: ignore[import-untyped]

        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        try:
            try:
                existing = win32service.OpenService(
                    manager, "HarnessTunnel", win32service.SERVICE_QUERY_STATUS
                )
            except pywintypes.error as exc:
                if exc.winerror != 1060:
                    raise
            else:
                existing.Close()
                raise PlatformError("Имя Windows-службы уже занято; существующая служба сохранена")
        finally:
            manager.Close()
    created_units: list[str] = []
    created_accounts: list[str] = []
    created_caches: list[Path] = []
    try:
        _finalize(owner, created_units, created_accounts, created_caches)
    except BaseException:
        if created_units:
            subprocess.run(["/usr/bin/systemctl", "disable", "--now", *created_units], check=False)
            for name in created_units:
                path = Path("/etc/systemd/system") / name
                path.unlink(missing_ok=True)
            subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=False)
        for cache in created_caches:
            shutil.rmtree(cache)
        # ROOT was empty or absent at preflight; no prior runtime/guard can be
        # erased here. The elevated copier separately rolls back its new tree.
        if ROOT.exists():
            for child in ROOT.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            if not root_existed:
                ROOT.rmdir()
        for account in created_accounts:
            subprocess.run(["/usr/sbin/userdel", account], check=False)
        raise


def _finalize(
    owner: str,
    created_units: list[str],
    created_accounts: list[str],
    created_caches: list[Path],
) -> None:
    # This entrypoint is reached only from the copied immutable runtime.
    from harness.tunnel_platform import require_owned
    from harness.tunnel_service import INSTALL, ROOT

    require_owned(INSTALL, directory=True)
    verify_bundle(INSTALL / "bundle", platform=sys.platform, required=("sing-box",))
    if os.name != "nt":
        INSTALL.chmod(0o755)
        (INSTALL / "bundle").chmod(0o755)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        require_owned(ROOT, directory=True)
    configuration: dict[str, Any] = {"owner_uid": 0, "runtime_uid": 0, "runtime_gid": 0}
    launcher = INSTALL / "packages/harness/tunnel_launcher.py"
    if os.name == "nt":
        import win32service

        from harness.tunnel_windows_ipc import secure_admin_path

        configuration["owner_sid"] = owner
        secure_admin_path(ROOT, directory=True, reader_sid=owner)
        write_private(ROOT / "service.json", json.dumps(configuration).encode())
        write_private(ROOT / "control.key", os.urandom(32))
        secure_admin_path(ROOT / "control.key", directory=False, reader_sid=owner)
        create_fakeip_cache(ROOT, configuration)
        created_caches.append(fakeip_cache_root(ROOT))
        import ensurepip

        ensurepip.bootstrap()
        from pip._vendor.distlib.scripts import ScriptMaker  # type: ignore[import-not-found]

        site = Path(sys.base_prefix) / "Lib/site-packages"
        (site / "harness-tunnel.pth").write_text(str(INSTALL / "packages") + "\n")
        maker = ScriptMaker(None, str(ROOT))
        maker.executable = str(INSTALL / "runtime/python.exe")
        maker.variants = {""}
        maker.make(
            "harness-tunnel-askpass = harness.tunnel_ssh:askpass_main",
            options={"interpreter_args": ["-I"]},
        )
        shutil.copyfile(ROOT / "harness-tunnel-askpass.exe", ROOT / "askpass.exe")
        secure_admin_path(ROOT / "askpass.exe", directory=False)
        # The current installer package must carry its native ASKPASS console
        # launcher. An unchecked .cmd/.py fallback is deliberately not used.
        binary = f'"{INSTALL / "runtime/python.exe"}" -I -S "{launcher}" windows-service'
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CREATE_SERVICE)
        service = win32service.CreateService(
            manager,
            "HarnessTunnel",
            "Harness Tunnel",
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            binary,
            None,
            0,
            None,
            None,
            None,
        )
        try:
            win32service.ChangeServiceConfig2(
                service,
                win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
                {
                    "ResetPeriod": 86400,
                    "RebootMsg": "",
                    "Command": "",
                    "Actions": [(1, 2000), (1, 5000), (1, 30000)],
                },
            )
            win32service.StartService(service, None)
        except BaseException:
            try:
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
            except Exception:
                pass
            win32service.DeleteService(service)
            raise
        finally:
            service.Close()
            manager.Close()
    else:
        import pwd

        uid = int(owner)
        if uid <= 0:
            raise PlatformError("У службы должен быть обычный пользователь-владелец")
        try:
            account = pwd.getpwnam("harness-tunnel")
        except KeyError:
            subprocess.run(
                [
                    "/usr/sbin/useradd",
                    "--system",
                    "--no-create-home",
                    "--comment",
                    "harness-tunnel-runtime-v1",
                    "--shell",
                    "/usr/sbin/nologin",
                    "harness-tunnel",
                ],
                check=True,
            )
            created_accounts.append("harness-tunnel")
            account = pwd.getpwnam("harness-tunnel")
        if (
            account.pw_uid <= 0
            or account.pw_uid == uid
            or account.pw_gecos != "harness-tunnel-runtime-v1"
            or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin"}
        ):
            raise PlatformError("Имя runtime-пользователя занято посторонней учётной записью")
        configuration.update(owner_uid=uid, runtime_uid=account.pw_uid, runtime_gid=account.pw_gid)
        write_private(ROOT / "service.json", json.dumps(configuration).encode())
        create_fakeip_cache(ROOT, configuration)
        created_caches.append(fakeip_cache_root(ROOT))
        python = (
            INSTALL / "runtime/bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        )
        units = systemd_units(python, launcher, ROOT)
        for name, body in units.items():
            target = Path("/etc/systemd/system") / name
            if target.exists():
                raise PlatformError("Имя службы уже занято; посторонний unit сохранён")
            # Exclusive creation closes the collision window after preflight.
            with target.open("xb") as output:
                created_units.append(name)
                output.write(body.encode())
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o644)
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
        subprocess.run(["/usr/bin/systemctl", "enable", "--now", *units], check=True)


if __name__ == "__main__":
    # Invoked as a file by an isolated interpreter after bootstrap copying.
    if len(sys.argv) != 3 or sys.argv[1] != "finalize":
        raise SystemExit(2)
    finalize(sys.argv[2])
