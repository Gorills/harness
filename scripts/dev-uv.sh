# Shared uv 0.12.5 resolution for checkout wrappers.
#
# Callers must set `repo_root` to this repository's root before sourcing.
# This file does not apply isolated-development XDG overrides.

REQUIRED_UV_VERSION="0.12.5"
REQUIRED_PYTHON="3.13"

uv_version() {
    local executable="$1"
    "$executable" --version 2>/dev/null | awk '{print $2}'
}

resolve_uv() {
    local candidate version
    if [[ -n "${HARNESS_DEV_UV:-}" && -x "${HARNESS_DEV_UV}" ]]; then
        candidate="$HARNESS_DEV_UV"
        version="$(uv_version "$candidate")"
        if [[ "$version" == "$REQUIRED_UV_VERSION" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi

    candidate="$repo_root/.harness/tools/uv"
    if [[ -x "$candidate" ]]; then
        version="$(uv_version "$candidate")"
        if [[ "$version" == "$REQUIRED_UV_VERSION" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi

    if command -v uv >/dev/null 2>&1; then
        candidate="$(command -v uv)"
        version="$(uv_version "$candidate")"
        if [[ "$version" == "$REQUIRED_UV_VERSION" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi
    return 1
}

bootstrap_uv() {
    local install_dir="$repo_root/.harness/tools"
    mkdir -p -- "$install_dir"
    chmod 700 -- "$repo_root/.harness" "$install_dir" 2>/dev/null || true

    if ! command -v curl >/dev/null 2>&1; then
        echo "Harness development requires uv $REQUIRED_UV_VERSION (curl is needed to bootstrap it)." >&2
        echo "Install uv $REQUIRED_UV_VERSION and retry, or set HARNESS_DEV_UV to that executable." >&2
        return 1
    fi

    echo "Bootstrapping uv $REQUIRED_UV_VERSION into $install_dir" >&2
    curl -LsSf "https://astral.sh/uv/${REQUIRED_UV_VERSION}/install.sh" \
        | env UV_INSTALL_DIR="$install_dir" UV_NO_MODIFY_PATH=1 sh
}

ensure_uv() {
    local executable
    if executable="$(resolve_uv)"; then
        printf '%s\n' "$executable"
        return 0
    fi
    bootstrap_uv
    if ! executable="$(resolve_uv)"; then
        echo "uv $REQUIRED_UV_VERSION is required for Harness development." >&2
        return 1
    fi
    printf '%s\n' "$executable"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "Source this file instead of executing it:" >&2
    echo "  . scripts/dev-uv.sh" >&2
    exit 1
fi
