# Repository-local Harness runtime isolation.
#
# Redirects canonical POSIX path selection (ADR-0007) into this checkout's
# `.harness/` tree so development does not share database or IPC state with a
# separately installed Harness build. The caller's XDG bases are saved as
# HARNESS_DEV_SAVED_XDG_* so `make install-global` can restore them.
#
# Usage:
#   . scripts/dev-env.sh
#   uv run --frozen harness status
#
# Prefer `scripts/dev`, which applies this environment and runs the checkout.

_harness_dev_repo_root() {
    # Resolve from this file, not the caller. Sourcing from `.envrc` or another
    # directory must not walk up from the caller's path or change the caller's cwd.
    local script_path="${BASH_SOURCE[0]}"
    local script_dir
    script_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)"
    (cd -- "$script_dir/.." && pwd)
}

_harness_dev_prepare_directory() {
    local directory="$1"
    mkdir -p -- "$directory"
    chmod 700 -- "$directory"
}

harness_dev_activate() {
    local root
    root="$(_harness_dev_repo_root)"
    if [[ -z "${HARNESS_DEV_ROOT:-}" ]]; then
        # Preserve the caller's canonical XDG so `make install-global` can leave
        # this overlay without moving the user-global daemon onto a different socket.
        export HARNESS_DEV_SAVED_XDG_STATE_HOME="${XDG_STATE_HOME-}"
        export HARNESS_DEV_SAVED_XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR-}"
    fi
    export HARNESS_DEV_ROOT="$root"
    export XDG_STATE_HOME="$root/.harness/state"
    export XDG_RUNTIME_DIR="$root/.harness/runtime"
    export HARNESS_SKILL_REGISTRY="$root/.harness/skills"
    export UV_CACHE_DIR="$root/.harness/uv-cache"

    mkdir -p -- "$root/.harness"
    chmod 700 -- "$root/.harness"
    _harness_dev_prepare_directory "$XDG_STATE_HOME"
    _harness_dev_prepare_directory "$XDG_RUNTIME_DIR"
    _harness_dev_prepare_directory "$HARNESS_SKILL_REGISTRY"
    _harness_dev_prepare_directory "$UV_CACHE_DIR"

    local venv_bin="$root/.venv/bin"
    if [[ -d "$venv_bin" ]]; then
        case ":$PATH:" in
            *":$venv_bin:"*) ;;
            *) export PATH="$venv_bin:$PATH" ;;
        esac
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "Source this file instead of executing it:" >&2
    echo "  . scripts/dev-env.sh" >&2
    exit 1
fi

harness_dev_activate
