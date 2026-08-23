import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.1.0.dev0"


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {shlex.join(command)}", flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout, end="")
    result.check_returncode()
    return result


def _venv_scripts_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def main() -> int:
    """Build a wheel, install it in isolation, and execute shipping console behavior."""
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for the wheel smoke test")

    with tempfile.TemporaryDirectory(prefix="harness-wheel-smoke-") as temp_dir:
        workspace = Path(temp_dir)
        dist = workspace / "dist"
        venv = workspace / "venv"

        _run(
            (
                uv,
                "build",
                "--wheel",
                "--no-sources",
                "--no-build-isolation",
                "--out-dir",
                str(dist),
            ),
            cwd=PROJECT_ROOT,
        )

        wheels = list(dist.glob("harness-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected exactly one Harness wheel, found {len(wheels)}")
        wheel = wheels[0]

        _run((uv, "venv", "--python", "3.13", "--no-project", str(venv)), cwd=workspace)
        scripts_dir = _venv_scripts_dir(venv)
        python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
        _run(
            (uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)),
            cwd=workspace,
        )

        isolated_env = os.environ.copy()
        isolated_env.pop("PYTHONPATH", None)
        suffix = ".exe" if os.name == "nt" else ""
        for name in ("harness", "harnessd"):
            executable = scripts_dir / f"{name}{suffix}"
            result = _run((str(executable), "--version"), cwd=workspace, env=isolated_env)
            expected = f"{name} {EXPECTED_VERSION}\n"
            if result.stdout != expected:
                raise RuntimeError(
                    f"unexpected {name} --version output: {result.stdout!r}; expected {expected!r}"
                )

        harness = scripts_dir / f"harness{suffix}"
        harnessd = scripts_dir / f"harnessd{suffix}"
        help_result = _run((str(harness), "--help"), cwd=workspace, env=isolated_env)
        for expected in ("doctor", "status", "scan", "search"):
            if expected not in help_result.stdout:
                raise RuntimeError(
                    f"installed harness --help did not contain {expected!r}: {help_result.stdout!r}"
                )

        status_help = _run((str(harness), "status", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--socket", "canonical per-user"):
            if expected not in status_help.stdout:
                raise RuntimeError(
                    f"installed harness status --help did not contain {expected!r}: "
                    f"{status_help.stdout!r}"
                )

        scan_help = _run((str(harness), "scan", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--socket", "deterministic", "Git Workspace"):
            if expected not in scan_help.stdout:
                raise RuntimeError(
                    f"installed harness scan --help did not contain {expected!r}: "
                    f"{scan_help.stdout!r}"
                )

        search_help = _run((str(harness), "search", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--socket", "--limit", "bounded path or identifier query"):
            if expected not in search_help.stdout:
                raise RuntimeError(
                    f"installed harness search --help did not contain {expected!r}: "
                    f"{search_help.stdout!r}"
                )

        serve_help = _run((str(harnessd), "serve", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--database", "--socket", "canonical per-user", "search", "scan"):
            if expected not in serve_help.stdout:
                raise RuntimeError(
                    f"installed harnessd serve --help did not contain {expected!r}: "
                    f"{serve_help.stdout!r}"
                )

        doctor = _run((str(harness), "doctor"), cwd=workspace, env=isolated_env)
        for expected in ("SQLite runtime: OK", "FTS5: OK"):
            if expected not in doctor.stdout:
                raise RuntimeError(
                    f"installed harness doctor output did not contain {expected!r}: {doctor.stdout!r}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
